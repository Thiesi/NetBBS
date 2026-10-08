"""The main menu's Find searches the caller's own mail (issue #824).

Only the caller's own mailbox -- their Inbox and their Sent folder -- is
searched, never anyone else's, and never a letter they deleted from their
side. A letter matches by its subject, its body as plain text, or the
From/To name the mailbox shows. Nothing is searched for a caller mail is
closed to (issue #816). A found letter opens in the mailbox's own message
view, and Back returns to the results.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import record_message
from netbbs.config import set_mail_min_level
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.guest import set_guest_user
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mail import (
    delete_for_recipient,
    delete_for_sender,
    get_mail,
    list_inbox,
    send_mail,
    send_system_mail,
)
from netbbs.net.char_input import InputHistory
from netbbs.net.mail_flow import LETTER_GONE_NOTICE, current_letter, open_letter
from netbbs.net.notices import take_notices
from netbbs.net.main_menu import _main_menu
from netbbs.search import search_mail
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_mail_flow import _link_context_with_known_peer
from tests.test_search import FakeSession, _run_main_menu, _visible_text


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
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def carol(db):
    return create_user(db, "carol", password="hunter2", user_level=10)


def _subjects(hits):
    return [hit.message.subject for hit in hits]


# -- search_mail ----------------------------------------------------------------


def test_matches_subject_body_and_names(db, alice, bob, carol):
    send_mail(db, bob, alice, "Picnic plans", "bring a blanket")
    send_mail(db, carol, alice, "Hello", "the quokka colony moved")
    send_mail(db, alice, bob, "Minutes", "nothing much")

    assert _subjects(search_mail(db, alice, "picnic")) == ["Picnic plans"]
    assert _subjects(search_mail(db, alice, "quokka")) == ["Hello"]
    # From on an Inbox letter, To on a Sent one.
    assert _subjects(search_mail(db, alice, "carol")) == ["Hello"]
    assert _subjects(search_mail(db, alice, "bob")) == ["Minutes", "Picnic plans"]
    hits = search_mail(db, alice, "minutes")
    assert [(hit.sent, hit.label) for hit in hits] == [(True, "bob")]


def test_every_word_must_match_somewhere(db, alice, bob):
    send_mail(db, bob, alice, "Picnic plans", "bring a blanket")

    assert _subjects(search_mail(db, alice, "bob blanket")) == ["Picnic plans"]
    assert search_mail(db, alice, "bob umbrella") == []


def test_words_match_whole_ignoring_case_and_accents_as_post_search_does(db, alice, bob):
    send_mail(db, bob, alice, "Café meetup", "Cats welcome")

    assert _subjects(search_mail(db, alice, "CAFE")) == ["Café meetup"]
    assert _subjects(search_mail(db, alice, "cats")) == ["Café meetup"]
    assert search_mail(db, alice, "cat") == []


def test_body_is_matched_as_plain_text_without_color_codes(db, alice, bob):
    send_mail(db, bob, alice, "Colors", "a |12quo|07kka in red")

    assert _subjects(search_mail(db, alice, "quokka")) == ["Colors"]


def test_never_searches_anyone_elses_mail(db, alice, bob, carol):
    send_mail(db, bob, carol, "Secret", "zeppelin")

    assert search_mail(db, alice, "zeppelin") == []
    assert search_mail(db, alice, "secret") == []
    assert _subjects(search_mail(db, carol, "zeppelin")) == ["Secret"]


def test_a_letter_deleted_from_the_callers_side_is_not_found(db, alice, bob):
    received = send_mail(db, bob, alice, "Old news", "zeppelin")
    sent = send_mail(db, alice, bob, "Reply", "zeppelin too")

    delete_for_recipient(db, alice, received)
    delete_for_sender(db, alice, sent)

    assert search_mail(db, alice, "zeppelin") == []


def test_the_other_sides_delete_leaves_the_callers_copy_searchable(db, alice, bob):
    letter = send_mail(db, alice, bob, "Lunch", "zeppelin")
    delete_for_recipient(db, bob, letter)

    assert _subjects(search_mail(db, alice, "zeppelin")) == ["Lunch"]
    assert search_mail(db, bob, "zeppelin") == []


def test_system_mail_is_the_callers_own(db, alice):
    send_system_mail(db, alice, "Post rejected", "your zeppelin post was off topic")

    hits = search_mail(db, alice, "zeppelin")
    assert [(hit.message.subject, hit.label, hit.sent) for hit in hits] == [("Post rejected", "System", False)]
    assert _subjects(search_mail(db, alice, "system")) == ["Post rejected"]


def test_a_letter_to_oneself_is_found_in_both_folders(db, alice):
    send_mail(db, alice, alice, "Note to self", "zeppelin")

    assert sorted(hit.sent for hit in search_mail(db, alice, "zeppelin")) == [False, True]


def test_newest_first_and_limited(db, alice, bob):
    for index in range(5):
        send_mail(db, bob, alice, f"Zeppelin {index}", "body")

    assert _subjects(search_mail(db, alice, "zeppelin", limit=3)) == ["Zeppelin 4", "Zeppelin 3", "Zeppelin 2"]


def test_link_mail_matches_by_the_address_the_mailbox_shows(db, alice):
    node_identity = bootstrap_node_identity("roanoke")
    farpoint = bootstrap_node_identity("farpoint")
    _link_context_with_known_peer(db, node_identity, farpoint, friendly_name="Farpoint")
    db.connection.execute(
        """
        INSERT INTO mail_messages (sender_user_id, sender_label, recipient_user_id, subject, body, created_at)
        VALUES (NULL, ?, ?, 'From afar', 'hello', '2026-01-01T00:00:00Z')
        """,
        (f"dave@{farpoint.fingerprint}", alice.id),
    )
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_user_id, recipient_remote_address, subject, body,
             created_at, link_delivery_status)
        VALUES (?, ?, NULL, ?, 'To afar', 'hello', '2026-01-01T00:00:00Z', 'pending')
        """,
        (alice.id, alice.username, f"erin@{farpoint.fingerprint}"),
    )
    db.connection.commit()

    hits = search_mail(db, alice, "farpoint")
    assert [hit.message.subject for hit in hits] == ["To afar", "From afar"]
    assert hits[0].label.startswith("erin@Farpoint")
    assert hits[1].label.startswith("dave@Farpoint")
    assert _subjects(search_mail(db, alice, "dave@farpoint")) == ["From afar"]
    # The stored fingerprint is not what the caller reads, so not matched.
    assert search_mail(db, alice, farpoint.fingerprint) == []


def test_nothing_for_a_caller_below_the_mail_level(db, alice, bob):
    send_mail(db, bob, alice, "Zeppelin", "body")
    set_mail_min_level(db, 20)

    assert search_mail(db, alice, "zeppelin") == []


def test_nothing_for_the_guest_account(db, alice, bob):
    send_mail(db, bob, alice, "Zeppelin", "body")
    set_guest_user(db, alice)

    assert search_mail(db, alice, "zeppelin") == []


def test_blank_or_punctuation_only_query_finds_nothing(db, alice, bob):
    send_mail(db, bob, alice, "Zeppelin", "body")

    assert search_mail(db, alice, "   ") == []
    assert search_mail(db, alice, "!!! \"") == []


# -- the Find screen ------------------------------------------------------------


def test_find_lists_mail_with_posts_under_its_own_tag(db, lane, alice, bob):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "zeppelin post", "about airships")
    send_mail(db, bob, alice, "zeppelin letter", "mailmarker about airships")

    session = _run_main_menu(db, lane, alice, ["/", "zeppelin", "b", "l", "y"])
    text = _visible_text(session)

    assert "[POST] general" in text
    assert "[MAIL] from bob: mailmarker" in text
    assert "your own mail" in text


def _result_rows(text):
    return [line for line in text.splitlines() if re.match(r"^(> |  )\d\d\. ", line)]


def test_find_lists_mail_first_ahead_of_posts_files_and_chat(db, lane, alice, bob):
    """Issue #918: the caller's own mail is often what they are looking for."""
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "zeppelin post", "body")
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "zeppelin.txt", b"data", description="zeppelin plans")
    channel = create_channel(db, "lobby", creator=alice)
    record_message(db, channel, kind="message", author_label="alice", body="zeppelin chat")
    send_mail(db, bob, alice, "zeppelin letter", "body")

    session = _run_main_menu(db, lane, alice, ["/", "zeppelin", "b", "l", "y"])
    rows = _result_rows(_visible_text(session))

    tags = [re.search(r"\[(MAIL|POST|FILE|CHAT)\]", row).group(1) for row in rows]
    assert tags == ["MAIL", "POST", "FILE", "CHAT"]
    assert rows[0].lstrip("> ").startswith("01. zeppelin letter")


def test_mail_first_keeps_its_cap_the_notice_and_the_numbering(db, lane, alice, bob):
    """Twenty letters at most, the notice when more matched, and the post
    after them still numbered on its own page and still opening."""
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "zeppelin post", "zeppelin " + "pad " * 10 + "postviewmarker")
    for i in range(21):
        send_mail(db, bob, alice, f"zeppelin letter {i:02d}", "body")

    # Page 1 holds the first letters, page 2 the rest and then the post.
    session = _run_main_menu(db, lane, alice, ["/", "zeppelin", ">", "0", "5", "\r", "b", "b", "b", "l", "y"])
    text = _visible_text(session)
    rows = _result_rows(text.split("postviewmarker")[0])

    assert "Showing the top 20 matches per category -- narrow your search terms for a complete list." in text
    assert sum("[MAIL]" in row for row in rows) == 20
    assert "zeppelin letter 00" not in text  # the oldest, past the cap
    assert "[POST]" in rows[-1] and rows[-1].lstrip("> ").startswith("05. ")
    assert "postviewmarker" in text


def test_find_opens_a_letter_marks_it_read_and_back_returns_to_the_results(db, lane, alice, bob):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "zeppelin post", "body")
    letter = send_mail(db, bob, alice, "zeppelin letter", "zeppelin " + "pad " * 10 + "bodyviewmarker")

    # Mail comes first (issue #918): the letter is result 1, the post
    # result 2. Open the letter, Back, open it again, Back, and leave.
    session = _run_main_menu(db, lane, alice, ["/", "zeppelin", "0", "1", "b", "0", "1", "b", "b", "l", "y"])
    text = _visible_text(session)

    assert text.count("bodyviewmarker") == 2
    assert "From: bob" in text
    assert get_mail(db, alice, letter.id).is_read


def test_find_opens_a_sent_letter_in_the_sent_view(db, lane, alice, bob):
    send_mail(db, alice, bob, "zeppelin letter", "zeppelin " + "pad " * 10 + "sentviewmarker")

    session = _run_main_menu(db, lane, alice, ["/", "zeppelin", "0", "1", "b", "b", "l", "y"])
    text = _visible_text(session)

    assert "sentviewmarker" in text
    assert "To: bob" in text
    assert "[MAIL] to bob" in text


def test_a_letter_deleted_in_the_view_leaves_the_results(db, lane, alice, bob):
    send_mail(db, bob, alice, "zeppelin letter", "body")

    session = _run_main_menu(db, lane, alice, ["/", "zeppelin", "0", "1", "d", "y", "b", "l", "y"])
    text = _visible_text(session)

    assert list_inbox(db, alice) == []
    assert "Message deleted." in text
    # The list redrawn after the delete has nothing left in it.
    assert "No matches." in text.split("Message deleted.")[-1]


def test_find_shows_no_mail_to_a_caller_mail_is_closed_to(db, lane, alice, bob):
    send_mail(db, bob, alice, "zeppelin letter", "body")
    set_mail_min_level(db, 20)

    session = _run_main_menu(db, lane, alice, ["/", "zeppelin", "l", "y"])
    text = _visible_text(session)

    assert "[MAIL]" not in text
    assert "No matches." in text
    assert "your own mail" not in text


def test_find_shows_no_mail_to_a_session_that_came_in_as_the_guest(db, lane, alice, bob):
    """A guest session keeps the guest's refusal even after the SysOp moves
    guest login off the account (issue #816's review): only the session can
    tell, so the screen checks it."""
    send_mail(db, bob, alice, "zeppelin letter", "body")
    session = FakeSession(["/", "zeppelin", "l", "y"])
    session.authenticated_without_credential = True
    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice, lane=lane)
    )
    text = _visible_text(session)

    assert "[MAIL]" not in text
    assert "No matches." in text


# -- open_letter ----------------------------------------------------------------


def test_open_letter_says_so_when_the_letter_is_already_gone(db, lane, alice, bob):
    letter = send_mail(db, bob, alice, "zeppelin letter", "body")
    delete_for_recipient(db, alice, letter)
    session = FakeSession([])

    still_there = asyncio.run(open_letter(session, lane, alice, letter.id, sent=False))

    assert still_there is False
    assert any(LETTER_GONE_NOTICE in notice for notice in take_notices(session))


def test_open_letter_rechecks_the_mail_gate(db, lane, alice, bob):
    letter = send_mail(db, bob, alice, "zeppelin letter", "body")
    set_mail_min_level(db, 20)
    session = FakeSession([])

    still_there = asyncio.run(open_letter(session, lane, alice, letter.id, sent=False))

    assert still_there is True
    assert not get_mail(db, alice, letter.id).is_read


def test_current_letter_is_per_side(db, alice, bob):
    letter = send_mail(db, alice, bob, "zeppelin letter", "body")
    delete_for_recipient(db, bob, letter)

    assert current_letter(db, alice, letter.id, sent=True) is not None
    assert current_letter(db, bob, letter.id, sent=False) is None
    # Not a party to it at all.
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    assert current_letter(db, carol, letter.id, sent=False) is None


# -- the mail_search index (issue #824) -------------------------------------------


def _indexed(db):
    return {
        row["rowid"]: (row["subject"], row["body"])
        for row in db.connection.execute("SELECT rowid, subject, body FROM mail_search")
    }


def test_sent_and_system_mail_are_indexed_as_plain_text(db, alice, bob):
    letter = send_mail(db, bob, alice, "Colors", "a |12red|07 word")
    notice = send_system_mail(db, alice, "Notice", "plain")

    assert _indexed(db) == {letter.id: ("Colors", "a red word"), notice.id: ("Notice", "plain")}


def test_one_sides_delete_keeps_the_entry_and_the_last_removes_it(db, alice, bob):
    letter = send_mail(db, bob, alice, "Hello", "zeppelin")

    delete_for_recipient(db, alice, letter)
    assert letter.id in _indexed(db)
    assert _subjects(search_mail(db, bob, "zeppelin")) == ["Hello"]

    delete_for_sender(db, bob, letter)
    assert _indexed(db) == {}


def test_eviction_by_the_mailbox_cap_removes_the_entry(db, alice, bob, monkeypatch):
    import netbbs.mail as mail_module

    monkeypatch.setattr(mail_module, "MAX_MAIL_PER_RECIPIENT", 1)
    first = send_mail(db, bob, alice, "First", "zeppelin")
    delete_for_sender(db, bob, first)
    mail_module.mark_read(db, alice, first)
    second = send_mail(db, bob, alice, "Second", "zeppelin")

    assert set(_indexed(db)) == {second.id}


def test_deleting_an_account_removes_the_entries_of_letters_nobody_has_left(db, alice, bob):
    from netbbs.auth.users import SYSOP_LEVEL, delete_user

    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    kept = send_mail(db, alice, bob, "Kept", "bob still has it")
    gone = send_mail(db, bob, alice, "Gone", "bob deleted his copy")
    delete_for_sender(db, bob, gone)

    delete_user(db, alice, deleted_by=sysop)

    assert set(_indexed(db)) == {kept.id}


def test_link_mail_is_indexed_both_ways(db, alice, bob):
    from netbbs.link.mail import compose_link_message, deliver_link_message
    from tests.test_link_mail import _incoming_message, _seed_peer

    node_identity = bootstrap_node_identity("roanoke")
    farpoint = bootstrap_node_identity("farpoint")
    _seed_peer(db, farpoint)
    compose_link_message(db, alice, f"erin@{farpoint.fingerprint}", "Outbound", "zeppelin out", node_identity=node_identity)
    incoming = _incoming_message(node_identity, farpoint, recipient="bob", subject="Inbound", body="zeppelin in")
    deliver_link_message(db, incoming.to_dict(), node_identity=node_identity)

    assert sorted(subject for subject, _body in _indexed(db).values()) == ["Inbound", "Outbound"]
    assert _subjects(search_mail(db, alice, "zeppelin")) == ["Outbound"]
    assert _subjects(search_mail(db, bob, "zeppelin")) == ["Inbound"]


def test_integrity_check_and_rebuild_cover_mail(db, alice, bob):
    from netbbs.search import check_index_integrity, rebuild_indexes

    letter = send_mail(db, bob, alice, "Hello", "zeppelin")
    assert check_index_integrity(db).is_clean
    db.connection.execute("DELETE FROM mail_search")
    db.connection.execute("INSERT INTO mail_search (rowid, subject, body) VALUES (999, 'Stale', 'left over')")
    db.connection.commit()

    report = check_index_integrity(db)
    assert report.mail.missing == (letter.id,)
    assert report.mail.extra == (999,)

    rebuild_indexes(db)
    assert check_index_integrity(db).is_clean
    assert _subjects(search_mail(db, alice, "zeppelin")) == ["Hello"]


def test_the_migration_indexes_mail_already_stored_as_plain_text(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, migration in enumerate(MIGRATIONS) if "`mail_search`" in migration.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    old = Database(tmp_path / "node.db")
    alice = create_user(old, "alice", password="hunter2", user_level=10)
    old.connection.execute(
        """
        INSERT INTO mail_messages (sender_user_id, sender_label, recipient_user_id, subject, body, created_at)
        VALUES (?, 'alice', ?, 'Note', 'a |12red|07 zeppelin', '2026-01-01T00:00:00Z')
        """,
        (alice.id, alice.id),
    )
    old.connection.commit()
    old.close()
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)

    db = Database(tmp_path / "node.db")
    try:
        assert list(_indexed(db).values()) == [("Note", "a red zeppelin")]
        assert sorted(hit.sent for hit in search_mail(db, alice, "red")) == [False, True]
    finally:
        db.close()
