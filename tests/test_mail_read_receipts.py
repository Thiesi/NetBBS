"""
Read receipts for local mail (issue #829).

On by default, opted out of in Profile, reciprocal (a caller who does not
share receipts sees none), and shown only when both sides shared receipts at
the first reading and both share them now (issue #922). A recipient who
opted out is always named as such in the sender's Sent view; a letter
deleted unopened is simply not read. Link and system mail have none.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user, delete_user, get_user_by_id
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import (
    RECEIPT_HIDDEN,
    RECEIPT_NOT_READ,
    RECEIPT_READ,
    RECEIPT_WITHHELD,
    delete_for_recipient,
    get_mail,
    list_inbox,
    list_sent,
    mark_read,
    mark_unread,
    new_mail_group_id,
    read_receipts,
    send_mail,
    send_system_mail,
    send_to_all_callers,
    set_shares_read_receipts,
    shares_read_receipts,
)
from netbbs.mail_groups import LetterRecipient, send_letter
from netbbs.net import profile_flow
from netbbs.net.mail_flow import browse_mail
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_login_flow_fullscreen_editor import FakeSession as ProfileSession
from tests.test_login_flow_fullscreen_editor import squeezed
from tests.test_mail_flow import FakeSession, _link_context_with_known_peer, _visible_text


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "node.db"


@pytest.fixture
def db(db_path):
    database = Database(db_path)
    yield database
    database.close()


@pytest.fixture
def lane(db_path, db):
    lane = DatabaseLane(db_path)
    yield lane
    lane.close()


def _user(db, name, **kwargs):
    kwargs.setdefault("user_level", 10)
    return create_user(db, name, password="hunter2pw", **kwargs)


def _read(db, reader):
    for message in list_inbox(db, reader):
        mark_read(db, reader, message)


def _receipt(db, sender, message):
    return read_receipts(db, sender, [get_mail(db, sender, message.id)])[message.id]


def _open_first_sent(db, lane, sender, *, width=80):
    session = FakeSession(keys=["s", "1", "b", "b", "b"])
    session.terminal_width = width
    asyncio.run(browse_mail(session, lane, sender))
    return _visible_text(session)


# -- the receipt --------------------------------------------------------------------


def test_receipts_are_on_by_default(db):
    assert shares_read_receipts(db, _user(db, "alice"))


def test_the_first_reading_is_the_receipt_and_mark_unread_keeps_it(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    message = send_mail(db, alice, bob, "Hello", "body")
    assert _receipt(db, alice, message).state == RECEIPT_NOT_READ

    first = mark_read(db, bob, message)
    assert first.first_read_at == first.read_at
    unread = mark_unread(db, bob, first)
    assert unread.read_at is None and unread.first_read_at == first.first_read_at
    again = mark_read(db, bob, unread)

    # Reading it again does not move the receipt.
    assert again.first_read_at == first.first_read_at
    receipt = _receipt(db, alice, message)
    assert (receipt.state, receipt.read_at) == (RECEIPT_READ, first.first_read_at)


def test_a_letter_deleted_unread_is_just_not_read(db, lane):
    # The recipient's deletion stays their own (issue #922).
    alice, bob = _user(db, "alice"), _user(db, "bob")
    message = send_mail(db, alice, bob, "Hello", "body")
    delete_for_recipient(db, bob, message)

    assert _receipt(db, alice, message).state == RECEIPT_NOT_READ
    text = _open_first_sent(db, lane, alice)
    assert re.search(r"1  bob +Hello +not read", text)
    assert "Read: not yet" in text
    assert "deleted" not in text.lower()


# -- the peek loophole (issue #922) -------------------------------------------------


def test_a_sender_who_turns_receipts_on_briefly_sees_nothing_read_while_off(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    message = send_mail(db, alice, bob, "Hello", "body")
    set_shares_read_receipts(db, alice, False)
    _read(db, bob)

    set_shares_read_receipts(db, alice, True)

    assert _receipt(db, alice, message).state == RECEIPT_NOT_READ


def test_a_reading_while_the_recipient_opted_out_never_becomes_a_receipt(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    message = send_mail(db, alice, bob, "Hello", "body")
    set_shares_read_receipts(db, bob, False)
    read = mark_read(db, bob, message)
    assert read.first_read_shared is False

    set_shares_read_receipts(db, bob, True)
    assert _receipt(db, alice, message).state == RECEIPT_NOT_READ

    # Nor does reading it again, now that both share: the first reading
    # is the one a receipt would report.
    again = mark_read(db, bob, mark_unread(db, bob, read))
    assert again.first_read_at == read.first_read_at and again.first_read_shared is False
    assert _receipt(db, alice, message).state == RECEIPT_NOT_READ


def test_a_reading_made_while_both_shared_is_recorded_as_shared(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    message = send_mail(db, alice, bob, "Hello", "body")
    read = mark_read(db, bob, message)
    assert read.first_read_shared is True

    # Reading it again after either side opted out does not unshare it.
    set_shares_read_receipts(db, bob, False)
    again = mark_read(db, bob, mark_unread(db, bob, read))
    assert again.first_read_shared is True


def test_opting_out_hides_receipts_both_ways_and_already_given_ones_too(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    to_bob = send_mail(db, alice, bob, "To Bob", "body")
    to_alice = send_mail(db, bob, alice, "To Alice", "body")
    _read(db, bob)
    _read(db, alice)

    set_shares_read_receipts(db, bob, False)

    # Bob's reading is no longer shown -- and Alice is told why.
    assert _receipt(db, alice, to_bob).state == RECEIPT_WITHHELD
    # Bob no longer sees Alice's, though she shares hers.
    assert _receipt(db, bob, to_alice).state == RECEIPT_HIDDEN

    set_shares_read_receipts(db, bob, True)
    assert _receipt(db, alice, to_bob).state == RECEIPT_READ
    assert _receipt(db, bob, to_alice).state == RECEIPT_READ


def test_a_recipient_who_opted_out_is_named_even_to_a_sender_who_did_too(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    message = send_mail(db, alice, bob, "Hello", "body")
    set_shares_read_receipts(db, alice, False)
    set_shares_read_receipts(db, bob, False)

    assert _receipt(db, alice, message).state == RECEIPT_WITHHELD


def test_link_system_and_deleted_account_mail_have_no_receipt(db):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    node_identity, farpoint = bootstrap_node_identity("roanoke"), bootstrap_node_identity("farpoint")
    _link_context_with_known_peer(db, node_identity, farpoint)
    send_letter(
        db, alice, [LetterRecipient(address=f"dave@{farpoint.fingerprint}")], "Link", "x",
        group_id=new_mail_group_id(), node_identity=node_identity,
    )
    send_mail(db, alice, carol, "Gone", "x")
    delete_user(db, carol, deleted_by=_user(db, "root", user_level=255))
    system = send_system_mail(db, bob, "Notice", "x")

    assert read_receipts(db, alice, list_sent(db, alice)) == {}
    assert read_receipts(db, bob, [system]) == {}


def test_only_the_senders_own_letters_have_receipts(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    message = send_mail(db, alice, bob, "Hello", "body")

    assert read_receipts(db, bob, [message]) == {}


def test_letters_already_read_before_the_upgrade_keep_their_reading(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "Issue #829" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    old = Database(tmp_path / "node.db")
    for name in ("alice", "bob"):
        old.connection.execute(
            "INSERT INTO users (username, password_hash, user_level, created_at) "
            "VALUES (?, 'x', 10, '2026-09-01T00:00:00.000Z')", (name,),
        )
    for subject, read_at in (("read", "2026-09-02T10:00:00.000Z"), ("unread", None)):
        old.connection.execute(
            "INSERT INTO mail_messages (sender_user_id, sender_label, recipient_user_id, subject, body, created_at, "
            "read_at) VALUES (1, 'alice', 2, ?, 'x', '2026-09-01T00:00:00.000Z', ?)", (subject, read_at),
        )
    old.connection.commit()
    old.close()

    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)
    upgraded = Database(tmp_path / "node.db")
    try:
        rows = upgraded.connection.execute(
            "SELECT subject, first_read_at, first_read_shared FROM mail_messages ORDER BY id"
        ).fetchall()
        # Before the upgrade everyone counted as sharing (issue #922).
        assert [tuple(row) for row in rows] == [("read", "2026-09-02T10:00:00.000Z", 1), ("unread", None, 0)]
        alice = get_user_by_id(upgraded, 1)
        receipts = read_receipts(upgraded, alice, list_sent(upgraded, alice))
        assert sorted(receipt.state for receipt in receipts.values()) == [RECEIPT_NOT_READ, RECEIPT_READ]
    finally:
        upgraded.close()


# -- Sent --------------------------------------------------------------------------


def test_sent_shows_when_a_letter_was_read(db, lane):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    send_mail(db, alice, bob, "Hello", "body")
    _read(db, bob)

    text = _open_first_sent(db, lane, alice)

    assert re.search(r"#  To +Subject +Status +Date", text)
    assert re.search(r"1  bob +Hello +read ", text)
    assert re.search(r"Read: \d", text)


def test_sent_says_a_letter_is_not_read_yet(db, lane):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    send_mail(db, alice, bob, "Hello", "body")

    text = _open_first_sent(db, lane, alice)

    assert re.search(r"1  bob +Hello +not read", text)
    assert "Read: not yet" in text


def test_sent_marks_a_recipient_who_does_not_share_receipts(db, lane):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    send_mail(db, alice, bob, "Hello", "body")
    _read(db, bob)
    set_shares_read_receipts(db, bob, False)

    text = _open_first_sent(db, lane, alice)

    assert re.search(r"1  bob +Hello +no receipt", text)
    assert "Read: not shown, as bob doesn't share read receipts" in text


def test_a_sender_who_does_not_share_receipts_sees_none(db, lane):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    send_mail(db, alice, bob, "Hello", "body")
    _read(db, bob)
    set_shares_read_receipts(db, alice, False)

    text = _open_first_sent(db, lane, alice)

    # No Status column in the list, and the view says why.
    assert "Status" not in text
    assert re.search(r"1  bob +Hello", text) and not re.search(r"Hello +read", text)
    assert "Read: not shown, as you don't share read receipts yourself (Profile)" in text


def test_a_letter_to_several_people_names_them_by_their_receipt(db, lane):
    alice, bob, carol, dave, erin = (_user(db, name) for name in ("alice", "bob", "carol", "dave", "erin"))
    send_letter(
        db, alice, [LetterRecipient(user=u) for u in (bob, carol, dave, erin)], "Lunch", "Friday?",
        group_id=new_mail_group_id(),
    )
    _read(db, bob)
    set_shares_read_receipts(db, dave, False)
    delete_for_recipient(db, erin, list_inbox(db, erin)[0])

    text = _open_first_sent(db, lane, alice, width=120)

    assert re.search(r"1  bob, carol, dave, erin +Lunch +some read", text)
    assert re.search(r"Read by: bob \(\d", text)
    # Erin deleted it unopened: to Alice, just not read yet (issue #922).
    assert "Not read yet: carol, erin" in text
    assert "Deleted unread" not in text
    assert "Don't share read receipts: dave" in text


def test_a_letter_to_several_people_all_read_is_read_in_the_list(db, lane):
    alice, bob, carol = _user(db, "alice"), _user(db, "bob"), _user(db, "carol")
    send_letter(
        db, alice, [LetterRecipient(user=bob), LetterRecipient(user=carol)], "Lunch", "Friday?",
        group_id=new_mail_group_id(),
    )
    _read(db, bob)
    _read(db, carol)
    # One who opted out does not keep the letter from reading as read.
    dave = _user(db, "dave")
    set_shares_read_receipts(db, dave, False)

    text = _open_first_sent(db, lane, alice)

    assert re.search(r"1  bob, carol +Lunch +read ", text)


def test_mail_to_all_callers_counts_readers_rather_than_naming_them(db, lane):
    root = _user(db, "root", user_level=255)
    bob, carol, dave = _user(db, "bob"), _user(db, "carol"), _user(db, "dave")
    send_to_all_callers(db, "Downtime", "Sunday", group_id=new_mail_group_id(), sender=root)
    _read(db, bob)
    set_shares_read_receipts(db, dave, False)

    text = _open_first_sent(db, lane, root)

    assert "Read: by 1 of the 2 who share read receipts (1 more don't share them)" in text
    assert "Read by:" not in text


# -- Profile ------------------------------------------------------------------------


def test_profile_turns_read_receipts_off_and_on(db, lane):
    alice = _user(db, "alice")
    session = ProfileSession(["x", "x", "x", "b"])

    asyncio.run(profile_flow._edit_profile(session, lane, alice))

    assert shares_read_receipts(db, alice) is False
    text = squeezed(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", "".join(session.written)))
    assert "E[x]change read receipts" in text
    assert "Let senders see when I've read their mail: no" in text
    assert "Let senders see when I've read their mail: yes" in text
