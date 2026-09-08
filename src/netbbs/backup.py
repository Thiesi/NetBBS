"""
Node backup and restore (design doc §13.4/§13.10, issue #60's first
operational slice, hardened by issue #75).

A node's recoverable state is fourteen `db_path`-relative artifacts, not
just the database: content blobs (`netbbs.files.storage`), node
identity (`netbbs.link.node_identity`), the SSH host key
(`netbbs.net.ssh`), the managed-DNS registration credential
(`netbbs.managed_dns.credential`, design doc §16 Decision 7, issue
#201), the welcome banner (`netbbs.net.welcome_banner`), the main-menu
masthead (`netbbs.net.main_menu_banner`, issue #161), the logoff banner
(`netbbs.net.logoff_banner`, issue #177), the two new-account banners
(`netbbs.net.new_account_banner_before`/`_after`, issue #177), and the
three submenu mastheads (`netbbs.net.board_list_banner`/`file_area_
banner`/`chat_channel_picker_banner`, issue #176) all live at derived
paths alongside the database, each with no independent config field of
its own. A backup covering only the database silently loses the SSH
host key (every client gets a MITM warning after restore) and, far more
seriously, the Link node identity -- root-key custody is explicitly
"part of ordinary node backup and restore" (design doc §4.5), not a
separate ceremony. This module treats all fourteen as one atomic backup
operation, never a DB-only one.

When its save directory exists, Voidrunner adds a checksummed component
containing careers, recovery copies, and scores. Capture it while game
sessions are closed, before the node database snapshot, so its user IDs
cannot be newer than that snapshot. Restore requires an explicit game
destination and stages that component on the destination filesystem.

Deliberately path-based, not `Database`-based: a backup must be safely
takeable against a live, running node, and opening a second `Database`
handle (migration-check side effects, a second long-lived WAL-mode
connection) is unnecessary work this module has no need for -- it only
ever needs `sqlite3.Connection.backup()` (via
`netbbs.selfupdate.snapshot_database`, reused rather than reinvented)
and plain filesystem copies. `_validate_backup_source`'s DB check is
the one deliberate exception -- opening a real `Database` there is the
point, see that function's own docstring.

Ordering is load-bearing, not just convention: the database snapshot is
always taken *before* the content blobs are copied. `netbbs.files.
entries` only ever creates a `files` row after its bytes are already
durably written to storage -- so every blob a given DB snapshot's rows
reference was already on disk before that snapshot was even taken.
Copying blobs afterward is guaranteed to include all of them, plus
possibly a few newer, still-unreferenced ones from uploads that landed
in between (harmless -- an orphaned blob `netbbs.files.gc` could still
reclaim, never a dangling reference). Reversing the order would risk
the opposite, genuinely broken case: a DB snapshot referencing a blob
the copy hadn't reached yet.

The live SysOp Backup screen and the standalone `python -m netbbs.backup
create` CLI both call `create_backup`.  The screen chooses a fresh,
timestamped destination under `<db-stem>_backups/` beside the database;
the CLI remains the path-selectable, cron-schedulable entry point.  Restore
stays CLI-only because it must replace node state while the node is stopped.
There is still no built-in scheduler: recurring backups remain an external
operator/cron responsibility.

**Restore (design doc §13.10, issue #75) validates everything before
touching a live path, stages a full copy, then switches via atomic
renames** -- never restores by copying directly onto a live path.
Interrupting a restore leaves either the previous generation (rolled
back automatically, best-effort) or a state file at `db_path.parent /
".netbbs-restore-state.json"` naming exactly what's where, never a
silent mixture. See `restore_backup`'s own docstring for the full
sequence.

**Explicitly deferred, not part of this slice**: encrypting backup
contents at rest (identity material is already unencrypted-by-default
on a live node -- see §4.5 -- and this tool preserves whatever it finds
rather than changing that policy); off-site/remote transport of a
completed backup directory; retention/rotation of old backups (this
now includes the rollback generation a successful restore leaves
behind -- an explicit operator/cron cleanup step, same boundary); and
any form of automatic scheduling.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from netbbs import __version__
from netbbs.config import get_config
from netbbs.link.node_identity import NodeIdentity, NodeIdentityError
from netbbs.managed_dns.credential import (
    credential_path_for as _managed_dns_credential_path_for,
    previous_credential_path_for as _managed_dns_previous_credential_path_for,
    save_credential as _save_managed_dns_credential,
    transition_credential_path_for as _managed_dns_transition_credential_path_for,
)
from netbbs.operational_history import record_operational_run
from netbbs.rendering.reflow import print_wrapped, terminal_wrapped
from netbbs.selfupdate import snapshot_database
from netbbs.storage.database import Database
from netbbs.storage.migrations import MIGRATIONS
from netbbs.timeutil import utc_now_iso

_MANIFEST_FILENAME = "manifest.json"
_LEGACY_DB_FILENAME = "netbbs.db"
_FILES_DIRNAME = "files"
_IDENTITY_DIRNAME = "identity"
_VOIDRUNNER_DIRNAME = "voidrunner"
_VOIDRUNNER_MAX_FILES = 10_000
_VOIDRUNNER_MAX_FILE_BYTES = 4 * 1024 * 1024
_VOIDRUNNER_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_RESERVED_BACKUP_ENTRIES = (_MANIFEST_FILENAME, _FILES_DIRNAME, _IDENTITY_DIRNAME, _VOIDRUNNER_DIRNAME)

# node_config keys (netbbs.config's generic key-value store) -- same
# reasoning as netbbs.selfupdate's own last-check bookkeeping: purely
# for the SysOp Backup screen, never required for restore.
_LAST_BACKUP_AT_CONFIG_KEY = "last_backup_at"
_LAST_BACKUP_PATH_CONFIG_KEY = "last_backup_path"

_RESTORE_STAGING_PREFIX = ".netbbs-restore-staging-"
_RESTORE_ROLLBACK_PREFIX = ".netbbs-restore-rollback-"
_RESTORE_STATE_FILENAME = ".netbbs-restore-state.json"
_MANAGED_DNS_SNAPSHOT_ATTEMPTS = 3


class BackupError(Exception):
    """Raised for any backup/restore failure."""


def _validate_database_filename(filename: object) -> str:
    """Return a safe, backup-root-relative database filename.

    The value is persisted in the manifest and therefore untrusted on
    restore. Keep it to one ordinary filename so it cannot escape the
    backup directory or collide with another reserved top-level artifact.
    """
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or filename in _RESERVED_BACKUP_ENTRIES
    ):
        raise BackupError(f"invalid database filename in backup: {filename!r}")
    return filename


def _database_filename_from_manifest(manifest: dict) -> str:
    # Backups created before custom database filenames were preserved always
    # stored their snapshot under this legacy canonical name.
    return _validate_database_filename(manifest.get("database_filename", _LEGACY_DB_FILENAME))


def default_backup_destination(db_path: Path, *, created_at: str | None = None) -> Path:
    """Return a fresh, human-readable destination for an in-session backup.

    Managed backups live beside the database under ``<db-stem>_backups``.
    A numeric suffix avoids reusing an existing directory when two backups
    share a timestamp; :func:`create_backup` remains the final atomic guard
    against a concurrent creator winning the same path.
    """
    raw_created_at = (created_at or utc_now_iso()).replace("Z", "+00:00")
    instant = datetime.fromisoformat(raw_created_at)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    timestamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = db_path.parent / f"{db_path.stem}_backups"
    base = root / f"backup-{timestamp}"
    destination = base
    suffix = 2
    while destination.exists():
        destination = root / f"{base.name}-{suffix}"
        suffix += 1
    return destination


def _storage_root_for(db_path: Path) -> Path:
    """Mirrors `netbbs.files.storage.storage_root`'s own one-line
    formula, duplicated rather than imported: that function takes a
    live `Database`, and this module deliberately never opens one for
    the reason its own docstring gives."""
    return db_path.parent / f"{db_path.stem}_files"


def _ssh_host_key_path_for(db_path: Path) -> Path:
    """Mirrors `netbbs.net.ssh.ensure_host_key`'s own derived path."""
    return db_path.parent / f"{db_path.stem}_ssh_host_key"


def _welcome_banner_path_for(db_path: Path) -> Path:
    """Mirrors `netbbs.net.welcome_banner.banner_path`'s own derived
    path."""
    return db_path.parent / f"{db_path.stem}_welcome_banner.ans"


def _main_menu_banner_path_for(db_path: Path) -> Path:
    """Mirrors `netbbs.net.main_menu_banner.main_menu_banner_path`'s
    own derived path (issue #161)."""
    return db_path.parent / f"{db_path.stem}_main_menu_banner.ans"


def _logoff_banner_path_for(db_path: Path) -> Path:
    """Mirrors `netbbs.net.logoff_banner.logoff_banner_path`'s own
    derived path (issue #177)."""
    return db_path.parent / f"{db_path.stem}_logoff_banner.ans"


def _new_account_banner_before_path_for(db_path: Path) -> Path:
    """Mirrors `netbbs.net.new_account_banner_before.
    new_account_banner_before_path`'s own derived path (issue #177)."""
    return db_path.parent / f"{db_path.stem}_new_account_banner_before.ans"


def _new_account_banner_after_path_for(db_path: Path) -> Path:
    """Mirrors `netbbs.net.new_account_banner_after.
    new_account_banner_after_path`'s own derived path (issue #177)."""
    return db_path.parent / f"{db_path.stem}_new_account_banner_after.ans"


def _board_list_banner_path_for(db_path: Path) -> Path:
    """Mirrors `netbbs.net.board_list_banner.board_list_banner_path`'s
    own derived path (issue #176)."""
    return db_path.parent / f"{db_path.stem}_board_list_banner.ans"


def _file_area_banner_path_for(db_path: Path) -> Path:
    """Mirrors `netbbs.net.file_area_banner.file_area_banner_path`'s
    own derived path (issue #176)."""
    return db_path.parent / f"{db_path.stem}_file_area_banner.ans"


def _chat_channel_picker_banner_path_for(db_path: Path) -> Path:
    """Mirrors `netbbs.net.chat_channel_picker_banner.
    chat_channel_picker_banner_path`'s own derived path (issue #176)."""
    return db_path.parent / f"{db_path.stem}_chat_channel_picker_banner.ans"


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_content_addressed_name(name: str) -> bool:
    """Whether `name` looks like one of `netbbs.files.storage`'s own
    sha256 blob filenames -- the identical shape `netbbs.files.gc.
    _is_content_addressed_name` already checks, duplicated rather than
    imported for the same "this module stays path-based, not reaching
    into a domain module" reasoning the rest of this file already
    follows. Anything else found under a `files/` tree is left alone,
    not treated as a corruption finding -- unexpected content GC itself
    already handles conservatively, not this module's job to judge."""
    return len(name) == 64 and all(c in "0123456789abcdef" for c in name)


def _read_optional_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _managed_dns_credential_generation(db_path: Path) -> tuple[bytes | None, ...]:
    """One ordered view of all mutable managed-DNS credential artifacts."""
    return tuple(_read_optional_file(path) for path in (
        _managed_dns_credential_path_for(db_path),
        _managed_dns_previous_credential_path_for(db_path),
        _managed_dns_transition_credential_path_for(db_path),
    ))


def _managed_dns_config_generation(db_path: Path) -> tuple[tuple[str, str], ...]:
    """The managed-DNS configuration stored in one SQLite generation."""
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return tuple(connection.execute(
            "SELECT key, value FROM node_config WHERE key GLOB 'managed_dns_*' ORDER BY key"
        ))
    finally:
        connection.close()


def _snapshot_database_and_managed_dns_credentials(
    db_path: Path, destination: Path, database_filename: str,
) -> None:
    """Capture the database and its three mutable DNS secrets coherently.

    Credential swaps are journaled and each individual file write is atomic,
    but the database and three files cannot be replaced in one filesystem
    transaction. Double-collect both sides around SQLite's online snapshot and
    retry whenever a transition overlaps the collection window.
    """
    staged_snapshot = destination / f".{database_filename}.managed-dns-snapshot"
    credential_paths = (
        _managed_dns_credential_path_for(db_path),
        _managed_dns_previous_credential_path_for(db_path),
        _managed_dns_transition_credential_path_for(db_path),
    )
    for _attempt in range(_MANAGED_DNS_SNAPSHOT_ATTEMPTS):
        before_credentials = _managed_dns_credential_generation(db_path)
        staged_snapshot.unlink(missing_ok=True)
        snapshot_database(db_path, staged_snapshot)
        captured_credentials = _managed_dns_credential_generation(db_path)
        snapshot_config = _managed_dns_config_generation(staged_snapshot)
        live_config = _managed_dns_config_generation(db_path)
        after_credentials = _managed_dns_credential_generation(db_path)
        if (
            before_credentials == captured_credentials == after_credentials
            and snapshot_config == live_config
        ):
            staged_snapshot.replace(destination / database_filename)
            for source_path, contents in zip(credential_paths, captured_credentials, strict=True):
                if contents is not None:
                    _save_managed_dns_credential(
                        destination / source_path.name, contents.decode("utf-8")
                    )
            return
    staged_snapshot.unlink(missing_ok=True)
    raise BackupError(
        "managed-DNS state kept changing while the backup was captured; retry shortly"
    )


def voidrunner_save_directory() -> Path:
    from netbbs.doors.bundled.voidrunner import _default_save_dir
    return _default_save_dir().resolve()


@contextlib.contextmanager
def _voidrunner_maintenance(directory: Path):
    from netbbs.doors.bundled.voidrunner import PilotBusy, maintenance_session
    try:
        with maintenance_session(directory):
            yield
    except PilotBusy as exc:
        raise BackupError("Voidrunner is active or undergoing maintenance; close its sessions and retry.") from exc
    except OSError as exc:
        raise BackupError(f"Voidrunner storage operation failed: {exc}") from exc


def _voidrunner_files(root: Path, *, archive: bool = False) -> list[Path]:
    """Only the game's retained files belong in the supported component."""
    result = []
    for entry in root.iterdir():
        if entry.is_symlink():
            raise BackupError(f"Voidrunner data contains a symbolic link: {entry.name}")
        if entry.name == "scores" and entry.is_dir():
            for score in entry.iterdir():
                if score.is_symlink():
                    raise BackupError(f"Voidrunner data contains a symbolic link: {score.name}")
                if not archive and score.is_file() and score.name.startswith(".") and score.name.endswith(".tmp"):
                    continue
                if score.is_symlink() or not score.is_file() or not re.fullmatch(r"[0-9]+\.json", score.name):
                    raise BackupError(f"Unsupported Voidrunner score entry: {score.name}")
                result.append(score.relative_to(root))
                if len(result) > _VOIDRUNNER_MAX_FILES:
                    raise BackupError("Voidrunner backup exceeds its file count limit.")
        elif not archive and entry.is_file() and re.fullmatch(r"\.[0-9]+\.lock|\..+\.tmp", entry.name):
            continue
        elif entry.is_file() and (entry.name == "leaderboard.json" or
                re.fullmatch(r"[0-9]+(?:(?:\.previous|\.recovery-[a-zA-Z0-9_-]+)?\.json|\.corrupt-[0-9]+)", entry.name)):
            result.append(entry.relative_to(root))
        else:
            raise BackupError(f"Unsupported Voidrunner data entry: {entry.name}")
        if len(result) > _VOIDRUNNER_MAX_FILES:
            raise BackupError("Voidrunner backup exceeds 10000 files.")
    names = [path.as_posix().casefold() for path in result]
    if len(set(names)) != len(names):
        raise BackupError("Voidrunner backup contains case-colliding filenames.")
    return sorted(result, key=lambda path: path.as_posix())


def _capture_voidrunner(directory: Path, destination: Path, checksums: dict) -> dict | None:
    if not directory.exists():
        return None
    if not directory.is_dir():
        raise BackupError("The configured Voidrunner save directory is not a directory.")
    if destination.resolve().is_relative_to(directory.resolve()):
        raise BackupError("A backup destination cannot be inside the Voidrunner save directory.")
    with _voidrunner_maintenance(directory):
        files = _voidrunner_files(directory)
        output = destination / _VOIDRUNNER_DIRNAME
        output.mkdir()
        total = 0
        for relative in files:
            with (directory / relative).open("rb") as handle:
                raw = handle.read(_VOIDRUNNER_MAX_FILE_BYTES + 1)
            total += len(raw)
            if len(raw) > _VOIDRUNNER_MAX_FILE_BYTES or total > _VOIDRUNNER_MAX_TOTAL_BYTES:
                raise BackupError("Voidrunner backup exceeds its file or total size limit.")
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            checksums[f"voidrunner/{relative.as_posix()}"] = hashlib.sha256(raw).hexdigest()
        return {"version": 1, "source_directory": str(directory),
                "files": [relative.as_posix() for relative in files]}


def _validate_voidrunner_component(source: Path, manifest: dict) -> bool:
    metadata = manifest.get("voidrunner")
    root = source / _VOIDRUNNER_DIRNAME
    if metadata is None:
        if root.exists():
            raise BackupError("Voidrunner component has no coverage manifest.")
        return False
    if (not isinstance(metadata, dict) or type(metadata.get("version")) is not int or metadata["version"] != 1
            or not isinstance(metadata.get("files"), list) or not root.is_dir() or root.is_symlink()):
        raise BackupError("Invalid Voidrunner coverage manifest.")
    actual = _voidrunner_files(root, archive=True)
    names = [path.as_posix() for path in actual]
    if metadata["files"] != names:
        raise BackupError("Voidrunner backup files do not match their coverage manifest.")
    total = 0
    for path in actual:
        key = f"voidrunner/{path.as_posix()}"
        size = (root / path).stat().st_size
        total += size
        if size > _VOIDRUNNER_MAX_FILE_BYTES or total > _VOIDRUNNER_MAX_TOTAL_BYTES:
            raise BackupError("Voidrunner backup exceeds its file or total size limit.")
        if key not in manifest.get("checksums", {}):
            raise BackupError(f"Voidrunner backup is missing checksum for {key}.")
    return True


def create_backup(*, db_path: Path, identity_dir: Path, destination: Path,
                  voidrunner_save_dir: Path | None = None) -> Path:
    """
    Create a complete, self-contained backup of one node's recoverable
    state at `destination` (created fresh -- refuses if it already
    exists, rather than silently merging into or overwriting a
    previous backup).

    Safe to run against a live, running node: the database step uses
    SQLite's own online backup API (`netbbs.selfupdate.
    snapshot_database`), the mutable managed-DNS credential set is checked
    against that database generation and retried if it moved, and every
    other artifact is either static once created or already rewritten via
    its own atomic-replace pattern --
    see the module docstring for the accepted exceptions (every banner/
    masthead singleton -- the welcome banner, the main-menu masthead,
    the logoff banner, both new-account banners, and the three submenu
    mastheads -- has no atomicity guarantee on its own writes; a backup
    landing mid-edit could capture a half-written one, purely cosmetic,
    no correctness consequence).

    The manifest's `checksums` (design doc §13.10, issue #75) cover
    every file captured *outside* the content-addressed `files/` tree
    -- the database snapshot, each identity file, the SSH host key, and
    every banner/masthead singleton. The blob tree needs no manifest
    entry at all:
    a blob's own path already *is* its claimed hash
    (`netbbs.files.storage`'s own layout), so restore verifies it by
    recomputing and comparing against the filename, not against
    anything recorded here.

    Returns `destination`.
    """
    if destination.exists():
        raise BackupError(f"backup destination already exists: {destination}")
    if not db_path.exists():
        raise BackupError(f"no database found at {db_path}")

    database_filename = _validate_database_filename(db_path.name)

    game_source = (voidrunner_save_dir or voidrunner_save_directory()).resolve()
    if destination.resolve().is_relative_to(game_source):
        raise BackupError("A backup destination cannot be inside the Voidrunner save directory.")
    destination.mkdir(parents=True)

    checksums = {}
    game_metadata = _capture_voidrunner(game_source, destination, checksums)
    _snapshot_database_and_managed_dns_credentials(db_path, destination, database_filename)
    checksums[database_filename] = _sha256_of_file(destination / database_filename)
    for credential_path in (
        _managed_dns_credential_path_for(db_path),
        _managed_dns_previous_credential_path_for(db_path),
        _managed_dns_transition_credential_path_for(db_path),
    ):
        captured_path = destination / credential_path.name
        if captured_path.exists():
            checksums[captured_path.name] = _sha256_of_file(captured_path)

    storage_root = _storage_root_for(db_path)
    if storage_root.is_dir():
        shutil.copytree(
            storage_root, destination / _FILES_DIRNAME, ignore=shutil.ignore_patterns(".incoming")
        )

    if identity_dir.is_dir():
        shutil.copytree(identity_dir, destination / _IDENTITY_DIRNAME)
        for entry in sorted((destination / _IDENTITY_DIRNAME).iterdir()):
            if entry.is_file():
                checksums[f"{_IDENTITY_DIRNAME}/{entry.name}"] = _sha256_of_file(entry)

    for extra_path in (
        _ssh_host_key_path_for(db_path),
        _welcome_banner_path_for(db_path),
        _main_menu_banner_path_for(db_path),
        _logoff_banner_path_for(db_path),
        _new_account_banner_before_path_for(db_path),
        _new_account_banner_after_path_for(db_path),
        _board_list_banner_path_for(db_path),
        _file_area_banner_path_for(db_path),
        _chat_channel_picker_banner_path_for(db_path),
    ):
        if extra_path.exists():
            shutil.copy2(extra_path, destination / extra_path.name)
            checksums[extra_path.name] = _sha256_of_file(destination / extra_path.name)

    manifest = {
        "created_at": utc_now_iso(),
        "netbbs_version": __version__,
        "db_user_version": _read_user_version(db_path),
        "database_filename": database_filename,
        "source_db_path": str(db_path),
        "source_identity_dir": str(identity_dir),
        "checksums": checksums,
        "voidrunner": game_metadata,
    }
    (destination / _MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))

    _record_backup_state(db_path, destination)
    return destination


def _read_user_version(db_path: Path) -> int:
    connection = sqlite3.connect(str(db_path))
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _record_backup_state(db_path: Path, destination: Path) -> None:
    """Best-effort: opening a full `Database` here (unlike the rest of
    this module) is deliberate, since `netbbs.config`'s key-value
    helpers need one -- but a failure to record this bookkeeping must
    never fail the backup that already genuinely succeeded above."""
    try:
        db = Database(db_path)
        try:
            created_at = utc_now_iso()
            with db.connection:
                db.connection.executemany(
                    """
                    INSERT INTO node_config (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (
                        (_LAST_BACKUP_AT_CONFIG_KEY, created_at),
                        (_LAST_BACKUP_PATH_CONFIG_KEY, str(destination)),
                    ),
                )
            record_operational_run(db, "backup", "succeeded", detail=str(destination))
        finally:
            db.close()
    except Exception:
        return


def get_last_backup_summary(db: Database) -> tuple[str | None, str | None]:
    """`(last_backup_at_iso, last_backup_path)`, either possibly `None`
    if no backup has ever been taken via `create_backup` on this node."""
    return get_config(db, _LAST_BACKUP_AT_CONFIG_KEY), get_config(db, _LAST_BACKUP_PATH_CONFIG_KEY)


def _validate_backup_source(source: Path, *, allow_migrate: bool) -> dict:
    """
    Full validation of a backup directory (design doc §13.10, issue
    #75) -- called *before* any live path is touched, and again
    (pointed at the staging copy, `allow_migrate=True` that time) after
    staging, to catch any corruption the staging copy itself might have
    introduced. Raises `BackupError` describing the specific problem
    found; returns the parsed manifest on success.

    `allow_migrate` controls whether the database snapshot is actually
    opened as a real `netbbs.storage.database.Database` (which applies
    any pending migration and so *mutates* the file) -- `False` for
    `source` itself, since a backup directory must stay byte-identical
    across repeated validation runs (a migrated-in-place snapshot would
    silently invalidate the manifest's own recorded checksum); `True`
    for the disposable staged copy, where migrating it forward as part
    of restore is actively desirable, not just tolerated. Either way,
    `PRAGMA integrity_check` and the schema-version-not-newer-than-this-
    build check (mirroring `Database._apply_migrations`'s own guard,
    without needing a full open to make it) always run.
    """
    manifest_path = source / _MANIFEST_FILENAME
    if not manifest_path.exists():
        raise BackupError(f"not a backup directory (no {_MANIFEST_FILENAME}): {source}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"could not read manifest at {manifest_path}: {exc}") from exc

    database_filename = _database_filename_from_manifest(manifest)
    _validate_voidrunner_component(source, manifest)
    db_snapshot = source / database_filename
    if not db_snapshot.exists():
        raise BackupError(f"backup is missing its database snapshot: {db_snapshot}")

    for relative_name, expected_hash in manifest.get("checksums", {}).items():
        candidate = source / relative_name
        if not candidate.exists():
            raise BackupError(f"backup is missing {relative_name!r}, listed in its own manifest")
        actual_hash = _sha256_of_file(candidate)
        if actual_hash != expected_hash:
            raise BackupError(
                f"checksum mismatch for {relative_name!r}: backup is corrupt or was modified "
                f"(expected {expected_hash}, got {actual_hash})"
            )

    connection = sqlite3.connect(str(db_snapshot))
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise BackupError(f"database snapshot failed PRAGMA integrity_check: {integrity}")
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    if schema_version > len(MIGRATIONS):
        raise BackupError(
            f"database snapshot's schema version ({schema_version}) is newer than this NetBBS "
            f"build supports ({len(MIGRATIONS)}) -- restore it with a matching or newer build"
        )
    if allow_migrate:
        try:
            Database(db_snapshot).close()
        except Exception as exc:
            raise BackupError(f"database snapshot could not be opened: {exc}") from exc

    files_dir = source / _FILES_DIRNAME
    if files_dir.is_dir():
        for path in files_dir.rglob("*"):
            if not path.is_file() or not _is_content_addressed_name(path.name):
                continue
            actual_hash = _sha256_of_file(path)
            if actual_hash != path.name:
                raise BackupError(
                    f"blob {path} does not match its own content hash (expected {path.name}, "
                    f"got {actual_hash}) -- backup is corrupt"
                )

    identity_in_backup = source / _IDENTITY_DIRNAME
    if identity_in_backup.is_dir():
        try:
            NodeIdentity.load(identity_in_backup)
        except NodeIdentityError as exc:
            raise BackupError(f"backed-up node identity does not load cleanly: {exc}") from exc

    return manifest


def _require_not_in_use(db_path: Path) -> None:
    try:
        connection = sqlite3.connect(str(db_path), timeout=0)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        finally:
            connection.close()
    except sqlite3.OperationalError as exc:
        raise BackupError(
            f"refusing to restore over {db_path}: it appears to be in use right now "
            "(could not acquire the database write lock) -- stop the node first"
        ) from exc


def _pid_file_path_for(db_path: Path) -> Path:
    """Mirrors this module's other derived-path helpers. Written by
    `netbbs.__main__` across every exit path (design doc §13.10, issue
    #75); read here to catch an idle-but-running node the transient
    write-lock probe (`_require_not_in_use`) cannot."""
    return db_path.parent / f"{db_path.stem}.pid"


def write_pid_file(db_path: Path) -> None:
    """Called once by `netbbs.__main__` right after its own `Database`
    opens successfully. Overwrites unconditionally -- a stale leftover
    from a previous unclean exit is exactly what this call replaces
    with the truth."""
    _pid_file_path_for(db_path).write_text(str(os.getpid()))


def remove_pid_file(db_path: Path) -> None:
    """Called by `netbbs.__main__` in the same `finally` block that
    already closes its `Database` on every exit path. Missing is not an
    error -- a startup failure before `write_pid_file` ever ran must
    not raise here."""
    _pid_file_path_for(db_path).unlink(missing_ok=True)


def _process_is_running(pid: int) -> bool:
    """Portable, best-effort liveness check. POSIX (design doc §2's
    actual deployment target) uses the standard signal-0 probe; Windows
    (this project's own dev/test environment, not a supported
    deployment target) shells out to `tasklist` rather than adding a
    `psutil` dependency for one narrow check. An undetermined result on
    an unanticipated platform returns `False` (assume not running)
    rather than blocking restore indefinitely on a platform quirk --
    the existing write-lock probe and the documented "stop the node
    first" operator precondition remain the backstops."""
    if pid == os.getpid():
        return True
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, just owned by someone else
        return True
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=5
        )
        return str(pid) in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def _require_node_not_running(db_path: Path) -> None:
    """The primary "is this node's process still alive" check (design
    doc §13.10, issue #75) -- catches the idle-but-running case
    `_require_not_in_use`'s transient lock probe cannot. A PID file
    present but pointing at a dead process is treated as a stale
    leftover from an unclean exit, not a hard refusal -- the same
    "operator responsibility, not a load-bearing distributed lock"
    framing this module already applies to a second instance running
    on an entirely different machine."""
    pid_file = _pid_file_path_for(db_path)
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return
    if _process_is_running(pid):
        raise BackupError(
            f"refusing to restore over {db_path}: a node process (PID {pid}) appears to still "
            f"be running, per {pid_file} -- stop it first"
        )


def _restore_state_path_for(db_path: Path) -> Path:
    return db_path.parent / _RESTORE_STATE_FILENAME


def _refuse_if_restore_in_progress(db_path: Path) -> None:
    state_path = _restore_state_path_for(db_path)
    if state_path.exists():
        raise BackupError(
            f"a previous restore did not complete cleanly -- state recorded at {state_path}; "
            "resolve it manually (check the staging/rollback directories it names) before "
            "starting a new restore"
        )


def _write_restore_state(state_path: Path, *, staging_dir: Path, rollback_dir: Path, pending: list[str],
                         external: dict | None = None) -> None:
    state_path.write_text(
        json.dumps(
            {
                "started_at": utc_now_iso(),
                "staging_dir": str(staging_dir),
                "rollback_dir": str(rollback_dir),
                "pending_artifacts": pending,
                "external_components": external or {},
            },
            indent=2,
        )
    )


def _restore_switch_plan(
    staging_dir: Path, db_path: Path, identity_dir: Path, database_filename: str,
) -> list[tuple[str, Path | None, Path]]:
    """`(name, staged_path, live_path)` for every artifact this backup
    actually contains, in the same DB/files/identity/extras order
    `create_backup` captures them (design doc §13.4's own ordering
    reasoning) -- SSH host key, welcome banner, and forward-compatibly
    anything a future `create_backup` adds restore generically, keeping
    their filename, exactly like the pre-issue-#75 restore logic did."""
    plan: list[tuple[str, Path, Path]] = [("db", staging_dir / database_filename, db_path)]

    staged_files = staging_dir / _FILES_DIRNAME
    if staged_files.is_dir():
        plan.append(("files", staged_files, _storage_root_for(db_path)))

    staged_identity = staging_dir / _IDENTITY_DIRNAME
    if staged_identity.is_dir():
        plan.append(("identity", staged_identity, identity_dir))

    for entry in sorted(staging_dir.iterdir()):
        if entry.name in (*_RESERVED_BACKUP_ENTRIES, database_filename):
            continue
        if entry.is_file():
            live_path = db_path.parent / entry.name
            if entry.name.endswith("_managed_dns_credential"):
                live_path = _managed_dns_credential_path_for(db_path)
            elif entry.name.endswith("_managed_dns_previous_credential"):
                live_path = _managed_dns_previous_credential_path_for(db_path)
            elif entry.name.endswith("_managed_dns_credential_transition"):
                live_path = _managed_dns_transition_credential_path_for(db_path)
            plan.append((entry.name, entry, live_path))

    for credential_path in (
        _managed_dns_credential_path_for(db_path),
        _managed_dns_previous_credential_path_for(db_path),
        _managed_dns_transition_credential_path_for(db_path),
    ):
        if not any(live_path == credential_path for _, _, live_path in plan):
            # Point-in-time restore must also restore absence: no newer
            # credential-generation artifact may survive over the snapshot.
            plan.append((credential_path.name, None, credential_path))

    return plan


class _SwitchRollbackError(BackupError):
    """The failing artifact could not be put back; retain the restore journal."""


def _switch_one(name: str, staged_path: Path | None, live_path: Path, rollback_dir: Path) -> None:
    """Roll back even the artifact whose second rename failed."""
    moved = False
    if live_path.exists():
        rollback_dir.mkdir(parents=True, exist_ok=True)
        live_path.rename(rollback_dir / name)
        moved = True
    try:
        if staged_path is not None:
            staged_path.rename(live_path)
    except Exception:
        if moved:
            try:
                (rollback_dir / name).rename(live_path)
            except Exception as exc:
                raise _SwitchRollbackError(f"Could not roll back failing artifact {name}.") from exc
        raise


def _rollback_switched(switched: list[tuple[str, Path | None, Path]], rollback_dir: Path) -> None:
    """Best-effort undo for whatever `_switch_one` already completed,
    in reverse order -- the staged content already switched into
    `live_path` is simply discarded (the original backup at `source` is
    never touched by any of this, so nothing is lost); the previous
    generation is renamed back from `rollback_dir`."""
    for name, _staged_path, live_path in reversed(switched):
        if live_path.is_dir():
            shutil.rmtree(live_path)
        elif live_path.exists():
            live_path.unlink()
        rolled_back = rollback_dir / name
        if rolled_back.exists():
            rolled_back.rename(live_path)


def restore_backup(*, source: Path, db_path: Path, identity_dir: Path,
                   voidrunner_to: Path | None = None) -> Path | None:
    """
    Restore a backup created by `create_backup` into `db_path`/
    `identity_dir` (and their derived sibling paths) -- staged and
    validated (design doc §13.10, issue #75), never by copying directly
    onto a live path.

    Sequence: (1) fully validate `source` -- manifest, checksums,
    database integrity/schema-version/opens cleanly, content-addressed
    blob tree self-check, identity actually loads -- before touching
    anything live; (2) refuse if `db_path` is in active use right now
    (`_require_not_in_use`) or a node process still appears to be
    running per its PID file (`_require_node_not_running`, catching the
    idle case the lock probe alone can't); (3) refuse if a previous
    restore didn't complete cleanly rather than starting a second one
    over it; (4) stage a full copy of `source` next to `db_path` and
    re-validate the staged copy; (5) switch each artifact into place
    with an atomic rename, recording progress in a state file as it
    goes; (6) on any single switch failure, best-effort roll back
    everything already switched and re-raise -- recovering the previous
    generation automatically in the common case, or leaving the state
    file as an explicit, non-silent record if the rollback itself also
    fails.

    Returns the rollback directory holding the previous generation's
    artifacts, or `None` if there was nothing live to preserve (a fresh
    target). Never deleted automatically on success -- an explicit
    operator/cron cleanup step, the same boundary `create_backup`'s own
    deferred-retention list already draws.

    Restoration always resumes the same node identity; there is still
    no supported way to run an old and a restored instance
    simultaneously -- a second instance of the same identity already
    running on a *different* machine remains an accepted, documented
    operator responsibility no PID file on this machine can catch.
    """
    manifest = _validate_backup_source(source, allow_migrate=False)
    has_game = manifest.get("voidrunner") is not None
    target = None
    if has_game:
        if voidrunner_to is None:
            raise BackupError("This backup contains Voidrunner careers; specify --voidrunner-to for the restored service.")
        target = voidrunner_to.resolve()
        protected_paths = [source, db_path, identity_dir, _storage_root_for(db_path),
                           _restore_state_path_for(db_path)]
        protected_paths.extend(live for _, _, live in _restore_switch_plan(
            source, db_path, identity_dir, _database_filename_from_manifest(manifest)))
        for protected_path in protected_paths:
            protected = protected_path.resolve()
            if target.is_relative_to(protected) or protected.is_relative_to(target):
                raise BackupError("Voidrunner restore target overlaps node or backup paths.")
        if target.exists():
            if not target.is_dir():
                raise BackupError("Voidrunner restore destination is not a directory.")
            _voidrunner_files(target)  # Refuse unrelated data before any live switch.
    elif voidrunner_to is not None:
        raise BackupError("This backup has no Voidrunner component; its save directory will not be changed.")
    if db_path.exists():
        _require_not_in_use(db_path)
    _require_node_not_running(db_path)
    _refuse_if_restore_in_progress(db_path)

    token = secrets.token_hex(6)
    staging_dir = db_path.parent / f"{_RESTORE_STAGING_PREFIX}{token}"
    rollback_dir = db_path.parent / f"{_RESTORE_ROLLBACK_PREFIX}{token}"
    state_path = _restore_state_path_for(db_path)
    game_stage = target.parent / f".{target.name}.netbbs-stage-{token}" if target else None
    game_rollback = target.parent / f".{target.name}.netbbs-rollback-{token}" if target else None
    external = ({"voidrunner": {"target": str(target), "staging": str(game_stage),
                                  "rollback": str(game_rollback)}} if target else {})

    with contextlib.ExitStack() as leases:
        if target:
            leases.enter_context(_voidrunner_maintenance(target))
        staging_dir.mkdir(parents=True)
        try:
            shutil.copytree(source, staging_dir, dirs_exist_ok=True)
            staged_manifest = _validate_backup_source(staging_dir, allow_migrate=True)
            plan = _restore_switch_plan(staging_dir, db_path, identity_dir,
                                        _database_filename_from_manifest(staged_manifest))
            if target:
                shutil.copytree(staging_dir / _VOIDRUNNER_DIRNAME, game_stage)
                # Verify the final filesystem-local staging copy too.
                for relative in staged_manifest["voidrunner"]["files"]:
                    if _sha256_of_file(game_stage / relative) != staged_manifest["checksums"][f"voidrunner/{relative}"]:
                        raise BackupError("Voidrunner local staging checksum mismatch.")
                plan.append(("voidrunner", game_stage, target))
            _write_restore_state(state_path, staging_dir=staging_dir, rollback_dir=rollback_dir,
                                 pending=[name for name, _, _ in plan], external=external)
            switched = []
            for name, staged_path, live_path in plan:
                component_rollback = game_rollback if name == "voidrunner" else rollback_dir
                try:
                    _switch_one(name, staged_path, live_path, component_rollback)
                except _SwitchRollbackError as exc:
                    raise BackupError(f"Restore switch and rollback failed; see {state_path} for manual recovery.") from exc
                except Exception as exc:
                    try:
                        for entry in reversed(switched):
                            root = game_rollback if entry[0] == "voidrunner" else rollback_dir
                            _rollback_switched([entry], root)
                    except Exception:
                        raise BackupError(f"Restore and automatic rollback failed; see {state_path} for manual recovery.") from exc
                    state_path.unlink(missing_ok=True)
                    raise BackupError(f"restore failed while switching {name!r}, automatically rolled back to "
                                      f"the previous generation: {exc}") from exc
                switched.append((name, staged_path, live_path))
                remaining = [n for n, _, _ in plan if n not in {item[0] for item in switched}]
                _write_restore_state(state_path, staging_dir=staging_dir, rollback_dir=rollback_dir,
                                     pending=remaining, external=external)
            if game_rollback and game_rollback.exists():
                rollback_dir.mkdir(parents=True, exist_ok=True)
                (rollback_dir / "voidrunner-rollback.json").write_text(json.dumps(external["voidrunner"], indent=2))
        finally:
            if not state_path.exists():
                # On unresolved failure, retain staging named by the journal.
                for path in (staging_dir, game_stage):
                    if path is not None and path.exists():
                        shutil.rmtree(path, ignore_errors=True)
        state_path.unlink(missing_ok=True)
        for path in (staging_dir, game_stage):
            if path is not None and path.exists():
                shutil.rmtree(path, ignore_errors=True)
    return rollback_dir if rollback_dir.exists() else None


# -- CLI ---------------------------------------------------------------

_DEFAULT_DB_PATH = Path("netbbs.db")
_DEFAULT_IDENTITY_DIR = Path("netbbs_identity")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m netbbs.backup", description="Back up or restore a NetBBS node's recoverable state."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a new backup.")
    create_parser.add_argument(
        "--db", type=Path, default=_DEFAULT_DB_PATH, help=f"path to the node's database file (default: {_DEFAULT_DB_PATH})"
    )
    create_parser.add_argument(
        "--identity-dir", type=Path, default=_DEFAULT_IDENTITY_DIR,
        help=f"path to the node's identity directory (default: {_DEFAULT_IDENTITY_DIR})",
    )
    create_parser.add_argument("--to", type=Path, required=True, dest="destination", help="backup destination directory (must not already exist)")

    restore_parser = subparsers.add_parser("restore", help="Restore from a backup.")
    restore_parser.add_argument("--from", type=Path, required=True, dest="source", help="backup directory created by 'create'")
    restore_parser.add_argument(
        "--db", type=Path, default=_DEFAULT_DB_PATH, help=f"path to restore the database to (default: {_DEFAULT_DB_PATH})"
    )
    restore_parser.add_argument(
        "--identity-dir", type=Path, default=_DEFAULT_IDENTITY_DIR,
        help=f"path to restore the identity directory to (default: {_DEFAULT_IDENTITY_DIR})",
    )

    create_parser.add_argument("--voidrunner-save-dir", type=Path,
                               help="Voidrunner source directory (default: service environment or home default)")
    restore_parser.add_argument("--voidrunner-to", type=Path,
                                help="explicit destination for backed-up Voidrunner careers")
    args = parser.parse_args(argv)

    if args.command == "create":
        try:
            destination = create_backup(db_path=args.db, identity_dir=args.identity_dir, destination=args.destination,
                                        voidrunner_save_dir=args.voidrunner_save_dir)
        except BackupError as exc:
            raise SystemExit(terminal_wrapped(f"backup failed: {exc}", stream=sys.stderr)) from exc
        print_wrapped(f"Backup created at {destination}")
        coverage = json.loads((destination / _MANIFEST_FILENAME).read_text()).get("voidrunner")
        source_directory = args.voidrunner_save_dir or voidrunner_save_directory()
        if coverage is None:
            print_wrapped(f"Voidrunner: no save directory found at {source_directory}.")
        else:
            print_wrapped(f"Voidrunner: included {len(coverage['files'])} retained files from {source_directory}.")
    else:
        try:
            rollback_dir = restore_backup(source=args.source, db_path=args.db, identity_dir=args.identity_dir,
                                          voidrunner_to=args.voidrunner_to)
        except BackupError as exc:
            raise SystemExit(terminal_wrapped(f"restore failed: {exc}", stream=sys.stderr)) from exc
        print_wrapped(f"Restored {args.source} into {args.db} / {args.identity_dir}")
        if args.voidrunner_to is not None:
            print_wrapped(f"Voidrunner restored to {args.voidrunner_to.resolve()}. MANUAL: configure the restored service's "
                          "VOIDRUNNER_SAVE_DIR to this directory before restarting.")
        if rollback_dir is not None:
            print_wrapped(
                f"Previous generation preserved at {rollback_dir} -- not deleted automatically, "
                "remove it yourself once you're satisfied the restore is good."
            )


if __name__ == "__main__":
    main()
