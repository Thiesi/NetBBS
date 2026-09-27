"""
Issue #710: a board post counts as read only once it is opened.

A board cursor's arrival id is a floor (everything at or below it is
read) plus the set of posts opened above it, `user_board_opened_posts`.
These tests run against real SQLite: the floor folding over an unbroken
run of opened posts, the cap on the set, [M]ark all read, and the
migration that turns existing cursors into floors.
"""

from __future__ import annotations

import pytest

from netbbs import activity
from netbbs.activity import (
    board_read_cursor,
    ensure_board_baseline,
    mark_board_read,
    record_post_opened,
    unread_post_count,
    unread_post_ids,
    unread_replies_to,
)
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import approve_post, create_post
from netbbs.storage.database import Database
from tests.legacy_schema import insert_user_on_old_schema


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


def _posts(db, board, author, count, monkeypatch):
    stamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(count))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    return [create_post(db, board, author, f"post {i}", "body") for i in range(count)]


def _opened_rows(db, user, board) -> list[int]:
    return [
        row[0] for row in db.connection.execute(
            "SELECT post_row_id FROM user_board_opened_posts WHERE user_id = ? AND board_id = ? "
            "ORDER BY post_row_id",
            (user.id, board.id),
        )
    ]


def _floor(db, user, board) -> int:
    return db.connection.execute(
        "SELECT last_seen_arrival_id FROM user_read_cursors "
        "WHERE user_id = ? AND object_type = 'board' AND object_id = ?",
        (user.id, board.id),
    ).fetchone()[0]


def test_opening_out_of_order_keeps_a_row_until_the_gap_is_read(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    first, second, third = _posts(db, board, alice, 3, monkeypatch)

    record_post_opened(db, bob, board, third)
    record_post_opened(db, bob, board, second)
    assert _opened_rows(db, bob, board) == [second.id, third.id]
    assert unread_post_ids(db, bob, board, [first, second, third]) == {first.id}

    record_post_opened(db, bob, board, first)

    # The run from the floor is unbroken: it folds into the floor.
    assert _opened_rows(db, bob, board) == []
    assert _floor(db, bob, board) == third.id
    assert unread_post_count(db, bob, board) == 0


def test_reading_in_order_never_stores_a_row(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 4, monkeypatch)

    for post in posts:
        record_post_opened(db, bob, board, post)
        assert _opened_rows(db, bob, board) == []

    assert _floor(db, bob, board) == posts[-1].id


def test_a_pending_post_is_not_a_gap(db, alice, bob, monkeypatch):
    """A post a reader cannot see cannot be read, so it does not hold the
    floor back -- and it becomes unread when it is approved."""
    board = create_board(db, "general", creator=alice, moderated=True)
    moderator = create_user(db, "mod", password="hunter2", user_level=SYSOP_LEVEL)
    ensure_board_baseline(db, bob, board)
    stamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(5))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    first = approve_post(db, create_post(db, board, alice, "first", "1"), approved_by=moderator)
    held = create_post(db, board, alice, "held", "2")
    third = approve_post(db, create_post(db, board, alice, "third", "3"), approved_by=moderator)

    record_post_opened(db, bob, board, first)
    record_post_opened(db, bob, board, third)

    assert _opened_rows(db, bob, board) == []
    assert unread_post_count(db, bob, board) == 0
    # Approved later, below the floor the run moved it past: it counts as
    # read. The floor cannot hold a post back it could not show.
    approve_post(db, held, approved_by=moderator)
    assert unread_post_count(db, bob, board) == 0


def test_the_set_is_capped_and_the_floor_takes_the_oldest_kept_row(db, alice, bob, monkeypatch):
    monkeypatch.setattr(activity, "OPENED_POSTS_CAP", 3)
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 7, monkeypatch)

    # Skip post 0; open 1, 3, 4, 5, 6 -- five rows past a cap of three.
    for index in (1, 3, 4, 5, 6):
        record_post_opened(db, bob, board, posts[index])

    rows = _opened_rows(db, bob, board)
    assert len(rows) <= 3
    # The floor moved up to the oldest kept row; what it passed counts as
    # read, and everything opened above it still is.
    assert unread_post_ids(db, bob, board, posts) == set()
    assert unread_post_count(db, bob, board) == 0


def test_the_cap_gives_up_only_the_oldest_gaps(db, alice, bob, monkeypatch):
    monkeypatch.setattr(activity, "OPENED_POSTS_CAP", 2)
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 8, monkeypatch)

    # Gaps at 0, 2 and 6; opened 1, 3, 5, 7.
    for index in (1, 3, 5, 7):
        record_post_opened(db, bob, board, posts[index])

    assert len(_opened_rows(db, bob, board)) <= 2
    # The newest gap is still unread; the oldest was given up.
    unread = unread_post_ids(db, bob, board, posts)
    assert posts[6].id in unread
    assert posts[0].id not in unread


def test_mark_board_read_moves_the_floor_and_the_jump_position(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 4, monkeypatch)
    record_post_opened(db, bob, board, posts[2])

    mark_board_read(db, bob, board)

    assert unread_post_count(db, bob, board) == 0
    assert _opened_rows(db, bob, board) == []
    assert board_read_cursor(db, bob, board) == (posts[-1].created_at, posts[-1].post_id)


def test_mark_board_read_leaves_a_held_post_to_be_new_when_approved(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice, moderated=True)
    moderator = create_user(db, "mod", password="hunter2", user_level=SYSOP_LEVEL)
    ensure_board_baseline(db, bob, board)
    stamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(5))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    approve_post(db, create_post(db, board, alice, "first", "1"), approved_by=moderator)
    held = create_post(db, board, alice, "held", "2")

    mark_board_read(db, bob, board)
    assert unread_post_count(db, bob, board) == 0

    approve_post(db, held, approved_by=moderator)
    assert unread_post_count(db, bob, board) == 1


def test_unread_replies_follow_the_opened_rule(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    stamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(4))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    question = create_post(db, board, bob, "question", "?")
    create_post(db, board, alice, "unrelated", "x")
    reply = create_post(db, board, alice, "Re: question", "!", parent_post_id=question.post_id)

    assert [r.post_id for r in unread_replies_to(db, bob)] == [reply.post_id]

    record_post_opened(db, bob, board, reply)  # out of order: "unrelated" is still unread

    assert unread_replies_to(db, bob) == []
    assert unread_post_count(db, bob, board) == 2  # "question" and "unrelated"


def test_migration_keeps_existing_cursors_as_floors(tmp_path, monkeypatch):
    """Nobody's history is reset: a cursor written before #710 is the
    floor, and one still without an arrival id gets the newest root at or
    before its feed position."""
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "user_board_opened_posts" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = insert_user_on_old_schema(db, "alice", user_level=10)
    bob = insert_user_on_old_schema(db, "bob", user_level=10)
    board = create_board(db, "general", creator=alice)
    stamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(3))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    first = create_post(db, board, alice, "first", "1")
    second = create_post(db, board, alice, "second", "2")
    create_post(db, board, alice, "third", "3")
    # alice's cursor is at `second` with its arrival id; bob's names
    # `second`'s position but lost its arrival id.
    for user, arrival in ((alice, second.id), (bob, None)):
        db.connection.execute(
            "INSERT INTO user_read_cursors (user_id, object_type, object_id, last_seen_created_at, "
            "last_seen_stable_id, last_seen_arrival_id, updated_at) VALUES (?, 'board', ?, ?, ?, ?, ?)",
            (user.id, board.id, second.created_at, second.post_id, arrival, second.created_at),
        )
    db.connection.commit()
    db.close()
    monkeypatch.undo()

    db = Database(db_path)
    try:
        assert unread_post_count(db, alice, board) == 1
        assert unread_post_count(db, bob, board) == 1
        assert _floor(db, bob, board) == second.id
        assert first.id < second.id
    finally:
        db.close()


# -- Codex review on #723 ---------------------------------------------------------


def test_the_jump_anchors_before_the_first_unread_post_not_past_it(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 3, monkeypatch)

    record_post_opened(db, bob, board, posts[2])
    # Post 0 is the first unread and the oldest post: jump from the start.
    assert board_read_cursor(db, bob, board) == ("", "")

    record_post_opened(db, bob, board, posts[0])
    # Post 1 is the first unread: jump from just before it.
    assert board_read_cursor(db, bob, board) == (posts[0].created_at, posts[0].post_id)

    record_post_opened(db, bob, board, posts[1])
    # Nothing unread: the newest post, so the jump lands on the newest page.
    assert board_read_cursor(db, bob, board) == (posts[2].created_at, posts[2].post_id)


def test_deleting_an_opened_post_drops_its_row(db, alice, bob, monkeypatch):
    """`posts.id` can be reused once the newest row is gone: a stale row
    would mark the next post read (Codex review on #723)."""
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 2, monkeypatch)
    record_post_opened(db, bob, board, posts[1])
    assert _opened_rows(db, bob, board) == [posts[1].id]

    db.connection.execute("DELETE FROM posts WHERE id = ?", (posts[1].id,))
    db.connection.commit()

    assert _opened_rows(db, bob, board) == []


def test_read_state_writes_for_a_deleted_board_do_nothing(db, alice, bob, monkeypatch):
    from netbbs.boards.boards import delete_board

    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 2, monkeypatch)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    delete_board(db, board, deleted_by=sysop)

    record_post_opened(db, bob, board, posts[1])
    mark_board_read(db, bob, board)
    ensure_board_baseline(db, bob, board)

    assert db.connection.execute(
        "SELECT COUNT(*) FROM user_read_cursors WHERE object_type = 'board' AND object_id = ?", (board.id,)
    ).fetchone()[0] == 0


def test_migration_carries_a_legacy_cursors_non_prefix_read_state(tmp_path, monkeypatch):
    """A legacy cursor with no arrival id read by feed position. A post
    newer by feed that arrived first was unread; an older one that arrived
    later was read. The floor must not collapse the two (Codex review)."""
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "user_board_opened_posts" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = insert_user_on_old_schema(db, "alice", user_level=10)
    bob = insert_user_on_old_schema(db, "bob", user_level=10)
    board = create_board(db, "general", creator=alice)
    stamps = iter(["2026-01-01T10:00:00.000000Z", "2026-01-01T08:00:00.000000Z"])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    newer_by_feed = create_post(db, board, alice, "newer by feed, arrived first", "1")
    older_by_feed = create_post(db, board, alice, "older by feed, arrived later", "2")
    assert older_by_feed.id > newer_by_feed.id
    db.connection.execute(
        "INSERT INTO user_read_cursors (user_id, object_type, object_id, last_seen_created_at, "
        "last_seen_stable_id, last_seen_arrival_id, updated_at) VALUES (?, 'board', ?, ?, ?, NULL, ?)",
        (bob.id, board.id, "2026-01-01T09:00:00.000000Z", "gone", "2026-01-01T09:00:00.000000Z"),
    )
    db.connection.commit()
    db.close()
    monkeypatch.undo()

    db = Database(db_path)
    try:
        board_now = db.connection.execute("SELECT id FROM boards WHERE id = ?", (board.id,)).fetchone()
        assert board_now is not None
        assert unread_post_ids(db, bob, board, [newer_by_feed, older_by_feed]) == {newer_by_feed.id}
        assert unread_post_count(db, bob, board) == 1
    finally:
        db.close()


def test_migration_caps_what_it_backfills(tmp_path, monkeypatch):
    """A legacy cursor can have read hundreds of posts that arrived after
    its first unread one; the migration keeps the newest 500 as the
    runtime would, and the floor takes the oldest kept (Codex review)."""
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "user_board_opened_posts" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    alice = insert_user_on_old_schema(db, "alice", user_level=10)
    board = create_board(db, "general", creator=alice)

    def _root(post_id: str, created_at: str) -> int:
        cursor = db.connection.execute(
            "INSERT INTO posts (post_id, board_id, parent_post_id, author_user_id, author_label, "
            "author_fingerprint, subject, body, created_at, status, root_post_id) "
            "VALUES (?, ?, NULL, ?, 'alice', ?, 's', 'b', ?, 'approved', ?)",
            (post_id, board.id, alice.id, alice.fingerprint, created_at, post_id),
        )
        return cursor.lastrowid

    _root("unread-first", "2026-02-01T00:00:00.000000Z")  # past the cursor, arrived first
    read_ids = [_root(f"read-{i:04d}", f"2026-01-01T00:00:{i % 60:02d}.{i:06d}Z") for i in range(520)]
    db.connection.execute(
        "INSERT INTO user_read_cursors (user_id, object_type, object_id, last_seen_created_at, "
        "last_seen_stable_id, last_seen_arrival_id, updated_at) VALUES (?, 'board', ?, ?, ?, NULL, ?)",
        (alice.id, board.id, "2026-01-15T00:00:00.000000Z", "gone", "2026-01-15T00:00:00.000000Z"),
    )
    db.connection.commit()
    db.close()
    monkeypatch.undo()

    db = Database(db_path)
    try:
        assert len(_opened_rows(db, alice, board)) == 499
        assert _floor(db, alice, board) == sorted(read_ids)[-500]
    finally:
        db.close()


def test_opening_a_post_deleted_meanwhile_writes_no_row(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 2, monkeypatch)
    db.connection.execute("DELETE FROM posts WHERE id = ?", (posts[1].id,))
    db.connection.commit()

    record_post_opened(db, bob, board, posts[1])  # the reader still had it

    assert _opened_rows(db, bob, board) == []


def test_the_floor_never_retreats(db, alice, bob, monkeypatch):
    """Another session of the same account can raise the floor while this
    one compacts from the old value; the lower result must not win."""
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 3, monkeypatch)
    mark_board_read(db, bob, board)
    high = _floor(db, bob, board)
    monkeypatch.setattr(activity, "_compact", lambda db, user, board, floor: (0, False))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: "2026-01-02T00:00:00.000000Z")
    later = create_post(db, board, alice, "later", "x")

    record_post_opened(db, bob, board, later)

    assert _floor(db, bob, board) == high == posts[-1].id


def test_a_stale_open_does_not_mark_a_post_that_reused_its_row_id(db, alice, bob, monkeypatch):
    """The newest post is deleted and a new one takes its row id while the
    reader still shows the old one: opening the old one must not mark the
    new one read (Codex review on #723)."""
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 2, monkeypatch)
    db.connection.execute("DELETE FROM posts WHERE id = ?", (posts[1].id,))
    db.connection.commit()
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: "2026-01-02T00:00:00.000000Z")
    replacement = create_post(db, board, alice, "replacement", "x")
    assert replacement.id == posts[1].id  # SQLite reused the row id

    record_post_opened(db, bob, board, posts[1])

    assert replacement.id in unread_post_ids(db, bob, board, [replacement])


def test_a_stale_board_does_not_take_a_replacements_read_state(db, alice, bob, monkeypatch):
    """`boards.id` is reused once the newest board is deleted; a caller
    still holding the old board must not write read state for the new one
    (Codex review on #723)."""
    from netbbs.boards.boards import delete_board

    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    old = create_board(db, "old", creator=alice)
    delete_board(db, old, deleted_by=sysop)
    replacement = create_board(db, "replacement", creator=alice)
    assert replacement.id == old.id  # SQLite reused the row id

    mark_board_read(db, bob, old)
    ensure_board_baseline(db, bob, old)

    assert unread_post_count(db, bob, replacement) is None  # never visited


def test_a_late_baseline_does_not_overwrite_one_already_made(db, alice, bob, monkeypatch):
    """Two sessions of one account both find no cursor; the one that
    writes second must not replace the other's floor (Codex review)."""
    board = create_board(db, "general", creator=alice)
    ensure_board_baseline(db, bob, board)
    posts = _posts(db, board, alice, 2, monkeypatch)
    mark_board_read(db, bob, board)
    raised = _floor(db, bob, board)
    # The stale session looked before any of that happened: no cursor, and
    # an empty board.
    monkeypatch.setattr(activity, "_get_cursor", lambda *args: None)
    monkeypatch.setattr(activity, "_newest_visible", lambda *args, **kwargs: None)
    activity.ensure_board_baseline(db, bob, board)
    monkeypatch.undo()

    assert _floor(db, bob, board) == raised == posts[-1].id
