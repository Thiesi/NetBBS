"""
Tests for the moderated-area approval flow and file maintenance/expiry
state machine (design doc §13/§15) in netbbs.files.entries — the
file-area mirror of tests/test_post_lifecycle.py's coverage, plus
get_file_by_name's own pending-visibility check (files only, no post
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
    delete_file,
    get_file,
    get_file_by_name,
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


# -- expired files stay reachable via get_file_by_name ----------------------


def test_expired_file_still_reachable_by_name(db, alice, bob):
    area = create_file_area(db, "docs", max_file_age_days=30, creator=alice)
    entry = upload_file(db, area, bob, "hello.txt", b"data")
    _age_file(db, entry, days_old=31)
    list_files_page(db, area, bob)  # sweep -> entry becomes 'expired'

    found = get_file_by_name(db, area, "hello.txt")
    assert found is not None
    assert found.status == "expired"


# -- get_file_by_name pending-visibility check (files only) -----------------


def test_get_file_by_name_hides_pending_file_from_stranger(db, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    upload_file(db, area, bob, "hello.txt", b"data")

    found = get_file_by_name(db, area, "hello.txt", requesting_user=alice)
    assert found is None


def test_get_file_by_name_hides_pending_file_with_no_requesting_user(db, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    upload_file(db, area, bob, "hello.txt", b"data")

    assert get_file_by_name(db, area, "hello.txt") is None


def test_get_file_by_name_shows_pending_file_to_its_own_uploader(db, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    upload_file(db, area, bob, "hello.txt", b"data")

    found = get_file_by_name(db, area, "hello.txt", requesting_user=bob)
    assert found is not None
    assert found.status == "pending"


def test_get_file_by_name_shows_pending_file_to_approve_holder(db, sysop, alice, bob):
    area = create_file_area(db, "reviewed", moderated=True, creator=alice)
    upload_file(db, area, bob, "hello.txt", b"data")
    grant_permissions(db, sysop, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop)

    found = get_file_by_name(db, area, "hello.txt", requesting_user=sysop)
    assert found is not None


def test_get_file_by_name_shows_approved_file_to_anyone(db, alice, bob):
    area = create_file_area(db, "docs", creator=alice)
    upload_file(db, area, bob, "hello.txt", b"data")

    found = get_file_by_name(db, area, "hello.txt")
    assert found is not None
    assert found.status == "approved"
