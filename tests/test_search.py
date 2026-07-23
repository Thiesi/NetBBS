"""
Tests for netbbs.search (design doc §6.6, issue #56's last piece) --
FTS5-backed local search over board posts, files, and retained channel
scrollback, its index-maintenance sync from every write path, and the
`[F]ind` main-menu screen.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import approve_post, create_post, delete_post, edit_post
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import record_message, set_scrollback_limit
from netbbs.files import entries as entries_module
from netbbs.files.areas import create_file_area
from netbbs.files.entries import approve_file, delete_file, upload_file
from netbbs.moderation import BoardPermission, grant_permissions
from netbbs.net.char_input import InputHistory
from netbbs.net.login_flow import _main_menu
from netbbs.search import (
    check_index_integrity,
    file_jump_cursor,
    post_jump_cursor,
    rebuild_indexes,
    search_channel_messages,
    search_files,
    search_posts,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


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


# -- search_posts ---------------------------------------------------------


def test_search_posts_finds_a_matching_approved_post(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "hello world", "nothing relevant here")
    create_post(db, board, alice, "unrelated", "also nothing relevant")

    hits = search_posts(db, alice, "hello")
    assert [hit.subject for hit in hits] == ["hello world"]


def test_search_posts_matches_body_too(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "subject only", "the word zephyr is buried in here")

    hits = search_posts(db, alice, "zephyr")
    assert len(hits) == 1


def test_search_posts_excludes_pending_posts_on_a_moderated_board(db, alice, bob):
    board = create_board(db, "general", creator=alice, moderated=True)
    create_post(db, board, bob, "hello world", "pending content")

    assert search_posts(db, alice, "hello") == []


def test_search_posts_includes_approved_posts_once_approved(db, alice, bob):
    board = create_board(db, "general", creator=alice, moderated=True)
    grant_permissions(
        db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=alice
    )
    post = create_post(db, board, bob, "hello world", "pending content")

    approve_post(db, post, approved_by=alice)

    assert [hit.subject for hit in search_posts(db, alice, "hello")] == ["hello world"]


def test_search_posts_reflects_latest_approved_edit_only(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    # Deterministic, strictly increasing timestamps -- create_post/
    # edit_post back-to-back can otherwise land in the same real-clock
    # microsecond, and _resolve_current_version's (created_at, post_id)
    # tie-break then picks arbitrarily between revisions (post_id is a
    # content hash, unrelated to recency). See the worklog note on that
    # pre-existing tie-break gap.
    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(2))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    post = create_post(db, board, alice, "hello world", "original body")
    edit_post(db, post, board, subject="hello universe", body="edited body", edited_by=alice)

    hits = search_posts(db, alice, "hello")
    assert len(hits) == 1
    assert hits[0].subject == "hello universe"
    # The superseded revision's own text must not still be independently
    # matchable -- only the resolved current version is ever indexed.
    assert search_posts(db, alice, "original") == []


def test_search_posts_excludes_deleted_posts(db, alice):
    board = create_board(db, "general", creator=alice)
    grant_permissions(
        db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=alice
    )
    post = create_post(db, board, alice, "hello world", "body")

    delete_post(db, post, deleted_by=alice)

    assert search_posts(db, alice, "hello") == []


def test_search_posts_hides_boards_below_the_searching_users_level(db, alice, bob):
    board = create_board(db, "restricted", creator=alice, min_read_level=50)
    sysop = create_user(db, "sysop", password="hunter2", user_level=100)
    create_post(db, board, sysop, "hello world", "secret content")

    assert search_posts(db, bob, "hello") == []
    assert [hit.subject for hit in search_posts(db, sysop, "hello")] == ["hello world"]


def test_search_posts_query_with_special_characters_does_not_raise(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "hello world", "body")

    # FTS5 query syntax (quotes, boolean operators, prefix *) must never
    # leak through from free-typed user input -- _match_expression quotes
    # every token, so this must search literally, not raise a syntax
    # error from inside MATCH.
    assert search_posts(db, alice, 'hello" OR 1=1 --') == []
    assert search_posts(db, alice, "") == []


# -- search_files -----------------------------------------------------------


def test_search_files_finds_a_matching_approved_file(db, alice):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "readme.txt", b"data", description="a helpful guide")
    upload_file(db, area, alice, "other.txt", b"data")

    hits = search_files(db, alice, "helpful")
    assert [hit.filename for hit in hits] == ["readme.txt"]


def test_search_files_excludes_pending_files(db, alice, bob):
    area = create_file_area(db, "downloads", creator=alice, moderated=True)
    upload_file(db, area, bob, "readme.txt", b"data", description="a helpful guide")

    assert search_files(db, alice, "helpful") == []


def test_search_files_excludes_deleted_files(db, alice):
    area = create_file_area(db, "downloads", creator=alice)
    grant_permissions(
        db, alice, object_type="file_area", object_id=area.id, permissions=BoardPermission.DELETE, granted_by=alice
    )
    entry = upload_file(db, area, alice, "readme.txt", b"data", description="a helpful guide")

    delete_file(db, entry, deleted_by=alice)

    assert search_files(db, alice, "helpful") == []


def test_search_files_hides_areas_below_the_searching_users_level(db, alice, bob):
    area = create_file_area(db, "restricted", creator=alice, min_read_level=50)
    upload_file(db, area, alice, "readme.txt", b"data", description="a helpful guide")

    assert search_files(db, bob, "helpful") == []


# -- search_channel_messages --------------------------------------------------


def test_search_channel_messages_finds_a_matching_message(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    record_message(db, channel, kind="message", author_label="alice", body="hello world")
    record_message(db, channel, kind="message", author_label="alice", body="unrelated")

    hits = search_channel_messages(db, alice, "hello", visible_channels=[channel])
    assert [hit.body for hit in hits] == ["hello world"]


def test_search_channel_messages_excludes_system_notices(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    record_message(db, channel, kind="join", author_label="alice")

    assert search_channel_messages(db, alice, "alice", visible_channels=[channel]) == []


def test_search_channel_messages_excludes_channels_not_in_visible_channels(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    record_message(db, channel, kind="message", author_label="alice", body="hello world")

    # Caller passes an empty visible_channels list -- simulating a
    # channel the searching user can no longer see (e.g. members_only).
    assert search_channel_messages(db, alice, "hello", visible_channels=[]) == []


def test_search_channel_messages_prunes_trimmed_scrollback(db, alice):
    channel = create_channel(db, "lobby", creator=alice)
    set_scrollback_limit(db, 2)
    record_message(db, channel, kind="message", author_label="alice", body="hello world")
    record_message(db, channel, kind="message", author_label="alice", body="filler one")
    record_message(db, channel, kind="message", author_label="alice", body="filler two")

    # The scrollback ring buffer (limit 2) has already trimmed "hello
    # world" out -- the search index must not still surface it.
    assert search_channel_messages(db, alice, "hello", visible_channels=[channel]) == []


# -- jump cursors -------------------------------------------------------------


def test_post_jump_cursor_lands_on_the_predecessor_of_the_hit(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(3))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    create_post(db, board, alice, "first", "1")
    second = create_post(db, board, alice, "second", "2")
    create_post(db, board, alice, "third", "3")

    cursor = post_jump_cursor(db, board.id, second.post_id)

    from netbbs.boards.posts import list_posts_page

    page = list_posts_page(db, board, alice, after=cursor)
    assert page.posts[0].subject == "second"


def test_post_jump_cursor_is_the_empty_sentinel_for_the_oldest_post(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(2))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    first = create_post(db, board, alice, "first", "1")
    create_post(db, board, alice, "second", "2")

    assert post_jump_cursor(db, board.id, first.post_id) == ("", "")


def test_file_jump_cursor_lands_on_the_predecessor_of_the_hit(db, alice, monkeypatch):
    area = create_file_area(db, "downloads", creator=alice)
    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(2))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    first = upload_file(db, area, alice, "a.txt", b"1")
    second = upload_file(db, area, alice, "b.txt", b"2")

    cursor = file_jump_cursor(db, area.id, second.file_id)

    from netbbs.files.entries import list_files_page

    page = list_files_page(db, area, alice, after=cursor)
    assert page.entries[0].filename == "b.txt"


# -- [F]ind screen --------------------------------------------------------


class FakeSession:
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_text(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", "".join(session.written))


def _run_main_menu(db, lane, user, keys):
    session = FakeSession(keys)
    asyncio.run(
        _main_menu(
            session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user, lane=lane
        )
    )
    return session


def test_find_is_always_shown_on_the_main_menu(db, alice):
    session = _run_main_menu(db, None, alice, ["l"])
    assert "[F]ind" in _visible_text(session)


def test_find_is_not_available_without_a_lane(db, alice):
    session = _run_main_menu(db, None, alice, ["f", "l"])
    assert "not available in this context" in _visible_text(session)


def test_find_cancels_on_an_empty_query(db, lane, alice):
    session = _run_main_menu(db, lane, alice, ["f", "", "l"])
    assert "Search cancelled." in _visible_text(session)


def test_find_selecting_a_post_jumps_to_it(db, lane, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "hello world", "body")

    session = _run_main_menu(db, lane, alice, ["f", "hello", "0", "1", "b", "l"])

    text = _visible_text(session)
    assert "hello world" in text


def test_find_selecting_a_file_jumps_to_it(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "readme.txt", b"data", description="a helpful guide")

    session = _run_main_menu(db, lane, alice, ["f", "helpful", "0", "1", "b", "l"])

    text = _visible_text(session)
    assert "readme.txt" in text


def test_find_no_matches(db, lane, alice):
    session = _run_main_menu(db, lane, alice, ["f", "nonexistentterm", "l"])
    assert "No matches." in _visible_text(session)


# -- check_index_integrity / rebuild_indexes (issue #74) -------------------


def test_check_index_integrity_is_clean_on_freshly_indexed_content(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "hello world", "body")
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "readme.txt", b"data", description="a guide")
    channel = create_channel(db, "lobby", creator=alice)
    record_message(db, channel, kind="message", author_label="alice", body="hi there")

    report = check_index_integrity(db)

    assert report.is_clean
    assert report.posts.missing == report.posts.stale == report.posts.extra == ()


def test_check_index_integrity_detects_a_missing_post_entry(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "hello world", "body")

    # Simulate a write path that committed the authoritative row but
    # never called reindex_post -- exactly the crash window issue #74
    # is about.
    db.connection.execute("DELETE FROM post_search WHERE root_post_id = ?", (post.root_post_id,))
    db.connection.commit()

    report = check_index_integrity(db)

    assert not report.is_clean
    assert report.posts.missing == (post.root_post_id,)
    assert report.posts.stale == ()
    assert report.posts.extra == ()
    # Only the id is reported, never the drifted content itself.
    assert "hello world" not in repr(report)


def test_check_index_integrity_detects_a_stale_post_entry(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "hello world", "body")

    db.connection.execute(
        "UPDATE post_search SET subject = ? WHERE root_post_id = ?", ("wrong subject", post.root_post_id)
    )
    db.connection.commit()

    report = check_index_integrity(db)

    assert report.posts.stale == (post.root_post_id,)
    assert report.posts.missing == ()
    assert report.posts.extra == ()


def test_check_index_integrity_detects_an_extra_post_entry(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "hello world", "body")

    db.connection.execute(
        "INSERT INTO post_search (subject, body, board_id, root_post_id) VALUES (?, ?, ?, ?)",
        ("orphaned", "orphaned", board.id, "a-root-post-id-that-does-not-exist"),
    )
    db.connection.commit()

    report = check_index_integrity(db)

    assert report.posts.extra == ("a-root-post-id-that-does-not-exist",)
    assert report.posts.missing == ()
    assert report.posts.stale == ()


def test_check_index_integrity_detects_drift_in_files_and_channel_messages(db, alice):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "readme.txt", b"data", description="a guide")
    channel = create_channel(db, "lobby", creator=alice)
    record_message(db, channel, kind="message", author_label="alice", body="hi there")

    db.connection.execute("DELETE FROM file_search")
    db.connection.execute("DELETE FROM channel_message_search")
    db.connection.commit()

    report = check_index_integrity(db)

    assert not report.is_clean
    assert len(report.files.missing) == 1
    assert len(report.channel_messages.missing) == 1


def test_rebuild_indexes_repairs_missing_stale_and_extra_entries(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "hello world", "body")
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "readme.txt", b"data", description="a guide")
    channel = create_channel(db, "lobby", creator=alice)
    record_message(db, channel, kind="message", author_label="alice", body="hi there")

    # Corrupt all three tables at once: a missing post entry, a stale
    # file entry, and an orphaned extra channel-message entry.
    db.connection.execute("DELETE FROM post_search WHERE root_post_id = ?", (post.root_post_id,))
    db.connection.execute("UPDATE file_search SET filename = 'wrong.txt'")
    db.connection.execute(
        "INSERT INTO channel_message_search (body, channel_id, message_id) VALUES ('orphan', ?, ?)",
        (channel.id, 999999),
    )
    db.connection.commit()

    before = rebuild_indexes(db)

    assert not before.is_clean
    assert before.posts.missing == (post.root_post_id,)
    assert before.files.stale != ()
    assert before.channel_messages.extra == (999999,)

    after = check_index_integrity(db)
    assert after.is_clean

    # The repaired content is genuinely searchable again, not just
    # reported as clean.
    assert [hit.subject for hit in search_posts(db, alice, "hello")] == ["hello world"]
    assert [hit.filename for hit in search_files(db, alice, "guide")] == ["readme.txt"]


def test_rebuild_indexes_is_idempotent(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "hello world", "body")

    rebuild_indexes(db)
    first = check_index_integrity(db)
    rebuild_indexes(db)
    second = check_index_integrity(db)

    assert first.is_clean
    assert second.is_clean


def test_rebuild_indexes_excludes_deleted_and_pending_content(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    grant_permissions(
        db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=alice
    )
    deleted_post = create_post(db, board, alice, "will be deleted", "body")
    delete_post(db, deleted_post, deleted_by=alice)

    moderated_board = create_board(db, "modboard", creator=alice, moderated=True)
    create_post(db, moderated_board, bob, "still pending", "body")

    rebuild_indexes(db)

    assert search_posts(db, alice, "deleted") == []
    assert search_posts(db, alice, "pending") == []
    assert check_index_integrity(db).is_clean
