"""
Tests for netbbs.mail (design doc round 93): local asynchronous
personal mail -- deliberately a different mechanism from /msg
(netbbs.chat.mailbox), which stays ephemeral and online-only.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.mail import (
    MAX_MAIL_PER_RECIPIENT,
    MailboxFullError,
    MailError,
    delete_for_recipient,
    delete_for_sender,
    get_mail,
    list_inbox,
    list_sent,
    mark_read,
    send_mail,
    unread_count,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2pw")


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2pw")


# -- sending ------------------------------------------------------------


def test_send_mail_round_trips(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "How are you?")
    assert message.sender_user_id == alice.id
    assert message.sender_label == "alice"
    assert message.recipient_user_id == bob.id
    assert message.subject == "Hello"
    assert message.body == "How are you?"
    assert message.is_read is False
    assert message.sender_deleted_at is None
    assert message.recipient_deleted_at is None


def test_send_mail_rejects_blank_subject(db, alice, bob):
    with pytest.raises(MailError, match="blank"):
        send_mail(db, alice, bob, "   ", "body")


def test_send_mail_rejects_oversized_subject(db, alice, bob):
    with pytest.raises(MailError, match="cannot exceed"):
        send_mail(db, alice, bob, "x" * 300, "body")


def test_send_mail_rejects_oversized_body(db, alice, bob):
    with pytest.raises(MailError, match="cannot exceed"):
        send_mail(db, alice, bob, "subject", "x" * 30_000)


# -- inbox / sent listing -------------------------------------------------


def test_list_inbox_shows_received_messages_newest_first(db, alice, bob):
    send_mail(db, alice, bob, "First", "one")
    send_mail(db, alice, bob, "Second", "two")

    inbox = list_inbox(db, bob)
    assert [m.subject for m in inbox] == ["Second", "First"]


def test_list_sent_shows_sent_messages(db, alice, bob):
    send_mail(db, alice, bob, "Hello", "body")

    assert [m.subject for m in list_sent(db, alice)] == ["Hello"]
    assert list_sent(db, bob) == []


def test_unread_count(db, alice, bob):
    assert unread_count(db, bob) == 0
    m1 = send_mail(db, alice, bob, "One", "body")
    send_mail(db, alice, bob, "Two", "body")
    assert unread_count(db, bob) == 2
    mark_read(db, bob, m1)
    assert unread_count(db, bob) == 1


# -- read state -----------------------------------------------------------


def test_mark_read_sets_read_at(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "body")
    assert message.is_read is False

    updated = mark_read(db, bob, message)
    assert updated.is_read is True


def test_mark_read_is_a_no_op_for_the_sender(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "body")
    updated = mark_read(db, alice, message)
    assert updated.is_read is False


def test_mark_read_is_idempotent(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "body")
    first = mark_read(db, bob, message)
    second = mark_read(db, bob, first)
    assert second.read_at == first.read_at


# -- access control ---------------------------------------------------------


def test_get_mail_denies_a_non_party_user(db, alice, bob):
    carol = create_user(db, "carol", password="hunter2pw")
    message = send_mail(db, alice, bob, "Hello", "body")
    with pytest.raises(MailError, match="not a party"):
        get_mail(db, carol, message.id)


def test_delete_for_recipient_denies_the_sender(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "body")
    with pytest.raises(MailError, match="not the recipient"):
        delete_for_recipient(db, alice, message)


def test_delete_for_sender_denies_the_recipient(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "body")
    with pytest.raises(MailError, match="not the sender"):
        delete_for_sender(db, bob, message)


# -- deletion: independent per side, hard-delete once both agree ------------


def test_recipient_delete_removes_it_from_their_inbox_but_not_sender_view(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "body")
    delete_for_recipient(db, bob, message)

    assert list_inbox(db, bob) == []
    assert [m.subject for m in list_sent(db, alice)] == ["Hello"]


def test_sender_delete_removes_it_from_sent_but_not_recipient_inbox(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "body")
    delete_for_sender(db, alice, message)

    assert list_sent(db, alice) == []
    assert [m.subject for m in list_inbox(db, bob)] == ["Hello"]


def test_message_hard_deletes_once_both_sides_have_deleted_it(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "body")
    delete_for_recipient(db, bob, message)
    delete_for_sender(db, alice, message)

    with pytest.raises(MailError, match="no such message"):
        get_mail(db, alice, message.id)


def test_deleting_only_one_side_does_not_hard_delete(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "body")
    delete_for_recipient(db, bob, message)

    # Row still exists -- sender can still see/act on their own copy.
    refetched = get_mail(db, alice, message.id)
    assert refetched.recipient_deleted_at is not None
    assert refetched.sender_deleted_at is None


def test_recipient_delete_is_idempotent(db, alice, bob):
    message = send_mail(db, alice, bob, "Hello", "body")
    delete_for_recipient(db, bob, message)
    delete_for_recipient(db, bob, message)  # must not raise
    assert list_inbox(db, bob) == []


# -- quota: bounce-not-silently-drop-unread (design doc round 93) ----------


def test_quota_evicts_the_oldest_already_read_message(db, alice, bob, monkeypatch):
    import netbbs.mail as mail_module

    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 2)

    first = send_mail(db, alice, bob, "First", "body")
    mark_read(db, bob, first)
    send_mail(db, alice, bob, "Second", "body")

    # Inbox is now at the (patched) cap of 2, with the oldest one read --
    # sending a third must silently evict "First", not bounce.
    send_mail(db, alice, bob, "Third", "body")

    subjects = [m.subject for m in list_inbox(db, bob)]
    assert subjects == ["Third", "Second"]


def test_quota_bounces_when_every_message_is_unread(db, alice, bob, monkeypatch):
    import netbbs.mail as mail_module

    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 2)

    send_mail(db, alice, bob, "First", "body")
    send_mail(db, alice, bob, "Second", "body")

    with pytest.raises(MailboxFullError, match="every message is still unread"):
        send_mail(db, alice, bob, "Third", "body")

    # The bounced message was never stored.
    subjects = [m.subject for m in list_inbox(db, bob)]
    assert subjects == ["Second", "First"]


def test_quota_default_is_generous(db, alice, bob):
    assert MAX_MAIL_PER_RECIPIENT >= 100


def test_deleted_messages_do_not_count_toward_the_quota(db, alice, bob, monkeypatch):
    import netbbs.mail as mail_module

    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 1)

    first = send_mail(db, alice, bob, "First", "body")
    delete_for_recipient(db, bob, first)

    # Recipient's inbox is empty again (the one message was deleted), so
    # a new message should send cleanly rather than bouncing.
    send_mail(db, alice, bob, "Second", "body")
    assert [m.subject for m in list_inbox(db, bob)] == ["Second"]
