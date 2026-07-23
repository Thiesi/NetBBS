"""
Tests for post editing: `netbbs.boards.posts.edit_post`, and the
`root_post_id`/
`edit_of_post_id`-based resolution `list_posts_page`/`list_pinned_posts`
now do so a post's feed position and pagination cursors stay stable
across edits while still showing the latest approved content.
"""

from __future__ import annotations

import datetime

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import (
    MAX_BODY_BYTES,
    MAX_SUBJECT_BYTES,
    PostError,
    approve_post,
    create_post,
    edit_post,
    get_post,
    list_pinned_posts,
    list_posts_page,
    set_post_pinned,
)
from netbbs.moderation import BoardPermission, grant_permissions
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


def _age(db, post, days_old: int) -> None:
    backdated = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_old)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", (backdated, post.id))
    db.connection.commit()


# -- basic edit behavior -----------------------------------------------


def test_new_post_is_the_root_of_its_own_chain(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Subject", "Body")
    assert post.root_post_id == post.post_id
    assert post.edit_of_post_id is None
    assert post.is_edited is False


def test_edit_creates_a_new_row_and_does_not_mutate_the_original(db, alice):
    board = create_board(db, "general", creator=alice)
    original = create_post(db, board, alice, "Subject", "Original body")

    edited = edit_post(db, original, board, subject="Subject", body="Revised body", edited_by=alice)

    assert edited.post_id != original.post_id
    assert edited.body == "Revised body"
    assert edited.root_post_id == original.post_id
    assert edited.edit_of_post_id == original.post_id

    # The original row is untouched, still reachable by its exact ID --
    # a reply referencing it directly would still resolve correctly.
    still_original = get_post(db, original.post_id)
    assert still_original.body == "Original body"


def test_feed_shows_latest_content_when_an_edit_collides_with_the_original_timestamp(db, alice, monkeypatch):
    """GitHub issue #68: `_resolve_current_version`'s tie-break among
    revisions sharing a root must use a row's own monotonic `id`, never
    the content-addressed `post_id` -- a hash has no relationship to
    creation order, so when an edit lands in the same `created_at`
    instant as the revision it supersedes (confirmed to happen often
    enough in practice, e.g. fast automated Link event replay, to
    matter), a `post_id`-based tie-break picks whichever hash sorts
    lexicographically larger, which is only actually "the latest edit"
    about half the time. Repeated over several independent post chains,
    each with its own forced same-instant edit, so this doesn't itself
    depend on getting lucky with hash ordering to catch a regression."""
    from netbbs.boards import posts as posts_module

    board = create_board(db, "general", creator=alice)
    for i in range(10):
        frozen_time = f"2026-01-01T00:00:00.{i:06d}Z"
        monkeypatch.setattr(posts_module, "utc_now_iso", lambda t=frozen_time: t)

        original = create_post(db, board, alice, f"Subject {i}", "Original body")
        edited = edit_post(db, original, board, subject=f"Subject {i}", body="Revised body", edited_by=alice)
        assert original.created_at == edited.created_at  # the forced collision

        page = list_posts_page(db, board, alice, limit=100)
        current = next(p for p in page.posts if p.post_id == original.post_id)
        assert current.body == "Revised body"
        assert current.is_edited is True


def test_feed_shows_latest_content_at_the_original_position(db, alice):
    board = create_board(db, "general", creator=alice)
    original = create_post(db, board, alice, "Subject", "Original body")
    _age(db, original, days_old=1)  # guarantee the edit sorts strictly newer
    original = get_post(db, original.post_id)  # re-fetch: created_at just changed
    edit_post(db, original, board, subject="Subject", body="Revised body", edited_by=alice)

    page = list_posts_page(db, board, alice)
    assert len(page.posts) == 1
    shown = page.posts[0]
    assert shown.body == "Revised body"
    assert shown.is_edited is True
    # Position/identity fields stay the root's, not the edit's -- this
    # is what keeps pagination cursors stable across an edit.
    assert shown.post_id == original.post_id
    assert shown.created_at == original.created_at


def test_editing_does_not_bump_a_post_to_the_top_of_the_feed(db, alice):
    board = create_board(db, "general", creator=alice)
    first = create_post(db, board, alice, "First", "Body 1")
    _age(db, first, days_old=1)  # guarantee ordering, not real-time timing
    second = create_post(db, board, alice, "Second", "Body 2")

    # Edit the OLDER post -- it must not jump ahead of `second`.
    edit_post(db, first, board, subject="First", body="Revised body 1", edited_by=alice)

    page = list_posts_page(db, board, alice)
    assert [p.subject for p in page.posts] == ["First", "Second"]
    assert page.posts[0].body == "Revised body 1"


def test_editing_with_no_actual_change_is_a_no_op(db, alice):
    """Regression test for GitHub issue #41: every edit_post() call
    computes a fresh created_at, which alone would produce a new
    content-addressed post_id (and mark the post "(edited)" via
    _resolve_current_version) even when the submitted subject/body are
    byte-identical to the current version."""
    board = create_board(db, "general", creator=alice)
    original = create_post(db, board, alice, "Subject", "Body")

    result = edit_post(db, original, board, subject="Subject", body="Body", edited_by=alice)

    assert result.post_id == original.post_id
    assert result.is_edited is False
    shown = list_posts_page(db, board, alice).posts[0]
    assert shown.is_edited is False
    assert shown.body == "Body"


def test_repeated_edits_all_chain_to_the_same_root(db, alice):
    board = create_board(db, "general", creator=alice)
    original = create_post(db, board, alice, "Subject", "v1")
    _age(db, original, days_old=2)  # guarantee strict created_at ordering below,
    v2 = edit_post(db, original, board, subject="Subject", body="v2", edited_by=alice)
    _age(db, v2, days_old=1)  # not real-time timing between fast successive calls
    # `v2` here is itself root-identified (post_id == original.post_id)
    # per _resolve_current_version's contract -- re-fetch the page to
    # get a Post shaped exactly like a caller would actually have one.
    shown = list_posts_page(db, board, alice).posts[0]
    v3 = edit_post(db, shown, board, subject="Subject", body="v3", edited_by=alice)

    assert v3.root_post_id == original.post_id
    assert v3.edit_of_post_id == v2.post_id  # chains to the immediate predecessor, not the root
    assert list_posts_page(db, board, alice).posts[0].body == "v3"


# -- content length limits (GitHub issue #32) -----------------------------


def test_create_post_rejects_an_oversized_subject(db, alice):
    board = create_board(db, "general", creator=alice)
    with pytest.raises(PostError):
        create_post(db, board, alice, "x" * (MAX_SUBJECT_BYTES + 1), "Body")


def test_create_post_rejects_an_oversized_body(db, alice):
    board = create_board(db, "general", creator=alice)
    with pytest.raises(PostError):
        create_post(db, board, alice, "Subject", "x" * (MAX_BODY_BYTES + 1))


def test_create_post_allows_exactly_the_maximum_body_size(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Subject", "x" * MAX_BODY_BYTES)
    assert len(post.body.encode("utf-8")) == MAX_BODY_BYTES


def test_edit_post_rejects_an_oversized_body(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Subject", "Body")
    with pytest.raises(PostError):
        edit_post(db, post, board, subject="Subject", body="x" * (MAX_BODY_BYTES + 1), edited_by=alice)


# -- who can edit --------------------------------------------------------


def test_author_can_edit_their_own_post_with_no_grant(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Subject", "Body")
    edited = edit_post(db, post, board, subject="Subject", body="New body", edited_by=alice)
    assert edited.body == "New body"


def test_non_author_without_edit_grant_cannot_edit(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Subject", "Body")
    with pytest.raises(PostError):
        edit_post(db, post, board, subject="Subject", body="Hijacked", edited_by=bob)


def test_moderator_with_edit_grant_can_edit_anothers_post(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Subject", "Body")
    grant_permissions(
        db, bob, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=alice
    )

    edited = edit_post(db, post, board, subject="Subject", body="Moderator fix", edited_by=bob)
    assert edited.body == "Moderator fix"
    # Authorship stays with the original author, not the moderator who edited it.
    assert edited.author_user_id == alice.id


# -- moderated boards: an edit re-enters moderation ----------------------


def test_edit_on_a_moderated_board_starts_pending_and_old_content_stays_shown(db, alice):
    board = create_board(db, "general", creator=alice, moderated=True)
    grant_permissions(
        db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=alice
    )
    original = create_post(db, board, alice, "Subject", "Original body")
    approve_post(db, original, approved_by=alice)  # simulate the original already being live
    _age(db, original, days_old=1)  # guarantee the edit sorts strictly newer once approved
    original = get_post(db, original.post_id)

    edited = edit_post(db, original, board, subject="Subject", body="Pending revision", edited_by=alice)
    assert edited.status == "pending"

    # The board feed still shows the last-*approved* content, not the
    # unapproved edit -- an edit must not bypass moderation.
    shown = list_posts_page(db, board, alice).posts[0]
    assert shown.body == "Original body"
    assert shown.is_edited is False

    approve_post(db, edited, approved_by=alice)
    shown_after_approval = list_posts_page(db, board, alice).posts[0]
    assert shown_after_approval.body == "Pending revision"
    assert shown_after_approval.is_edited is True


def test_cannot_edit_a_post_with_no_currently_approved_version(db, alice):
    board = create_board(db, "general", creator=alice, moderated=True)
    post = create_post(db, board, alice, "Subject", "Body")  # starts 'pending', never approved
    with pytest.raises(PostError):
        edit_post(db, post, board, subject="Subject", body="New", edited_by=alice)


# -- expiry interaction ---------------------------------------------------


def test_an_edit_keeps_a_post_alive_even_after_the_original_root_expires(db, alice):
    board = create_board(db, "general", creator=alice, max_post_age_days=30)
    original = create_post(db, board, alice, "Subject", "Old body")
    edited = edit_post(db, original, board, subject="Subject", body="Fresh body", edited_by=alice)
    # Old enough to sweep to 'expired' (past max_post_age_days=30) but
    # not old enough to be hard-deleted (default grace period is 7
    # more days) -- the "does an edit still surviving past hard-delete"
    # case is covered separately, more precisely, by
    # tests/test_post_lifecycle.py::
    # test_expired_post_still_referenced_by_an_edit_is_not_deleted.
    _age(db, original, days_old=35)

    page = list_posts_page(db, board, alice)
    assert len(page.posts) == 1
    assert page.posts[0].body == "Fresh body"


# -- pinned posts also resolve to latest content --------------------------


def test_pinned_post_shows_latest_content_after_being_edited(db, alice):
    board = create_board(db, "general", creator=alice)
    grant_permissions(
        db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=alice
    )
    post = create_post(db, board, alice, "Subject", "Body")
    set_post_pinned(db, post, True, changed_by=alice)
    _age(db, post, days_old=1)  # guarantee the edit sorts strictly newer
    post = get_post(db, post.post_id)
    edit_post(db, post, board, subject="Subject", body="Revised", edited_by=alice)

    pinned = list_pinned_posts(db, board, requesting_user=alice)
    assert len(pinned) == 1
    assert pinned[0].body == "Revised"
