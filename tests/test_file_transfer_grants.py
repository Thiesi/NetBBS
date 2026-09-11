"""
Transfer grants (issue #475): the bounded, single-use permission that
lets a caller whose terminal cannot speak Zmodem move a file over HTTP.

The point of these tests is that a grant is *not* a capability. It
names who asked for what, and every gate the terminal path enforces is
enforced again when it is redeemed -- so the tests mostly consist of
changing the world underneath a live grant and checking that redeeming
it stops working.
"""

from __future__ import annotations

import pytest

from netbbs.attestation import attest_name, set_birthdate
from netbbs.auth.users import create_user, set_user_disabled
from netbbs.files.areas import create_file_area
from netbbs.files.entries import approve_file, delete_file, upload_file
from netbbs.moderation.roles import BoardPermission, grant_permissions
from netbbs.net.file_transfer import (
    DOWNLOAD,
    UPLOAD,
    TransferError,
    TransferGrants,
    hash_and_measure,
    resolve,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# -- the grant table ----------------------------------------------------


def test_a_grant_is_single_use(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    grant = grants.issue(direction=UPLOAD, user=alice, area=area)

    assert grants.redeem(grant.token) is not None
    assert grants.redeem(grant.token) is None


def test_a_grant_expires(db, alice):
    clock = _Clock()
    grants = TransferGrants(ttl_seconds=60, clock=clock)
    area = create_file_area(db, "docs", creator=alice)
    grant = grants.issue(direction=UPLOAD, user=alice, area=area)

    clock.now += 61
    assert grants.redeem(grant.token) is None
    assert len(grants) == 0  # and it is swept, not merely refused


def test_an_unknown_token_is_refused(db, alice):
    assert TransferGrants().redeem("nothing-like-a-real-token") is None


def test_tokens_are_unguessable_and_distinct(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    tokens = {
        grants.issue(direction=UPLOAD, user=alice, area=area).token
        for _ in range(20)
    }
    assert len(tokens) == 20
    assert all(len(token) >= 32 for token in tokens)


def test_outstanding_grants_are_bounded(db, alice):
    grants = TransferGrants(max_outstanding=3)
    area = create_file_area(db, "docs", creator=alice)
    for _ in range(3):
        grants.issue(direction=UPLOAD, user=alice, area=area)

    with pytest.raises(TransferError):
        grants.issue(direction=UPLOAD, user=alice, area=area)


def test_expired_grants_make_room_for_new_ones(db, alice):
    clock = _Clock()
    grants = TransferGrants(max_outstanding=2, ttl_seconds=60, clock=clock)
    area = create_file_area(db, "docs", creator=alice)
    grants.issue(direction=UPLOAD, user=alice, area=area)
    grants.issue(direction=UPLOAD, user=alice, area=area)

    clock.now += 61
    assert grants.issue(direction=UPLOAD, user=alice, area=area) is not None


def test_a_node_with_no_public_url_prints_none(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    grant = grants.issue(direction=UPLOAD, user=alice, area=area)
    assert grants.url_for(grant) is None

    addressed = TransferGrants(base_url="https://bbs.example.org/")
    grant = addressed.issue(direction=UPLOAD, user=alice, area=area)
    assert addressed.url_for(grant) == f"https://bbs.example.org/transfer/{grant.token}"


# -- redemption re-checks everything ------------------------------------


def _download_grant(db, grants, user, area, entry):
    return grants.issue(direction=DOWNLOAD, user=user, area=area, file_id=entry.file_id)


def test_a_download_grant_resolves_to_its_file(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")

    resolved = resolve(db, _download_grant(db, grants, alice, area, entry))

    assert resolved.entry.file_id == entry.file_id
    assert resolved.user.id == alice.id
    assert resolved.area.id == area.id


def test_a_disabled_account_cannot_redeem(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    grant = _download_grant(db, grants, alice, area, entry)
    set_user_disabled(db, alice, True, changed_by=alice)

    with pytest.raises(TransferError):
        resolve(db, grant)


def test_a_raised_read_level_stops_a_live_download_grant(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    grant = _download_grant(db, grants, alice, area, entry)

    db.connection.execute("UPDATE file_areas SET min_read_level = 250 WHERE id = ?", (area.id,))
    db.connection.commit()

    with pytest.raises(TransferError):
        resolve(db, grant)


def test_a_raised_write_level_stops_a_live_upload_grant(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    grant = grants.issue(direction=UPLOAD, user=alice, area=area)

    db.connection.execute("UPDATE file_areas SET min_write_level = 250 WHERE id = ?", (area.id,))
    db.connection.commit()

    with pytest.raises(TransferError):
        resolve(db, grant)


def test_a_deleted_file_stops_its_grant(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    grant = _download_grant(db, grants, alice, area, entry)
    grant_permissions(
        db, alice, object_type="file_area", object_id=area.id,
        permissions=BoardPermission.DELETE, granted_by=alice,
    )
    delete_file(db, entry, deleted_by=alice)

    with pytest.raises(TransferError):
        resolve(db, grant)


def test_a_deleted_area_stops_its_grant(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    grant = grants.issue(direction=UPLOAD, user=alice, area=area)
    db.connection.execute("DELETE FROM file_areas WHERE id = ?", (area.id,))
    db.connection.commit()

    with pytest.raises(TransferError):
        resolve(db, grant)


def test_someone_elses_pending_upload_is_not_downloadable(db, alice):
    grants = TransferGrants()
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    area = create_file_area(db, "docs", creator=bob, moderated=True)
    entry = upload_file(db, area, alice, "game.zip", b"payload")

    # Its own uploader may fetch it back while it waits.
    assert resolve(db, _download_grant(db, grants, alice, area, entry)).entry.file_id == entry.file_id

    with pytest.raises(TransferError):
        resolve(db, _download_grant(db, grants, bob, area, entry))


def test_an_approved_file_is_downloadable_by_anyone_who_may_read_the_area(db, alice):
    grants = TransferGrants()
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    area = create_file_area(db, "docs", creator=bob, moderated=True)
    grant_permissions(
        db, bob, object_type="file_area", object_id=area.id,
        permissions=BoardPermission.APPROVE, granted_by=bob,
    )
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    approve_file(db, entry, approved_by=bob)

    assert resolve(db, _download_grant(db, grants, bob, area, entry)).entry.file_id == entry.file_id


def test_an_age_requirement_is_re_checked(db, alice):
    from datetime import date

    grants = TransferGrants()
    area = create_file_area(db, "adults", creator=alice, min_age=18)
    set_birthdate(db, alice, date(1990, 1, 1))
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    grant = _download_grant(db, grants, alice, area, entry)

    db.connection.execute("UPDATE file_areas SET min_age = 21 WHERE id = ?", (area.id,))
    db.connection.commit()
    set_birthdate(db, alice, date.today().replace(year=date.today().year - 19))

    with pytest.raises(TransferError):
        resolve(db, grant)


def test_a_name_requirement_is_re_checked(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "named", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    grant = _download_grant(db, grants, alice, area, entry)

    db.connection.execute(
        "UPDATE file_areas SET name_requirement = 'verified' WHERE id = ?", (area.id,)
    )
    db.connection.commit()

    with pytest.raises(TransferError):
        resolve(db, grant)

    verifier = create_user(db, "verifier", password="hunter2", user_level=255)
    attest_name(db, alice, "Alice Example", verifier=verifier)
    assert resolve(db, _download_grant(db, grants, alice, area, entry)).entry is not None


def test_a_file_whose_content_is_gone_is_refused(db, alice, tmp_path):
    from pathlib import Path

    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    Path(entry.storage_path).unlink()

    with pytest.raises(TransferError):
        resolve(db, _download_grant(db, grants, alice, area, entry))


# -- streaming helpers --------------------------------------------------


def test_hash_and_measure_matches_a_stored_upload(db, alice, tmp_path):
    from pathlib import Path

    area = create_file_area(db, "docs", creator=alice)
    payload = b"the same bytes zmodem would have hashed incrementally" * 500
    entry = upload_file(db, area, alice, "game.zip", payload)

    digest, size = hash_and_measure(Path(entry.storage_path), chunk_size=64)

    assert digest == entry.sha256
    assert size == len(payload)


def test_a_reused_account_row_id_does_not_inherit_a_live_grant(db, alice):
    """Codex review: `users.id` is a SQLite rowid, so deleting the
    highest-numbered account lets the next one reuse that number. A
    grant issued to the old account must not follow the id to whoever
    now holds it."""
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    grant = grants.issue(direction=UPLOAD, user=alice, area=area)

    db.connection.execute("UPDATE users SET username = 'someone_else' WHERE id = ?", (alice.id,))
    db.connection.commit()

    with pytest.raises(TransferError):
        resolve(db, grant)


def test_a_grant_names_its_area_by_a_content_addressed_id(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    grant = grants.issue(direction=UPLOAD, user=alice, area=area)

    assert grant.area_id == area.area_id
    assert grant.area_id != area.id  # not the reusable rowid


def test_a_moderator_may_redeem_a_link_for_a_pending_file(db, alice):
    """The terminal lookup authorises a pending file for its uploader or
    an APPROVE holder; redemption has to agree, or a moderator who asked
    for the file by name gets a link that always refuses (Codex
    review)."""
    grants = TransferGrants()
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    area = create_file_area(db, "docs", creator=sysop, moderated=True)
    grant_permissions(
        db, sysop, object_type="file_area", object_id=area.id,
        permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    entry = upload_file(db, area, alice, "game.zip", b"payload")

    resolved = resolve(
        db, grants.issue(direction=DOWNLOAD, user=sysop, area=area, file_id=entry.file_id)
    )
    assert resolved.entry.file_id == entry.file_id

    # ... and an ordinary caller still cannot.
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    with pytest.raises(TransferError):
        resolve(db, grants.issue(direction=DOWNLOAD, user=bob, area=area, file_id=entry.file_id))


def test_peeking_does_not_spend_a_grant(db, alice):
    grants = TransferGrants()
    area = create_file_area(db, "docs", creator=alice)
    grant = grants.issue(direction=UPLOAD, user=alice, area=area)

    assert grants.peek(grant.token) is not None
    assert grants.peek(grant.token) is not None
    assert grants.redeem(grant.token) is not None
    assert grants.peek(grant.token) is None
