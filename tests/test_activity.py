"""
Tests for netbbs.activity (design doc §6.6, issue #56) -- per-user read
cursors for boards/file areas/channels, cross-board unread replies, and
follow/favourite state.
"""

from __future__ import annotations

import pytest

from netbbs.activity import (
    follow,
    is_following,
    list_followed,
    record_board_seen,
    record_channel_seen,
    record_file_area_seen,
    unfollow,
    unread_channel_count,
    unread_file_count,
    unread_post_count,
    unread_replies_to,
)
from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board, delete_board
from netbbs.boards.posts import create_post, edit_post
from netbbs.chat.channels import create_channel, delete_channel
from netbbs.chat.scrollback import record_message
from netbbs.communities import create_community, delete_community
from netbbs.files import entries as entries_module
from netbbs.files.areas import create_file_area, delete_file_area
from netbbs.files.entries import upload_file
from netbbs.storage.database import Database


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


def _deterministic_timestamps(monkeypatch, module, count: int, *, prefix="2026-01-01T00:00:"):
    timestamps = iter(f"{prefix}{i:02d}.000000Z" for i in range(count))
    monkeypatch.setattr(module, "utc_now_iso", lambda: next(timestamps))


# -- board read cursors -------------------------------------------------


def test_unread_post_count_is_none_before_any_visit(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "hello", "world")
    assert unread_post_count(db, bob, board) is None


def test_unread_post_count_is_zero_once_caught_up(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _deterministic_timestamps(monkeypatch, posts_module, 1)
    post = create_post(db, board, alice, "hello", "world")

    record_board_seen(db, bob, board, post)

    assert unread_post_count(db, bob, board) == 0


def test_unread_post_count_reflects_posts_after_the_cursor(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _deterministic_timestamps(monkeypatch, posts_module, 3)
    first = create_post(db, board, alice, "first", "1")
    create_post(db, board, alice, "second", "2")
    create_post(db, board, alice, "third", "3")

    record_board_seen(db, bob, board, first)

    assert unread_post_count(db, bob, board) == 2


def test_cursor_never_retreats_on_an_older_view(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _deterministic_timestamps(monkeypatch, posts_module, 2)
    first = create_post(db, board, alice, "first", "1")
    second = create_post(db, board, alice, "second", "2")

    record_board_seen(db, bob, board, second)  # newest first
    record_board_seen(db, bob, board, first)  # then an older page view

    assert unread_post_count(db, bob, board) == 0  # still caught up, not regressed


def test_editing_an_already_read_post_does_not_make_it_unread_again(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _deterministic_timestamps(monkeypatch, posts_module, 2)
    post = create_post(db, board, alice, "hello", "world")
    record_board_seen(db, bob, board, post)

    edit_post(db, post, board, subject="hello (edited)", body="world, edited", edited_by=alice)

    assert unread_post_count(db, bob, board) == 0


def test_unread_post_count_excludes_posts_with_no_approved_version(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice, moderated=True)
    _deterministic_timestamps(monkeypatch, posts_module, 2)
    first = create_post(db, board, alice, "first", "1")
    record_board_seen(db, bob, board, first)
    create_post(db, board, alice, "pending", "not yet approved")  # moderated board -> stays pending

    assert unread_post_count(db, bob, board) == 0


# -- file area read cursors ----------------------------------------------


def test_unread_file_count_is_none_before_any_visit(db, alice, bob):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "a.txt", b"hello")
    assert unread_file_count(db, bob, area) is None


def test_unread_file_count_reflects_files_after_the_cursor(db, alice, bob, monkeypatch):
    area = create_file_area(db, "downloads", creator=alice)
    # entries_module, not posts_module -- upload_file's own utc_now_iso
    # binding lives in netbbs.files.entries; patching the boards module
    # here was a no-op that let two uploads race for the same real-clock
    # microsecond, breaking (created_at, file_id) ordering unpredictably
    # (file_id is a content hash, not creation-ordered) often enough to
    # flake this assertion.
    _deterministic_timestamps(monkeypatch, entries_module, 2)
    first = upload_file(db, area, alice, "a.txt", b"hello")
    upload_file(db, area, alice, "b.txt", b"world")

    record_file_area_seen(db, bob, area, first)

    assert unread_file_count(db, bob, area) == 1


# -- channel read cursors -------------------------------------------------


def test_unread_channel_count_is_none_before_any_visit(db, alice, bob):
    channel = create_channel(db, "lobby", creator=alice)
    record_message(db, channel, kind="message", author_label="alice", body="hi")
    assert unread_channel_count(db, bob, channel) is None


def test_unread_channel_count_reflects_messages_after_the_cursor(db, alice, bob):
    channel = create_channel(db, "lobby", creator=alice)
    first = record_message(db, channel, kind="message", author_label="alice", body="hi")
    record_message(db, channel, kind="message", author_label="alice", body="how's it going")

    record_channel_seen(db, bob, channel, first)

    assert unread_channel_count(db, bob, channel) == 1


def test_unread_channel_count_excludes_system_notices(db, alice, bob):
    channel = create_channel(db, "lobby", creator=alice)
    first = record_message(db, channel, kind="message", author_label="alice", body="hi")
    record_channel_seen(db, bob, channel, first)
    record_message(db, channel, kind="join", author_label="carol")
    record_message(db, channel, kind="nick", author_label="carol", body="dave")

    assert unread_channel_count(db, bob, channel) == 0


def test_channel_cursor_compares_ids_numerically_not_as_strings(db, alice, bob):
    """Regression: comparing stable_id as a string would rank '9' ahead
    of '10', silently losing unread messages 10-99 the moment a channel
    passes 9 retained messages."""
    channel = create_channel(db, "lobby", creator=alice)
    messages = [
        record_message(db, channel, kind="message", author_label="alice", body=f"msg {i}") for i in range(12)
    ]
    record_channel_seen(db, bob, channel, messages[8])  # the 9th message, id likely single-digit-adjacent

    assert unread_channel_count(db, bob, channel) == 3  # messages 10, 11, 12


# -- cross-board unread replies -------------------------------------------


def test_unread_replies_to_finds_a_reply_and_ignores_non_replies(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _deterministic_timestamps(monkeypatch, posts_module, 3)
    bobs_post = create_post(db, board, bob, "question", "how do I do X?")
    create_post(db, board, alice, "unrelated", "just chatting")
    reply = create_post(db, board, alice, "Re: question", "like this", parent_post_id=bobs_post.post_id)

    unread = unread_replies_to(db, bob)

    assert [p.post_id for p in unread] == [reply.post_id]


def test_unread_replies_to_respects_the_boards_own_cursor(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _deterministic_timestamps(monkeypatch, posts_module, 2)
    bobs_post = create_post(db, board, bob, "question", "how do I do X?")
    reply = create_post(db, board, alice, "Re: question", "like this", parent_post_id=bobs_post.post_id)

    record_board_seen(db, bob, board, reply)  # bob already saw the reply

    assert unread_replies_to(db, bob) == []


# -- follows ---------------------------------------------------------------


def test_follow_unfollow_and_is_following(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    assert is_following(db, bob, "board", board.id) is False

    follow(db, bob, "board", board.id)
    assert is_following(db, bob, "board", board.id) is True
    assert list_followed(db, bob, "board") == [board.id]

    unfollow(db, bob, "board", board.id)
    assert is_following(db, bob, "board", board.id) is False
    assert list_followed(db, bob, "board") == []


def test_following_twice_is_a_no_op(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    follow(db, bob, "board", board.id)
    follow(db, bob, "board", board.id)
    assert list_followed(db, bob, "board") == [board.id]


# -- cleanup on deletion -----------------------------------------------------


def test_deleting_a_board_removes_its_cursor_and_follow_rows(db, alice, bob, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _deterministic_timestamps(monkeypatch, posts_module, 1)
    post = create_post(db, board, alice, "hello", "world")
    record_board_seen(db, bob, board, post)
    follow(db, bob, "board", board.id)

    delete_board(db, board, deleted_by=alice)

    row_cursor = db.connection.execute(
        "SELECT 1 FROM user_read_cursors WHERE object_type = 'board' AND object_id = ?", (board.id,)
    ).fetchone()
    row_follow = db.connection.execute(
        "SELECT 1 FROM user_follows WHERE object_type = 'board' AND object_id = ?", (board.id,)
    ).fetchone()
    assert row_cursor is None
    assert row_follow is None


def test_deleting_a_channel_removes_its_cursor_and_follow_rows(db, alice, bob):
    channel = create_channel(db, "lobby", creator=alice)
    message = record_message(db, channel, kind="message", author_label="alice", body="hi")
    record_channel_seen(db, bob, channel, message)
    follow(db, bob, "channel", channel.id)

    delete_channel(db, channel, deleted_by=alice)

    assert db.connection.execute(
        "SELECT 1 FROM user_read_cursors WHERE object_type = 'channel' AND object_id = ?", (channel.id,)
    ).fetchone() is None
    assert db.connection.execute(
        "SELECT 1 FROM user_follows WHERE object_type = 'channel' AND object_id = ?", (channel.id,)
    ).fetchone() is None


def test_deleting_a_file_area_removes_its_cursor_and_follow_rows(db, alice, bob):
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "a.txt", b"hello")
    record_file_area_seen(db, bob, area, entry)
    follow(db, bob, "file_area", area.id)

    delete_file_area(db, area, deleted_by=alice)

    assert db.connection.execute(
        "SELECT 1 FROM user_read_cursors WHERE object_type = 'file_area' AND object_id = ?", (area.id,)
    ).fetchone() is None
    assert db.connection.execute(
        "SELECT 1 FROM user_follows WHERE object_type = 'file_area' AND object_id = ?", (area.id,)
    ).fetchone() is None


def test_deleting_a_community_removes_its_follow_rows(db, alice, bob):
    community = create_community(db, "Vintage Computing", creator=alice)
    follow(db, bob, "community", community.id)

    delete_community(db, community, deleted_by=alice)

    assert db.connection.execute(
        "SELECT 1 FROM user_follows WHERE object_type = 'community' AND object_id = ?", (community.id,)
    ).fetchone() is None


# -- issue #72: last_seen_arrival_id migration backfill ------------------


def test_migration_backfills_arrival_id_for_a_pre_existing_board_cursor(tmp_path, monkeypatch):
    """A cursor row written before this migration existed has no
    last_seen_arrival_id of its own -- the migration must compute it
    from the post its last_seen_stable_id already names, preserving
    exactly what that user had already read rather than resetting
    anyone to a fresh, all-unread state."""
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    db_path = tmp_path / "node.db"

    # Apply every migration before this one, matching the schema shape a
    # real pre-upgrade database would have on disk. Found by description
    # rather than MIGRATIONS[:-1] -- this migration is no longer
    # guaranteed to be the last one in the list as later migrations
    # (e.g. issue #85's) get appended after it.
    arrival_id_migration_index = next(
        i for i, m in enumerate(MIGRATIONS) if "last_seen_arrival_id" in m.description
    )
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:arrival_id_migration_index])
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "hello", "world")
    # Write the cursor row the same shape the pre-#72 code actually
    # wrote -- record_board_seen itself now assumes last_seen_arrival_id
    # already exists, so it can't be used against this older schema.
    db.connection.execute(
        "INSERT INTO user_read_cursors "
        "(user_id, object_type, object_id, last_seen_created_at, last_seen_stable_id, updated_at) "
        "VALUES (?, 'board', ?, ?, ?, ?)",
        (alice.id, board.id, post.created_at, post.post_id, post.created_at),
    )
    db.connection.commit()
    db.close()
    monkeypatch.undo()

    # Reopen with the real, full migration list -- the arrival_id
    # migration (and any later ones) now run.
    db = Database(db_path)
    try:
        row = db.connection.execute(
            "SELECT last_seen_arrival_id, last_seen_stable_id FROM user_read_cursors "
            "WHERE user_id = ? AND object_type = 'board' AND object_id = ?",
            (alice.id, board.id),
        ).fetchone()
        post_row = db.connection.execute(
            "SELECT id FROM posts WHERE post_id = ?", (row["last_seen_stable_id"],)
        ).fetchone()
        assert row["last_seen_arrival_id"] == post_row["id"]

        # And unread counting works immediately using the backfilled value.
        assert unread_post_count(db, alice, board) == 0
        new_post = create_post(db, board, alice, "hello again", "world again")
        assert unread_post_count(db, alice, board) == 1
        record_board_seen(db, alice, board, new_post)
        assert unread_post_count(db, alice, board) == 0
    finally:
        db.close()


def test_migration_backfills_arrival_id_for_a_pre_existing_file_area_cursor(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    db_path = tmp_path / "node.db"

    # Found by description rather than MIGRATIONS[:-1] -- this migration
    # is no longer guaranteed to be the last one in the list.
    arrival_id_migration_index = next(
        i for i, m in enumerate(MIGRATIONS) if "last_seen_arrival_id" in m.description
    )
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:arrival_id_migration_index])
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "readme.txt", b"data")
    db.connection.execute(
        "INSERT INTO user_read_cursors "
        "(user_id, object_type, object_id, last_seen_created_at, last_seen_stable_id, updated_at) "
        "VALUES (?, 'file_area', ?, ?, ?, ?)",
        (alice.id, area.id, entry.created_at, entry.file_id, entry.created_at),
    )
    db.connection.commit()
    db.close()
    monkeypatch.undo()

    db = Database(db_path)
    try:
        row = db.connection.execute(
            "SELECT last_seen_arrival_id, last_seen_stable_id FROM user_read_cursors "
            "WHERE user_id = ? AND object_type = 'file_area' AND object_id = ?",
            (alice.id, area.id),
        ).fetchone()
        file_row = db.connection.execute(
            "SELECT id FROM files WHERE file_id = ?", (row["last_seen_stable_id"],)
        ).fetchone()
        assert row["last_seen_arrival_id"] == file_row["id"]
        assert unread_file_count(db, alice, area) == 0
    finally:
        db.close()


def test_migration_backfills_arrival_id_for_a_pre_existing_channel_cursor(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    db_path = tmp_path / "node.db"

    # Found by description rather than MIGRATIONS[:-1] -- this migration
    # is no longer guaranteed to be the last one in the list.
    arrival_id_migration_index = next(
        i for i, m in enumerate(MIGRATIONS) if "last_seen_arrival_id" in m.description
    )
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:arrival_id_migration_index])
    db = Database(db_path)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    channel = create_channel(db, "lobby", creator=alice)
    message = record_message(db, channel, kind="message", author_label="alice", body="hi")
    db.connection.execute(
        "INSERT INTO user_read_cursors "
        "(user_id, object_type, object_id, last_seen_created_at, last_seen_stable_id, updated_at) "
        "VALUES (?, 'channel', ?, ?, ?, ?)",
        (alice.id, channel.id, message.created_at, str(message.id), message.created_at),
    )
    db.connection.commit()
    db.close()
    monkeypatch.undo()

    db = Database(db_path)
    try:
        row = db.connection.execute(
            "SELECT last_seen_arrival_id, last_seen_stable_id FROM user_read_cursors "
            "WHERE user_id = ? AND object_type = 'channel' AND object_id = ?",
            (alice.id, channel.id),
        ).fetchone()
        assert row["last_seen_arrival_id"] == int(row["last_seen_stable_id"]) == message.id
        assert unread_channel_count(db, alice, channel) == 0
    finally:
        db.close()
