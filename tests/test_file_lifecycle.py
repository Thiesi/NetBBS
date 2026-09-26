"""
Tests for the moderated-area approval flow and file maintenance/expiry
state machine (design doc §13/§15) in netbbs.files.entries — the
file-area mirror of tests/test_post_lifecycle.py's coverage, plus the
SysOp's expired-file recovery listing (issue #639, files only, no post
equivalent).
"""

from __future__ import annotations

import datetime

import pytest

from netbbs.auth.users import create_user
from netbbs.config import get_expiry_grace_period_days, set_expiry_grace_period_days
from netbbs.files.areas import create_file_area
from netbbs.files.entries import (
    FileEntryError,
    approve_file,
    count_visible_files,
    delete_file,
    expired_file_purge_at,
    get_file,
    list_expired_files,
    list_files_page,
    list_pending_files,
    list_pinned_files,
    set_file_exempt,
    set_file_pinned,
    upload_file,
)
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


def _age_file(db, entry, days_old: int) -> None:
    """Backdate a file's created_at, mirroring test_post_lifecycle.py's
    _age_post helper."""
    backdated = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_old)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    db.connection.execute("UPDATE files SET created_at = ? WHERE id = ?", (backdated, entry.id))
    db.connection.commit()


# -- moderated approval flow: initial status ---------------------------


def test_file_on_non_moderated_area_starts_approved(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "hello.txt", b"data")
    assert entry.status == "approved"


def test_file_on_moderated_area_starts_pending(db, alice):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    entry = upload_file(db, area, alice, "hello.txt", b"data")
    assert entry.status == "pending"


def test_pending_file_is_hidden_from_normal_listing(db, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    upload_file(db, area, alice, "hello.txt", b"data")
    page = list_files_page(db, area, bob)
    assert page.entries == []


# -- moderation queue: list_pending_files --------------------------------


def test_list_pending_files_visible_to_approve_holder(db, sysop, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    upload_file(db, area, bob, "hello.txt", b"data")
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop)

    pending = list_pending_files(db, area, requesting_user=sysop)
    assert len(pending) == 1


def test_list_pending_files_shows_only_own_uploads_without_approve(db, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    upload_file(db, area, alice, "alice.txt", b"data")
    upload_file(db, area, bob, "bob.txt", b"data")

    pending = list_pending_files(db, area, requesting_user=alice)
    assert [e.filename for e in pending] == ["alice.txt"]


def test_list_pending_files_empty_for_uninvolved_user(db, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    upload_file(db, area, alice, "hello.txt", b"data")

    pending = list_pending_files(db, area, requesting_user=bob)
    assert pending == []


# -- approve_file -----------------------------------------------------------


def test_approve_file_requires_approve_permission(db, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    with pytest.raises(FileEntryError):
        approve_file(db, entry, approved_by=bob)


def test_approve_file_transitions_to_approved_and_becomes_visible(db, sysop, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop)
    entry = upload_file(db, area, bob, "hello.txt", b"data")

    approved = approve_file(db, entry, approved_by=sysop)
    assert approved.status == "approved"

    page = list_files_page(db, area, bob)
    assert [e.filename for e in page.entries] == ["hello.txt"]


def test_approve_file_is_logged(db, sysop, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop)
    entry = upload_file(db, area, bob, "hello.txt", b"data")

    approve_file(db, entry, approved_by=sysop)
    entries = list_actions_for_target_user(db, bob.id)
    assert any(e.action == "approve" for e in entries)


# -- delete_file (and reject-via-delete) -----------------------------------


def test_delete_file_requires_delete_permission(db, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    with pytest.raises(FileEntryError):
        delete_file(db, entry, deleted_by=bob)


def test_delete_file_removes_it_from_listing(db, sysop, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.DELETE, granted_by=sysop)
    entry = upload_file(db, area, bob, "hello.txt", b"data")

    delete_file(db, entry, deleted_by=sysop)
    page = list_files_page(db, area, bob)
    assert page.entries == []


def test_delete_pending_file_logs_reject_not_delete(db, sysop, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.DELETE, granted_by=sysop)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    assert entry.status == "pending"

    delete_file(db, entry, deleted_by=sysop)
    entries = list_actions_for_target_user(db, bob.id)
    assert entries[-1].action == "reject"


def test_delete_approved_file_logs_delete(db, sysop, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.DELETE, granted_by=sysop)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    assert entry.status == "approved"

    delete_file(db, entry, deleted_by=sysop)
    entries = list_actions_for_target_user(db, bob.id)
    assert entries[-1].action == "delete"


# -- pin/exempt: require edit permission -----------------------------------


def test_set_file_pinned_requires_edit_permission(db, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    with pytest.raises(FileEntryError):
        set_file_pinned(db, entry, True, changed_by=bob)


def test_set_file_pinned_marks_file_pinned(db, sysop, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=sysop)
    entry = upload_file(db, area, bob, "hello.txt", b"data")

    pinned = set_file_pinned(db, entry, True, changed_by=sysop)
    assert pinned.pinned is True


def test_set_file_exempt_requires_edit_permission(db, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    with pytest.raises(FileEntryError):
        set_file_exempt(db, entry, True, changed_by=bob)


def test_set_file_exempt_marks_file_exempt(db, sysop, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=sysop)
    entry = upload_file(db, area, bob, "hello.txt", b"data")

    exempted = set_file_exempt(db, entry, True, changed_by=sysop)
    assert exempted.exempt_from_expiry is True


# -- list_pinned_files --------------------------------------------------


def test_list_pinned_files_returns_only_pinned(db, sysop, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=sysop)
    pinned_entry = upload_file(db, area, bob, "pinned.txt", b"data")
    upload_file(db, area, bob, "not-pinned.txt", b"data")
    set_file_pinned(db, pinned_entry, True, changed_by=sysop)

    pinned = list_pinned_files(db, area, requesting_user=bob)
    assert [e.filename for e in pinned] == ["pinned.txt"]


def test_list_pinned_files_empty_by_default(db, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    upload_file(db, area, bob, "hello.txt", b"data")
    assert list_pinned_files(db, area, requesting_user=bob) == []


# -- count_visible_files -----------------------------------------------------


def test_count_visible_files_on_an_empty_area(db, alice):
    area = create_file_area(db, "docs", creator=alice)
    count, last_created_at = count_visible_files(db, area)
    assert count == 0
    assert last_created_at is None


def test_count_visible_files_counts_approved_and_reports_latest(db, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    upload_file(db, area, bob, "first.txt", b"data")
    newest = upload_file(db, area, bob, "second.txt", b"data")

    count, last_created_at = count_visible_files(db, area)
    assert count == 2
    assert last_created_at == newest.created_at


def test_count_visible_files_excludes_pending_files(db, alice, bob):
    area = create_file_area(db, "docs", creator=alice, moderated=True)
    upload_file(db, area, bob, "needs-approval.txt", b"data")

    count, last_created_at = count_visible_files(db, area)
    assert count == 0
    assert last_created_at is None


# -- expiry sweep -----------------------------------------------------------


def test_file_within_max_age_stays_approved(db, alice, bob):
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    _age_file(db, entry, days_old=5)

    page = list_files_page(db, area, bob)
    assert [e.filename for e in page.entries] == ["hello.txt"]


def test_file_past_max_age_becomes_expired_and_is_delisted(db, alice, bob):
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    _age_file(db, entry, days_old=31)

    page = list_files_page(db, area, bob)
    assert page.entries == []

    still_there = get_file(db, entry.file_id)
    assert still_there.status == "expired"


def test_exempt_file_never_expires(db, sysop, alice, bob):
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=sysop)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    set_file_exempt(db, entry, True, changed_by=sysop)
    _age_file(db, entry, days_old=365)

    page = list_files_page(db, area, bob)
    assert [e.filename for e in page.entries] == ["hello.txt"]


def test_file_with_no_max_age_never_expires(db, alice, bob):
    area = create_file_area(db, "docs", creator=alice)  # max_file_age_days=None
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    _age_file(db, entry, days_old=10_000)

    page = list_files_page(db, area, bob)
    assert [e.filename for e in page.entries] == ["hello.txt"]


def test_expired_file_past_grace_period_is_actually_deleted(db, alice, bob):
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    set_expiry_grace_period_days(db, 5)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    _age_file(db, entry, days_old=40)  # 30 (age) + 5 (grace) + margin

    list_files_page(db, area, bob)  # triggers the sweep

    with pytest.raises(FileEntryError):
        get_file(db, entry.file_id)


def test_expired_file_within_grace_period_is_not_yet_deleted(db, alice, bob):
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    set_expiry_grace_period_days(db, 30)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    _age_file(db, entry, days_old=31)  # expired, but well within the 30-day grace period

    list_files_page(db, area, bob)

    still_there = get_file(db, entry.file_id)
    assert still_there.status == "expired"


def test_default_grace_period_is_seven_days(db):
    assert get_expiry_grace_period_days(db) == 7


# -- expired files: gone to callers, recoverable by the SysOp (issue #639) --


def test_expired_file_keeps_its_row_until_purged(db, alice, bob):
    """Renamed from test_expired_file_still_reachable_by_name (issue
    #639). Expiry ends a *caller's* reach, but the row -- and the bytes
    behind it -- stay until the grace period ends, and SysOp recovery
    depends on exactly that. What changed was the promise about callers,
    not this return value."""
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    _age_file(db, entry, days_old=31)
    list_files_page(db, area, bob)  # sweep -> entry becomes 'expired'

    found = get_file(db, entry.file_id)
    assert found.status == "expired"


def test_list_expired_files_is_the_sysops_recovery_listing(db, sysop, alice, bob):
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    expired = upload_file(db, area, bob, "old.txt", b"old")
    upload_file(db, area, bob, "new.txt", b"new")
    _age_file(db, expired, days_old=31)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop)

    # No listing has swept the area yet: list_expired_files sweeps itself,
    # so a file that expired since the area was last browsed is there.
    found = list_expired_files(db, area, requesting_user=sysop)
    assert [f.filename for f in found] == ["old.txt"]


def test_list_expired_files_refuses_the_uploader(db, alice, bob):
    """An uploader whose file expired has lost it the same as every other
    caller -- there is no own-uploads view as the pending queue has."""
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    _age_file(db, entry, days_old=31)

    with pytest.raises(FileEntryError):
        list_expired_files(db, area, requesting_user=bob)


def test_list_expired_files_drops_a_file_once_its_grace_period_ends(db, sysop, alice, bob):
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    set_expiry_grace_period_days(db, 5)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    _age_file(db, entry, days_old=40)
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop)

    assert list_expired_files(db, area, requesting_user=sysop) == []


def test_expired_file_purge_at_is_age_plus_grace(db, alice, bob):
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    set_expiry_grace_period_days(db, 5)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    db.connection.execute(
        "UPDATE files SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:00.000000Z", entry.id)
    )
    db.connection.commit()

    assert expired_file_purge_at(db, area, get_file(db, entry.file_id)) == "2026-02-05T00:00:00.000000Z"


def test_expired_file_purge_at_is_none_when_no_sweep_will_purge_it(db, sysop, alice, bob):
    no_limit = create_file_area(db, "archive", creator=alice)
    entry = upload_file(db, no_limit, bob, "hello.txt", b"data")
    assert expired_file_purge_at(db, no_limit, entry) is None

    limited = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    grant_permissions(db, sysop, object_type="file_area", object_id=limited.id, permissions=BoardPermission.EDIT, granted_by=sysop)
    exempt = set_file_exempt(db, upload_file(db, limited, bob, "keep.txt", b"keep"), True, changed_by=sysop)
    assert expired_file_purge_at(db, limited, exempt) is None
