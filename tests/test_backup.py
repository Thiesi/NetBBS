"""
Tests for netbbs.backup (design doc §13.4, issue #60's first
operational slice): create_backup/restore_backup capturing and
restoring all fourteen recoverable-state artifacts, the ordering/safety
invariants around them, and the `python -m netbbs.backup` CLI.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3

import pytest

from netbbs.backup import (
    BackupError,
    create_backup,
    default_backup_destination,
    get_last_backup_summary,
    main,
    remove_pid_file,
    restore_backup,
    write_pid_file,
)
from netbbs import backup as backup_module
from netbbs.link.node_identity import NodeIdentity, bootstrap_node_identity
from netbbs.managed_dns.state import (
    RegistrationStatus,
    get_previous_name,
    get_registered_name,
    get_registration_status,
    set_cancelled_rename_state,
    set_pending_rename_state,
)
from netbbs.storage.database import Database

_BLOB_CONTENT = b"blob content"
# A real content-addressed store always names a blob after its own
# sha256 (netbbs.files.storage) -- issue #75's restore validation now
# actually checks this, so a fixture with a fabricated, non-matching
# hash (this test's own pre-issue-#75 shape) would be correctly
# rejected as "corrupt."
_BLOB_HASH = hashlib.sha256(_BLOB_CONTENT).hexdigest()


def _storage_root(db_path):
    return db_path.parent / f"{db_path.stem}_files"


def _ssh_host_key_path(db_path):
    return db_path.parent / f"{db_path.stem}_ssh_host_key"


def _managed_dns_credential_path(db_path):
    return db_path.parent / f"{db_path.stem}_managed_dns_credential"


def _managed_dns_previous_credential_path(db_path):
    return db_path.parent / f"{db_path.stem}_managed_dns_previous_credential"


def _managed_dns_transition_credential_path(db_path):
    return db_path.parent / f"{db_path.stem}_managed_dns_credential_transition"


def _welcome_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_welcome_banner.ans"


def _main_menu_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_main_menu_banner.ans"


def _logoff_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_logoff_banner.ans"


def _new_account_banner_before_path(db_path):
    return db_path.parent / f"{db_path.stem}_new_account_banner_before.ans"


def _new_account_banner_after_path(db_path):
    return db_path.parent / f"{db_path.stem}_new_account_banner_after.ans"


def _board_list_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_board_list_banner.ans"


def _file_area_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_file_area_banner.ans"


def _chat_channel_picker_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_chat_channel_picker_banner.ans"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "netbbs.db"
    Database(path).close()
    return path


@pytest.fixture
def identity_dir(tmp_path):
    return tmp_path / "netbbs_identity"


def _seed_full_node(db_path, identity_dir) -> NodeIdentity:
    """Populate the ordinary backup artifacts with
    distinguishable content, including the transient `.incoming`
    staging file that must never survive into a backup."""
    blob_path = _storage_root(db_path) / _BLOB_HASH[:2] / _BLOB_HASH
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(_BLOB_CONTENT)

    incoming_path = _storage_root(db_path) / ".incoming" / "partial-upload"
    incoming_path.parent.mkdir(parents=True)
    incoming_path.write_bytes(b"should never be backed up")

    identity = bootstrap_node_identity("test-node")
    identity.save(identity_dir)

    _ssh_host_key_path(db_path).write_bytes(b"fake ssh host key")
    _managed_dns_credential_path(db_path).write_text("fake managed-dns credential")
    _managed_dns_previous_credential_path(db_path).write_text("fake previous managed-dns credential")
    _welcome_banner_path(db_path).write_text("fake banner")
    _main_menu_banner_path(db_path).write_text("fake masthead")
    _logoff_banner_path(db_path).write_text("fake logoff banner")
    _new_account_banner_before_path(db_path).write_text("fake before banner")
    _new_account_banner_after_path(db_path).write_text("fake after banner")
    _board_list_banner_path(db_path).write_text("fake board masthead")
    _file_area_banner_path(db_path).write_text("fake file area masthead")
    _chat_channel_picker_banner_path(db_path).write_text("fake channel masthead")

    return identity


# -- create_backup --------------------------------------------------------


def test_default_backup_destination_is_timestamped_beside_the_database(tmp_path):
    db_path = tmp_path / "data" / "node.db"

    destination = default_backup_destination(
        db_path, created_at="2026-09-04T12:34:56.123456+00:00"
    )

    assert destination == tmp_path / "data" / "node_backups" / "backup-20260904T123456Z"


def test_default_backup_destination_does_not_reuse_an_existing_directory(tmp_path):
    db_path = tmp_path / "node.db"
    first = default_backup_destination(db_path, created_at="2026-09-04T12:34:56+00:00")
    first.mkdir(parents=True)

    second = default_backup_destination(db_path, created_at="2026-09-04T12:34:56+00:00")

    assert second == first.with_name(first.name + "-2")


def test_create_backup_still_succeeds_if_status_bookkeeping_fails(
    tmp_path, db_path, identity_dir, monkeypatch,
):
    destination = tmp_path / "backup1"

    def fail_status_open(*args, **kwargs):
        raise sqlite3.OperationalError("database busy")

    monkeypatch.setattr(backup_module, "Database", fail_status_open)

    assert create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=destination
    ) == destination
    assert (destination / "manifest.json").exists()


def test_create_backup_captures_a_staged_credential_transition(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    _managed_dns_transition_credential_path(db_path).write_text("staged secrets")

    destination = create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup-transition"
    )

    assert (destination / _managed_dns_transition_credential_path(db_path).name).read_text() == "staged secrets"


def test_create_backup_retries_when_cancellation_overlaps_the_database_and_credential_snapshot(
    tmp_path, db_path, identity_dir, monkeypatch,
):
    live_db = Database(db_path)
    set_pending_rename_state(
        live_db,
        name="new-name",
        previous_name="old-name",
        previous_status=RegistrationStatus.MATURED,
        previous_published=True,
    )
    primary = _managed_dns_credential_path(db_path)
    previous = _managed_dns_previous_credential_path(db_path)
    transition = _managed_dns_transition_credential_path(db_path)
    primary.write_text("replacement-secret")
    previous.write_text("old-secret")
    transition.write_text('{"primary":"replacement-secret","previous":"old-secret"}')

    real_snapshot = backup_module.snapshot_database
    snapshot_calls = 0

    def cancellation_after_first_snapshot(source, target):
        nonlocal snapshot_calls
        snapshot_calls += 1
        real_snapshot(source, target)
        if snapshot_calls == 1:
            set_cancelled_rename_state(
                live_db, name="old-name", status=RegistrationStatus.MATURED, published=True,
            )
            primary.write_text("old-secret")
            previous.unlink()
            transition.unlink()

    monkeypatch.setattr(backup_module, "snapshot_database", cancellation_after_first_snapshot)

    destination = create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup-raced"
    )

    assert snapshot_calls == 2
    backed_up_db = Database(destination / "netbbs.db")
    try:
        assert get_registered_name(backed_up_db) == "old-name"
        assert get_registration_status(backed_up_db) is RegistrationStatus.MATURED
        assert get_previous_name(backed_up_db) is None
    finally:
        backed_up_db.close()
        live_db.close()
    assert (destination / primary.name).read_text() == "old-secret"
    assert not (destination / previous.name).exists()
    assert not (destination / transition.name).exists()


def test_create_backup_captures_all_ordinary_artifacts(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    assert (destination / "netbbs.db").exists()
    assert (destination / "files" / _BLOB_HASH[:2] / _BLOB_HASH).read_bytes() == b"blob content"
    assert (destination / "identity" / "root.identity").exists()
    assert (destination / "identity" / "transitions.json").exists()
    assert (destination / f"{db_path.stem}_ssh_host_key").read_bytes() == b"fake ssh host key"
    assert (destination / f"{db_path.stem}_managed_dns_credential").read_text() == "fake managed-dns credential"
    assert (destination / f"{db_path.stem}_managed_dns_previous_credential").read_text() == "fake previous managed-dns credential"
    assert (destination / f"{db_path.stem}_welcome_banner.ans").read_text() == "fake banner"
    assert (destination / f"{db_path.stem}_main_menu_banner.ans").read_text() == "fake masthead"
    assert (destination / f"{db_path.stem}_logoff_banner.ans").read_text() == "fake logoff banner"
    assert (destination / f"{db_path.stem}_new_account_banner_before.ans").read_text() == "fake before banner"
    assert (destination / f"{db_path.stem}_new_account_banner_after.ans").read_text() == "fake after banner"
    assert (destination / f"{db_path.stem}_board_list_banner.ans").read_text() == "fake board masthead"
    assert (destination / f"{db_path.stem}_file_area_banner.ans").read_text() == "fake file area masthead"
    assert (destination / f"{db_path.stem}_chat_channel_picker_banner.ans").read_text() == "fake channel masthead"
    assert (destination / "manifest.json").exists()


def test_create_backup_excludes_incoming_staging(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    assert not (destination / "files" / ".incoming").exists()


def test_create_backup_writes_a_readable_manifest(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["source_db_path"] == str(db_path)
    assert manifest["source_identity_dir"] == str(identity_dir)
    assert manifest["database_filename"] == db_path.name
    assert isinstance(manifest["db_user_version"], int)
    assert manifest["netbbs_version"]
    assert manifest["created_at"]


def test_backup_preserves_a_custom_database_filename(tmp_path, identity_dir):
    db_path = tmp_path / "dogfood-blue.db"
    Database(db_path).close()
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    assert (destination / db_path.name).exists()
    assert not (destination / "netbbs.db").exists()
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["database_filename"] == db_path.name
    assert db_path.name in manifest["checksums"]

    db_path.unlink()
    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)
    assert db_path.exists()


def test_restore_accepts_legacy_backup_without_database_filename(tmp_path, db_path, identity_dir):
    destination = create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup1"
    )
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["database_filename"]
    manifest_path.write_text(json.dumps(manifest, indent=2))

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_rejects_unsafe_database_filename(tmp_path, db_path, identity_dir):
    destination = create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup1"
    )
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["database_filename"] = "../outside.db"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    with pytest.raises(BackupError, match="invalid database filename"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_create_backup_refuses_if_destination_already_exists(tmp_path, db_path, identity_dir):
    destination = tmp_path / "backup1"
    destination.mkdir()

    with pytest.raises(BackupError, match="already exists"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)


def test_create_backup_refuses_if_database_missing(tmp_path, identity_dir):
    with pytest.raises(BackupError, match="no database found"):
        create_backup(db_path=tmp_path / "missing.db", identity_dir=identity_dir, destination=tmp_path / "backup1")


def test_create_backup_tolerates_no_identity_files_or_extras(tmp_path, db_path, identity_dir):
    """A brand-new node that has never uploaded a file, never had its
    welcome banner or main-menu masthead customized, or (implausibly,
    but not this module's job to assume otherwise) has no identity
    directory yet should still back up cleanly -- every artifact past
    the database is optional."""
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    assert (destination / "netbbs.db").exists()
    assert not (destination / "files").exists()
    assert not (destination / "identity").exists()


def test_create_backup_records_last_backup_state(tmp_path, db_path, identity_dir):
    destination = tmp_path / "backup1"
    assert get_last_backup_summary(Database(db_path)) == (None, None)

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    checked_at, path = get_last_backup_summary(Database(db_path))
    assert checked_at is not None
    assert path == str(destination)


def test_create_backup_rolls_back_both_summary_fields_if_either_write_fails(
    tmp_path, db_path, identity_dir,
):
    db = Database(db_path)
    db.connection.execute(
        """
        CREATE TRIGGER fail_last_backup_path
        BEFORE INSERT ON node_config
        WHEN NEW.key = 'last_backup_path'
        BEGIN
            SELECT RAISE(ABORT, 'simulated summary write failure');
        END
        """
    )
    db.connection.commit()
    db.close()

    create_backup(
        db_path=db_path,
        identity_dir=identity_dir,
        destination=tmp_path / "backup1",
    )

    db = Database(db_path)
    try:
        assert get_last_backup_summary(db) == (None, None)
    finally:
        db.close()


def test_create_backup_appends_to_operational_run_history(tmp_path, db_path, identity_dir):
    # Dogfood follow-up: get_last_backup_summary only ever tracks the
    # single most recent point in time -- a SysOp couldn't tell "this
    # runs on a healthy schedule" from "it happened to succeed once".
    from netbbs.operational_history import list_operational_run_history

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup1")
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup2")

    history = list_operational_run_history(Database(db_path), "backup")
    assert [r.outcome for r in history] == ["succeeded", "succeeded"]
    assert history[0].detail == str(tmp_path / "backup2")
    assert history[1].detail == str(tmp_path / "backup1")


# -- restore_backup ---------------------------------------------------------


def test_restore_backup_round_trip(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    # A marker written *before* the snapshot -- proves the database
    # itself round-trips, distinct from create_backup's own last-backup
    # bookkeeping (netbbs.config), which is written to the live node
    # *after* the snapshot is taken and so is never itself present
    # inside the backup it describes.
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO node_config (key, value) VALUES ('marker', 'present-before-backup')")
    conn.commit()
    conn.close()
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    # Simulate data loss: wipe every ordinary artifact.
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM node_config")
    conn.commit()
    conn.close()

    shutil.rmtree(_storage_root(db_path))
    shutil.rmtree(identity_dir)
    _ssh_host_key_path(db_path).unlink()
    _managed_dns_credential_path(db_path).unlink()
    _managed_dns_previous_credential_path(db_path).unlink()
    _welcome_banner_path(db_path).unlink()
    _main_menu_banner_path(db_path).unlink()
    _logoff_banner_path(db_path).unlink()
    _new_account_banner_before_path(db_path).unlink()
    _new_account_banner_after_path(db_path).unlink()
    _board_list_banner_path(db_path).unlink()
    _file_area_banner_path(db_path).unlink()
    _chat_channel_picker_banner_path(db_path).unlink()

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    assert (_storage_root(db_path) / _BLOB_HASH[:2] / _BLOB_HASH).read_bytes() == b"blob content"
    assert (identity_dir / "root.identity").exists()
    assert _ssh_host_key_path(db_path).read_bytes() == b"fake ssh host key"
    assert _managed_dns_credential_path(db_path).read_text() == "fake managed-dns credential"
    assert _managed_dns_previous_credential_path(db_path).read_text() == "fake previous managed-dns credential"
    assert _welcome_banner_path(db_path).read_text() == "fake banner"
    assert _main_menu_banner_path(db_path).read_text() == "fake masthead"
    assert _logoff_banner_path(db_path).read_text() == "fake logoff banner"
    assert _new_account_banner_before_path(db_path).read_text() == "fake before banner"
    assert _new_account_banner_after_path(db_path).read_text() == "fake after banner"
    assert _board_list_banner_path(db_path).read_text() == "fake board masthead"
    assert _file_area_banner_path(db_path).read_text() == "fake file area masthead"
    assert _chat_channel_picker_banner_path(db_path).read_text() == "fake channel masthead"
    conn = sqlite3.connect(str(db_path))
    marker = conn.execute("SELECT value FROM node_config WHERE key = 'marker'").fetchone()
    conn.close()
    assert marker == ("present-before-backup",)


def test_restore_removes_newer_managed_dns_credentials_absent_from_backup(
    tmp_path, db_path, identity_dir,
):
    _seed_full_node(db_path, identity_dir)
    primary = _managed_dns_credential_path(db_path)
    previous = _managed_dns_previous_credential_path(db_path)
    primary.unlink()
    previous.unlink()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup1")
    transition = _managed_dns_transition_credential_path(db_path)
    primary.write_text("post-backup primary secret")
    previous.write_text("post-backup previous secret")
    transition.write_text("post-backup staged secrets")

    rollback = restore_backup(source=source, db_path=db_path, identity_dir=identity_dir)

    assert not primary.exists()
    assert not previous.exists()
    assert not transition.exists()
    assert rollback is not None
    assert (rollback / primary.name).read_text() == "post-backup primary secret"
    assert (rollback / previous.name).read_text() == "post-backup previous secret"
    assert (rollback / transition.name).read_text() == "post-backup staged secrets"


def test_restore_backup_onto_a_fresh_target_with_nothing_existing_yet(tmp_path, db_path, identity_dir):
    """Restoring into a brand-new location -- no prior files/identity
    directory at all -- must not assume there's anything there to
    remove first."""
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    fresh_db_path = tmp_path / "restored" / "netbbs.db"
    fresh_identity_dir = tmp_path / "restored_identity"
    fresh_db_path.parent.mkdir()

    restore_backup(source=destination, db_path=fresh_db_path, identity_dir=fresh_identity_dir)

    assert fresh_db_path.exists()
    assert (_storage_root(fresh_db_path) / _BLOB_HASH[:2] / _BLOB_HASH).exists()
    assert (fresh_identity_dir / "root.identity").exists()


def test_restore_backup_refuses_without_a_manifest(tmp_path, db_path, identity_dir):
    not_a_backup = tmp_path / "not-a-backup"
    not_a_backup.mkdir()

    with pytest.raises(BackupError, match="not a backup directory"):
        restore_backup(source=not_a_backup, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_refuses_if_the_target_database_is_in_use(tmp_path, db_path, identity_dir):
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    holder = sqlite3.connect(str(db_path), timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(BackupError, match="appears to be in use"):
            restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_restore_backup_succeeds_once_the_holder_releases_the_lock(tmp_path, db_path, identity_dir):
    """Confirms the precondition check isn't just permanently tripped
    by the backup/restore process's own prior connections -- it
    reflects real, current lock state."""
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    holder = sqlite3.connect(str(db_path), timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("ROLLBACK")
    holder.close()

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)  # must not raise


# -- staged/validated restore (design doc §13.10, issue #75) ----------------


def test_restore_backup_validates_before_touching_any_live_path(tmp_path, db_path, identity_dir):
    """A corrupt backup must be refused before a single live artifact
    is overwritten -- not partway through, and not after."""
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    # Corrupt the database snapshot after the fact -- a truncated file,
    # not a well-formed-but-tampered one, so PRAGMA integrity_check
    # itself (not just the checksum) has something real to catch too.
    (destination / "netbbs.db").write_bytes(b"not a real sqlite file")

    live_db_bytes_before = db_path.read_bytes()

    with pytest.raises(BackupError):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    assert db_path.read_bytes() == live_db_bytes_before  # untouched


def test_restore_backup_refuses_on_checksum_mismatch(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    # Tamper with the SSH host key after the manifest recorded its
    # checksum -- a well-formed file, just not the one the manifest
    # says it should be.
    (destination / "netbbs_ssh_host_key").write_bytes(b"tampered")

    with pytest.raises(BackupError, match="checksum mismatch"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_refuses_on_a_corrupted_blob(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    blob_in_backup = destination / "files" / _BLOB_HASH[:2] / _BLOB_HASH
    blob_in_backup.write_bytes(b"corrupted content, wrong hash now")

    with pytest.raises(BackupError, match="does not match its own content hash"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_refuses_on_missing_checksummed_file(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    (destination / "identity" / "signing.identity").unlink()

    with pytest.raises(BackupError, match="missing"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_refuses_if_identity_does_not_load_cleanly(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    # Swap the root identity file's bytes for the signing key's --
    # still a file that "exists" and (if checksums didn't already catch
    # it) wouldn't parse/verify as the right key, so this also proves
    # the identity check is a real functional load, not just presence.
    # Recompute the checksum too, isolating this test to the identity-
    # load check specifically rather than tripping the checksum check
    # first.
    swapped = (destination / "identity" / "signing.identity").read_bytes()
    (destination / "identity" / "root.identity").write_bytes(swapped)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["checksums"]["identity/root.identity"] = hashlib.sha256(swapped).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2))

    with pytest.raises(BackupError, match="node identity does not load cleanly"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_refuses_a_snapshot_from_a_newer_schema_version(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    db_snapshot = destination / "netbbs.db"
    conn = sqlite3.connect(str(db_snapshot))
    conn.execute("PRAGMA user_version = 999999")
    conn.commit()
    conn.close()
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["checksums"]["netbbs.db"] = backup_module._sha256_of_file(db_snapshot)
    manifest_path.write_text(json.dumps(manifest, indent=2))

    with pytest.raises(BackupError, match="newer than this NetBBS build supports"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_source_directory_is_never_mutated_by_validation(tmp_path, db_path, identity_dir):
    """A backup must stay byte-identical across repeated restores --
    validating it must never itself apply a schema migration to the
    original snapshot (only to the disposable staged copy)."""
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    db_snapshot_bytes_before = (destination / "netbbs.db").read_bytes()

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    assert (destination / "netbbs.db").read_bytes() == db_snapshot_bytes_before


def test_restore_backup_does_not_delete_the_rollback_generation(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO node_config (key, value) VALUES ('marker', 'pre-restore-generation')")
    conn.commit()
    conn.close()

    rollback_dir = restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    assert rollback_dir is not None
    assert rollback_dir.exists()
    conn = sqlite3.connect(str(rollback_dir / "db"))
    marker = conn.execute("SELECT value FROM node_config WHERE key = 'marker'").fetchone()
    conn.close()
    assert marker == ("pre-restore-generation",)


def test_restore_backup_returns_none_when_nothing_was_live_to_preserve(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    fresh_db_path = tmp_path / "restored" / "netbbs.db"
    fresh_identity_dir = tmp_path / "restored_identity"
    fresh_db_path.parent.mkdir()

    rollback_dir = restore_backup(source=destination, db_path=fresh_db_path, identity_dir=fresh_identity_dir)

    assert rollback_dir is None


def test_restore_rebases_managed_dns_credential_to_a_different_database_stem(
    tmp_path, db_path, identity_dir,
):
    _seed_full_node(db_path, identity_dir)
    source = create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "portable-backup"
    )
    target_db = tmp_path / "restored" / "renamed.db"
    target_identity = tmp_path / "restored-identity"

    restore_backup(source=source, db_path=target_db, identity_dir=target_identity)

    assert _managed_dns_credential_path(target_db).read_text() == "fake managed-dns credential"
    assert _managed_dns_previous_credential_path(target_db).read_text() == "fake previous managed-dns credential"
    assert not (target_db.parent / f"{db_path.stem}_managed_dns_credential").exists()
    assert not (target_db.parent / f"{db_path.stem}_managed_dns_previous_credential").exists()


def test_restore_backup_no_staging_or_state_files_left_behind_on_success(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    remaining = {p.name for p in db_path.parent.iterdir()}
    assert not any(name.startswith(".netbbs-restore-staging-") for name in remaining)
    assert ".netbbs-restore-state.json" not in remaining


def test_restore_backup_recovers_the_previous_generation_when_a_switch_step_fails(
    tmp_path, db_path, identity_dir, monkeypatch
):
    """Simulates an interruption partway through the switch phase (the
    third artifact fails) and confirms everything already switched is
    rolled back automatically -- the live node ends up exactly as it
    was before the restore was attempted, not a mixture."""
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO node_config (key, value) VALUES ('marker', 'original-before-failed-restore')")
    conn.commit()
    conn.close()
    original_identity_root_bytes = (identity_dir / "root.identity").read_bytes()

    real_switch_one = backup_module._switch_one
    call_count = 0

    def _flaky_switch_one(name, staged_path, live_path, rollback_dir):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise OSError("simulated interruption")
        real_switch_one(name, staged_path, live_path, rollback_dir)

    monkeypatch.setattr(backup_module, "_switch_one", _flaky_switch_one)

    with pytest.raises(BackupError, match="automatically rolled back"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    # The live node is back to exactly its pre-restore state.
    conn = sqlite3.connect(str(db_path))
    marker = conn.execute("SELECT value FROM node_config WHERE key = 'marker'").fetchone()
    conn.close()
    assert marker == ("original-before-failed-restore",)
    assert (identity_dir / "root.identity").read_bytes() == original_identity_root_bytes

    # No leftover state file -- the rollback fully recovered, so the
    # marker is cleared, not left as a stuck "restore in progress" sign.
    assert not (db_path.parent / ".netbbs-restore-state.json").exists()


def test_restore_backup_refuses_a_second_restore_over_an_unresolved_state_file(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    state_path = db_path.parent / ".netbbs-restore-state.json"
    state_path.write_text(json.dumps({"started_at": "2026-01-01T00:00:00Z", "pending_artifacts": ["db"]}))

    with pytest.raises(BackupError, match="did not complete cleanly"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


# -- PID-file liveness check (design doc §13.10, issue #75) -----------------


def test_restore_backup_refuses_while_the_pid_file_names_a_live_process(tmp_path, db_path, identity_dir):
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    write_pid_file(db_path)  # writes this test process's own PID -- genuinely alive
    try:
        with pytest.raises(BackupError, match="appears to still be running"):
            restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)
    finally:
        remove_pid_file(db_path)


def test_restore_backup_tolerates_a_stale_pid_file(tmp_path, db_path, identity_dir):
    """A PID file naming a process that no longer exists (crash, kill
    -9, power loss -- anything that skipped the normal remove_pid_file
    cleanup) must not permanently block restore."""
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    # An implausibly large PID essentially guaranteed not to be a real,
    # currently-running process on any platform this runs on.
    (db_path.parent / f"{db_path.stem}.pid").write_text("999999999")

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)  # must not raise


def test_write_and_remove_pid_file_round_trip(tmp_path, db_path):
    pid_path = db_path.parent / f"{db_path.stem}.pid"
    assert not pid_path.exists()

    write_pid_file(db_path)
    assert pid_path.exists()

    remove_pid_file(db_path)
    assert not pid_path.exists()

    remove_pid_file(db_path)  # must not raise if already gone


# -- CLI ----------------------------------------------------------------


def test_cli_create_then_restore_round_trip(tmp_path, capsys):
    db_path = tmp_path / "netbbs.db"
    identity_dir = tmp_path / "netbbs_identity"
    Database(db_path).close()
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"

    main(["create", "--db", str(db_path), "--identity-dir", str(identity_dir), "--to", str(destination)])
    assert "Backup created" in capsys.readouterr().out
    assert (destination / "manifest.json").exists()

    main(["restore", "--from", str(destination), "--db", str(db_path), "--identity-dir", str(identity_dir)])
    assert "Restored" in capsys.readouterr().out


def test_cli_create_exits_cleanly_on_failure(tmp_path, capsys):
    with pytest.raises(SystemExit, match="backup failed"):
        main(["create", "--db", str(tmp_path / "missing.db"), "--to", str(tmp_path / "backup1")])
