"""
Tests for netbbs.files.entries.list_files_page and get_file_by_name
(design doc round 31, issue #10's file-area follow-up) -- mirrors
tests/test_post_pagination.py's structure and coverage exactly, since
list_files_page deliberately mirrors list_posts_page's design.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.files import entries as entries_module
from netbbs.files.areas import create_file_area
from netbbs.files.entries import get_file_by_name, list_files_page, upload_file
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _upload_with_distinct_timestamps(db, area, uploader, monkeypatch, count: int):
    timestamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(count))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    return [
        upload_file(db, area, uploader, f"file{i}.txt", f"content {i}".encode())
        for i in range(count)
    ]


# -- empty / single-page areas --------------------------------------------


def test_empty_area_returns_empty_page(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    page = list_files_page(db, area, alice)
    assert page.entries == []
    assert page.has_older is False
    assert page.has_newer is False


def test_single_page_area_has_no_older_or_newer(db, alice, monkeypatch):
    area = create_file_area(db, "docs", creator=alice)
    _upload_with_distinct_timestamps(db, area, alice, monkeypatch, count=3)

    page = list_files_page(db, area, alice, limit=5)

    assert [f.filename for f in page.entries] == ["file0.txt", "file1.txt", "file2.txt"]
    assert page.has_older is False
    assert page.has_newer is False


# -- page boundaries / newest-page default -------------------------------


def test_newest_page_shows_most_recent_files_in_chronological_order(db, alice, monkeypatch):
    area = create_file_area(db, "docs", creator=alice)
    _upload_with_distinct_timestamps(db, area, alice, monkeypatch, count=7)

    page = list_files_page(db, area, alice, limit=3)

    assert [f.filename for f in page.entries] == ["file4.txt", "file5.txt", "file6.txt"]
    assert page.has_older is True
    assert page.has_newer is False


def test_paging_older_returns_the_correct_previous_page(db, alice, monkeypatch):
    area = create_file_area(db, "docs", creator=alice)
    _upload_with_distinct_timestamps(db, area, alice, monkeypatch, count=7)

    newest_page = list_files_page(db, area, alice, limit=3)
    oldest_of_newest = newest_page.entries[0]
    older_page = list_files_page(
        db, area, alice, limit=3, before=(oldest_of_newest.created_at, oldest_of_newest.file_id)
    )

    assert [f.filename for f in older_page.entries] == ["file1.txt", "file2.txt", "file3.txt"]
    assert older_page.has_older is True
    assert older_page.has_newer is True


def test_full_backward_traversal_visits_every_file_exactly_once(db, alice, monkeypatch):
    area = create_file_area(db, "docs", creator=alice)
    created = _upload_with_distinct_timestamps(db, area, alice, monkeypatch, count=11)

    seen = []
    page = list_files_page(db, area, alice, limit=4)
    seen = list(page.entries) + seen
    while page.has_older:
        oldest = page.entries[0]
        page = list_files_page(db, area, alice, limit=4, before=(oldest.created_at, oldest.file_id))
        seen = list(page.entries) + seen

    assert [f.file_id for f in seen] == [f.file_id for f in created]


# -- stable ordering under a genuine timestamp tie ----------------------


def test_ordering_is_stable_when_timestamps_tie(db, alice, monkeypatch):
    area = create_file_area(db, "docs", creator=alice)
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: "2026-01-01T00:00:00.000000Z")
    upload_file(db, area, alice, "a.txt", b"1")
    upload_file(db, area, alice, "b.txt", b"2")

    first_query = list_files_page(db, area, alice)
    second_query = list_files_page(db, area, alice)

    assert [f.file_id for f in first_query.entries] == [f.file_id for f in second_query.entries]
    ids = [f.file_id for f in first_query.entries]
    assert ids == sorted(ids)


# -- input validation ---------------------------------------------------


def test_before_and_after_together_is_rejected(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    with pytest.raises(ValueError):
        list_files_page(db, area, alice, before=("x", "y"), after=("x", "y"))


# -- composite index exists -------------------------------------------------


def test_composite_file_pagination_index_exists(db):
    rows = db.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'files'"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert "idx_files_area_id_created_at_file_id" in names


# -- get_file_by_name -----------------------------------------------------


def test_get_file_by_name_finds_a_file_not_on_the_newest_page(db, alice, monkeypatch):
    """The whole reason get_file_by_name exists (design doc round 31):
    /download must still work for a file that isn't on the currently
    displayed (newest) page."""
    area = create_file_area(db, "docs", creator=alice)
    _upload_with_distinct_timestamps(db, area, alice, monkeypatch, count=10)

    # The newest page (default limit) won't include file0.txt once
    # there are more than a page's worth of uploads.
    newest_page = list_files_page(db, area, alice)
    assert "file0.txt" not in [f.filename for f in newest_page.entries]

    found = get_file_by_name(db, area, "file0.txt")
    assert found is not None
    assert found.filename == "file0.txt"


def test_get_file_by_name_returns_none_when_not_found(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    assert get_file_by_name(db, area, "nonexistent.txt") is None


def test_get_file_by_name_returns_oldest_match_for_duplicate_names(db, alice, monkeypatch):
    area = create_file_area(db, "docs", creator=alice)
    timestamps = iter(["2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:01.000000Z"])
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    first = upload_file(db, area, alice, "same.txt", b"version 1")
    upload_file(db, area, alice, "same.txt", b"version 2")

    found = get_file_by_name(db, area, "same.txt")
    assert found.file_id == first.file_id
