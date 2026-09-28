"""Trust policy for Link mail (issue #804, design doc §12.4).

Node trust covers mail: a user on probation whose home node is established is
delivered; a quarantined or blocked sender, user or node, is refused and the
sender's node records a bounce. The sending node refuses a peer it holds on
probation at the To prompt, expires mail its own policy holds back when the
work item dead-letters, and wakes that mail once the peer is established.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from tests.link_sync_wait import run_sync_briefly as _run_sync_briefly

from netbbs.auth.users import create_user
from netbbs.identity.encryption import encrypt_for
from netbbs.link.enforcement import (
    REASON_MANUAL_BLOCK,
    REASON_NODE_PROBATIONARY,
    REASON_USER_PROBATIONARY,
    REASON_USER_QUARANTINED,
    decide_event_authorship,
)
from netbbs.link.events import build_endpoint_descriptor, build_link_message
from netbbs.link.mail import compose_link_message
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode, PeerRecord
from netbbs.link.store import save_peer
from netbbs.link.sync import _push_pending_link_mail, _wake_mail_for_established_targets, run_link_sync
from netbbs.link.transport import LinkServer, persist_accepted_events
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    register_subject,
    set_trust_override,
)
from netbbs.link.work_items import (
    KIND_LINK_MAIL_ACK,
    KIND_LINK_MAIL_DELIVERY,
    POLICY_REFUSED_TARGET_ERROR,
    list_work_items,
    load_due_work_items,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

NOW = "2026-01-01T00:00:00+00:00"


class _NodeDb:
    def __init__(self, tmp_path, name: str) -> None:
        self.db = Database(tmp_path / f"{name}.db")
        self.lane = DatabaseLane(self.db.path)

    def close(self) -> None:
        self.lane.close()
        self.db.close()


def _set_state(db, subject, state, dimensions=(TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR)):
    if db.connection.execute(
        "SELECT 1 FROM link_trust_subjects WHERE subject_id = ?", (subject.subject_id,)
    ).fetchone() is None:
        register_subject(db, subject, first_accepted_at=NOW, now_iso=NOW)
    for dimension in dimensions:
        set_trust_override(db, subject, dimension, state, reason="test", now_iso=NOW)


def _establish_node(db, fingerprint):
    _set_state(db, TrustSubject.node(fingerprint), TrustState.ESTABLISHED)


def _mail_envelope(home="home", user="nib"):
    return {"envelope": {"object_type": "link_message", "payload": {"sender": {
        "kind": "node_vouched_user", "home_node_fingerprint": home, "local_user_id": user,
    }}}}


def _seed_peer(db, identity):
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key,
        subject_fingerprint=identity.fingerprint,
        addresses=None,
        outgoing_only=True,
        created_at=NOW,
    )
    save_peer(db, PeerRecord(
        fingerprint=identity.fingerprint,
        root_public_key=bytes(identity.root.verify_key),
        transitions=identity.transitions,
        descriptor=descriptor,
    ))


# -- the receiving node's rule ------------------------------------------------


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def test_mail_from_a_probationary_user_of_an_established_node_is_delivered(db):
    _establish_node(db, "home")
    decision = decide_event_authorship(db, _mail_envelope(), transport_peer_fingerprint="home")
    assert decision.allowed
    assert not decision.requires_approval


def test_user_probation_still_holds_that_users_posts_and_refuses_other_content(db):
    _establish_node(db, "home")
    post = {"envelope": {"object_type": "board_post", "payload": {"author": {
        "home_node_fingerprint": "home", "opaque_user_id": "nib",
    }}}}
    assert decide_event_authorship(db, post, transport_peer_fingerprint="home").requires_approval
    upload = {"envelope": {"object_type": "file_descriptor", "payload": {"author": {
        "home_node_fingerprint": "home", "opaque_user_id": "nib",
    }}}}
    assert decide_event_authorship(
        db, upload, transport_peer_fingerprint="home"
    ).reason_code == REASON_USER_PROBATIONARY


def test_mail_from_a_node_still_on_probation_is_refused(db):
    decision = decide_event_authorship(db, _mail_envelope(), transport_peer_fingerprint="home")
    assert not decision.allowed
    assert decision.reason_code == REASON_NODE_PROBATIONARY


def test_mail_from_a_quarantined_user_or_a_blocked_node_is_refused(db):
    _establish_node(db, "home")
    _set_state(db, TrustSubject.user("home", "nib"), TrustState.QUARANTINED)
    assert decide_event_authorship(
        db, _mail_envelope(), transport_peer_fingerprint="home"
    ).reason_code == REASON_USER_QUARANTINED
    _set_state(db, TrustSubject.node("home"), TrustState.BLOCKED)
    assert decide_event_authorship(
        db, _mail_envelope(user="other"), transport_peer_fingerprint="home"
    ).reason_code == REASON_MANUAL_BLOCK


# -- mail picked up from a relay mailbox -------------------------------------


def _signed_mail(sender_identity, recipient_identity, *, user="nib"):
    ciphertext = encrypt_for(
        recipient_identity.signing_key.verify_key,
        json.dumps({"subject": "Hi", "body": "From afar"}).encode("utf-8"),
    )
    return build_link_message(
        signing_identity=sender_identity.signing_key,
        home_node_fingerprint=sender_identity.fingerprint,
        local_user_id=user,
        recipient_home_node_fingerprint=recipient_identity.fingerprint,
        recipient_local_user_id="bob",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=ciphertext,
        created_at=NOW,
    )


def _persist_picked_up(tmp_path, *, quarantine_user: bool):
    sender_identity = bootstrap_node_identity("sender")
    recipient_identity = bootstrap_node_identity("recipient")
    node = LinkNode(identity=recipient_identity)
    recipient = _NodeDb(tmp_path, "recipient")
    create_user(recipient.db, "bob", password="hunter2pw", user_level=10)
    _establish_node(recipient.db, sender_identity.fingerprint)
    if quarantine_user:
        _set_state(recipient.db, TrustSubject.user(sender_identity.fingerprint, "nib"), TrustState.QUARANTINED)
    message = _signed_mail(sender_identity, recipient_identity)
    node.events[message.content_id] = message.to_dict()

    asyncio.run(persist_accepted_events(
        recipient.lane, node, [message.content_id],
        sender_fingerprint=sender_identity.fingerprint, max_carried_boards=None,
        enforce_trust_policy=True,
    ))
    return recipient, sender_identity


def test_relayed_mail_from_a_quarantined_user_bounces_as_blocked_sender(tmp_path):
    recipient, _sender = _persist_picked_up(tmp_path, quarantine_user=True)
    try:
        assert recipient.db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
        ack = json.loads(recipient.db.connection.execute(
            "SELECT ack_event_json FROM link_mail_acknowledgements"
        ).fetchone()[0])
        assert ack["envelope"]["object_type"] == "link_message_bounced"
        assert ack["envelope"]["payload"]["reason"] == "blocked_sender"
    finally:
        recipient.close()


def test_relayed_mail_from_a_probationary_user_is_delivered_and_registers_the_sender(tmp_path):
    recipient, sender = _persist_picked_up(tmp_path, quarantine_user=False)
    try:
        row = recipient.db.connection.execute("SELECT subject FROM mail_messages").fetchone()
        assert row["subject"] == "Hi"
        # The receiving SysOp can find the sender to establish them.
        subject = TrustSubject.user(sender.fingerprint, "nib")
        assert recipient.db.connection.execute(
            "SELECT 1 FROM link_trust_subjects WHERE subject_id = ?", (subject.subject_id,)
        ).fetchone() is not None
    finally:
        recipient.close()


def test_a_bounce_goes_back_to_a_peer_still_on_probation(tmp_path):
    """Refused relayed mail from a node on probation here is answered with a
    bounce; this node's policy must not hold that bounce back."""
    sender_identity = bootstrap_node_identity("sender")
    recipient_identity = bootstrap_node_identity("recipient")
    node = LinkNode(identity=recipient_identity)
    recipient = _NodeDb(tmp_path, "recipient")
    try:
        create_user(recipient.db, "bob", password="hunter2pw", user_level=10)
        message = _signed_mail(sender_identity, recipient_identity)
        node.events[message.content_id] = message.to_dict()
        asyncio.run(persist_accepted_events(
            recipient.lane, node, [message.content_id],
            sender_fingerprint=sender_identity.fingerprint, max_carried_boards=None,
            enforce_trust_policy=True,
        ))
        assert recipient.db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0

        asyncio.run(_push_pending_link_mail(node, None, recipient.lane, enforce_trust_policy=True))
        (item,) = list_work_items(recipient.db, kind=KIND_LINK_MAIL_ACK)
        # Attempted (no address is known in this test), not held by policy.
        assert item.last_error != POLICY_REFUSED_TARGET_ERROR
    finally:
        recipient.close()


# -- the sending node's own policy -------------------------------------------


def _held_mail(tmp_path):
    """A message this node queued to a peer it still holds on probation."""
    identity = bootstrap_node_identity("sender")
    remote = bootstrap_node_identity("remote")
    sender = _NodeDb(tmp_path, "sender")
    alice = create_user(sender.db, "alice", password="hunter2pw", user_level=10)
    _seed_peer(sender.db, remote)
    message = compose_link_message(
        sender.db, alice, f"bob@{remote.fingerprint}", "Hi", "Held", node_identity=identity,
    )
    return sender, LinkNode(identity=identity), remote, message


def _status(db, message):
    return db.connection.execute(
        "SELECT link_delivery_status FROM mail_messages WHERE link_event_content_id = ?",
        (message.content_id,),
    ).fetchone()[0]


def test_mail_held_by_this_nodes_policy_expires_when_its_work_item_dead_letters(tmp_path):
    sender, node, _remote, message = _held_mail(tmp_path)
    try:
        sender.db.connection.execute("UPDATE link_work_items SET attempts = 9")
        sender.db.connection.commit()
        asyncio.run(_push_pending_link_mail(node, None, sender.lane, enforce_trust_policy=True))
        item = list_work_items(sender.db, kind=KIND_LINK_MAIL_DELIVERY)[0]
        assert item.status == "dead_lettered"
        assert item.last_error == POLICY_REFUSED_TARGET_ERROR
        assert _status(sender.db, message) == "expired"
    finally:
        sender.close()


def test_establishing_the_peer_wakes_mail_held_for_it(tmp_path):
    sender, node, remote, _message = _held_mail(tmp_path)
    try:
        asyncio.run(_push_pending_link_mail(node, None, sender.lane, enforce_trust_policy=True))
        assert load_due_work_items(sender.db, kind=KIND_LINK_MAIL_DELIVERY) == []

        # Still on probation: nothing wakes.
        _wake_mail_for_established_targets(sender.db)
        assert load_due_work_items(sender.db, kind=KIND_LINK_MAIL_DELIVERY) == []

        _establish_node(sender.db, remote.fingerprint)
        _wake_mail_for_established_targets(sender.db)
        due = load_due_work_items(sender.db, kind=KIND_LINK_MAIL_DELIVERY)
        assert len(due) == 1 and due[0].attempts == 1
    finally:
        sender.close()


# -- over a real transport: the recipient's refusal becomes a bounce ----------


def _exchange(tmp_path, *, establish_sender_at_recipient: bool):
    sender_identity = bootstrap_node_identity("sender")
    recipient_identity = bootstrap_node_identity("recipient")
    sender_node = LinkNode(identity=sender_identity)
    recipient_node = LinkNode(identity=recipient_identity)
    sender = _NodeDb(tmp_path, "sender")
    recipient = _NodeDb(tmp_path, "recipient")
    alice = create_user(sender.db, "alice", password="hunter2pw", user_level=10)
    create_user(recipient.db, "bob", password="hunter2pw", user_level=10)
    _establish_node(sender.db, recipient_identity.fingerprint)
    if establish_sender_at_recipient:
        _establish_node(recipient.db, sender_identity.fingerprint)

    async def scenario():
        recipient_server = LinkServer(
            host="127.0.0.1", port=0, node=recipient_node,
            own_hello_provider=lambda: recipient_node.build_hello(
                addresses=[{"protocol": "http", "address": "127.0.0.1", "port": recipient_server.port}],
                outgoing_only=False, created_at=NOW,
            ),
            lane=recipient.lane, enforce_trust_policy=True,
        )
        await recipient_server.start()
        seed_url = f"http://127.0.0.1:{recipient_server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                hello_pass = asyncio.create_task(run_link_sync(
                    sender_node, session, [seed_url],
                    lambda: sender_node.build_hello(addresses=None, outgoing_only=True, created_at=NOW),
                    sender.lane, interval_seconds=60.0,
                ))
                await _run_sync_briefly(hello_pass)
                message = compose_link_message(
                    sender.db, alice, f"bob@{recipient_identity.fingerprint}", "Hi", "Over the wire",
                    node_identity=sender_identity,
                )
                await _push_pending_link_mail(sender_node, session, sender.lane, enforce_trust_policy=True)
        finally:
            await recipient_server.stop()
        return message

    return sender, recipient, asyncio.run(scenario())


def test_a_recipient_policy_refusal_is_recorded_as_a_bounce_not_retried(tmp_path):
    sender, recipient, message = _exchange(tmp_path, establish_sender_at_recipient=False)
    try:
        assert _status(sender.db, message) == "bounced"
        item = list_work_items(sender.db, kind=KIND_LINK_MAIL_DELIVERY)[0]
        assert item.status == "pushed"
        assert recipient.db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
    finally:
        sender.close()
        recipient.close()


def test_a_probationary_user_of_an_established_node_gets_mail_through(tmp_path):
    sender, recipient, message = _exchange(tmp_path, establish_sender_at_recipient=True)
    try:
        row = recipient.db.connection.execute(
            "SELECT body, link_source_event_id FROM mail_messages"
        ).fetchone()
        assert row["body"] == "Over the wire"
        assert row["link_source_event_id"] == message.content_id
        assert list_work_items(recipient.db, kind=KIND_LINK_MAIL_ACK)
        assert _status(sender.db, message) == "pending"  # until the acceptance comes back
    finally:
        sender.close()
        recipient.close()
