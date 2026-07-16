"""Tests for netbbs.files — file area/file creation, level-gating, sort
orders, content IDs. Mirrors tests/test_boards.py's structure and its
explicit-timestamp approach to sort-order tests (see that file's history:
relying on two back-to-back calls landing on distinct wall-clock values
is what caused a stale test to pass on Windows by accident and fail on
NetBSD's finer clock resolution)."""

from __future__ import annotations

import hashlib

import pytest

from netbbs.auth.users import create_user
from netbbs.files import (
    FileAreaError,
    FileEntryError,
    create_file_area,
    delete_file_area,
    download_file,
    get_file,
    get_file_area_by_name,
    list_file_areas,
    list_files_page,
    update_file_area,
    upload_file,
    upload_file_from_temp,
)
from netbbs.files.categories import create_category
from netbbs.files.storage import new_incoming_temp_path, storage_root
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


# -- file area creation -------------------------------------------------


def test_create_file_area(db, alice):
    area = create_file_area(db, "docs", description="Documents", creator=alice)
    assert area.name == "docs"
    assert area.description == "Documents"
    assert area.min_read_level == 0
    assert area.min_write_level == 0


def test_create_file_area_generates_content_addressed_id(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    assert len(area.area_id) == 64
    int(area.area_id, 16)


def test_create_duplicate_file_area_name_fails(db, alice):
    create_file_area(db, "docs", creator=alice)
    with pytest.raises(FileAreaError):
        create_file_area(db, "docs", creator=alice)


def test_two_file_areas_have_different_content_ids_even_with_same_creator(db, alice):
    a = create_file_area(db, "area-a", creator=alice)
    b = create_file_area(db, "area-b", creator=alice)
    assert a.area_id != b.area_id


# -- list_file_areas sort orders -----------------------------------------


def test_list_file_areas_default_order_is_by_last_activity_most_recent_first(db, alice):
    create_file_area(db, "first", creator=alice)
    create_file_area(db, "second", creator=alice)
    db.connection.execute(
        "UPDATE file_areas SET created_at = ? WHERE name = ?",
        ("2026-01-01T00:00:00.000000Z", "first"),
    )
    db.connection.execute(
        "UPDATE file_areas SET created_at = ? WHERE name = ?",
        ("2026-01-02T00:00:00.000000Z", "second"),
    )
    db.connection.commit()

    areas = list_file_areas(db)
    assert [a.name for a in areas] == ["second", "first"]


def test_list_file_areas_activity_order_uses_latest_upload_not_creation_time(db, alice):
    first = create_file_area(db, "first", creator=alice)
    create_file_area(db, "second", creator=alice)
    db.connection.execute(
        "UPDATE file_areas SET created_at = ? WHERE name = ?",
        ("2026-01-01T00:00:00.000000Z", "first"),
    )
    db.connection.execute(
        "UPDATE file_areas SET created_at = ? WHERE name = ?",
        ("2026-01-02T00:00:00.000000Z", "second"),
    )
    db.connection.commit()

    entry = upload_file(db, first, alice, "readme.txt", b"hello")
    db.connection.execute(
        "UPDATE files SET created_at = ? WHERE id = ?",
        ("2026-01-03T00:00:00.000000Z", entry.id),
    )
    db.connection.commit()

    areas = list_file_areas(db)
    assert [a.name for a in areas] == ["first", "second"]


def test_list_file_areas_alphabetical_order_is_case_insensitive(db, alice):
    create_file_area(db, "Zebra", creator=alice)
    create_file_area(db, "apple", creator=alice)
    create_file_area(db, "Banana", creator=alice)

    areas = list_file_areas(db, order_by="alphabetical")
    assert [a.name for a in areas] == ["apple", "Banana", "Zebra"]


def test_list_file_areas_volume_order_is_by_file_count_descending(db, alice):
    quiet = create_file_area(db, "quiet", creator=alice)
    busy = create_file_area(db, "busy", creator=alice)
    create_file_area(db, "empty", creator=alice)
    upload_file(db, quiet, alice, "one.txt", b"a")
    for i in range(3):
        upload_file(db, busy, alice, f"file{i}.txt", f"content {i}".encode())

    areas = list_file_areas(db, order_by="volume")
    assert [a.name for a in areas] == ["busy", "quiet", "empty"]


# -- GitHub issue #36: hidden lifecycle rows must not leak into
# -- activity/volume sorting -----------------------------------------


def test_list_file_areas_volume_does_not_count_pending_files(db, alice):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    upload_file(db, area, alice, "one.txt", b"a")  # never approved
    upload_file(db, area, alice, "two.txt", b"b")

    areas = list_file_areas(db, order_by="volume")
    reviewed = next(a for a in areas if a.name == "reviewed")
    pending_count = db.connection.execute(
        "SELECT COUNT(*) AS n FROM files WHERE area_id = ? AND status = 'pending'", (reviewed.id,)
    ).fetchone()
    assert pending_count["n"] == 2  # the files really exist, just shouldn't be counted
    volume_row = db.connection.execute(
        "SELECT COUNT(f.id) AS n FROM file_areas a LEFT JOIN files f ON f.area_id = a.id AND f.status = 'approved' WHERE a.id = ?",
        (reviewed.id,),
    ).fetchone()
    assert volume_row["n"] == 0


def test_list_file_areas_activity_does_not_surface_pending_files(db, alice):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    other = create_file_area(db, "other", creator=alice)
    db.connection.execute("UPDATE file_areas SET created_at = ? WHERE name = ?", ("2026-01-01T00:00:00.000000Z", "reviewed"))
    db.connection.execute("UPDATE file_areas SET created_at = ? WHERE name = ?", ("2026-01-02T00:00:00.000000Z", "other"))
    db.connection.commit()
    entry = upload_file(db, area, alice, "one.txt", b"a")
    db.connection.execute("UPDATE files SET created_at = ? WHERE id = ?", ("2026-01-05T00:00:00.000000Z", entry.id))
    db.connection.commit()

    areas = list_file_areas(db)
    assert [a.name for a in areas] == ["other", "reviewed"]


# -- GitHub issue #36 (reopened): effectively-expired-but-not-yet-swept
# -- rows must not leak into activity/volume either -----------------------


def test_list_file_areas_activity_excludes_effectively_expired_files_before_any_sweep(db, alice):
    """A file already past its own area's max_file_age_days, but still
    physically stored as 'approved' -- expiry sweeping is lazy (see
    netbbs.files.entries._sweep_expired_files), and this test
    deliberately never browses the area via list_files_page, so no
    sweep has run -- must not count as area activity, the same as an
    already-swept 'expired' row wouldn't."""
    stale = create_file_area(db, "stale", max_file_age_days=30, creator=alice)
    fresh = create_file_area(db, "fresh", creator=alice)
    db.connection.execute("UPDATE file_areas SET created_at = ? WHERE name = ?", ("2020-01-01T00:00:00.000000Z", "stale"))
    db.connection.execute("UPDATE file_areas SET created_at = ? WHERE name = ?", ("2020-01-02T00:00:00.000000Z", "fresh"))
    db.connection.commit()

    old_entry = upload_file(db, stale, alice, "old.txt", b"a")
    db.connection.execute("UPDATE files SET created_at = ? WHERE id = ?", ("2020-06-01T00:00:00.000000Z", old_entry.id))
    db.connection.commit()
    assert (
        db.connection.execute("SELECT status FROM files WHERE id = ?", (old_entry.id,)).fetchone()["status"]
        == "approved"
    )  # confirms the sweep really never ran

    areas = list_file_areas(db)
    # "stale"'s file's raw created_at (2020-06) is later than "fresh"'s
    # own creation time (2020-01-02) -- it would win on activity if
    # still (wrongly) being counted despite being 30+ days past its own
    # area's retention window.
    assert [a.name for a in areas] == ["fresh", "stale"]


def test_list_file_areas_activity_still_counts_an_exempt_file_past_its_age_limit(db, alice):
    stale = create_file_area(db, "stale", max_file_age_days=30, creator=alice)
    fresh = create_file_area(db, "fresh", creator=alice)
    db.connection.execute("UPDATE file_areas SET created_at = ? WHERE name = ?", ("2020-01-01T00:00:00.000000Z", "stale"))
    db.connection.execute("UPDATE file_areas SET created_at = ? WHERE name = ?", ("2020-01-02T00:00:00.000000Z", "fresh"))
    db.connection.commit()

    old_entry = upload_file(db, stale, alice, "old.txt", b"a")
    db.connection.execute(
        "UPDATE files SET created_at = ?, exempt_from_expiry = 1 WHERE id = ?",
        ("2020-06-01T00:00:00.000000Z", old_entry.id),
    )
    db.connection.commit()

    areas = list_file_areas(db)
    assert [a.name for a in areas] == ["stale", "fresh"]  # exempt -- its late created_at wins now


def test_list_file_areas_volume_excludes_effectively_expired_files_before_any_sweep(db, alice):
    stale = create_file_area(db, "stale", max_file_age_days=30, creator=alice)
    fresh = create_file_area(db, "fresh", creator=alice)
    upload_file(db, fresh, alice, "new.txt", b"a")  # one genuinely-counted file
    old_entry = upload_file(db, stale, alice, "old.txt", b"b")
    db.connection.execute("UPDATE files SET created_at = ? WHERE id = ?", ("2020-01-01T00:00:00.000000Z", old_entry.id))
    db.connection.commit()
    assert (
        db.connection.execute("SELECT status FROM files WHERE id = ?", (old_entry.id,)).fetchone()["status"]
        == "approved"
    )

    areas = list_file_areas(db, order_by="volume")
    # "fresh" has one genuinely-counted file; "stale"'s only file is
    # effectively expired and must count as zero, not one.
    assert [a.name for a in areas] == ["fresh", "stale"]


def test_list_file_areas_volume_still_counts_an_exempt_file_past_its_age_limit(db, alice):
    stale = create_file_area(db, "stale", max_file_age_days=30, creator=alice)
    empty = create_file_area(db, "empty", creator=alice)
    old_entry = upload_file(db, stale, alice, "old.txt", b"a")
    db.connection.execute(
        "UPDATE files SET created_at = ?, exempt_from_expiry = 1 WHERE id = ?",
        ("2020-01-01T00:00:00.000000Z", old_entry.id),
    )
    db.connection.commit()

    areas = list_file_areas(db, order_by="volume")
    assert [a.name for a in areas] == ["stale", "empty"]


def test_list_file_areas_pinned_areas_sort_first_regardless_of_order_by(db, alice):
    create_file_area(db, "apple", creator=alice)
    create_file_area(db, "banana", creator=alice)
    create_file_area(db, "zzz-pinned", pinned=True, creator=alice)

    areas = list_file_areas(db, order_by="alphabetical")
    assert [a.name for a in areas] == ["zzz-pinned", "apple", "banana"]


def test_list_file_areas_rejects_unknown_order_by(db):
    with pytest.raises(ValueError):
        list_file_areas(db, order_by="nonsense")


# -- file upload/download ------------------------------------------------


def test_upload_file(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "readme.txt", b"hello world", description="A readme")
    assert entry.filename == "readme.txt"
    assert entry.size_bytes == len(b"hello world")
    assert entry.description == "A readme"
    assert entry.uploader_label == "alice"
    assert entry.area_id == area.id


def test_upload_file_generates_content_addressed_id(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "readme.txt", b"hello world")
    assert len(entry.file_id) == 64
    int(entry.file_id, 16)


def test_two_uploads_with_identical_content_have_different_file_ids(db, alice, monkeypatch):
    """Content-addressing includes metadata (filename, uploader,
    timestamp), not just the bytes' hash -- two otherwise-identical
    uploads are still distinct events, mirroring how two boards created
    by the same creator get different board_ids. Timestamps are patched
    to guaranteed-distinct values rather than relying on two back-to-back
    calls landing on different wall-clock instants: identical content
    uploaded within the same clock tick is exactly the collision
    netbbs.boards.posts.create_post already documents as an accepted
    edge case ("identical content posted twice in the same instant") --
    this test flaked on exactly that before being pinned down."""
    import netbbs.files.entries as entries_module

    area = create_file_area(db, "docs", creator=alice)
    timestamps = iter(["2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.000001Z"])
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))

    a = upload_file(db, area, alice, "readme.txt", b"same content")
    b = upload_file(db, area, alice, "readme.txt", b"same content")
    assert a.file_id != b.file_id


def test_two_uploads_with_identical_content_share_stored_bytes(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    a = upload_file(db, area, alice, "readme.txt", b"same content")
    b = upload_file(db, area, alice, "copy.txt", b"same content")
    assert a.sha256 == b.sha256
    assert a.storage_path == b.storage_path


def test_download_file_returns_original_bytes(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "readme.txt", b"hello world")
    assert download_file(entry) == b"hello world"


def test_get_file(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    created = upload_file(db, area, alice, "readme.txt", b"hello")
    fetched = get_file(db, created.file_id)
    assert fetched.file_id == created.file_id


def test_get_nonexistent_file_fails(db):
    with pytest.raises(FileEntryError):
        get_file(db, "nonexistent")


def test_uploader_fingerprint_is_none_for_password_only_user(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "readme.txt", b"hello")
    assert entry.uploader_fingerprint is None


def test_list_files_page_returns_all_in_order(db, alice, monkeypatch):
    import netbbs.files.entries as entries_module

    # Explicit, distinct timestamps -- real wall-clock calls in quick
    # succession can land on the same microsecond (exactly what this
    # file's own module docstring warns about); list_files_page's
    # deterministic tie-breaker for same-timestamp entries is file_id
    # (a content hash), which doesn't preserve upload order, so a tie
    # would make this assertion flaky without them (design doc round 31).
    timestamps = iter(["2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.000001Z"])
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))

    area = create_file_area(db, "docs", creator=alice)
    upload_file(db, area, alice, "first.txt", b"1")
    upload_file(db, area, alice, "second.txt", b"2")
    page = list_files_page(db, area, alice)
    assert [f.filename for f in page.entries] == ["first.txt", "second.txt"]


# -- level-gating ---------------------------------------------------------


def test_upload_blocked_below_min_write_level(db, alice, bob):
    area = create_file_area(db, "staff-only", min_write_level=50, creator=alice)
    with pytest.raises(InsufficientLevelError):
        upload_file(db, area, bob, "readme.txt", b"hello")


def test_upload_allowed_at_exact_min_write_level(db, bob):
    area = create_file_area(db, "docs", min_write_level=0, creator=bob)
    entry = upload_file(db, area, bob, "readme.txt", b"hello")
    assert entry.filename == "readme.txt"


# -- GitHub issue #34: streaming upload path (upload_file_from_temp) -------


def _temp_file_with(db, content: bytes) -> tuple:
    temp_path = new_incoming_temp_path(db)
    temp_path.write_bytes(content)
    return temp_path, hashlib.sha256(content).hexdigest()


def test_upload_file_from_temp_creates_an_entry_matching_upload_file(db, alice):
    """The streaming and non-streaming paths must produce equivalent
    results for identical content -- proves upload_file_from_temp isn't
    a second, subtly-different upload implementation."""
    area = create_file_area(db, "docs", creator=alice)
    temp_path, sha256 = _temp_file_with(db, b"hello world")

    entry = upload_file_from_temp(
        db, area, alice, "readme.txt",
        temp_path=temp_path, sha256=sha256, size_bytes=11, description="A readme",
    )

    assert entry.filename == "readme.txt"
    assert entry.size_bytes == 11
    assert entry.sha256 == sha256
    assert entry.description == "A readme"
    assert entry.uploader_label == "alice"
    assert download_file(entry) == b"hello world"


def test_upload_file_from_temp_moves_the_temp_file_not_copies(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    temp_path, sha256 = _temp_file_with(db, b"hello world")

    upload_file_from_temp(db, area, alice, "readme.txt", temp_path=temp_path, sha256=sha256, size_bytes=11)

    assert not temp_path.exists()


def test_upload_file_from_temp_shares_storage_with_upload_file_for_identical_content(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    bytes_entry = upload_file(db, area, alice, "a.txt", b"same content")
    temp_path, sha256 = _temp_file_with(db, b"same content")

    temp_entry = upload_file_from_temp(
        db, area, alice, "b.txt", temp_path=temp_path, sha256=sha256, size_bytes=12
    )

    assert temp_entry.storage_path == bytes_entry.storage_path
    assert not temp_path.exists()  # discarded -- content already stored


def test_upload_file_from_temp_cleans_up_the_temp_file_on_permission_failure(db, alice, bob):
    area = create_file_area(db, "staff-only", min_write_level=50, creator=alice)
    temp_path, sha256 = _temp_file_with(db, b"hello world")

    with pytest.raises(InsufficientLevelError):
        upload_file_from_temp(
            db, area, bob, "readme.txt", temp_path=temp_path, sha256=sha256, size_bytes=11
        )

    assert not temp_path.exists()  # never leaked as an orphaned staging file
    # And nothing was ever moved into permanent storage either.
    assert not (storage_root(db) / sha256[:2] / sha256).exists()


def test_list_files_blocked_below_min_read_level(db, alice, bob):
    area = create_file_area(db, "staff-only", min_read_level=50, creator=alice)
    with pytest.raises(InsufficientLevelError):
        list_files_page(db, area, bob)


def test_list_files_allowed_at_sufficient_level(db, alice):
    area = create_file_area(db, "docs", min_read_level=5, creator=alice)
    upload_file(db, area, alice, "readme.txt", b"hello")
    page = list_files_page(db, area, alice)
    assert len(page.entries) == 1


# -- update/delete (design doc -- board/area management round) -------------


def test_create_file_area_records_an_audit_entry(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    entries = list_actions_for_object(db, "file_area", area.id)
    assert any(e.action == "create_file_area" for e in entries)


def test_update_file_area_replaces_the_full_state(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    updated = update_file_area(
        db, area, name="docs2", description="new desc", min_read_level=1, min_write_level=2,
        category_id=None, pinned=True, moderated=True, max_file_age_days=30,
        min_age=18, name_requirement="verified", changed_by=alice,
    )
    assert updated.name == "docs2"
    assert updated.description == "new desc"
    assert updated.min_read_level == 1
    assert updated.min_write_level == 2
    assert updated.pinned is True
    assert updated.moderated is True
    assert updated.max_file_age_days == 30
    assert updated.min_age == 18
    assert updated.name_requirement == "verified"
    entries = list_actions_for_object(db, "file_area", area.id)
    assert any(e.action == "update_file_area" for e in entries)


def test_update_file_area_rejects_a_name_collision(db, alice):
    create_file_area(db, "taken", creator=alice)
    area = create_file_area(db, "docs", creator=alice)
    with pytest.raises(FileAreaError):
        update_file_area(
            db, area, name="taken", description=None, min_read_level=0, min_write_level=0,
            category_id=None, pinned=False, moderated=False, max_file_age_days=None,
            min_age=None, name_requirement=None, changed_by=alice,
        )


def test_update_file_area_rejects_invalid_name_requirement(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    with pytest.raises(FileAreaError, match="name_requirement"):
        update_file_area(
            db, area, name="docs", description=None, min_read_level=0, min_write_level=0,
            category_id=None, pinned=False, moderated=False, max_file_age_days=None,
            min_age=None, name_requirement="bogus", changed_by=alice,
        )


def test_create_file_area_rejects_invalid_name_requirement(db, alice):
    with pytest.raises(FileAreaError, match="name_requirement"):
        create_file_area(db, "docs", creator=alice, name_requirement="bogus")


def test_create_file_area_defaults_no_age_or_name_gate(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    assert area.min_age is None
    assert area.name_requirement is None


def test_delete_file_area_removes_files_and_moderator_grants(db, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    upload_file(db, area, alice, "readme.txt", b"hello")
    grant_permissions(
        db, bob, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE,
        granted_by=alice,
    )

    delete_file_area(db, area, deleted_by=alice)

    with pytest.raises(FileAreaError):
        get_file_area_by_name(db, "docs")
    assert db.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM moderator_grants").fetchone()[0] == 0


def test_delete_file_area_does_not_touch_its_category(db, alice):
    category = create_category(db, "Software", created_by=alice)
    area = create_file_area(db, "docs", category_id=category.id, creator=alice)
    delete_file_area(db, area, deleted_by=alice)
    from netbbs.files.categories import get_category_by_id

    still_there = get_category_by_id(db, category.id)
    assert still_there.name == "Software"


def test_delete_file_area_records_an_audit_entry_before_deleting(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    area_id = area.id
    delete_file_area(db, area, deleted_by=alice)
    entries = list_actions_for_object(db, "file_area", area_id)
    assert any(e.action == "delete_file_area" for e in entries)
