"""
Tests for netbbs.boards.posts.list_posts_page (issue #10) -- page
boundaries, stable ordering, and empty/single-page
boards, per the issue's acceptance criteria. Level-gating enforcement
itself is already covered in tests/test_boards.py; not duplicated here.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, list_posts_page
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _create_posts_with_distinct_timestamps(db, board, author, monkeypatch, count: int):
    """Real wall-clock create_post calls in quick succession can share a
    microsecond timestamp (this is what a first version of these tests
    actually hit) -- explicit, strictly increasing timestamps make
    ordering assertions deterministic rather than occasionally-flaky."""
    timestamps = iter(
        f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(count)
    )
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    return [create_post(db, board, author, f"Subject {i}", f"Body {i}") for i in range(count)]


# -- empty / single-page boards ------------------------------------------


def test_empty_board_returns_empty_page(db, alice):
    board = create_board(db, "general", creator=alice)
    page = list_posts_page(db, board, alice)
    assert page.posts == []
    assert page.has_older is False
    assert page.has_newer is False


def test_single_page_board_has_no_older_or_newer(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _create_posts_with_distinct_timestamps(db, board, alice, monkeypatch, count=3)

    page = list_posts_page(db, board, alice, limit=5)

    assert [p.subject for p in page.posts] == ["Subject 0", "Subject 1", "Subject 2"]
    assert page.has_older is False
    assert page.has_newer is False


# -- page boundaries / newest-page default -------------------------------


def test_newest_page_shows_most_recent_posts_in_chronological_order(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _create_posts_with_distinct_timestamps(db, board, alice, monkeypatch, count=7)

    page = list_posts_page(db, board, alice, limit=3)

    # The 3 *newest* posts (subjects 4, 5, 6), but chronological
    # (oldest-first) *within* the page -- confirmed with Thiesi that
    # page selection works backward from now, while reading order
    # within a page stays natural top-to-bottom.
    assert [p.subject for p in page.posts] == ["Subject 4", "Subject 5", "Subject 6"]
    assert page.has_older is True
    assert page.has_newer is False


def test_paging_older_returns_the_correct_previous_page(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _create_posts_with_distinct_timestamps(db, board, alice, monkeypatch, count=7)

    newest_page = list_posts_page(db, board, alice, limit=3)
    oldest_of_newest = newest_page.posts[0]
    older_page = list_posts_page(
        db, board, alice, limit=3, before=(oldest_of_newest.created_at, oldest_of_newest.post_id)
    )

    assert [p.subject for p in older_page.posts] == ["Subject 1", "Subject 2", "Subject 3"]
    assert older_page.has_older is True  # Subject 0 remains
    assert older_page.has_newer is True  # Subjects 4-6 remain


def test_paging_to_the_oldest_page_stops_correctly(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _create_posts_with_distinct_timestamps(db, board, alice, monkeypatch, count=7)

    page = list_posts_page(db, board, alice, limit=3)  # newest: 4,5,6
    page = list_posts_page(
        db, board, alice, limit=3, before=(page.posts[0].created_at, page.posts[0].post_id)
    )  # 1,2,3
    page = list_posts_page(
        db, board, alice, limit=3, before=(page.posts[0].created_at, page.posts[0].post_id)
    )  # 0

    assert [p.subject for p in page.posts] == ["Subject 0"]
    assert page.has_older is False
    assert page.has_newer is True


def test_paging_newer_from_an_older_page_returns_toward_now(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    _create_posts_with_distinct_timestamps(db, board, alice, monkeypatch, count=7)

    page = list_posts_page(db, board, alice, limit=3)  # 4,5,6
    page = list_posts_page(
        db, board, alice, limit=3, before=(page.posts[0].created_at, page.posts[0].post_id)
    )  # 1,2,3

    newer_page = list_posts_page(
        db, board, alice, limit=3, after=(page.posts[-1].created_at, page.posts[-1].post_id)
    )
    assert [p.subject for p in newer_page.posts] == ["Subject 4", "Subject 5", "Subject 6"]
    assert newer_page.has_newer is False
    assert newer_page.has_older is True


def test_full_backward_traversal_visits_every_post_exactly_once(db, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    created = _create_posts_with_distinct_timestamps(db, board, alice, monkeypatch, count=11)

    seen: list[str] = []
    page = list_posts_page(db, board, alice, limit=4)
    seen = list(page.posts) + seen
    while page.has_older:
        oldest = page.posts[0]
        page = list_posts_page(db, board, alice, limit=4, before=(oldest.created_at, oldest.post_id))
        seen = list(page.posts) + seen

    assert [p.post_id for p in seen] == [p.post_id for p in created]


# -- stable ordering under a genuine timestamp tie ----------------------


def test_ordering_is_stable_when_timestamps_tie(db, alice, monkeypatch):
    """Two posts created with the *identical* created_at (a real,
    if rare, possibility at real deployments -- and the reason the
    issue asked for post_id as a tie-breaker at all) must still sort
    deterministically and repeatably, not by incidental row storage
    order."""
    board = create_board(db, "general", creator=alice)
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: "2026-01-01T00:00:00.000000Z")
    create_post(db, board, alice, "A", "1")
    create_post(db, board, alice, "B", "2")

    first_query = list_posts_page(db, board, alice)
    second_query = list_posts_page(db, board, alice)

    assert [p.post_id for p in first_query.posts] == [p.post_id for p in second_query.posts]
    # Deterministic tie-break is by post_id, ascending, within the page.
    ids = [p.post_id for p in first_query.posts]
    assert ids == sorted(ids)


# -- input validation ---------------------------------------------------


def test_before_and_after_together_is_rejected(db, alice):
    board = create_board(db, "general", creator=alice)
    with pytest.raises(ValueError):
        list_posts_page(db, board, alice, before=("x", "y"), after=("x", "y"))


# -- composite index actually exists --------------------------------------


def test_composite_post_pagination_index_exists(db):
    rows = db.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'posts'"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert "idx_posts_board_id_created_at_post_id" in names
