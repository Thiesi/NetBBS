"""Tests for netbbs.boards — board/post creation, level-gating, content IDs."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import BoardError, create_board, get_board_by_name, list_boards
from netbbs.boards.posts import PostError, create_post, get_post, list_posts
from netbbs.permissions import InsufficientLevelError
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
    return create_user(db, "bob", password="hunter2", user_level=0)


# -- board creation ---------------------------------------------------------


def test_create_board(db, alice):
    board = create_board(db, "general", description="General discussion", creator=alice)
    assert board.name == "general"
    assert board.description == "General discussion"
    assert board.min_read_level == 0
    assert board.min_write_level == 0


def test_create_board_generates_content_addressed_id(db, alice):
    board = create_board(db, "general", creator=alice)
    assert len(board.board_id) == 64
    int(board.board_id, 16)


def test_create_duplicate_board_name_fails(db, alice):
    create_board(db, "general", creator=alice)
    with pytest.raises(BoardError):
        create_board(db, "general", creator=alice)


def test_get_board_by_name(db, alice):
    create_board(db, "general", creator=alice)
    board = get_board_by_name(db, "general")
    assert board.name == "general"


def test_get_nonexistent_board_fails(db):
    with pytest.raises(BoardError):
        get_board_by_name(db, "nope")


def test_list_boards_default_order_is_by_last_activity_most_recent_first(db, alice):
    # Creation-order sorting was explicitly rejected as the default (design
    # doc round 18) in favor of "activity". With no posts on either board,
    # activity falls back to each board's own created_at, so the more
    # recently *created* board should sort first. Timestamps are set
    # explicitly (rather than relying on two back-to-back create_board()
    # calls landing on distinct wall-clock values) because on a coarse
    # clock they can tie, and the resulting tie-break order is whatever the
    # table scan happens to produce — that's exactly what let a since-fixed
    # stale test (asserting creation order) pass on Windows by accident
    # while failing on NetBSD's finer-grained clock.
    create_board(db, "first", creator=alice)
    create_board(db, "second", creator=alice)
    db.connection.execute(
        "UPDATE boards SET created_at = ? WHERE name = ?",
        ("2026-01-01T00:00:00.000000Z", "first"),
    )
    db.connection.execute(
        "UPDATE boards SET created_at = ? WHERE name = ?",
        ("2026-01-02T00:00:00.000000Z", "second"),
    )
    db.connection.commit()

    boards = list_boards(db)
    assert [b.name for b in boards] == ["second", "first"]


def test_list_boards_activity_order_uses_latest_post_not_creation_time(db, alice):
    first = create_board(db, "first", creator=alice)
    create_board(db, "second", creator=alice)
    db.connection.execute(
        "UPDATE boards SET created_at = ? WHERE name = ?",
        ("2026-01-01T00:00:00.000000Z", "first"),
    )
    db.connection.execute(
        "UPDATE boards SET created_at = ? WHERE name = ?",
        ("2026-01-02T00:00:00.000000Z", "second"),
    )
    db.connection.commit()

    post = create_post(db, first, alice, "New activity", "bumps first board to the top")
    db.connection.execute(
        "UPDATE posts SET created_at = ? WHERE id = ?",
        ("2026-01-03T00:00:00.000000Z", post.id),
    )
    db.connection.commit()

    boards = list_boards(db)
    assert [b.name for b in boards] == ["first", "second"]


def test_list_boards_alphabetical_order_is_case_insensitive(db, alice):
    create_board(db, "Zebra", creator=alice)
    create_board(db, "apple", creator=alice)
    create_board(db, "Banana", creator=alice)

    boards = list_boards(db, order_by="alphabetical")
    assert [b.name for b in boards] == ["apple", "Banana", "Zebra"]


def test_list_boards_volume_order_is_by_post_count_descending(db, alice):
    quiet = create_board(db, "quiet", creator=alice)
    medium = create_board(db, "medium", creator=alice)
    create_board(db, "empty", creator=alice)
    create_post(db, quiet, alice, "Only post", "body")
    for i in range(3):
        create_post(db, medium, alice, f"Post {i}", "body")

    boards = list_boards(db, order_by="volume")
    assert [b.name for b in boards] == ["medium", "quiet", "empty"]


def test_list_boards_pinned_boards_sort_first_regardless_of_order_by(db, alice):
    create_board(db, "apple", creator=alice)
    create_board(db, "banana", creator=alice)
    create_board(db, "zzz-pinned", pinned=True, creator=alice)

    boards = list_boards(db, order_by="alphabetical")
    assert [b.name for b in boards] == ["zzz-pinned", "apple", "banana"]


def test_list_boards_rejects_unknown_order_by(db):
    with pytest.raises(ValueError):
        list_boards(db, order_by="nonsense")


def test_two_boards_have_different_content_ids_even_with_same_creator(db, alice):
    a = create_board(db, "board-a", creator=alice)
    b = create_board(db, "board-b", creator=alice)
    assert a.board_id != b.board_id


# -- post creation ------------------------------------------------------


def test_create_post(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Hello", "This is a test post.")
    assert post.subject == "Hello"
    assert post.body == "This is a test post."
    assert post.author_label == "alice"
    assert post.board_id == board.id


def test_create_post_generates_content_addressed_id(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Hello", "Body")
    assert len(post.post_id) == 64
    int(post.post_id, 16)


def test_post_records_author_fingerprint_when_present():
    import tempfile
    from pathlib import Path

    import nacl.signing

    signing_key = nacl.signing.SigningKey.generate()

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "node.db")
        user = create_user(database, "carol", verify_key=signing_key.verify_key, user_level=10)
        board = create_board(database, "general", creator=user)
        post = create_post(database, board, user, "Hello", "Body")
        assert post.author_fingerprint == user.fingerprint
        database.close()


def test_post_author_fingerprint_is_none_for_password_only_user(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Hello", "Body")
    assert post.author_fingerprint is None


def test_get_post(db, alice):
    board = create_board(db, "general", creator=alice)
    created = create_post(db, board, alice, "Hello", "Body")
    fetched = get_post(db, created.post_id)
    assert fetched.post_id == created.post_id


def test_get_nonexistent_post_fails(db):
    with pytest.raises(PostError):
        get_post(db, "nonexistent")


def test_reply_references_parent(db, alice):
    board = create_board(db, "general", creator=alice)
    parent = create_post(db, board, alice, "Hello", "Body")
    reply = create_post(db, board, alice, "Re: Hello", "A reply", parent_post_id=parent.post_id)
    assert reply.parent_post_id == parent.post_id


def test_reply_to_nonexistent_parent_fails(db, alice):
    board = create_board(db, "general", creator=alice)
    with pytest.raises(PostError):
        create_post(db, board, alice, "Re: Hello", "A reply", parent_post_id="nonexistent")


def test_reply_to_parent_on_different_board_fails(db, alice):
    board_a = create_board(db, "board-a", creator=alice)
    board_b = create_board(db, "board-b", creator=alice)
    parent = create_post(db, board_a, alice, "Hello", "Body")
    with pytest.raises(PostError):
        create_post(db, board_b, alice, "Re: Hello", "A reply", parent_post_id=parent.post_id)


def test_list_posts_returns_all_in_order(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "First", "1")
    create_post(db, board, alice, "Second", "2")
    posts = list_posts(db, board, alice)
    assert [p.subject for p in posts] == ["First", "Second"]


# -- level-gating ---------------------------------------------------------


def test_write_blocked_below_min_write_level(db, alice, bob):
    board = create_board(db, "staff-only", min_write_level=50, creator=alice)
    with pytest.raises(InsufficientLevelError):
        create_post(db, board, bob, "Hello", "Body")


def test_write_allowed_at_exact_min_write_level(db, bob):
    board = create_board(db, "general", min_write_level=0, creator=bob)
    post = create_post(db, board, bob, "Hello", "Body")
    assert post.subject == "Hello"


def test_read_blocked_below_min_read_level(db, alice, bob):
    board = create_board(db, "staff-only", min_read_level=50, creator=alice)
    with pytest.raises(InsufficientLevelError):
        list_posts(db, board, bob)


def test_read_allowed_at_sufficient_level(db, alice):
    board = create_board(db, "general", min_read_level=5, creator=alice)
    create_post(db, board, alice, "Hello", "Body")
    posts = list_posts(db, board, alice)
    assert len(posts) == 1
