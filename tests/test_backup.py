"""
Tests for netbbs.backup (design doc §13.4, issue #60's first
operational slice): create_backup/restore_backup capturing and
restoring all five recoverable-state artifacts, the ordering/safety
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
    get_last_backup_summary,
    main,
    remove_pid_file,
    restore_backup,
    write_pid_file,
)
from netbbs import backup as backup_module
from netbbs.link.node_identity import NodeIdentity, bootstrap_node_identity
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


def _welcome_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_welcome_banner.ans"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "netbbs.db"
    Database(path).close()
    return path


@pytest.fixture
def identity_dir(tmp_path):
    return tmp_path / "netbbs_identity"


def _seed_full_node(db_path, identity_dir) -> NodeIdentity:
    """Populate every one of the five backup artifacts with
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
    _welcome_banner_path(db_path).write_text("fake banner")

    return identity


# -- create_backup --------------------------------------------------------


def test_create_backup_captures_all_five_artifacts(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    assert (destination / "netbbs.db").exists()
    assert (destination / "files" / _BLOB_HASH[:2] / _BLOB_HASH).read_bytes() == b"blob content"
    assert (destination / "identity" / "root.identity").exists()
    assert (destination / "identity" / "transitions.json").exists()
    assert (destination / f"{db_path.stem}_ssh_host_key").read_bytes() == b"fake ssh host key"
    assert (destination / f"{db_path.stem}_welcome_banner.ans").read_text() == "fake banner"
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
    assert isinstance(manifest["db_user_version"], int)
    assert manifest["netbbs_version"]
    assert manifest["created_at"]


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
    welcome banner customized, or (implausibly, but not this module's
    job to assume otherwise) has no identity directory yet should still
    back up cleanly -- every artifact past the database is optional."""
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

    # Simulate data loss: wipe every one of the five artifacts.
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM node_config")
    conn.commit()
    conn.close()

    shutil.rmtree(_storage_root(db_path))
    shutil.rmtree(identity_dir)
    _ssh_host_key_path(db_path).unlink()
    _welcome_banner_path(db_path).unlink()

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    assert (_storage_root(db_path) / _BLOB_HASH[:2] / _BLOB_HASH).read_bytes() == b"blob content"
    assert (identity_dir / "root.identity").exists()
    assert _ssh_host_key_path(db_path).read_bytes() == b"fake ssh host key"
    assert _welcome_banner_path(db_path).read_text() == "fake banner"
    conn = sqlite3.connect(str(db_path))
    marker = conn.execute("SELECT value FROM node_config WHERE key = 'marker'").fetchone()
    conn.close()
    assert marker == ("present-before-backup",)


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
