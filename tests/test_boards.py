"""Tests for netbbs.boards — board/post creation, level-gating, content IDs."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import BoardError, create_board, delete_board, get_board_by_name, list_boards, update_board
from netbbs.boards.categories import create_category
from netbbs.boards.posts import PostError, approve_post, create_post, edit_post, get_post, list_posts_page
from netbbs.moderation.log import list_actions_for_object
from netbbs.moderation.roles import BoardPermission, grant_permissions
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


# -- GitHub issue #36: hidden lifecycle rows/edit revisions must not
# -- leak into activity/volume sorting --------------------------------


def test_list_boards_volume_does_not_count_pending_posts(db, alice):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    create_post(db, board, alice, "Pending", "body")  # never approved
    create_post(db, board, alice, "Also pending", "body")

    boards = list_boards(db, order_by="volume")
    reviewed = next(b for b in boards if b.name == "reviewed")
    row = db.connection.execute(
        "SELECT COUNT(*) AS n FROM posts WHERE board_id = ? AND status = 'pending'", (reviewed.id,)
    ).fetchone()
    assert row["n"] == 2  # the posts really exist, just shouldn't be counted
    volume_row = db.connection.execute(
        """
        SELECT COUNT(p.id) AS n FROM boards b
        LEFT JOIN posts p ON p.board_id = b.id AND p.post_id = p.root_post_id
            AND EXISTS (SELECT 1 FROM posts v WHERE v.root_post_id = p.root_post_id AND v.board_id = p.board_id AND v.status = 'approved')
        WHERE b.id = ?
        """,
        (reviewed.id,),
    ).fetchone()
    assert volume_row["n"] == 0


def test_list_boards_activity_does_not_surface_pending_posts(db, alice):
    board = create_board(db, "reviewed", moderated=True, creator=alice)
    other = create_board(db, "other", creator=alice)
    db.connection.execute("UPDATE boards SET created_at = ? WHERE name = ?", ("2026-01-01T00:00:00.000000Z", "reviewed"))
    db.connection.execute("UPDATE boards SET created_at = ? WHERE name = ?", ("2026-01-02T00:00:00.000000Z", "other"))
    db.connection.commit()
    post = create_post(db, board, alice, "Pending", "body")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", ("2026-01-05T00:00:00.000000Z", post.id))
    db.connection.commit()

    boards = list_boards(db)
    # The pending post's fresh timestamp must not bump "reviewed" ahead
    # of "other", which has real (if older) approved activity via its
    # own creation time -- a pending submission is invisible to
    # ordinary readers and must not leak that it exists via ranking.
    assert [b.name for b in boards] == ["other", "reviewed"]


def test_list_boards_volume_counts_edit_revisions_once(db, alice):
    board = create_board(db, "general", creator=alice)
    grant_permissions(db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=alice)
    post = create_post(db, board, alice, "Subject", "v1")
    v2 = edit_post(db, post, board, subject="Subject", body="v2", edited_by=alice)
    edit_post(db, get_post(db, v2.post_id), board, subject="Subject", body="v3", edited_by=alice)

    boards = list_boards(db, order_by="volume")
    assert boards[0].name == "general"
    # Confirmed via the actual count, not just first place, since a
    # single board can't be mis-ranked against nothing else.
    row = db.connection.execute(
        """
        SELECT COUNT(p.id) AS n FROM boards b
        LEFT JOIN posts p ON p.board_id = b.id AND p.post_id = p.root_post_id
            AND EXISTS (SELECT 1 FROM posts v WHERE v.root_post_id = p.root_post_id AND v.board_id = p.board_id AND v.status = 'approved')
        WHERE b.name = 'general'
        """
    ).fetchone()
    assert row["n"] == 1


def test_list_boards_activity_reflects_a_fresh_edit_of_an_old_post(db, alice):
    board = create_board(db, "general", creator=alice)
    other = create_board(db, "other", creator=alice)
    grant_permissions(db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=alice)
    old_post = create_post(db, board, alice, "Subject", "v1")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", ("2020-01-01T00:00:00.000000Z", old_post.id))
    db.connection.execute("UPDATE boards SET created_at = ? WHERE name = ?", ("2020-01-01T00:00:00.000000Z", "general"))
    other_post = create_post(db, other, alice, "Other", "body")  # newer than the (backdated) edit target
    db.connection.commit()

    edit_post(db, get_post(db, old_post.post_id), board, subject="Subject", body="revised now", edited_by=alice)

    boards = list_boards(db)
    # The fresh edit's own created_at must win -- editing counts as
    # board-list activity even though it never moves the post's own
    # position within its own board's feed (a different, narrower
    # guarantee -- see netbbs.boards.posts._resolve_current_version).
    assert boards[0].name == "general"


# -- GitHub issue #36 (reopened): effectively-expired-but-not-yet-swept
# -- rows must not leak into activity/volume either -----------------------


def test_list_boards_activity_excludes_effectively_expired_posts_before_any_sweep(db, alice):
    """A post already past its own board's max_post_age_days, but still
    physically stored as 'approved' -- expiry sweeping is lazy (see
    _sweep_expired_posts's own docstring), and this test deliberately
    never browses the board via list_posts_page, so no sweep has run --
    must not count as board activity, the same as an already-swept
    'expired' row wouldn't."""
    stale = create_board(db, "stale", max_post_age_days=30, creator=alice)
    fresh = create_board(db, "fresh", creator=alice)
    db.connection.execute("UPDATE boards SET created_at = ? WHERE name = ?", ("2020-01-01T00:00:00.000000Z", "stale"))
    db.connection.execute("UPDATE boards SET created_at = ? WHERE name = ?", ("2020-01-02T00:00:00.000000Z", "fresh"))
    db.connection.commit()

    old_post = create_post(db, stale, alice, "Old", "body")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", ("2020-06-01T00:00:00.000000Z", old_post.id))
    db.connection.commit()
    assert (
        db.connection.execute("SELECT status FROM posts WHERE id = ?", (old_post.id,)).fetchone()["status"]
        == "approved"
    )  # confirms the sweep really never ran

    boards = list_boards(db)
    # "stale"'s post's raw created_at (2020-06) is later than "fresh"'s
    # own creation time (2020-01-02) -- it would win on activity if
    # still (wrongly) being counted despite being 30+ days past its own
    # board's retention window.
    assert [b.name for b in boards] == ["fresh", "stale"]


def test_list_boards_activity_still_counts_an_exempt_post_past_its_age_limit(db, alice):
    stale = create_board(db, "stale", max_post_age_days=30, creator=alice)
    fresh = create_board(db, "fresh", creator=alice)
    db.connection.execute("UPDATE boards SET created_at = ? WHERE name = ?", ("2020-01-01T00:00:00.000000Z", "stale"))
    db.connection.execute("UPDATE boards SET created_at = ? WHERE name = ?", ("2020-01-02T00:00:00.000000Z", "fresh"))
    db.connection.commit()

    old_post = create_post(db, stale, alice, "Old", "body")
    db.connection.execute(
        "UPDATE posts SET created_at = ?, exempt_from_expiry = 1 WHERE id = ?",
        ("2020-06-01T00:00:00.000000Z", old_post.id),
    )
    db.connection.commit()

    boards = list_boards(db)
    assert [b.name for b in boards] == ["stale", "fresh"]  # exempt -- its late created_at wins now


def test_list_boards_volume_excludes_effectively_expired_posts_before_any_sweep(db, alice):
    stale = create_board(db, "stale", max_post_age_days=30, creator=alice)
    fresh = create_board(db, "fresh", creator=alice)
    create_post(db, fresh, alice, "Fresh", "body")  # one genuinely-counted post
    old_post = create_post(db, stale, alice, "Old", "body")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", ("2020-01-01T00:00:00.000000Z", old_post.id))
    db.connection.commit()
    assert (
        db.connection.execute("SELECT status FROM posts WHERE id = ?", (old_post.id,)).fetchone()["status"]
        == "approved"
    )

    boards = list_boards(db, order_by="volume")
    # "fresh" has one genuinely-counted post; "stale"'s only post is
    # effectively expired and must count as zero, not one.
    assert [b.name for b in boards] == ["fresh", "stale"]


def test_list_boards_volume_still_counts_an_exempt_post_past_its_age_limit(db, alice):
    stale = create_board(db, "stale", max_post_age_days=30, creator=alice)
    empty = create_board(db, "empty", creator=alice)
    old_post = create_post(db, stale, alice, "Old", "body")
    db.connection.execute(
        "UPDATE posts SET created_at = ?, exempt_from_expiry = 1 WHERE id = ?",
        ("2020-01-01T00:00:00.000000Z", old_post.id),
    )
    db.connection.commit()

    boards = list_boards(db, order_by="volume")
    assert [b.name for b in boards] == ["stale", "empty"]


def test_list_boards_volume_excludes_a_post_whose_every_revision_is_effectively_expired(db, alice):
    """The 'root row' case the reopened issue specifically called out:
    counting must key off whether *any* revision in a post's edit
    chain is still effectively live, not just whether some row in the
    chain has status='approved' -- an old post every one of whose
    revisions (root and every edit) is past the board's retention
    window must count as zero, not one. Board names are chosen so a
    still-buggy "any approved row counts, regardless of how stale"
    implementation and this fixed one produce different orderings --
    zzz-general would out-count aaa-empty (1 > 0) and sort first
    despite the name; only the effective-expiry fix makes both tie at
    zero and fall back to the alphabetical tiebreak."""
    board = create_board(db, "zzz-general", max_post_age_days=30, creator=alice)
    empty = create_board(db, "aaa-empty", creator=alice)
    grant_permissions(
        db, alice, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=alice
    )
    post = create_post(db, board, alice, "Subject", "v1")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", ("2020-01-01T00:00:00.000000Z", post.id))
    db.connection.commit()
    edited = edit_post(db, get_post(db, post.post_id), board, subject="Subject", body="v2", edited_by=alice)
    db.connection.execute(
        "UPDATE posts SET created_at = ? WHERE id = ?", ("2020-02-01T00:00:00.000000Z", edited.id)
    )
    db.connection.commit()

    boards = list_boards(db, order_by="volume")
    assert [b.name for b in boards] == ["aaa-empty", "zzz-general"]


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


def test_list_posts_page_returns_all_in_order(db, alice, monkeypatch):
    # Explicit, distinct timestamps -- real wall-clock calls in quick
    # succession can land on the same microsecond (this is exactly what
    # happened when this test was first written against real timing;
    # post_id, the deterministic tie-breaker list_posts_page now uses
    # for same-timestamp posts, doesn't preserve creation order, since
    # it's a content hash -- see design doc round 30).
    timestamps = iter(["2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.000001Z"])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))

    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "First", "1")
    create_post(db, board, alice, "Second", "2")
    page = list_posts_page(db, board, alice)
    assert [p.subject for p in page.posts] == ["First", "Second"]


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
        list_posts_page(db, board, bob)


def test_read_allowed_at_sufficient_level(db, alice):
    board = create_board(db, "general", min_read_level=5, creator=alice)
    create_post(db, board, alice, "Hello", "Body")
    page = list_posts_page(db, board, alice)
    assert len(page.posts) == 1


# -- update/delete (design doc -- board/area management round) -------------


def test_create_board_records_an_audit_entry(db, alice):
    board = create_board(db, "general", creator=alice)
    entries = list_actions_for_object(db, "board", board.id)
    assert any(e.action == "create_board" for e in entries)


def test_update_board_replaces_the_full_state(db, alice):
    board = create_board(db, "general", creator=alice)
    updated = update_board(
        db, board, name="general2", description="new desc", min_read_level=1, min_write_level=2,
        category_id=None, pinned=True, moderated=True, max_post_age_days=30,
        min_age=18, name_requirement="verified", community_id=None, changed_by=alice,
    )
    assert updated.name == "general2"
    assert updated.description == "new desc"
    assert updated.min_read_level == 1
    assert updated.min_write_level == 2
    assert updated.pinned is True
    assert updated.moderated is True
    assert updated.max_post_age_days == 30
    assert updated.min_age == 18
    assert updated.name_requirement == "verified"
    entries = list_actions_for_object(db, "board", board.id)
    assert any(e.action == "update_board" for e in entries)


def test_update_board_rejects_a_name_collision(db, alice):
    create_board(db, "taken", creator=alice)
    board = create_board(db, "general", creator=alice)
    with pytest.raises(BoardError):
        update_board(
            db, board, name="taken", description=None, min_read_level=0, min_write_level=0,
            category_id=None, pinned=False, moderated=False, max_post_age_days=None,
            min_age=None, name_requirement=None, community_id=None, changed_by=alice,
        )


def test_update_board_rejects_invalid_name_requirement(db, alice):
    board = create_board(db, "general", creator=alice)
    with pytest.raises(BoardError, match="name_requirement"):
        update_board(
            db, board, name="general", description=None, min_read_level=0, min_write_level=0,
            category_id=None, pinned=False, moderated=False, max_post_age_days=None,
            min_age=None, name_requirement="bogus", community_id=None, changed_by=alice,
        )


def test_create_board_rejects_invalid_name_requirement(db, alice):
    with pytest.raises(BoardError, match="name_requirement"):
        create_board(db, "general", creator=alice, name_requirement="bogus")


def test_create_board_defaults_no_age_or_name_gate(db, alice):
    board = create_board(db, "general", creator=alice)
    assert board.min_age is None
    assert board.name_requirement is None


def test_delete_board_removes_posts_and_moderator_grants(db, alice, bob):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "Hello", "Body")
    grant_permissions(
        db, bob, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE,
        granted_by=alice,
    )

    delete_board(db, board, deleted_by=alice)

    with pytest.raises(BoardError):
        get_board_by_name(db, "general")
    assert db.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM moderator_grants").fetchone()[0] == 0


def test_delete_board_does_not_touch_its_category(db, alice):
    category = create_category(db, "Vintage", created_by=alice)
    board = create_board(db, "general", category_id=category.id, creator=alice)
    delete_board(db, board, deleted_by=alice)
    from netbbs.boards.categories import get_category_by_id

    still_there = get_category_by_id(db, category.id)
    assert still_there.name == "Vintage"


def test_delete_board_records_an_audit_entry_before_deleting(db, alice):
    board = create_board(db, "general", creator=alice)
    board_id = board.id
    delete_board(db, board, deleted_by=alice)
    entries = list_actions_for_object(db, "board", board_id)
    assert any(e.action == "delete_board" for e in entries)
