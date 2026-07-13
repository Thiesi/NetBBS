"""
Tests for the moderated-board approval flow and post maintenance/expiry
state machine (design doc §13/§15, sign-off round 35) in
netbbs.boards.posts, built on top of netbbs.moderation.roles's grants
(round 34).
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import (
    PostError,
    approve_post,
    create_post,
    delete_post,
    list_pending_posts,
    list_pinned_posts,
    list_posts_page,
    set_post_exempt,
    set_post_pinned,
)
from netbbs.config import get_expiry_grace_period_days, set_expiry_grace_period_days
from netbbs.moderation import BoardPermission, grant_permissions, list_actions_for_target_user
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=100)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


def _age_post(db, post, days_old: int) -> None:
    """Backdate a post's created_at by `days_old` days -- same
    direct-SQL-manipulation approach test_boards.py already uses to
    simulate old content, rather than monkeypatching a clock."""
    import datetime

    backdated = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_old)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", (backdated, post.id))
    db.connection.commit()


# -- moderated approval flow: initial status ---------------------------


def test_post_on_non_moderated_board_starts_approved(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Hello", "Body")
    assert post.status == "approved"


def test_post_on_moderated_board_starts_pending(db, alice):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    post = create_post(db, board, alice, "Hello", "Body")
    assert post.status == "pending"


def test_pending_post_is_hidden_from_normal_listing(db, alice, bob):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    create_post(db, board, alice, "Hello", "Body")
    page = list_posts_page(db, board, bob)
    assert page.posts == []


# -- moderation queue: list_pending_posts --------------------------------


def test_list_pending_posts_visible_to_approve_holder(db, sysop, alice, bob):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    create_post(db, board, bob, "Hello", "Body")
    grant_permissions(db, sysop, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=sysop)

    pending = list_pending_posts(db, board, requesting_user=sysop)
    assert len(pending) == 1


def test_list_pending_posts_shows_only_own_posts_without_approve(db, alice, bob):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    create_post(db, board, alice, "Alice's post", "Body")
    create_post(db, board, bob, "Bob's post", "Body")

    pending = list_pending_posts(db, board, requesting_user=alice)
    assert [p.subject for p in pending] == ["Alice's post"]


def test_list_pending_posts_empty_for_uninvolved_user(db, alice, bob):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    create_post(db, board, alice, "Hello", "Body")

    pending = list_pending_posts(db, board, requesting_user=bob)
    assert pending == []


# -- approve_post ---------------------------------------------------------


def test_approve_post_requires_approve_permission(db, alice, bob):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    post = create_post(db, board, bob, "Hello", "Body")
    with pytest.raises(PostError):
        approve_post(db, post, approved_by=bob)


def test_approve_post_transitions_to_approved_and_becomes_visible(db, sysop, alice, bob):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    grant_permissions(db, sysop, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=sysop)
    post = create_post(db, board, bob, "Hello", "Body")

    approved = approve_post(db, post, approved_by=sysop)
    assert approved.status == "approved"

    page = list_posts_page(db, board, bob)
    assert [p.subject for p in page.posts] == ["Hello"]


def test_approve_post_is_logged(db, sysop, alice, bob):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    grant_permissions(db, sysop, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=sysop)
    post = create_post(db, board, bob, "Hello", "Body")

    approve_post(db, post, approved_by=sysop)
    entries = list_actions_for_target_user(db, bob.id)
    assert any(e.action == "approve" for e in entries)


# -- delete_post (and reject-via-delete) -----------------------------------


def test_delete_post_requires_delete_permission(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, bob, "Hello", "Body")
    with pytest.raises(PostError):
        delete_post(db, post, deleted_by=bob)


def test_delete_post_removes_it_from_listing(db, sysop, alice, bob):
    board = create_board(db, "general", creator=alice)
    grant_permissions(db, sysop, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=sysop)
    post = create_post(db, board, bob, "Hello", "Body")

    delete_post(db, post, deleted_by=sysop)
    page = list_posts_page(db, board, bob)
    assert page.posts == []


def test_delete_pending_post_logs_reject_not_delete(db, sysop, alice, bob):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    grant_permissions(db, sysop, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=sysop)
    post = create_post(db, board, bob, "Hello", "Body")
    assert post.status == "pending"

    delete_post(db, post, deleted_by=sysop)
    entries = list_actions_for_target_user(db, bob.id)
    assert entries[-1].action == "reject"


def test_delete_approved_post_logs_delete(db, sysop, alice, bob):
    board = create_board(db, "general", creator=alice)
    grant_permissions(db, sysop, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=sysop)
    post = create_post(db, board, bob, "Hello", "Body")
    assert post.status == "approved"

    delete_post(db, post, deleted_by=sysop)
    entries = list_actions_for_target_user(db, bob.id)
    assert entries[-1].action == "delete"


# -- pin/exempt: require edit permission -----------------------------------


def test_set_post_pinned_requires_edit_permission(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, bob, "Hello", "Body")
    with pytest.raises(PostError):
        set_post_pinned(db, post, True, changed_by=bob)


def test_set_post_pinned_marks_post_pinned(db, sysop, alice, bob):
    board = create_board(db, "general", creator=alice)
    grant_permissions(db, sysop, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=sysop)
    post = create_post(db, board, bob, "Hello", "Body")

    pinned = set_post_pinned(db, post, True, changed_by=sysop)
    assert pinned.pinned is True


def test_set_post_exempt_requires_edit_permission(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, bob, "Hello", "Body")
    with pytest.raises(PostError):
        set_post_exempt(db, post, True, changed_by=bob)


def test_set_post_exempt_marks_post_exempt(db, sysop, alice, bob):
    board = create_board(db, "general", creator=alice)
    grant_permissions(db, sysop, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=sysop)
    post = create_post(db, board, bob, "Hello", "Body")

    exempted = set_post_exempt(db, post, True, changed_by=sysop)
    assert exempted.exempt_from_expiry is True


# -- list_pinned_posts ------------------------------------------------------


def test_list_pinned_posts_returns_only_pinned(db, sysop, alice, bob):
    board = create_board(db, "general", creator=alice)
    grant_permissions(db, sysop, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=sysop)
    pinned_post = create_post(db, board, bob, "Pinned", "Body")
    create_post(db, board, bob, "Not pinned", "Body")
    set_post_pinned(db, pinned_post, True, changed_by=sysop)

    pinned = list_pinned_posts(db, board, requesting_user=bob)
    assert [p.subject for p in pinned] == ["Pinned"]


def test_list_pinned_posts_empty_by_default(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, bob, "Hello", "Body")
    assert list_pinned_posts(db, board, requesting_user=bob) == []


# -- expiry sweep -----------------------------------------------------------


def test_post_within_max_age_stays_approved(db, alice, bob):
    board = create_board(db, "general", max_post_age_days=30, creator=alice)
    post = create_post(db, board, bob, "Hello", "Body")
    _age_post(db, post, days_old=5)

    page = list_posts_page(db, board, bob)
    assert [p.subject for p in page.posts] == ["Hello"]


def test_post_past_max_age_becomes_expired_and_is_delisted(db, alice, bob):
    board = create_board(db, "general", max_post_age_days=30, creator=alice)
    post = create_post(db, board, bob, "Hello", "Body")
    _age_post(db, post, days_old=31)

    page = list_posts_page(db, board, bob)
    assert page.posts == []

    from netbbs.boards.posts import get_post

    still_there = get_post(db, post.post_id)
    assert still_there.status == "expired"


def test_exempt_post_never_expires(db, sysop, alice, bob):
    board = create_board(db, "general", max_post_age_days=30, creator=alice)
    grant_permissions(db, sysop, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=sysop)
    post = create_post(db, board, bob, "Hello", "Body")
    set_post_exempt(db, post, True, changed_by=sysop)
    _age_post(db, post, days_old=365)

    page = list_posts_page(db, board, bob)
    assert [p.subject for p in page.posts] == ["Hello"]


def test_post_with_no_max_age_never_expires(db, alice, bob):
    board = create_board(db, "general", creator=alice)  # max_post_age_days=None
    post = create_post(db, board, bob, "Hello", "Body")
    _age_post(db, post, days_old=10_000)

    page = list_posts_page(db, board, bob)
    assert [p.subject for p in page.posts] == ["Hello"]


def test_expired_post_past_grace_period_is_actually_deleted(db, alice, bob):
    board = create_board(db, "general", max_post_age_days=30, creator=alice)
    set_expiry_grace_period_days(db, 5)
    post = create_post(db, board, bob, "Hello", "Body")
    _age_post(db, post, days_old=40)  # 30 (age) + 5 (grace) + margin

    list_posts_page(db, board, bob)  # triggers the sweep

    from netbbs.boards.posts import PostError, get_post

    with pytest.raises(PostError):
        get_post(db, post.post_id)


def test_expired_post_within_grace_period_is_not_yet_deleted(db, alice, bob):
    board = create_board(db, "general", max_post_age_days=30, creator=alice)
    set_expiry_grace_period_days(db, 30)
    post = create_post(db, board, bob, "Hello", "Body")
    _age_post(db, post, days_old=31)  # expired, but well within the 30-day grace period

    list_posts_page(db, board, bob)

    from netbbs.boards.posts import get_post

    still_there = get_post(db, post.post_id)
    assert still_there.status == "expired"


def test_default_grace_period_is_seven_days(db):
    assert get_expiry_grace_period_days(db) == 7


def test_set_expiry_grace_period_days_rejects_negative(db):
    with pytest.raises(ValueError):
        set_expiry_grace_period_days(db, -1)


# -- reply to an expired post is still allowed -----------------------------


def test_can_reply_to_an_expired_post(db, alice, bob):
    board = create_board(db, "general", max_post_age_days=30, creator=alice)
    parent = create_post(db, board, bob, "Hello", "Body")
    _age_post(db, parent, days_old=31)
    list_posts_page(db, board, bob)  # sweep -> parent becomes 'expired'

    reply = create_post(db, board, bob, "Re: Hello", "A reply", parent_post_id=parent.post_id)
    assert reply.parent_post_id == parent.post_id
