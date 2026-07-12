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


def test_list_boards_returns_all_in_creation_order(db, alice):
    create_board(db, "first", creator=alice)
    create_board(db, "second", creator=alice)
    boards = list_boards(db)
    assert [b.name for b in boards] == ["first", "second"]


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
