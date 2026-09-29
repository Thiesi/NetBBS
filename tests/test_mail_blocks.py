"""Blocking a mail sender (issue #817, design doc §6.4 and §10.3).

A caller refuses mail from one sender: a local account, blocked by id, or a
Link sender, blocked by the `user@<fingerprint>` address its mail came from.
The sender is told -- refused at the To prompt and at Send locally, a
`blocked_by_recipient` bounce over Link -- and mail from the system or from
this node's SysOp cannot be blocked.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from netbbs import mail as mail_module
from netbbs.auth.users import SYSOP_LEVEL, create_user, delete_user, set_user_level
from netbbs.link.mail import bounce_reason_text, deliver_link_message
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.mail_refusals import TRUST_REASONS, refusal_reason_text
from netbbs.mail import (
    MailBlockError,
    MailSenderBlocked,
    block_link_sender,
    block_local_sender,
    blocks_link_sender,
    blocks_local_sender,
    list_inbox,
    list_mail_blocks,
    list_sent,
    mail_sender_refusal,
    send_mail,
    send_system_mail,
    unblock_local_sender,
)
from netbbs.net.mail_flow import blocked_senders_screen, browse_mail
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_link_mail import _incoming_message, _seed_peer
from tests.test_mail_flow import FakeSession as LineSession, _visible_text
from tests.test_mail_list import FakeSession


@pytest.fixture
def node(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    lane = DatabaseLane(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    set_redraw_in_place_enabled(db, bob, True)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    stamps = iter(f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}.000000Z" for i in range(3600))
    monkeypatch.setattr(mail_module, "utc_now_iso", lambda: next(stamps))
    yield db, lane, bob, alice
    lane.close()
    db.close()


@pytest.fixture
def identities():
    return bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")


def _bounce_reason(db) -> str:
    row = db.connection.execute("SELECT ack_event_json FROM link_mail_acknowledgements").fetchone()
    envelope = json.loads(row["ack_event_json"])["envelope"]
    assert envelope["object_type"] == "link_message_bounced"
    return envelope["payload"]["reason"]


# -- the rules ---------------------------------------------------------------------


def test_a_blocked_local_sender_is_refused_with_a_reason_they_can_read(node):
    db, _, bob, alice = node
    assert block_local_sender(db, bob, alice) is True

    with pytest.raises(MailSenderBlocked, match="bob does not accept mail from you."):
        send_mail(db, alice, bob, "Hello", "again")

    assert list_inbox(db, bob) == []
    assert list_sent(db, alice) == []


def test_a_block_is_one_sender_and_one_direction(node):
    db, _, bob, alice = node
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    block_local_sender(db, bob, alice)

    send_mail(db, carol, bob, "From carol", "fine")
    send_mail(db, bob, alice, "From bob", "bob may still write to alice")

    assert [m.subject for m in list_inbox(db, bob)] == ["From carol"]
    assert [m.subject for m in list_inbox(db, alice)] == ["From bob"]


def test_unblocking_lets_mail_through_again(node):
    db, _, bob, alice = node
    block_local_sender(db, bob, alice)
    assert block_local_sender(db, bob, alice) is False  # already blocked

    assert unblock_local_sender(db, bob, alice.id) is True
    send_mail(db, alice, bob, "Hello", "again")

    assert len(list_inbox(db, bob)) == 1


def test_a_block_follows_the_account_through_a_rename(node):
    db, _, bob, alice = node
    block_local_sender(db, bob, alice)
    db.connection.execute("UPDATE users SET username = 'alicia' WHERE id = ?", (alice.id,))
    db.connection.commit()

    assert mail_sender_refusal(db, bob, sender=alice) is not None


def test_nobody_can_block_themselves(node):
    db, _, bob, _ = node
    with pytest.raises(MailBlockError, match="your own mail"):
        block_local_sender(db, bob, bob)


def test_the_sysop_cannot_be_blocked(node):
    db, _, bob, _ = node
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)

    with pytest.raises(MailBlockError, match="runs this BBS"):
        block_local_sender(db, bob, sysop)
    assert list_mail_blocks(db, bob) == []


def test_a_block_does_not_hold_against_someone_who_became_sysop(node):
    db, _, bob, alice = node
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    block_local_sender(db, bob, alice)
    alice = set_user_level(db, alice, SYSOP_LEVEL, changed_by=sysop)

    send_mail(db, alice, bob, "Account notice", "from your SysOp")

    assert len(list_inbox(db, bob)) == 1


def test_system_mail_is_never_blocked(node):
    db, _, bob, alice = node
    block_local_sender(db, bob, alice)

    send_system_mail(db, bob, "Notice", "from the BBS")

    assert [m.subject for m in list_inbox(db, bob)] == ["Notice"]


def test_deleting_either_account_removes_the_block(node):
    db, _, bob, alice = node
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    block_local_sender(db, bob, alice)
    block_link_sender(db, bob, "dave@" + "ab" * 16)
    block_local_sender(db, carol, bob)

    delete_user(db, alice, deleted_by=sysop)
    assert [b.blocked_address for b in list_mail_blocks(db, bob)] == ["dave@" + "ab" * 16]

    delete_user(db, bob, deleted_by=sysop)
    count = db.connection.execute("SELECT COUNT(*) FROM mail_blocks").fetchone()[0]
    assert count == 0


# -- Link mail -----------------------------------------------------------------------


def test_link_mail_from_a_blocked_sender_bounces_blocked_by_recipient(node, identities):
    db, _, bob, _ = node
    home, remote = identities
    block_link_sender(db, bob, f"dave@{remote.fingerprint}")
    message = _incoming_message(home, remote, recipient="bob", sender="Dave")

    deliver_link_message(db, message.to_dict(), node_identity=home)

    assert list_inbox(db, bob) == []
    assert _bounce_reason(db) == "blocked_by_recipient"
    # Not the node-policy wording `blocked_sender` has: this was the person.
    assert bounce_reason_text("blocked_by_recipient") == "the recipient does not accept mail from you"
    assert "BBS" not in bounce_reason_text("blocked_by_recipient")
    # And the recipient node's SysOp sees it in the refusal log (#820) as
    # the person's choice, not a trust matter.
    assert refusal_reason_text("blocked_by_recipient") == "the recipient blocked its sender"
    assert "blocked_by_recipient" not in TRUST_REASONS


def test_link_mail_from_anyone_else_on_that_node_still_arrives(node, identities):
    db, _, bob, _ = node
    home, remote = identities
    block_link_sender(db, bob, f"dave@{remote.fingerprint}")
    message = _incoming_message(home, remote, recipient="bob", sender="erin")

    deliver_link_message(db, message.to_dict(), node_identity=home)

    assert len(list_inbox(db, bob)) == 1


# -- the To prompt and Send -------------------------------------------------------------


def test_the_to_prompt_refuses_a_recipient_who_blocked_you_and_asks_again(node):
    db, lane, bob, alice = node
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    block_local_sender(db, bob, alice)
    session = LineSession(keys=["c", "s", "b"], lines=["bob", "carol", "Hello", "Hi there", "/done"])

    asyncio.run(browse_mail(session, lane, alice))

    text = " ".join(_visible_text(session).split())
    assert "bob does not accept mail from you." in text
    assert "Message sent." in text
    assert list_inbox(db, bob) == []
    assert [m.subject for m in list_inbox(db, carol)] == ["Hello"]


def test_send_refuses_a_recipient_who_blocked_you_while_you_wrote(node, monkeypatch):
    db, lane, bob, alice = node
    session = LineSession(keys=["c", "s", "c", "y", "b", "b"], lines=["bob", "Hello", "Hi there", "/done"])
    real_send = mail_module.send_mail

    def block_then_send(db_, sender, recipient, subject, body, **kwargs):
        block_local_sender(db_, recipient, sender)
        return real_send(db_, sender, recipient, subject, body, **kwargs)

    monkeypatch.setattr("netbbs.net.mail_flow.send_mail", block_then_send)
    asyncio.run(browse_mail(session, lane, alice))

    assert "Could not send: bob does not accept mail from you." in " ".join(_visible_text(session).split())
    assert list_inbox(db, bob) == []


# -- blocking from a letter ----------------------------------------------------------------


def test_k_on_a_letter_blocks_its_sender_and_again_unblocks(node):
    db, lane, bob, alice = node
    send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(["1", "k", "k", "b", "b"])

    asyncio.run(browse_mail(session, lane, bob))

    screens = session.screens()
    assert "Bloc[k] sender" in screens[1]
    assert "Blocked alice: mail from them is refused from now on" in " ".join(screens[2].split())
    assert "Unbloc[k] sender" in screens[2]
    assert "Unblocked alice: their mail is accepted again." in " ".join(screens[3].split())
    assert not blocks_local_sender(db, bob, alice)


def test_k_on_link_mail_blocks_the_senders_address(node, identities):
    db, lane, bob, _ = node
    home, remote = identities
    deliver_link_message(db, _incoming_message(home, remote, recipient="bob", sender="dave").to_dict(), node_identity=home)
    session = FakeSession(["1", "k", "b", "b"])

    asyncio.run(browse_mail(session, lane, bob))

    assert blocks_link_sender(db, bob, f"dave@{remote.fingerprint}")


def test_system_mail_and_sysop_mail_offer_no_block(node):
    db, lane, bob, _ = node
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    send_system_mail(db, bob, "Notice", "from the BBS")
    send_mail(db, sysop, bob, "Welcome", "from your SysOp")
    session = FakeSession(["1", "b", "2", "b", "b"])

    asyncio.run(browse_mail(session, lane, bob))

    views = [screen for screen in session.screens() if "Date:" in screen]
    assert len(views) == 2
    assert all("sender" not in view.split("Date:")[1] for view in views)
    assert list_mail_blocks(db, bob) == []


# -- Profile > Blocked senders ----------------------------------------------------------------


def test_the_list_adds_by_name_and_unblocks(node):
    db, lane, bob, alice = node
    session = FakeSession(["a", "alice", "u", "01", "b"])

    asyncio.run(blocked_senders_screen(session, lane, bob))

    screens = session.screens()
    assert "You block no one." in screens[0]
    assert "Blocked alice: mail from them is refused from now on" in " ".join(screens[1].split())
    assert "alice" in screens[1] and "on this BBS" in screens[1]
    assert "Unblocked alice" in screens[-1]
    assert list_mail_blocks(db, bob) == []


def test_the_list_adds_a_link_sender_by_address(node, identities):
    db, lane, bob, _ = node
    _, remote = identities
    _seed_peer(db, remote)
    session = FakeSession(["a", f"Dave@{remote.fingerprint}", "b"])

    asyncio.run(blocked_senders_screen(session, lane, bob))

    assert blocks_link_sender(db, bob, f"dave@{remote.fingerprint}")
    assert "on a linked BBS" in session.screens()[-1]


def test_the_list_says_why_the_sysop_cannot_be_added(node):
    db, lane, bob, _ = node
    create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    session = FakeSession(["a", "sysop", "a", "nobody", "b"])

    asyncio.run(blocked_senders_screen(session, lane, bob))

    text = " ".join(session.visible().split())
    assert "sysop runs this BBS; mail from its SysOp can't be blocked." in text
    assert "No such user: 'nobody'" in text
    assert list_mail_blocks(db, bob) == []


def test_the_list_says_a_block_on_a_sysop_is_not_applied(node):
    db, lane, bob, alice = node
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    block_local_sender(db, bob, alice)
    set_user_level(db, alice, SYSOP_LEVEL, changed_by=sysop)
    session = FakeSession(["b"])

    asyncio.run(blocked_senders_screen(session, lane, bob))

    assert "a SysOp now, so not applied" in " ".join(session.screens()[-1].split())
