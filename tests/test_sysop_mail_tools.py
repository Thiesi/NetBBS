"""SysOp mail tools (issue #820): the Link mail this node refused, mailbox
sizes, and trust actions on a peer's screen.

Mail is private: nothing here may keep or show a letter's subject, body or
recipient. The refusal log is bounded because its sender decides how much
arrives.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.link.enforcement import ensure_node_subject
from netbbs.link.mail_refusals import (
    VIA_DIRECT,
    VIA_RELAY,
    list_link_mail_refusals,
    record_link_mail_refusal,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.link.store import save_candidate_descriptor, save_peer
from netbbs.link.transport import persist_accepted_events
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    get_effective_trust_state,
    list_trust_overrides,
)
from netbbs.mail import (
    MailMessage,
    delete_for_recipient,
    get_mail,
    inbox_sizes,
    mark_read,
    send_mail,
    send_system_mail,
)
from netbbs.net.admin_flow import admin_menu
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _link_context, _normalized_visible, _written_text
from tests.test_admin_flow_node_map import _record
from tests.test_link_mail_policy import (
    _establish_node,
    _exchange,
    _NodeDb,
    _persist_picked_up,
    _signed_mail,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _mail(sender_name="nib"):
    sender = bootstrap_node_identity("sender")
    recipient = bootstrap_node_identity("recipient")
    return sender, _signed_mail(sender, recipient, user=sender_name)


# -- the record ----------------------------------------------------------------


def test_the_refusal_log_keeps_no_subject_body_or_recipient(db):
    columns = {row["name"] for row in db.connection.execute("PRAGMA table_info(link_mail_refusals)")}
    assert not columns & {"subject", "body", "recipient", "recipient_user_id", "ciphertext", "envelope_json"}
    sender, message = _mail()
    record_link_mail_refusal(db, message.to_dict(), "link_policy_manual_block", via=VIA_RELAY)
    row = dict(db.connection.execute("SELECT * FROM link_mail_refusals").fetchone())
    assert "bob" not in row.values()  # the letter's recipient
    assert row["sender_node_fingerprint"] == sender.fingerprint
    assert row["sender_user"] == "nib"


def test_a_letter_refused_again_is_one_row_with_its_tries_counted(db):
    _sender, message = _mail()
    for _ in range(3):
        record_link_mail_refusal(db, message.to_dict(), "link_policy_node_probationary_read_only", via=VIA_DIRECT)
    [refusal] = list_link_mail_refusals(db)
    assert refusal.attempts == 3
    assert refusal.via == VIA_DIRECT


def test_the_refusal_log_is_bounded_and_keeps_the_newest(db, monkeypatch):
    import netbbs.link.mail_refusals as refusals

    monkeypatch.setattr(refusals, "MAX_LINK_MAIL_REFUSALS_KEPT", 2)
    names = ["first", "second", "third"]
    for name in names:
        _sender, message = _mail(name)
        record_link_mail_refusal(db, message.to_dict(), "link_policy_manual_block", via=VIA_RELAY)
    assert [refusal.sender_user for refusal in list_link_mail_refusals(db)] == ["third", "second"]


def test_a_push_names_the_node_that_made_it_and_not_a_sender_it_merely_claims(db):
    """A direct push is refused before its letters' signatures are checked:
    the node that authenticated the push is who it is recorded against, and
    a letter claiming another home node is not believed about its sender."""
    _sender, message = _mail()
    pusher = bootstrap_node_identity("pusher")
    record_link_mail_refusal(
        db, message.to_dict(), "link_policy_node_probationary_read_only", via=VIA_DIRECT,
        sender_node_fingerprint=pusher.fingerprint,
    )
    [refusal] = list_link_mail_refusals(db)
    assert refusal.sender_node_fingerprint == pusher.fingerprint
    assert refusal.sender_user is None


def test_every_reason_a_sender_can_be_told_has_words_for_the_sysop_too():
    """The two sides of one refusal: whatever code the sender's bounce can
    carry, the refusing node's SysOp reads in words of their own."""
    from netbbs.link.events import _VALID_BOUNCE_REASONS
    from netbbs.link.mail import _BOUNCE_REASON_TEXT
    from netbbs.link.mail_refusals import _REASON_TEXT

    assert set(_VALID_BOUNCE_REASONS) - {"blocked_sender"} <= set(_REASON_TEXT)
    assert set(_BOUNCE_REASON_TEXT) - {"blocked_sender"} <= set(_REASON_TEXT)


def test_one_node_cannot_crowd_other_nodes_out_of_the_log(db, monkeypatch):
    """A direct push is refused unverified, so its letters cost the pusher
    nothing to invent; its share of the log is capped."""
    import netbbs.link.mail_refusals as refusals

    monkeypatch.setattr(refusals, "MAX_LINK_MAIL_REFUSALS_KEPT", 4)
    monkeypatch.setattr(refusals, "MAX_LINK_MAIL_REFUSALS_PER_NODE", 2)
    _sender, early = _mail("early")
    record_link_mail_refusal(db, early.to_dict(), "link_policy_manual_block", via=VIA_RELAY)
    flooder = bootstrap_node_identity("flooder")
    for name in ("a", "b", "c", "d", "e"):
        _other, message = _mail(name)
        record_link_mail_refusal(
            db, message.to_dict(), "link_policy_node_probationary_read_only", via=VIA_DIRECT,
            sender_node_fingerprint=flooder.fingerprint,
        )
    kept = list_link_mail_refusals(db)
    assert [refusal.sender_node_fingerprint for refusal in kept].count(flooder.fingerprint) == 2
    assert any(refusal.sender_user == "early" for refusal in kept)


def test_an_unreadable_letter_is_not_recorded_and_does_not_raise(db):
    record_link_mail_refusal(db, {"envelope": {"payload": {}}}, "malformed", via=VIA_DIRECT)
    record_link_mail_refusal(db, {}, "malformed", via=VIA_DIRECT)
    # A float anywhere makes the content id itself refuse (ContentIdError),
    # and the push it came in is still to be answered with a clean 403.
    _sender, message = _mail()
    raw = message.to_dict()
    raw["envelope"]["payload"]["created_at"] = 1.5
    record_link_mail_refusal(db, raw, "link_policy_node_probationary_read_only", via=VIA_DIRECT)
    assert list_link_mail_refusals(db) == []


# -- where refusals come from --------------------------------------------------


def _picked_up(tmp_path, *, quarantine_user: bool, establish_node: bool = True):
    """`_persist_picked_up`, with the relay pickup's own `mail_via`
    (`netbbs.link.sync` passes it; a direct push never reaches this refusal)."""
    import netbbs.link.transport as transport

    original = transport.persist_accepted_events

    async def _relayed(*args, **kwargs):
        return await original(*args, mail_via=VIA_RELAY, **kwargs)

    import tests.test_link_mail_policy as policy_tests

    policy_tests.persist_accepted_events = _relayed
    try:
        return _persist_picked_up(tmp_path, quarantine_user=quarantine_user, establish_node=establish_node)
    finally:
        policy_tests.persist_accepted_events = original


def test_relayed_mail_refused_for_its_node_is_recorded(tmp_path):
    recipient, sender = _picked_up(tmp_path, quarantine_user=False, establish_node=False)
    try:
        [refusal] = list_link_mail_refusals(recipient.db)
        assert refusal.sender_node_fingerprint == sender.fingerprint
        assert refusal.sender_user == "nib"
        assert refusal.reason == "link_policy_node_probationary_read_only"
        assert refusal.via == VIA_RELAY
    finally:
        recipient.close()


def test_relayed_mail_from_a_quarantined_user_is_recorded_with_that_reason(tmp_path):
    recipient, _sender = _picked_up(tmp_path, quarantine_user=True)
    try:
        [refusal] = list_link_mail_refusals(recipient.db)
        assert refusal.reason == "link_policy_user_quarantined"
    finally:
        recipient.close()


def test_delivered_mail_records_nothing(tmp_path):
    recipient, _sender = _picked_up(tmp_path, quarantine_user=False)
    try:
        assert list_link_mail_refusals(recipient.db) == []
    finally:
        recipient.close()


def test_a_delivery_bounce_is_recorded(tmp_path):
    sender_identity = bootstrap_node_identity("sender")
    recipient_identity = bootstrap_node_identity("recipient")
    node = LinkNode(identity=recipient_identity)
    recipient = _NodeDb(tmp_path, "recipient")
    try:
        _establish_node(recipient.db, sender_identity.fingerprint)
        message = _signed_mail(sender_identity, recipient_identity)  # to "bob", who has no account here
        node.events[message.content_id] = message.to_dict()
        asyncio.run(persist_accepted_events(
            recipient.lane, node, [message.content_id],
            sender_fingerprint=sender_identity.fingerprint, max_carried_boards=None,
            enforce_trust_policy=True,
        ))
        [refusal] = list_link_mail_refusals(recipient.db)
        assert refusal.reason == "unknown_recipient"
        assert refusal.sender_user == "nib"
        assert refusal.via == VIA_DIRECT
    finally:
        recipient.close()


def test_a_push_refused_by_policy_is_recorded_on_the_receiving_node(tmp_path):
    sender, recipient, _message = _exchange(tmp_path, establish_sender_at_recipient=False)
    try:
        [refusal] = list_link_mail_refusals(recipient.db)
        assert refusal.sender_user == "alice"
        assert refusal.reason == "link_policy_node_probationary_read_only"
        assert refusal.via == VIA_DIRECT
        assert list_link_mail_refusals(sender.db) == []
    finally:
        sender.close()
        recipient.close()


def test_only_a_letter_its_node_really_signed_counts_as_from_it():
    sender, message = _mail()
    recipient = LinkNode(identity=bootstrap_node_identity("recipient"))
    raw = message.to_dict()
    # A node never met: its URL is all a push has, and that proves nothing.
    assert not recipient.is_signed_letter_from(raw, sender.fingerprint)
    recipient.peers[sender.fingerprint] = _record(sender, name="Sender")
    assert recipient.is_signed_letter_from(raw, sender.fingerprint)
    forged = message.to_dict()
    forged["envelope"]["payload"]["sender"]["local_user_id"] = "mallory"
    assert not recipient.is_signed_letter_from(forged, sender.fingerprint)
    stranger = bootstrap_node_identity("stranger")
    recipient.peers[stranger.fingerprint] = _record(stranger, name="Stranger")
    assert not recipient.is_signed_letter_from(raw, stranger.fingerprint)


def test_a_forged_push_is_refused_and_not_recorded(tmp_path):
    """Anyone can reach the events endpoint and name any node in its URL: a
    letter that node never signed must not appear as refused mail from it."""
    import aiohttp

    from netbbs.link.transport import LINK_PATH_PREFIX, LinkServer

    sender = bootstrap_node_identity("sender")
    recipient_identity = bootstrap_node_identity("recipient")
    recipient_node = LinkNode(identity=recipient_identity)
    recipient_node.peers[sender.fingerprint] = _record(sender, name="Sender")
    recipient = _NodeDb(tmp_path, "recipient")
    forger = bootstrap_node_identity("forger")
    forged = _signed_mail(forger, recipient_identity).to_dict()
    forged["envelope"]["payload"]["sender"]["home_node_fingerprint"] = sender.fingerprint

    async def scenario():
        server = LinkServer(
            host="127.0.0.1", port=0, node=recipient_node,
            own_hello_provider=lambda: recipient_node.build_hello(
                addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00",
            ),
            lane=recipient.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{server.port}{LINK_PATH_PREFIX}/events/{sender.fingerprint}", json=[forged],
                ) as response:
                    return response.status
        finally:
            await server.stop()

    try:
        assert asyncio.run(scenario()) == 403
        assert list_link_mail_refusals(recipient.db) == []
    finally:
        recipient.close()


# -- mailbox sizes ---------------------------------------------------------------


def test_inbox_sizes_count_what_the_cap_counts_fullest_first(db):
    alice = create_user(db, "alice", password="hunter2")
    bob = create_user(db, "bob", password="hunter2")
    carol = create_user(db, "carol", password="hunter2")
    create_user(db, "dave", password="hunter2")  # no mail: not listed
    for index in range(3):
        send_mail(db, alice, bob, f"Hello {index}", "Body")
    first: MailMessage = send_mail(db, bob, carol, "One", "Body")
    send_mail(db, bob, carol, "Two", "Body")
    send_system_mail(db, carol, "Notice", "From the BBS")
    mark_read(db, carol, get_mail(db, carol, first.id))
    gone = send_mail(db, alice, bob, "Deleted", "Body")
    delete_for_recipient(db, bob, get_mail(db, bob, gone.id))

    sizes = inbox_sizes(db)
    assert [(size.username, size.total, size.unread, size.read, size.system) for size in sizes] == [
        ("bob", 3, 3, 0, 0),
        ("carol", 3, 2, 1, 1),
    ]


# -- the console -----------------------------------------------------------------


def test_mailboxes_show_counts_and_never_a_subject(db, lane, sysop):
    alice = create_user(db, "alice", password="hunter2")
    bob = create_user(db, "bob", password="hunter2")
    send_mail(db, alice, bob, "Secret plans", "Nobody else should read this")
    # o: Operations; m: Mail; m: Mailboxes; o: by name; b x4: out.
    session = FakeSession(["o", "m", "m", "o", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    text = _normalized_visible(_written_text(session))
    assert "Accounts with mail: 2" not in text  # only bob has mail
    assert "Accounts with mail: 1" in text
    assert "Fullest first." in text and "By account name." in text
    assert "bob 1 1 0 0 0 0%" in text  # letters, unread, read, kept, system
    assert "Secret plans" not in text and "Nobody else" not in text
    assert "never a subject or a body" in text


def test_the_sysop_establishes_a_refused_sender_node_from_the_refusal(db, lane, sysop):
    sender, message = _mail()
    ensure_node_subject(db, sender.fingerprint)
    record_link_mail_refusal(
        db, message.to_dict(), "link_policy_node_probationary_read_only", via=VIA_DIRECT,
        sender_node_fingerprint=sender.fingerprint,
    )
    session = FakeSession([
        "o", "m", "r", "o", "0", "1",  # Operations, Mail, Refused Link mail, open the only one
        "n",  # its node's trust
        "e", "r", "we know them", "s", "y",  # Establish: reason, save, confirm the deviation
        "b", "b", "b", "b", "b", "b",
    ])
    session.terminal_height = 60
    asyncio.run(admin_menu(session, lane, sysop))

    subject = TrustSubject.node(sender.fingerprint)
    for dimension in TrustDimension:
        assert get_effective_trust_state(db, subject, dimension).state == TrustState.ESTABLISHED
    text = _normalized_visible(_written_text(session))
    assert "its node is still on probation here" in text
    assert "Set to established in all three dimensions; audited." in text
    assert "Hi" not in text.split("Refused letter", 1)[1].split("Trust here", 1)[0]


def test_a_refused_sender_that_is_not_a_subject_has_no_trust_action(db, lane, sysop):
    _sender, message = _mail()
    record_link_mail_refusal(db, message.to_dict(), "link_policy_manual_block", via=VIA_RELAY)
    session = FakeSession(["o", "m", "r", "o", "0", "1", "b", "b", "b", "b", "b"])
    session.terminal_height = 60
    asyncio.run(admin_menu(session, lane, sysop))

    text = _normalized_visible(_written_text(session))
    letter = text[text.rindex("Refused letter"):]
    assert "not a trust subject here yet" in letter
    assert "[N]ode trust" not in letter and "[U]ser trust" not in letter


def test_a_peer_screen_establishes_blocks_and_clears(db, lane, sysop):
    link_context = _link_context()
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer, name="Alpha Peer"))
    ensure_node_subject(db, peer.fingerprint)
    subject = TrustSubject.node(peer.fingerprint)

    session = FakeSession([
        "s", "l", "p", "0", "1",  # Settings, Link status, Peers, the only node
        "k", "r", "spam", "s",  # Block: reason, save
        "c", "0", "1",  # Clear override: all three at once
        "e", "r", "reviewed", "s", "y",  # Establish
        "b", "b", "b", "b", "b", "b",
    ])
    session.terminal_height = 60
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _normalized_visible(_written_text(session))
    assert "Set to blocked in all three dimensions; audited." in text
    assert "Overrides cleared; recovery policy was recomputed." in text
    assert "Set to established in all three dimensions; audited." in text
    # Drawn again after each action, from the database.
    assert "Identity trust: blocked" in text
    assert "Identity trust: established" in text
    assert {item.state for item in list_trust_overrides(db, subject)} == {TrustState.ESTABLISHED}
    assert len(list_trust_overrides(db, subject)) == 3


def test_a_peer_list_candidate_offers_no_trust_action(db, lane, sysop):
    link_context = _link_context()
    candidate = bootstrap_node_identity("candidate")
    save_candidate_descriptor(db, candidate.fingerprint, _record(candidate, name="Beta Candidate").descriptor)
    session = FakeSession(["s", "l", "p", "0", "1", "b", "b", "b", "b", "b"])
    session.terminal_height = 60
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    text = _normalized_visible(_written_text(session))
    detail = text[text.rindex("Name: Beta Candidate"):]
    assert "[E]stablish" not in detail and "[T]rust details" not in detail
