"""
Tests for netbbs.backup (design doc §13.4, issue #60's first
operational slice): create_backup/restore_backup capturing and
restoring all five recoverable-state artifacts, the ordering/safety
invariants around them, and the `python -m netbbs.backup` CLI.
"""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from netbbs.backup import (
    BackupError,
    create_backup,
    get_last_backup_summary,
    main,
    restore_backup,
)
from netbbs.link.node_identity import NodeIdentity, bootstrap_node_identity
from netbbs.storage.database import Database

_BLOB_HASH = "ab" + "0" * 62


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
    blob_path.write_bytes(b"blob content")

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
