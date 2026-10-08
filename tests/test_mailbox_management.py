"""Managing a mailbox (issue #828): marking letters and deleting them together,
deleting everything read, a Kept folder the cap never evicts from, and a
listing by conversation.

Before #828 a letter could only be deleted one at a time from its own view,
and a mailbox at the cap evicted the oldest read letter whatever it was.
Since #921 Kept is outside the 500-letter cap and has a limit of its own."""

from __future__ import annotations

import re

import pytest

from netbbs import mail as mail_module
from netbbs.mail import (
    KeptFullError,
    MailboxFullError,
    delete_letters,
    get_mail,
    inbox_count,
    inbox_sizes,
    kept_count,
    list_inbox,
    list_sent,
    mail_has_room,
    mark_read,
    send_mail,
    send_system_mail,
    set_kept,
    thread_key,
    thread_subject,
)
from netbbs.net import mail_flow
from netbbs.net.mail_flow import kept_capacity_note, mailbox_capacity_note
from netbbs.rendering.width import display_width
from tests.test_mail_list import FakeSession, _rows, _run, node  # noqa: F401  (fixture)


def _indexed(db) -> set[int]:
    return {row["rowid"] for row in db.connection.execute("SELECT rowid FROM mail_search")}


def _row_ids(db) -> set[int]:
    return {row["id"] for row in db.connection.execute("SELECT id FROM mail_messages")}


# -- the domain -------------------------------------------------------------------


def test_deleting_many_follows_the_one_letter_rule(node):
    """Each letter goes from the caller's side only: one the sender still
    has is marked, one nobody else has is removed with its search entry."""
    db, _lane, bob, alice, carol = node
    kept_by_sender = send_mail(db, alice, bob, "Alice keeps hers", "body")
    gone_elsewhere = send_mail(db, carol, bob, "Carol deleted hers", "body")
    delete_letters(db, carol, [gone_elsewhere.id], sent=True)
    notice = send_system_mail(db, bob, "Notice", "body")
    not_bobs = send_mail(db, alice, carol, "Not for bob", "body")

    deleted = delete_letters(
        db, bob, [kept_by_sender.id, gone_elsewhere.id, notice.id, not_bobs.id], sent=False,
    )

    assert deleted == 3
    assert list_inbox(db, bob) == []
    assert _row_ids(db) == {kept_by_sender.id, not_bobs.id}
    assert _indexed(db) == _row_ids(db)
    assert [m.subject for m in list_sent(db, alice)] == ["Not for bob", "Alice keeps hers"]
    # Deleting again does nothing: the side is already gone.
    assert delete_letters(db, bob, [kept_by_sender.id], sent=False) == 0


def test_deleting_sent_letters_leaves_the_recipients_copies(node):
    db, _lane, bob, alice, _carol = node
    first = send_mail(db, bob, alice, "One", "body")
    second = send_mail(db, bob, alice, "Two", "body")
    delete_letters(db, alice, [second.id], sent=False)

    assert delete_letters(db, bob, [first.id, second.id], sent=True) == 2

    assert list_sent(db, bob) == []
    assert [m.subject for m in list_inbox(db, alice)] == ["One"]
    assert _row_ids(db) == {first.id}
    assert _indexed(db) == {first.id}


def test_a_kept_letter_is_never_evicted_and_does_not_count(node, monkeypatch):
    """Issue #921: Kept is outside the cap, so keeping a letter makes room
    in the Inbox, and the cap evicts only among the Inbox's read letters."""
    db, _lane, bob, alice, _carol = node
    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 3)
    oldest = mark_read(db, bob, send_mail(db, alice, bob, "Oldest", "body"))
    middle = mark_read(db, bob, send_mail(db, alice, bob, "Middle", "body"))
    send_mail(db, alice, bob, "Unread", "body")
    assert set_kept(db, bob, [oldest.id], kept=True) == 1
    assert (inbox_count(db, bob), kept_count(db, bob)) == (2, 1)

    # Room without evicting anything: the kept letter is not counted.
    send_mail(db, alice, bob, "New", "body")
    assert get_mail(db, alice, middle.id).recipient_deleted_at is None
    send_mail(db, alice, bob, "Newer", "body")

    # The oldest read letter that is not kept went; the kept one stayed.
    assert {m.subject for m in list_inbox(db, bob)} == {"Oldest", "Unread", "New", "Newer"}
    assert get_mail(db, bob, oldest.id).kept_at is not None
    # Evicted from Bob's side; Alice's Sent copy is still hers.
    assert get_mail(db, alice, middle.id).recipient_deleted_at is not None
    assert (inbox_count(db, bob), kept_count(db, bob)) == (3, 1)


def test_a_mailbox_full_of_unread_mail_refuses_new_mail_whatever_is_kept(node, monkeypatch):
    db, _lane, bob, alice, _carol = node
    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 2)
    kept = mark_read(db, bob, send_mail(db, alice, bob, "Kept", "body"))
    set_kept(db, bob, [kept.id], kept=True)
    send_mail(db, alice, bob, "Unread", "body")
    send_mail(db, alice, bob, "Unread too", "body")

    # The group send's room check (issue #827) agrees with delivery.
    assert not mail_has_room(db, bob)
    with pytest.raises(MailboxFullError, match="every message is still unread"):
        send_mail(db, alice, bob, "Turned away", "body")

    [size] = inbox_sizes(db)
    assert (size.total, size.inbox, size.unread, size.kept, size.evictable) == (3, 2, 2, 1, 0)
    # Keeping an unread letter makes room for the next.
    set_kept(db, bob, [list_inbox(db, bob)[0].id], kept=True)
    assert mail_has_room(db, bob)


def test_keeping_is_refused_once_kept_is_full(node, monkeypatch):
    db, _lane, bob, alice, _carol = node
    monkeypatch.setattr(mail_module, "MAX_KEPT_PER_RECIPIENT", 2)
    letters = [send_mail(db, alice, bob, f"Letter {n}", "body") for n in range(4)]
    assert set_kept(db, bob, [letters[0].id, letters[1].id], kept=True) == 2

    with pytest.raises(KeptFullError, match=r"Kept is full \(2 letters\) -- move some back to the Inbox"):
        set_kept(db, bob, [letters[2].id], kept=True)
    assert get_mail(db, bob, letters[2].id).kept_at is None
    # Moving back is never refused, and makes room again.
    assert set_kept(db, bob, [letters[0].id], kept=False) == 1
    assert set_kept(db, bob, [letters[2].id], kept=True) == 1


def test_a_bulk_keep_that_does_not_fit_keeps_none(node, monkeypatch):
    """All or none: keeping as many as fit would leave an arbitrary part of
    the marked letters behind."""
    db, _lane, bob, alice, _carol = node
    monkeypatch.setattr(mail_module, "MAX_KEPT_PER_RECIPIENT", 3)
    letters = [send_mail(db, alice, bob, f"Letter {n}", "body") for n in range(5)]
    set_kept(db, bob, [letters[0].id], kept=True)

    with pytest.raises(KeptFullError, match="Kept has room for 2 more letters") as refused:
        set_kept(db, bob, [letter.id for letter in letters[1:]], kept=True)
    assert refused.value.room == 2
    assert kept_count(db, bob) == 1
    # Letters already kept, or not the caller's, are not counted against it.
    other = send_mail(db, bob, alice, "Not bob's", "body")
    assert set_kept(db, bob, [letters[0].id, letters[1].id, letters[2].id, other.id], kept=True) == 2


def test_moving_letters_back_keeps_the_account_within_both_limits(node, monkeypatch):
    """Moving kept letters back can take the Inbox past the cap, but an
    account never holds more than the two limits together: new mail then
    arrives only in place of a read letter."""
    db, _lane, bob, alice, _carol = node
    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 3)
    monkeypatch.setattr(mail_module, "MAX_KEPT_PER_RECIPIENT", 2)
    kept = [mark_read(db, bob, send_mail(db, alice, bob, f"Kept {n}", "body")) for n in range(2)]
    set_kept(db, bob, [letter.id for letter in kept], kept=True)
    for n in range(3):
        mark_read(db, bob, send_mail(db, alice, bob, f"Inbox {n}", "body"))
    set_kept(db, bob, [letter.id for letter in kept], kept=False)
    assert (inbox_count(db, bob), kept_count(db, bob)) == (5, 0)

    send_mail(db, alice, bob, "More", "body")

    assert (inbox_count(db, bob), kept_count(db, bob)) == (5, 0)


def test_keeping_only_touches_the_callers_own_inbox(node):
    db, _lane, bob, alice, _carol = node
    to_bob = send_mail(db, alice, bob, "To bob", "body")
    to_alice = send_mail(db, bob, alice, "To alice", "body")

    assert set_kept(db, bob, [to_bob.id, to_alice.id], kept=True) == 1
    assert set_kept(db, bob, [to_bob.id], kept=True) == 0
    assert get_mail(db, alice, to_alice.id).kept_at is None
    assert set_kept(db, bob, [to_bob.id], kept=False) == 1
    assert get_mail(db, bob, to_bob.id).kept_at is None


def test_a_conversation_is_its_correspondent_and_subject_without_prefixes(node):
    db, _lane, bob, alice, carol = node
    assert thread_subject("Re: FWD:  re: Lunch?") == "lunch?"
    assert thread_subject("Fw: Lunch?") == thread_subject("lunch?")
    assert thread_subject("Rebate") == "rebate"
    first = send_mail(db, alice, bob, "Lunch?", "body")
    reply = send_mail(db, alice, bob, "Re: Lunch?", "body")
    other = send_mail(db, carol, bob, "Re: Lunch?", "body")
    notice = send_system_mail(db, bob, "Lunch?", "body")
    assert thread_key(first, sent=False) == thread_key(reply, sent=False)
    assert thread_key(first, sent=False) != thread_key(other, sent=False)
    assert thread_key(first, sent=False) != thread_key(notice, sent=False)
    sent = send_mail(db, bob, alice, "Re: Lunch?", "body")
    assert thread_key(sent, sent=True) == thread_key(send_mail(db, bob, alice, "Lunch?", "b"), sent=True)


def test_the_capacity_notes_speak_of_unread_mail_and_of_kept():
    """Issue #921 took back #828's "unread and kept mail": the cap is about
    the Inbox, and Kept says when it is full."""
    cap = mail_module.MAX_MAIL_PER_RECIPIENT
    text, tone = mailbox_capacity_note(cap, cap)
    assert tone == "error" and "full of unread mail" in text and "kept" not in text.replace("Kept", "")
    text, tone = mailbox_capacity_note(cap, cap - 5)
    assert tone == "warning" and "Kept" in text
    assert "Unread mail is never removed." in mailbox_capacity_note(mail_module.MAILBOX_NEARLY_FULL, 0)[0]
    assert kept_capacity_note(mail_module.MAX_KEPT_PER_RECIPIENT - 1) is None
    assert kept_capacity_note(mail_module.MAX_KEPT_PER_RECIPIENT) == (
        "Kept is full (100 letters): [K]ept in the Inbox is refused until you move some back to the "
        "Inbox or delete them."
    )


# -- the list ---------------------------------------------------------------------


def test_marked_letters_are_deleted_with_one_confirmation(node):
    db, lane, bob, alice, _carol = node
    for subject in ("One", "Two", "Three"):
        send_mail(db, alice, bob, subject, "body")
    # Newest first: Three, Two, One. Mark the first two.
    session = FakeSession(["m", "m", "e", "y", "b"])

    _run(session, lane, bob)

    marked = session.screens()[2]
    assert re.search(r"^ \*1 +new +alice +Three", marked, re.MULTILINE)
    assert re.search(r"^>\*2", marked, re.MULTILINE) is None
    assert re.search(r"^ \*2 +new +alice +Two", marked, re.MULTILINE)
    assert "2 marked" in marked
    assert "Delete the 2 marked messages?" in session.visible()
    assert [m.subject for m in list_inbox(db, bob)] == ["One"]
    last = session.screens()[-1]
    assert "Deleted 2 messages." in last
    assert "marked" not in last


def test_delete_with_nothing_marked_takes_the_highlighted_letter(node):
    db, lane, bob, alice, _carol = node
    send_mail(db, alice, bob, "Stays", "body")
    send_mail(db, alice, bob, "Goes", "body")
    session = FakeSession(["e", "n", "e", "y", "b"])

    _run(session, lane, bob)

    assert 'Delete "Goes"?' in session.visible()
    assert [m.subject for m in list_inbox(db, bob)] == ["Stays"]


def test_purge_read_says_how_many_and_spares_unread_and_kept_mail(node):
    db, lane, bob, alice, _carol = node
    mark_read(db, bob, send_mail(db, alice, bob, "Read one", "body"))
    mark_read(db, bob, send_mail(db, alice, bob, "Read two", "body"))
    kept = mark_read(db, bob, send_mail(db, alice, bob, "Read and kept", "body"))
    set_kept(db, bob, [kept.id], kept=True)
    send_mail(db, alice, bob, "Unread", "body")
    session = FakeSession(["p", "y", "b"])

    _run(session, lane, bob)

    assert "[P]urge read" in session.screens()[0]
    assert "Delete all 2 read messages in your Inbox?" in session.visible()
    assert {m.subject for m in list_inbox(db, bob)} == {"Unread", "Read and kept"}
    last = session.screens()[-1]
    assert "Deleted 2 read messages." in last
    # Nothing read is left, so nothing is offered.
    assert "[P]urge read" not in last


def test_keep_moves_letters_to_kept_and_back(node):
    db, lane, bob, alice, _carol = node
    send_mail(db, alice, bob, "Older", "body")
    send_mail(db, alice, bob, "Precious", "body")
    session = FakeSession(["k", "v", "k", "b", "b"])

    _run(session, lane, bob)

    screens = session.screens()
    assert "Moved 1 message to Kept." in screens[1]
    assert "Precious" not in screens[1].split("Moved")[0].split("Date")[-1]
    assert "1 unread in Kept" in screens[1]
    kept_screen = screens[2]
    assert "NetBBS › Mail › Kept" in kept_screen
    assert re.search(r"> 1 +new +alice +Precious", kept_screen)
    assert "[K]ept: yes" in kept_screen
    # Each folder counts against its own limit (issue #921).
    assert "1 of 100" in kept_screen and "of 500" not in kept_screen
    assert "1 of 500" in screens[1]
    assert "Moved 1 message back to the Inbox." in screens[3]
    assert all(m.kept_at is None for m in list_inbox(db, bob))


def test_a_kept_letter_opens_with_kept_yes(node):
    db, lane, bob, alice, _carol = node
    letter = send_mail(db, alice, bob, "Precious", "body")
    set_kept(db, bob, [letter.id], kept=True)
    session = FakeSession(["v", "1", "k", "b", "b"])

    _run(session, lane, bob)

    assert "Mail › Kept" in session.visible()
    assert "[K]ept: yes" in session.visible()
    assert get_mail(db, bob, letter.id).kept_at is None


def test_the_inbox_view_keeps_a_letter(node):
    db, lane, bob, alice, _carol = node
    letter = send_mail(db, alice, bob, "Precious", "body")
    session = FakeSession(["1", "k", "b"])

    _run(session, lane, bob)

    assert "[K]ept: no" in session.visible()
    assert get_mail(db, bob, letter.id).kept_at is not None
    assert "Moved to Kept." in session.screens()[-1]


def test_order_lists_by_conversation(node):
    db, lane, bob, alice, carol = node
    send_mail(db, alice, bob, "Lunch?", "body")
    send_mail(db, carol, bob, "Party", "body")
    send_mail(db, alice, bob, "Re: Lunch?", "body")
    session = FakeSession(["o", "o", "b"])

    _run(session, lane, bob)

    threaded = session.screens()[-1]
    assert "by conversation" in threaded
    listed = re.findall(r"^[> ][ *] ?(\d+) +new +(\w+)  (.*?) +\d\d\.", threaded, re.MULTILINE)
    # Alice's conversation first (it has the newest letter), its later
    # letter indented under it; then Carol's, though it arrived in between.
    assert listed == [("1", "alice", "Re: Lunch?"), ("2", "alice", "  Lunch?"), ("3", "carol", "Party")]
    assert mail_flow._mail_order(db, bob) == "threads"


def test_sent_order_switches_between_newest_and_conversation(node):
    db, lane, bob, alice, _carol = node
    send_mail(db, bob, alice, "Lunch?", "body")
    send_mail(db, bob, alice, "Other", "body")
    send_mail(db, bob, alice, "Re: Lunch?", "body")
    session = FakeSession(["s", "o", "o", "b", "b"])

    _run(session, lane, bob)

    screens = session.screens()
    assert "by conversation" in screens[2]
    assert re.search(r"alice    Lunch\?", screens[2])
    assert "Newest mail first." in screens[3]


def test_marks_and_delete_work_in_sent(node):
    db, lane, bob, alice, _carol = node
    send_mail(db, bob, alice, "One", "body")
    send_mail(db, bob, alice, "Two", "body")
    session = FakeSession(["s", " ", " ", "e", "y", "b", "b"])

    _run(session, lane, bob)

    assert list_sent(db, bob) == []
    assert len(list_inbox(db, alice)) == 2


@pytest.mark.parametrize("folder_keys", [[], ["v"], ["s"]])
@pytest.mark.parametrize(("width", "height"), [(80, 24), (40, 12)])
def test_every_folder_fits_with_marks(node, width, height, folder_keys):
    db, lane, bob, alice, _carol = node
    for i in range(40):
        mark_read(db, bob, send_mail(db, alice, bob, f"Subject {i}", "body"))
        send_mail(db, bob, alice, f"Sent {i}", "body")
    set_kept(db, bob, [m.id for m in list_inbox(db, bob)[:20]], kept=True)
    path = mail_flow._letter_draft_path(lane, bob)
    path.write_text("A letter", encoding="utf-8")
    # The cursor ends on a marked row, the busiest bar (review on #908).
    session = FakeSession([*folder_keys, "m", "m", "UP", "b", "b"], width=width, height=height)

    _run(session, lane, bob)

    for screen in session.screens():
        rows = _rows(screen)
        # Wrapped as the terminal would.
        assert sum(max(1, -(-display_width(row) // width)) for row in rows) <= height
    assert "2 marked" in session.screens()[len(folder_keys) + 2]


def test_upgrading_keeps_every_letter_and_keeps_none_of_them(tmp_path, monkeypatch):
    """Migration 100 adds `kept_at` NULL: a letter stored before it is in the
    Inbox, where the cap may evict it as before."""
    from netbbs.storage import database as database_module
    from netbbs.storage.database import Database
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "Issue #828" in m.description)
    assert index == 99  # schema version 100
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    old = Database(tmp_path / "node.db")
    old.connection.execute(
        "INSERT INTO users (username, password_hash, user_level, created_at) VALUES ('bob', 'x', 10, 't')"
    )
    old.connection.execute(
        "INSERT INTO mail_messages (sender_label, recipient_user_id, subject, body, created_at, sender_deleted_at) "
        "VALUES ('dave@abc', 1, 'Hello', 'body', 't', 't')"
    )
    old.connection.commit()
    old.close()

    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)
    upgraded = Database(tmp_path / "node.db")
    try:
        rows = upgraded.connection.execute("SELECT subject, kept_at FROM mail_messages").fetchall()
        assert [tuple(row) for row in rows] == [("Hello", None)]
    finally:
        upgraded.close()


@pytest.mark.parametrize("width", [80, 40])
def test_the_inbox_header_says_when_kept_mail_is_unread(node, width):
    """The main menu counts an unread kept letter as unread; the Inbox
    must not read "caught up" without saying where it is (review on #908)."""
    db, lane, bob, alice, _carol = node
    letter = send_mail(db, alice, bob, "Unopened", "body")
    set_kept(db, bob, [letter.id], kept=True)
    session = FakeSession(["b"], width=width, height=24)

    _run(session, lane, bob)

    header = " ".join(session.screens()[0].split())
    assert "1 unread in Kept" in header


# -- Kept's own limit (issue #921) ------------------------------------------------


def _full_kept(db, bob, alice, monkeypatch, limit=2):
    monkeypatch.setattr(mail_module, "MAX_KEPT_PER_RECIPIENT", limit)
    monkeypatch.setattr(mail_flow, "MAX_KEPT_PER_RECIPIENT", limit)
    kept = [send_mail(db, alice, bob, f"Kept {n}", "body") for n in range(limit)]
    set_kept(db, bob, [letter.id for letter in kept], kept=True)


def test_keeping_from_the_list_is_refused_in_place_with_the_marks_kept(node, monkeypatch):
    db, lane, bob, alice, _carol = node
    _full_kept(db, bob, alice, monkeypatch)
    send_mail(db, alice, bob, "One", "body")
    send_mail(db, alice, bob, "Two", "body")
    # m, m: mark both; k: keep -> refused; b: out.
    session = FakeSession(["m", "m", "k", "b"])

    _run(session, lane, bob)

    refused = " ".join(session.screens()[-1].split())
    assert "Kept is full (2 letters) -- move some back to the Inbox or delete them first." in refused
    assert "2 marked" in refused
    assert kept_count(db, bob) == 2


def test_keeping_from_a_letter_is_refused_and_the_letter_stays_open(node, monkeypatch):
    db, lane, bob, alice, _carol = node
    _full_kept(db, bob, alice, monkeypatch)
    letter = send_mail(db, alice, bob, "Precious", "body")
    session = FakeSession(["1", "k", "b", "b"])

    _run(session, lane, bob)

    refused = " ".join(session.screens()[2].split())
    assert "Kept is full (2 letters)" in refused
    assert "Mail › Inbox › Precious" in refused
    assert "[K]ept: no" in refused
    assert get_mail(db, bob, letter.id).kept_at is None


def test_a_full_kept_folder_says_so(node, monkeypatch):
    db, lane, bob, alice, _carol = node
    _full_kept(db, bob, alice, monkeypatch)
    session = FakeSession(["v", "b", "b"])

    _run(session, lane, bob)

    kept_screen = " ".join(session.screens()[1].split())
    assert "2 of 2" in kept_screen
    assert "Kept is full (2 letters): [K]ept in the Inbox is refused" in kept_screen
    # The Inbox counts only its own letters against the cap.
    assert "0 of 500" in " ".join(session.screens()[0].split())
