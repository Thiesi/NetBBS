"""Mailbox integrity (issue #818, the last §5 slice of the mail audit #803).

Three things a mailbox must not do quietly:

- fill up without its owner knowing: the Inbox counts against its cap
  ("N of 500"), warns as it nears it, and the owner is told at the main menu
  when the cap removed old read mail to make room;
- take mail for an account that cannot read it: a disabled account and a
  signup awaiting approval refuse local mail in plain words, and Link mail
  bounces `recipient_unavailable`;
- lose a sender's Sent copy when the recipient's account is deleted, or
  leave behind rows nobody can see or delete.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user, delete_user, get_user_by_username, set_user_disabled
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.link.events import _VALID_BOUNCE_REASONS
from netbbs.link.mail import bounce_reason_text, deliver_link_message
from netbbs.link.mail_refusals import refusal_reason_text
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import (
    MAILBOX_NEARLY_FULL,
    MAX_MAIL_PER_RECIPIENT,
    MailboxFullError,
    MailRecipientRefused,
    acknowledge_eviction_notice,
    delete_for_recipient,
    delete_for_sender,
    get_mail,
    inbox_count,
    list_inbox,
    list_sent,
    mail_recipient_bounce_reason,
    mark_read,
    pending_eviction_notice,
    send_mail,
    send_system_mail,
)
from netbbs.net.char_input import InputHistory
from netbbs.net.mail_flow import _display_recipient_label, browse_mail, mailbox_capacity_note
from netbbs.net.main_menu import _main_menu
from netbbs.storage import database as database_module
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.storage.migrations import MIGRATIONS
from tests.legacy_schema import insert_user_on_old_schema
from tests.test_link_mail import _incoming_message
from tests.test_mail_flow import FakeSession, _visible_text
from tests.test_mail_list import FakeSession as ListSession


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
    return create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2pw", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2pw", user_level=10)


def _fill_inbox(db, sender, recipient, count, *, read):
    """`count` letters straight into `recipient`'s inbox: the cap is 500, and
    500 sends through `send_mail` would each commit."""
    db.connection.executemany(
        """
        INSERT INTO mail_messages (sender_user_id, sender_label, recipient_user_id, subject, body, created_at, read_at)
        VALUES (?, ?, ?, ?, 'body', '2026-01-01T00:00:00.000000Z', ?)
        """,
        [
            (sender.id, sender.username, recipient.id, f"old {i}", "2026-01-02T00:00:00.000000Z" if read else None)
            for i in range(count)
        ],
    )
    db.connection.commit()


def _bounce_reason(db):
    row = db.connection.execute("SELECT ack_event_json FROM link_mail_acknowledgements").fetchone()
    envelope = json.loads(row["ack_event_json"])["envelope"]
    assert envelope["object_type"] == "link_message_bounced"
    return envelope["payload"]["reason"]


def _menu(db, lane, user, keys):
    session = FakeSession(keys=keys, lines=["y"])
    asyncio.run(_main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user, lane=lane))
    return session


# -- the cap, visible ------------------------------------------------------------


def test_the_inbox_counts_against_the_cap(db, lane, alice, bob):
    send_mail(db, alice, bob, "Hello", "body")
    session = ListSession(["b"])

    asyncio.run(browse_mail(session, lane, bob))

    assert f"1 unread message · 1 of {MAX_MAIL_PER_RECIPIENT}" in session.visible()
    assert "nearly full" not in session.visible()


def test_a_nearly_full_inbox_warns_its_owner(db, lane, alice, bob):
    _fill_inbox(db, alice, bob, MAILBOX_NEARLY_FULL, read=True)
    session = ListSession(["b"])

    asyncio.run(browse_mail(session, lane, bob))

    text = " ".join(session.visible().split())
    assert f"{MAILBOX_NEARLY_FULL} of {MAX_MAIL_PER_RECIPIENT}" in text
    assert "Your mailbox is nearly full" in text
    assert "Unread mail is never removed." in text


def test_the_capacity_note_says_what_happens_at_each_stage():
    assert mailbox_capacity_note(MAILBOX_NEARLY_FULL - 1, 0) is None
    assert mailbox_capacity_note(MAILBOX_NEARLY_FULL, 0)[1] == "warning"
    full_text, tone = mailbox_capacity_note(MAX_MAIL_PER_RECIPIENT, 3)
    assert tone == "warning" and "removes your oldest read one" in full_text
    all_unread, tone = mailbox_capacity_note(MAX_MAIL_PER_RECIPIENT, MAX_MAIL_PER_RECIPIENT)
    assert tone == "error" and "turned away" in all_unread


def test_a_removal_to_make_room_is_counted_and_told_once(db, lane, alice, bob):
    _fill_inbox(db, alice, bob, MAX_MAIL_PER_RECIPIENT, read=True)
    send_mail(db, alice, bob, "New one", "body")
    send_mail(db, alice, bob, "Newer one", "body")

    assert inbox_count(db, bob) == MAX_MAIL_PER_RECIPIENT
    line, evicted = pending_eviction_notice(db, bob)
    assert evicted == 2
    assert "your 2 oldest read messages were removed" in line
    # Nothing about which ones.
    assert "old 0" not in line

    session = _menu(db, lane, bob, ["l"])
    assert "your 2 oldest read messages were removed to make room" in " ".join(_visible_text(session).split())
    assert pending_eviction_notice(db, bob) == (None, 0)


def test_a_removal_after_the_notice_was_read_is_told_next_time(db, alice, bob):
    _fill_inbox(db, alice, bob, MAX_MAIL_PER_RECIPIENT, read=True)
    send_mail(db, alice, bob, "New one", "body")
    _, evicted = pending_eviction_notice(db, bob)
    send_mail(db, alice, bob, "While it was on screen", "body")

    acknowledge_eviction_notice(db, bob, evicted)

    line, evicted = pending_eviction_notice(db, bob)
    assert evicted == 1 and "your oldest read message was removed" in line


def test_link_delivery_counts_its_removals_too(db, alice, bob):
    node_identity = bootstrap_node_identity("roanoke")
    remote = bootstrap_node_identity("farpoint")
    _fill_inbox(db, alice, bob, MAX_MAIL_PER_RECIPIENT, read=True)

    deliver_link_message(db, _incoming_message(node_identity, remote).to_dict(), node_identity=node_identity)

    assert pending_eviction_notice(db, bob)[1] == 1


def test_an_inbox_full_of_unread_mail_removes_nothing_and_counts_nothing(db, alice, bob):
    _fill_inbox(db, alice, bob, MAX_MAIL_PER_RECIPIENT, read=False)
    with pytest.raises(MailboxFullError):
        send_mail(db, alice, bob, "No room", "body")
    assert pending_eviction_notice(db, bob) == (None, 0)


# -- accounts that take no mail -----------------------------------------------------


def test_a_disabled_account_refuses_local_and_system_mail(db, sysop, alice, bob):
    bob = set_user_disabled(db, bob, True, changed_by=sysop)
    with pytest.raises(MailRecipientRefused, match="bob's account is disabled, so it can't receive mail."):
        send_mail(db, alice, bob, "Hello", "body")
    with pytest.raises(MailRecipientRefused):
        send_system_mail(db, bob, "Notice", "body")
    assert list_inbox(db, bob) == []


def test_mail_already_in_a_disabled_account_stays_and_new_mail_returns_with_it(db, sysop, alice, bob):
    send_mail(db, alice, bob, "Before", "body")
    bob = set_user_disabled(db, bob, True, changed_by=sysop)
    assert [m.subject for m in list_inbox(db, bob)] == ["Before"]

    bob = set_user_disabled(db, bob, False, changed_by=sysop)
    send_mail(db, alice, bob, "After", "body")
    assert {m.subject for m in list_inbox(db, bob)} == {"Before", "After"}


def test_a_signup_awaiting_approval_refuses_mail(db, alice):
    waiting = create_user(db, "newbie", password="hunter2pw", user_level=10, pending_approval=True)
    with pytest.raises(MailRecipientRefused, match="still waiting for approval"):
        send_mail(db, alice, waiting, "Welcome", "body")


@pytest.mark.parametrize("state", ["disabled", "pending"])
def test_link_mail_to_an_account_that_takes_none_bounces_recipient_unavailable(db, sysop, state):
    if state == "disabled":
        bob = create_user(db, "bob", password="hunter2pw", user_level=10)
        set_user_disabled(db, bob, True, changed_by=sysop)
    else:
        create_user(db, "bob", password="hunter2pw", user_level=10, pending_approval=True)
    node_identity = bootstrap_node_identity("roanoke")
    remote = bootstrap_node_identity("farpoint")

    deliver_link_message(db, _incoming_message(node_identity, remote).to_dict(), node_identity=node_identity)

    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0
    assert _bounce_reason(db) == "recipient_unavailable"


def test_the_unavailable_bounce_does_not_say_why():
    assert "recipient_unavailable" in _VALID_BOUNCE_REASONS
    sender_text = bounce_reason_text("recipient_unavailable")
    assert sender_text == "that account is not taking mail at the moment"
    assert "disabled" not in sender_text and "approv" not in sender_text
    # The refusing node's SysOp is told plainly.
    assert "disabled" in refusal_reason_text("recipient_unavailable")


def test_the_guest_account_still_bounces_no_mailbox(db, alice):
    from netbbs.guest import set_guest_user

    set_guest_user(db, alice)
    assert mail_recipient_bounce_reason(db, alice) == "no_mailbox"


def test_the_to_prompt_refuses_a_disabled_account_and_asks_again(db, lane, sysop, alice, bob):
    set_user_disabled(db, bob, True, changed_by=sysop)
    session = FakeSession(keys=["c", "s", "b"], lines=["bob", "sysop", "Hello", "Hi there", "/done"])

    asyncio.run(browse_mail(session, lane, alice))

    text = " ".join(_visible_text(session).split())
    assert "bob's account is disabled, so it can't receive mail." in text
    assert "Message sent." in text
    assert list_inbox(db, bob) == []


# -- deleting an account ------------------------------------------------------------


def test_deleting_the_recipient_keeps_the_senders_sent_copy(db, lane, sysop, alice, bob):
    sent = send_mail(db, alice, bob, "Hello", "body")

    delete_user(db, bob, deleted_by=sysop)

    [kept] = list_sent(db, alice)
    assert kept.id == sent.id
    assert kept.recipient_user_id is None and kept.recipient_remote_address is None
    assert kept.recipient_label == "bob"
    assert asyncio.run(_label(lane, kept)) == "bob (deleted account)"

    delete_for_sender(db, alice, kept)
    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0


async def _label(lane, message):
    return await _display_recipient_label(lane, message)


def test_the_sent_list_names_the_deleted_recipient(db, lane, sysop, alice, bob):
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    send_mail(db, alice, bob, "To bob", "body")
    send_mail(db, alice, carol, "To carol", "body")
    delete_user(db, bob, deleted_by=sysop)
    delete_user(db, carol, deleted_by=sysop)
    session = ListSession(["s", "b", "b"])

    asyncio.run(browse_mail(session, lane, alice))

    text = session.visible()
    # Two deleted recipients are two names, not one cached label.
    assert "bob (deleted account)" in text
    assert "carol (deleted acc" in text  # the column cuts the rest


def test_a_letter_no_one_can_see_goes_with_the_account(db, sysop, alice, bob):
    sent = send_mail(db, alice, bob, "Deleted from Sent", "body")
    delete_for_sender(db, alice, sent)
    send_mail(db, bob, bob, "Note to self", "body")
    send_system_mail(db, bob, "Notice", "body")

    delete_user(db, bob, deleted_by=sysop)

    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0


def test_a_letter_the_recipient_deleted_stays_for_its_sender(db, sysop, alice, bob):
    sent = send_mail(db, alice, bob, "Hello", "body")
    delete_for_recipient(db, bob, get_mail(db, bob, sent.id))

    delete_user(db, bob, deleted_by=sysop)

    assert [m.subject for m in list_sent(db, alice)] == ["Hello"]


def test_a_deleted_senders_letter_goes_when_its_recipient_deletes_it(db, sysop, alice, bob):
    sent = send_mail(db, alice, bob, "Hello", "body")
    delete_user(db, alice, deleted_by=sysop)
    [received] = list_inbox(db, bob)
    assert received.sender_label == "alice"

    delete_for_recipient(db, bob, received)

    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages WHERE id = ?", (sent.id,)).fetchone()[0] == 0


def test_deleting_a_sender_drops_a_letter_its_recipient_already_deleted(db, sysop, alice, bob):
    sent = send_mail(db, alice, bob, "Hello", "body")
    delete_for_recipient(db, bob, get_mail(db, bob, sent.id))

    delete_user(db, alice, deleted_by=sysop)

    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0


def test_link_mail_goes_when_its_recipient_deletes_it(db, bob):
    node_identity = bootstrap_node_identity("roanoke")
    remote = bootstrap_node_identity("farpoint")
    deliver_link_message(db, _incoming_message(node_identity, remote).to_dict(), node_identity=node_identity)
    [received] = list_inbox(db, bob)

    delete_for_recipient(db, bob, mark_read(db, bob, received))

    assert db.connection.execute("SELECT COUNT(*) FROM mail_messages").fetchone()[0] == 0


def test_a_deleted_senders_outbound_link_mail_keeps_its_row(db, sysop, alice):
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_remote_address, subject, body, created_at,
             link_event_content_id, link_delivery_status)
        VALUES (?, 'alice', 'bob@abc', 'Out', 'body', '2026-01-01T00:00:00.000000Z', 'content-1', 'pending')
        """,
        (alice.id,),
    )
    db.connection.commit()

    delete_user(db, alice, deleted_by=sysop)

    row = db.connection.execute("SELECT sender_user_id, link_delivery_status FROM mail_messages").fetchone()
    assert row["sender_user_id"] is None and row["link_delivery_status"] == "pending"


def test_a_row_with_no_recipient_of_either_kind_is_still_refused(db, alice):
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO mail_messages (sender_user_id, sender_label, subject, body, created_at) "
            "VALUES (?, 'alice', 's', 'b', '2026-01-01T00:00:00.000000Z')",
            (alice.id,),
        )


# -- the upgrade ------------------------------------------------------------------


def test_the_upgrade_keeps_mail_and_clears_rows_no_one_can_see(tmp_path, monkeypatch):
    index = next(i for i, m in enumerate(MIGRATIONS) if m.description.startswith("Issue #818"))
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    old = Database(tmp_path / "node.db")
    alice = insert_user_on_old_schema(old, "alice", user_level=10)
    bob = insert_user_on_old_schema(old, "bob", user_level=10)
    stamp = "2026-01-01T00:00:00.000000Z"
    conn = old.connection
    rows = [
        # (subject, sender_user_id, recipient_user_id, remote, sender_deleted_at, recipient_deleted_at, source)
        ("local", alice.id, bob.id, None, None, None, None),
        ("outbound", alice.id, None, "carol@abc", None, None, None),
        ("link in, kept", None, bob.id, None, None, None, "ev-1"),
        ("link in, deleted", None, bob.id, None, None, stamp, "ev-2"),
        ("deleted sender, deleted", None, bob.id, None, None, stamp, None),
        ("recipient deleted it", alice.id, bob.id, None, None, stamp, None),
    ]
    for subject, sender, recipient, remote, s_del, r_del, source in rows:
        conn.execute(
            """
            INSERT INTO mail_messages
                (sender_user_id, sender_label, recipient_user_id, recipient_remote_address, subject, body,
                 created_at, sender_deleted_at, recipient_deleted_at, link_source_event_id,
                 link_delivery_reason, link_delivery_notice_pending, from_system)
            VALUES (?, 'x', ?, ?, ?, 'body', ?, ?, ?, ?, 'mailbox_full', 1, 0)
            """,
            (sender, recipient, remote, subject, stamp, s_del, r_del, source),
        )
    conn.commit()
    old.close()

    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)
    upgraded = Database(tmp_path / "node.db")
    try:
        conn = upgraded.connection
        subjects = {row["subject"] for row in conn.execute("SELECT subject FROM mail_messages")}
        assert subjects == {"local", "outbound", "link in, kept", "recipient deleted it"}
        kept = conn.execute("SELECT * FROM mail_messages WHERE subject = 'link in, kept'").fetchone()
        assert kept["sender_deleted_at"] is not None and kept["recipient_deleted_at"] is None
        # Columns 93 and 94 added survive the rebuild.
        local = conn.execute("SELECT * FROM mail_messages WHERE subject = 'local'").fetchone()
        assert local["link_delivery_reason"] == "mailbox_full" and local["link_delivery_notice_pending"] == 1
        assert local["from_system"] == 0 and local["recipient_label"] is None
        indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {
            "idx_mail_messages_recipient", "idx_mail_messages_sender", "idx_mail_messages_link_event_content_id",
            "idx_mail_messages_link_pending", "idx_mail_messages_link_delivery_notice",
        } <= indexes
        fk = {row["from"]: row["on_delete"] for row in conn.execute("PRAGMA foreign_key_list(mail_messages)")}
        assert fk == {"sender_user_id": "SET NULL", "recipient_user_id": "SET NULL"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        sysop = create_user(upgraded, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
        delete_user(upgraded, get_user_by_username(upgraded, "bob"), deleted_by=sysop)
        left = {row["subject"] for row in conn.execute("SELECT subject FROM mail_messages")}
        # alice keeps both Sent copies; bob's Link mail went with him.
        assert left == {"local", "outbound", "recipient deleted it"}
    finally:
        upgraded.close()


def test_removing_an_account_without_releasing_its_mail_fails_loudly(db, alice, bob):
    # The worklog's invariant: the CHECK refuses the SET NULL, so a future
    # path that deletes a user row directly cannot orphan mail in silence.
    send_mail(db, alice, bob, "Hello", "body")
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute("DELETE FROM users WHERE id = ?", (bob.id,))
    db.connection.rollback()


def test_the_removal_notice_waits_while_mail_is_closed_to_its_owner(db, lane, alice, bob):
    from netbbs.config import set_mail_min_level

    _fill_inbox(db, alice, bob, MAX_MAIL_PER_RECIPIENT, read=True)
    send_mail(db, alice, bob, "New one", "body")
    set_mail_min_level(db, 20)

    session = _menu(db, lane, bob, ["l"])

    assert "removed to make room" not in " ".join(_visible_text(session).split())
    assert pending_eviction_notice(db, bob)[1] == 1
