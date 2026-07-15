"""Tests for netbbs.files.gc — reference-aware blob garbage collection
(GitHub issue #35)."""

from __future__ import annotations

import time

import pytest

from netbbs.auth.users import create_user
from netbbs.config import set_expiry_grace_period_days
from netbbs.files.areas import create_file_area, delete_file_area
from netbbs.files.entries import delete_file, list_files_page, upload_file
from netbbs.files.gc import find_orphaned_blobs, reclaim_orphaned_blobs
from netbbs.files.storage import storage_path_for
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _age_blob(path, seconds_old: float) -> None:
    """Backdate a blob's mtime -- same direct-manipulation approach the
    rest of this test suite uses to simulate old content, rather than
    monkeypatching a clock."""
    import os

    backdated = time.time() - seconds_old
    os.utime(path, (backdated, backdated))


def test_no_orphans_when_nothing_uploaded(db):
    assert find_orphaned_blobs(db) == []


def test_referenced_blob_is_not_orphaned(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)
    assert blob_path not in find_orphaned_blobs(db)


def test_deleting_the_only_reference_orphans_the_blob(db, alice):
    from netbbs.moderation import BoardPermission, grant_permissions

    area = create_file_area(db, "docs", creator=alice)
    grant_permissions(db, alice, object_type="file_area", object_id=area.id, permissions=BoardPermission.DELETE, granted_by=alice)
    entry = upload_file(db, area, alice, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)

    delete_file(db, entry, deleted_by=alice)

    assert blob_path in find_orphaned_blobs(db)


def test_two_entries_sharing_one_blob_deleting_one_keeps_it_live(db, alice):
    """Regression test for the exact scenario content-addressing exists
    for: two file entries with byte-identical content share one blob
    on disk -- deleting either entry alone must not orphan it while the
    other still references it."""
    from netbbs.moderation import BoardPermission, grant_permissions

    area = create_file_area(db, "docs", creator=alice)
    grant_permissions(db, alice, object_type="file_area", object_id=area.id, permissions=BoardPermission.DELETE, granted_by=alice)
    first = upload_file(db, area, alice, "one.txt", b"identical content")
    second = upload_file(db, area, alice, "two.txt", b"identical content")
    assert first.sha256 == second.sha256  # same bytes -- same blob
    blob_path = storage_path_for(db, first.sha256)

    delete_file(db, first, deleted_by=alice)

    assert blob_path not in find_orphaned_blobs(db)  # "two.txt" still references it

    delete_file(db, second, deleted_by=alice)

    assert blob_path in find_orphaned_blobs(db)  # now genuinely unreferenced


def test_rejected_pending_upload_is_reclaimed(db, alice):
    from netbbs.moderation import BoardPermission, grant_permissions

    area = create_file_area(db, "docs", moderated=True, creator=alice)
    grant_permissions(db, alice, object_type="file_area", object_id=area.id, permissions=BoardPermission.DELETE, granted_by=alice)
    entry = upload_file(db, area, alice, "file.txt", b"hello")  # starts pending
    blob_path = storage_path_for(db, entry.sha256)

    delete_file(db, entry, deleted_by=alice)  # doubles as "reject" for a pending upload

    assert blob_path in find_orphaned_blobs(db)


def test_expired_and_grace_period_elapsed_entry_is_reclaimed(db, alice):
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    set_expiry_grace_period_days(db, 5)
    entry = upload_file(db, area, alice, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)

    db.connection.execute(
        "UPDATE files SET created_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00.000000Z", entry.id),
    )
    db.connection.commit()

    list_files_page(db, area, alice)  # triggers the expiry sweep, including hard-delete

    assert blob_path in find_orphaned_blobs(db)


def test_area_deleted_entries_are_reclaimed(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)

    delete_file_area(db, area, deleted_by=alice)

    assert blob_path in find_orphaned_blobs(db)


def test_unrelated_file_under_storage_root_is_ignored(db, alice):
    """A file that doesn't look like one of this module's own sha256
    blob names is left alone entirely, not treated as reclaimable just
    because it's under the storage root (GitHub issue #35's "handled
    conservatively" requirement)."""
    from netbbs.files.storage import storage_root

    area = create_file_area(db, "docs", creator=alice)
    upload_file(db, area, alice, "file.txt", b"hello")  # ensures the root exists
    root = storage_root(db)
    stray = root / "README.txt"
    stray.write_text("not a blob")

    assert stray not in find_orphaned_blobs(db)


def test_dry_run_reports_but_does_not_delete(db, alice):
    from netbbs.moderation import BoardPermission, grant_permissions

    area = create_file_area(db, "docs", creator=alice)
    grant_permissions(db, alice, object_type="file_area", object_id=area.id, permissions=BoardPermission.DELETE, granted_by=alice)
    entry = upload_file(db, area, alice, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)
    delete_file(db, entry, deleted_by=alice)
    _age_blob(blob_path, seconds_old=7200)

    report = reclaim_orphaned_blobs(db, dry_run=True, min_age_seconds=3600)

    assert report.dry_run is True
    assert report.reclaimable_blobs == 1
    assert report.reclaimable_bytes == len(b"hello")
    assert blob_path.exists()  # nothing actually deleted


def test_real_run_deletes_the_orphan(db, alice):
    from netbbs.moderation import BoardPermission, grant_permissions

    area = create_file_area(db, "docs", creator=alice)
    grant_permissions(db, alice, object_type="file_area", object_id=area.id, permissions=BoardPermission.DELETE, granted_by=alice)
    entry = upload_file(db, area, alice, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)
    delete_file(db, entry, deleted_by=alice)
    _age_blob(blob_path, seconds_old=7200)

    report = reclaim_orphaned_blobs(db, dry_run=False, min_age_seconds=3600)

    assert report.dry_run is False
    assert report.reclaimable_blobs == 1
    assert not blob_path.exists()


def test_recently_orphaned_blob_is_skipped_by_default_safety_age(db, alice):
    """Guards the store_bytes()-writes-before-the-row-commits race
    (module docstring) -- a blob that only just became (transiently or
    genuinely) unreferenced is not immediately eligible."""
    from netbbs.moderation import BoardPermission, grant_permissions

    area = create_file_area(db, "docs", creator=alice)
    grant_permissions(db, alice, object_type="file_area", object_id=area.id, permissions=BoardPermission.DELETE, granted_by=alice)
    entry = upload_file(db, area, alice, "file.txt", b"hello")
    blob_path = storage_path_for(db, entry.sha256)
    delete_file(db, entry, deleted_by=alice)
    # Freshly orphaned -- mtime is "now", well inside the default safety age.

    report = reclaim_orphaned_blobs(db, dry_run=False)

    assert report.reclaimable_blobs == 0
    assert report.skipped_recent == 1
    assert blob_path.exists()
