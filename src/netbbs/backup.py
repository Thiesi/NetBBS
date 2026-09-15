"""
Node backup and restore (design doc §13.4/§13.10, issue #60's first
operational slice, hardened by issue #75).

A node's recoverable state is fifteen `db_path`-relative artifacts, not
just the database: content blobs (`netbbs.files.storage`), node
identity (`netbbs.link.node_identity`), the SSH host key
(`netbbs.net.ssh`), the managed-DNS registration credential
(`netbbs.managed_dns.credential`, design doc §16 Decision 7, issue
#201), the welcome banner (`netbbs.net.welcome_banner`), the main-menu
masthead (`netbbs.net.main_menu_banner`, issue #161), the logoff banner
(`netbbs.net.logoff_banner`, issue #177), the two new-account banners
(`netbbs.net.new_account_banner_before`/`_after`, issue #177), the
three submenu mastheads (`netbbs.net.board_list_banner`/`file_area_
banner`/`chat_channel_picker_banner`, issue #176), and the door
outbound receipts (`netbbs.doors.outbound`, issue #556) all live at
derived paths alongside the database, each with no independent config
field of its own. A backup covering only the database silently loses
the SSH host key (every client gets a MITM warning after restore) and,
far more seriously, the Link node identity -- root-key custody is
explicitly "part of ordinary node backup and restore" (design doc
§4.5), not a separate ceremony. This module treats all fifteen as one
atomic backup operation, never a DB-only one.

The receipts are the one of the fifteen with a generation of its own to
respect. A receipt records what became of one door's post request, and
a `"posted"` one names a `post_id` in the database beside it -- so it
is captured *before* the database snapshot, the same direction the
Voidrunner component is captured for the same reason. That ordering
can leave a post in the snapshot whose receipt is missing, which the
door contract already covers ("a missing receipt is not proof of
publication"); the reverse -- a receipt naming a post the restored
database never had -- is the one a door could act on, and this ordering
rules out for every post the node still had. A post the node itself
deleted is the exception, and not one this ordering could repair:
`delete_board` removes its board's posts outright and leaves the
receipts, so the live node is already holding a receipt for a post its
own database no longer has. The archive reproduces that pair rather
than inventing or tidying it -- a backup restores the node it was taken
from. Restore replaces the live receipts wholesale with the archive's
own, including replacing them with nothing when the archive predates
this component: receipts from a newer generation beside an older
database claim post IDs that database never had.

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
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
_WAR_DIALER_DIRNAME = "war-dialer"
_DOOR_INSTALLS_DIRNAME = "door-installs"
_DOOR_OUTBOUND_DIRNAME = "door-outbound"
_WAR_DIALER_MAX_WORLDS = 64
_WAR_DIALER_MAX_BYTES = 512 * 1024 * 1024
_VOIDRUNNER_MAX_FILES = 10_000
_VOIDRUNNER_MAX_FILE_BYTES = 4 * 1024 * 1024
_VOIDRUNNER_MAX_TOTAL_BYTES = 512 * 1024 * 1024
#: One directory per door that has ever had its outbound hook switched on.
#: Nothing removes one when a door is deleted, so this is an accumulation
#: bound rather than a count of doors; 64 of them on one node means stale
#: directories to clear, not a node this tool should quietly half-cover.
_DOOR_OUTBOUND_MAX_DOORS = 64
#: What a receipt can be, on the way in from an untrusted archive. NetBBS
#: writes a few hundred bytes of JSON; the per-door count is generous
#: against `netbbs.doors.outbound.RESULTS_KEPT` so an archive written by a
#: build with a different retention still validates. The two together bound
#: what a restore will stage.
_DOOR_OUTBOUND_MAX_RECEIPTS = 1024
_DOOR_OUTBOUND_MAX_RECEIPT_BYTES = 64 * 1024
#: How many entries either listing below will enumerate before giving up.
#: `sorted(iterdir())` materializes a whole directory before any retention
#: bound can apply, and a door writes into this tree -- the same reasoning
#: `netbbs.doors.outbound._MAX_REQUESTS_SCANNED` already applies to the drop
#: directory, at the same multiple of what is kept.
_DOOR_OUTBOUND_SCAN_FACTOR = 4
#: A door id is a SQLite rowid, so nineteen digits is all of them.
_DOOR_ID_PATTERN = re.compile(r"[0-9]{1,19}")
_RESERVED_BACKUP_ENTRIES = (_MANIFEST_FILENAME, _FILES_DIRNAME, _IDENTITY_DIRNAME)

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


def _door_outbound_root_for(db_path: Path) -> Path:
    """The live door outbound receipts root (issue #556).

    Imported rather than mirrored, unlike the derived paths below it: their
    counterparts need a live `Database` this module deliberately never opens,
    while `results_root` was given the node path for exactly this caller. The
    component restores into this directory and doors read it by the same
    formula, so the two must not be able to drift apart.
    """
    from netbbs.doors.outbound import results_root

    return results_root(db_path)


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


def _extra_artifact_paths(db_path: Path) -> tuple[Path, ...]:
    """Known node paths, including optional artifacts absent from an archive."""
    return (
        _ssh_host_key_path_for(db_path),
        _welcome_banner_path_for(db_path),
        _main_menu_banner_path_for(db_path),
        _logoff_banner_path_for(db_path),
        _new_account_banner_before_path_for(db_path),
        _new_account_banner_after_path_for(db_path),
        _board_list_banner_path_for(db_path),
        _file_area_banner_path_for(db_path),
        _chat_channel_picker_banner_path_for(db_path),
    )


def _runtime_reserved_paths(db_path: Path) -> list[Path]:
    """Runtime namespaces remain reserved even when not captured by a backup."""
    parent = db_path.parent
    pat = parent / f"{db_path.stem}_github_pat"
    credentials = (_managed_dns_credential_path_for(db_path), _managed_dns_previous_credential_path_for(db_path),
                   _managed_dns_transition_credential_path_for(db_path), pat)
    return [
        parent / "netbbs.log", *(parent / f"netbbs.log.{index}" for index in range(1, 6)),
        pat, *(Path(str(path) + ".tmp") for path in credentials),
        parent / "doors", parent / "door-nodes", parent / f"{db_path.name}_drafts",
        parent / f"{db_path.stem}_backups",
        *(Path(str(path) + ".draft") for path in _extra_artifact_paths(db_path) if path.suffix == ".ans"),
    ]


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


def voidrunner_save_directory(db_path: Path | None = None) -> tuple[Path, str]:
    """Where this node's Voidrunner saves live, and how that was decided.

    The second element is the provenance -- `"node"` when the node
    recorded it, `"guess"` when nothing was recorded and this process's
    own home was used. (`"operator"` is the third value the manifest
    carries; it belongs to `--voidrunner-save-dir` and never originates
    here.)

    Issue #555. The door resolves its save directory from `Path.home()`
    of whatever process launched it, which is right for a door and wrong
    for this CLI: a node started by `examples/netbbs.rc` runs with
    `HOME=<state dir>`, while a SysOp running the documented backup
    command from their own shell has their own HOME. The two resolve
    `Path.home()` differently, so the backup looked somewhere the node
    never writes, found nothing, and printed "Voidrunner: no save
    directory found at ..." -- which reads as a fact about the node,
    exits 0, and leaves every career out of the archive that exists to
    be the rollback point before an upgrade.

    The node records the answer at startup
    (`netbbs.doors.runtime.record_voidrunner_save_dir`), so the first
    thing tried is the node's own word for it. Falling back to this
    process's `Path.home()` keeps a database written by an older version
    working; the returned provenance says the location was guessed rather
    than read, so a caller can report a guess as a guess instead of
    presenting it as a finding.

    War Dialer needs none of this: v7.0.0 moved its world to
    `<db path>.doors/`, derived from an argument this module already has.
    """
    if db_path is not None and db_path.exists():
        try:
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    "SELECT value FROM node_config WHERE key = 'voidrunner_save_dir'"
                ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None and row[0]:
            return Path(row[0]).resolve(), "node"
    from netbbs.doors.bundled.voidrunner import _default_save_dir
    return _default_save_dir().resolve(), "guess"


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
        elif not archive and entry.is_file() and re.fullmatch(r"\.(?:[0-9]+|maintenance)\.lock|\..+\.tmp", entry.name):
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
        legacy_database = _database_filename_from_manifest(manifest).casefold() == _VOIDRUNNER_DIRNAME
        if root.exists() and not (legacy_database and root.is_file()):
            raise BackupError("Voidrunner component has no coverage manifest.")
        return False
    if _database_filename_from_manifest(manifest).casefold() == _VOIDRUNNER_DIRNAME:
        raise BackupError("Voidrunner component collides with the database snapshot filename.")
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


#: Opt-in: whether a backup also copies each door's installation directory.
#: Off by default and deliberately so -- those directories are operator-owned
#: game installations outside NetBBS's own state, they can be arbitrarily
#: large, and including them changes what a backup costs for every door.
DOOR_INSTALLS_CONFIG_KEY = "backup_door_installs"


def door_installs_included(db) -> bool:
    from netbbs.config import get_config
    return get_config(db, DOOR_INSTALLS_CONFIG_KEY, "0") == "1"


def set_door_installs_included(db, included: bool) -> None:
    from netbbs.config import set_config
    set_config(db, DOOR_INSTALLS_CONFIG_KEY, "1" if included else "0")
    db.connection.commit()


def _door_install_sources(db_path: Path) -> list[tuple[str, Path]]:
    """Each door's installation directory, read-only, when the SysOp opted in.

    Returns (door name, directory) pairs. Directories are de-duplicated by
    resolved path, and one nested inside another already being captured is
    dropped rather than copied twice.
    """
    from netbbs.doors.registry import list_doors
    node = db_path.resolve()
    connection = sqlite3.connect(node.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        db = SimpleNamespace(path=node, connection=connection)
        if not door_installs_included(db):
            return []
        found: list[tuple[str, Path]] = []
        for door in list_doors(db):
            if door.profile is None or not door.profile.install_dir:
                continue
            directory = Path(door.profile.install_dir).resolve()
            if not directory.is_dir():
                raise BackupError(
                    f"Door {door.name!r} has installation directory {directory}, which does not exist. "
                    "Create it, correct the door, or turn off backing up door installation directories.")
            if not os.access(directory, os.R_OK | os.X_OK):
                raise BackupError(
                    f"Cannot read door {door.name!r}'s installation directory {directory}. "
                    "Fix its permissions, or turn off backing up door installation directories.")
            found.append((door.name, directory))
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        raise BackupError(f"Cannot discover door installation directories: {exc}") from exc
    finally:
        connection.close()
    unique: list[tuple[str, Path]] = []
    for name, directory in sorted(found, key=lambda pair: str(pair[1])):
        if any(directory == kept or directory.is_relative_to(kept) for _, kept in unique):
            continue
        unique.append((name, directory))
    return unique


def _ignore_special_files(directory, names):
    """Names in `directory` which are neither a regular file, a directory nor
    a symlink -- sockets, FIFOs and device nodes. Backing up a game
    installation means its data, and a live socket cannot be copied at all.
    """
    skipped = set()
    for name in names:
        path = Path(directory) / name
        try:
            if path.is_symlink() or path.is_file() or path.is_dir():
                continue
        except OSError:
            # Unreadable is not the same as special; leave it to the copy,
            # which reports it against the door rather than silently dropping.
            continue
        skipped.add(name)
    return skipped


def _capture_door_installs(db_path: Path, destination: Path) -> dict | None:
    """Copy opted-in door installations verbatim beside the node's own state.

    Symlinks are copied as symlinks rather than followed: a game installation
    is operator-owned content, and following a link out of it would pull
    unrelated host data into the backup.

    Recorded by file count and total size rather than per-file checksums --
    these trees are not node state NetBBS can validate or restore, and a large
    installation would otherwise bloat the manifest with thousands of entries.
    """
    sources = _door_install_sources(db_path)
    if not sources:
        return None
    output = destination / _DOOR_INSTALLS_DIRNAME
    output.mkdir()
    captured = []
    for index, (name, directory) in enumerate(sources, 1):
        if destination.resolve() == directory or destination.resolve().is_relative_to(directory):
            raise BackupError(
                f"A backup destination cannot be inside door {name!r}'s installation directory {directory}.")
        target = output / str(index)
        # Sockets, FIFOs and device nodes are not data and cannot be copied:
        # `copytree` raises on a live Unix socket and aborts the whole backup.
        # A door service's health socket lives inside its installation
        # directory by documented convention, so this is the ordinary case
        # rather than an exotic one -- and the pathname can outlive the
        # service, so halting it first is not enough.
        shutil.copytree(directory, target, symlinks=True, ignore=_ignore_special_files)
        files = [path for path in target.rglob("*") if path.is_file() and not path.is_symlink()]
        captured.append({"key": str(index), "door_name": name, "source_path": str(directory),
                         "file_count": len(files), "total_bytes": sum(path.stat().st_size for path in files)})
    return {"version": 1, "installations": captured}


def _war_dialer_sources(db_path: Path) -> list[Path]:
    """Discover registered overrides plus a retained node-default world, read-only."""
    from netbbs.doors.registry import list_doors
    from netbbs.doors.runtime import war_dialer_world_path, war_dialer_path_problem
    node = db_path.resolve()
    paths = {node.parent / (node.name + ".doors") / "war-dialer.db"}
    connection = sqlite3.connect(node.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        db = SimpleNamespace(path=node, connection=connection)
        for door in list_doors(db):
            path = war_dialer_world_path(db, door)
            if path is not None:
                if problem := war_dialer_path_problem(door, path):
                    raise BackupError(problem)
                paths.add(path)
        if override := os.environ.get("WAR_DIALER_DB_PATH"):
            paths.add(Path(override).expanduser().resolve())
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        raise BackupError(f"Cannot discover War Dialer worlds: {exc}") from exc
    finally:
        connection.close()
    if len(paths) > _WAR_DIALER_MAX_WORLDS:
        raise BackupError("War Dialer backup exceeds 64 configured worlds.")
    return sorted((path for path in paths if path.exists()), key=str)


def _war_dialer_owner(node: Path) -> str | None:
    with contextlib.closing(sqlite3.connect(node.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT value FROM node_config WHERE key='war_dialer_owner'").fetchone()
        return row[0] if row else None


def _inspect_war_dialer(path: Path, owner: str | None) -> int:
    from netbbs.doors.bundled import war_dialer as wd
    if not path.is_file() or path.is_symlink() or path.stat().st_size > _WAR_DIALER_MAX_BYTES:
        raise BackupError(f"War Dialer world is not a regular database within the 512 MiB limit: {path}")
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            version = wd._world_schema_version(conn)
            wd._validate_world_layout(conn, version)
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("War Dialer world failed SQLite integrity validation.")
            bound = conn.execute("SELECT value FROM meta WHERE key='node_owner'").fetchone()
            if owner is None or bound is None or bound[0] != owner:
                raise BackupError("War Dialer world does not belong to this node's user-ID namespace. "
                                  "For an unbound legacy world, verify user ownership and launch it once through its owning node.")
            return version
    except (sqlite3.Error, wd.WorldStateError) as exc:
        raise BackupError(f"War Dialer world cannot be read: {exc}") from exc


@contextlib.contextmanager
def _war_dialer_maintenance(path: Path):
    from netbbs.doors.bundled import war_dialer as wd
    try:
        with wd.world_session(path, maintenance=True):
            yield
    except (wd.WorldStateError, sqlite3.Error, OSError) as exc:
        raise BackupError(f"War Dialer maintenance unavailable at {path}: {exc}") from exc


def _capture_war_dialer(db_path: Path, destination: Path, checksums: dict) -> dict | None:
    paths = _war_dialer_sources(db_path)
    if not paths:
        return None
    owner = _war_dialer_owner(db_path)
    output = destination / _WAR_DIALER_DIRNAME
    output.mkdir()
    worlds = []
    for index, path in enumerate(paths, 1):
        key = str(index)
        with _war_dialer_maintenance(path):
            _inspect_war_dialer(path, owner)
            snapshot = output / (key + ".db")
            snapshot_database(path, snapshot)
            with contextlib.closing(sqlite3.connect(snapshot)) as saved:
                saved.execute("PRAGMA journal_mode=DELETE")
            version = _inspect_war_dialer(snapshot, owner)
        checksums[f"{_WAR_DIALER_DIRNAME}/{key}.db"] = _sha256_of_file(snapshot)
        worlds.append({"key": key, "source_path": str(path), "schema_version": version})
    return {"version": 1, "owner": owner, "worlds": worlds}


def _validate_war_dialer_component(source: Path, manifest: dict) -> None:
    metadata = manifest.get("war_dialer")
    root = source / _WAR_DIALER_DIRNAME
    if metadata is None:
        if root.exists() and _database_filename_from_manifest(manifest) != _WAR_DIALER_DIRNAME:
            raise BackupError("War Dialer component has no coverage manifest.")
        return
    if (not isinstance(metadata, dict) or metadata.get("version") != 1
            or not isinstance(metadata.get("worlds"), list) or not 1 <= len(metadata["worlds"]) <= _WAR_DIALER_MAX_WORLDS
            or not root.is_dir() or root.is_symlink()):
        raise BackupError("Invalid War Dialer coverage manifest.")
    owner = _war_dialer_owner(source / _database_filename_from_manifest(manifest))
    if metadata.get("owner") != owner or owner is None:
        raise BackupError("War Dialer component owner does not match the node database snapshot.")
    expected = set()
    for index, world in enumerate(metadata["worlds"], 1):
        if not isinstance(world, dict) or world.get("key") != str(index):
            raise BackupError("Invalid War Dialer world key in manifest.")
        name = str(index) + ".db"
        expected.add(name)
        relative = f"{_WAR_DIALER_DIRNAME}/{name}"
        if relative not in manifest.get("checksums", {}):
            raise BackupError("War Dialer snapshot has no checksum.")
        if _inspect_war_dialer(root / name, owner) != world.get("schema_version"):
            raise BackupError("War Dialer schema does not match its manifest.")
    if {path.name for path in root.iterdir()} != expected:
        raise BackupError("War Dialer files do not match their coverage manifest.")


def _is_capturable_receipt_name(name: str, suffix: str) -> bool:
    """A receipt this tool will carry between platforms.

    `_write_result` builds a receipt's name from the door's own request
    filename, and a door on the POSIX target may legally call a request
    `foo\\bar.json`. The resulting receipt is a perfectly good file there and
    a path with a directory in it on Windows, so an archive must not contain
    one: capture and validation ask the same question here rather than capture
    taking a name that validation then refuses, which used to fail the whole
    backup over one door's odd filename.
    """
    return (name.endswith(suffix) and name not in {".", ".."}
            and "/" not in name and "\\" not in name)


def _door_outbound_receipts(directory: Path, kept: int) -> tuple[list[Path], int, int]:
    """One door's receipts, the newest `kept` of them, and what was left behind.

    Only what NetBBS itself wrote and can read back: a regular file named the
    way `_write_result` names one, within the size such a file has. A door
    runs as the BBS user and can put anything here, and the half-written
    `.part` of an interrupted write is already an ordinary case. None of that
    is node state, so it is left alone rather than captured or treated as
    corruption -- the same judgment `_is_content_addressed_name` makes about
    the blob tree -- but it is counted, so an archive never silently claims to
    hold more than it does.

    Returned as `(receipts, skipped, pruned)`: what the node discarded while
    this ran is a different fact about the archive from what was never a
    receipt, and reporting both as one number would describe a receipt this
    backup lost as a door's stray file.

    `kept` mirrors the door module's own retention rule instead of inventing a
    second one: anything past it is what the next drain would prune anyway.
    Enumeration stops well before that, at a multiple of it, and through
    `scandir` rather than `iterdir`: the latter orders a whole directory before
    any bound can apply, and a door is what fills this one.
    """
    from netbbs.doors.outbound import RESULT_SUFFIX

    receipts: list[tuple[float, str, Path]] = []
    skipped = pruned = 0
    limit = _DOOR_OUTBOUND_SCAN_FACTOR * kept
    try:
        with os.scandir(directory) as entries:
            for scanned, entry in enumerate(entries, 1):
                if scanned > limit:
                    raise BackupError(
                        f"Door outbound receipts at {directory} hold more than {limit} entries, "
                        f"where this node keeps {kept}. Receipts are disposable -- clear what a "
                        "door has left there, then retry.")
                path = Path(entry.path)
                try:
                    if (not _is_capturable_receipt_name(entry.name, RESULT_SUFFIX)
                            or entry.is_symlink() or not entry.is_file()
                            or entry.stat().st_size > _DOOR_OUTBOUND_MAX_RECEIPT_BYTES):
                        skipped += 1
                        continue
                    receipts.append((entry.stat().st_mtime, entry.name, path))
                except FileNotFoundError:
                    pruned += 1
                except OSError as exc:
                    raise BackupError(f"Cannot read door outbound receipt {path}: {exc}") from exc
    except (FileNotFoundError, NotADirectoryError):
        # `disable_outbound` releases a door's whole directory, and a SysOp
        # may switch a hook off while this backup runs. The same judgment the
        # copy below makes: what the node is discarding is not worth failing
        # an archive over.
        return [], 0, 0
    except OSError as exc:
        raise BackupError(f"Cannot read door outbound receipts at {directory}: {exc}") from exc
    # Oldest first out, exactly as `_prune_results` chooses what to keep, and
    # ordered here rather than by the filesystem: `scandir` promises no order.
    receipts.sort()
    skipped += max(0, len(receipts) - kept)
    return [path for _, _, path in receipts[-kept:]], skipped, pruned


def _capture_door_outbound(db_path: Path, destination: Path, checksums: dict) -> dict | None:
    """Capture every door's outbound receipts (issue #556).

    Called before the database snapshot: a `"posted"` receipt names a post
    that was committed before the receipt was written, so capturing receipts
    first guarantees the snapshot taken after them contains every post they
    name.

    Returns metadata whenever the node has a receipts root at all, even an
    empty one. Its presence in the manifest is what tells restore that this
    archive has an opinion about receipts, and "the node had none" is an
    opinion worth restoring: leaving a later generation's receipts in place
    would pair them with a database that never issued those posts.

    Live-safe, like the rest of this module: a receipt is written
    temp-then-rename, so a capture taken while a door is running never copies
    a half-written one, and a receipt a concurrent drain prunes between the
    listing and the copy is counted as pruned rather than failing the whole
    archive. What it is not is one instant: a SysOp who switches a hook off
    between this capture and the database snapshot leaves an archive whose
    receipts are a moment older than its snapshot, the same boundary the game
    components already draw. It is the direction that matters here, and it is
    the one this ordering fixes.

    A symlinked root is followed rather than reported absent -- doors reach it
    through the same symlink, and this is the SysOp's own placement of node
    state, exactly like a symlinked blob tree. (Restore then puts a real
    directory at the node path and preserves the link in the rollback
    generation, since it switches by rename like every other artifact.) A
    symlink *inside* it is a different thing and is still skipped: NetBBS
    never writes one there.
    """
    from netbbs.doors.outbound import RESULTS_KEPT

    root = _door_outbound_root_for(db_path)
    if not root.is_dir():
        return None
    resolved = destination.resolve()
    if resolved == root.resolve() or resolved.is_relative_to(root.resolve()):
        raise BackupError("A backup destination cannot be inside the door outbound receipts directory.")
    doors: list[Path] = []
    skipped = pruned = 0
    limit = _DOOR_OUTBOUND_SCAN_FACTOR * _DOOR_OUTBOUND_MAX_DOORS
    try:
        with os.scandir(root) as entries:
            for scanned, entry in enumerate(entries, 1):
                if scanned > limit:
                    raise BackupError(
                        f"Door outbound receipts at {root} hold more than {limit} entries. "
                        "Receipts are disposable -- remove what belongs to doors this node no "
                        "longer has, then retry.")
                try:
                    if (entry.is_dir() and not entry.is_symlink()
                            and _DOOR_ID_PATTERN.fullmatch(entry.name)):
                        doors.append(Path(entry.path))
                    else:
                        skipped += 1
                except FileNotFoundError:
                    # `disable_outbound` releases a door's whole directory, and
                    # a SysOp can do that between this listing and the metadata
                    # call it needs -- a `DirEntry` stats lazily. The per-door
                    # scan already treats that removal as harmless; failing the
                    # whole backup here would make the two disagree.
                    pruned += 1
    except OSError as exc:
        raise BackupError(f"Cannot read door outbound receipts at {root}: {exc}") from exc
    if len(doors) > _DOOR_OUTBOUND_MAX_DOORS:
        raise BackupError(
            f"Door outbound receipts cover more than {_DOOR_OUTBOUND_MAX_DOORS} doors at {root}. "
            "Receipts are disposable -- remove the directories belonging to doors this node no "
            "longer has, then retry.")
    output = destination / _DOOR_OUTBOUND_DIRNAME
    output.mkdir()
    captured = []
    for entry in sorted(doors, key=lambda path: (int(path.name), path.name)):
        receipts, ignored, gone = _door_outbound_receipts(entry, RESULTS_KEPT)
        skipped += ignored
        pruned += gone
        if not receipts:
            continue
        target = output / entry.name
        target.mkdir()
        names = []
        for path in receipts:
            captured_path = target / path.name
            try:
                shutil.copy2(path, captured_path)
            except FileNotFoundError:
                # A drain running while this backup does can prune a receipt
                # between the listing above and this copy. Losing a receipt
                # the node itself was discarding is the outcome a door already
                # expects; failing an entire archive over it is not. Only that
                # one failure, though -- an unreadable or unwritable receipt is
                # an incomplete archive presented as a complete one.
                captured_path.unlink(missing_ok=True)
                pruned += 1
                continue
            except OSError as exc:
                raise BackupError(f"Cannot capture door outbound receipt {path}: {exc}") from exc
            checksums[f"{_DOOR_OUTBOUND_DIRNAME}/{entry.name}/{path.name}"] = _sha256_of_file(captured_path)
            names.append(path.name)
        if not names:
            # No empty directory the manifest does not account for: the
            # component's own validator refuses one, and rightly.
            target.rmdir()
            continue
        captured.append({"key": entry.name, "source_path": str(entry), "receipts": sorted(names)})
    return {"version": 1, "doors": captured, "skipped": skipped, "pruned": pruned}


def _refuse_unlisted_entries(directory: Path, expected: set[str], what: str) -> None:
    """`directory` holds exactly `expected`, checked while enumerating it.

    An archive is operator-supplied and may be corrupt or hostile, so this
    never materializes a listing the manifest did not describe: the first name
    the manifest does not claim ends the scan, which bounds the work at what
    the manifest itself declares.
    """
    seen = set()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name not in expected:
                    raise BackupError(f"{what} do not match their coverage manifest.")
                seen.add(entry.name)
    except OSError as exc:
        raise BackupError(f"Cannot read {what.lower()} at {directory}: {exc}") from exc
    if seen != expected:
        raise BackupError(f"{what} do not match their coverage manifest.")


def _validate_door_outbound_component(source: Path, manifest: dict) -> None:
    """Everything this component claims, checked before a live path moves.

    An archive is operator-supplied and its manifest is untrusted on restore,
    so names are checked for being plain filenames rather than paths, entries
    for being real files rather than links, and the tree for holding nothing
    the manifest does not list -- restore switches this directory in whole, so
    an unlisted file in the archive would land beside the node database.
    """
    from netbbs.doors.outbound import RESULT_SUFFIX

    metadata = manifest.get("door_outbound")
    root = source / _DOOR_OUTBOUND_DIRNAME
    if metadata is None:
        # A node database *named* `door-outbound` keeps that name in the
        # archive when this component is absent, exactly as the War Dialer
        # and Voidrunner components already allow for.
        if root.exists() and _database_filename_from_manifest(manifest) != _DOOR_OUTBOUND_DIRNAME:
            raise BackupError("Door outbound component has no coverage manifest.")
        return
    if (not isinstance(metadata, dict) or metadata.get("version") != 1
            or not isinstance(metadata.get("doors"), list)
            or len(metadata["doors"]) > _DOOR_OUTBOUND_MAX_DOORS
            or not root.is_dir() or root.is_symlink()):
        raise BackupError("Invalid door outbound coverage manifest.")
    expected_doors = set()
    for door in metadata["doors"]:
        if (not isinstance(door, dict) or not isinstance(door.get("key"), str)
                or not _DOOR_ID_PATTERN.fullmatch(door["key"]) or door["key"] in expected_doors
                or not isinstance(door.get("receipts"), list)
                or not 1 <= len(door["receipts"]) <= _DOOR_OUTBOUND_MAX_RECEIPTS):
            raise BackupError("Invalid door outbound receipt manifest.")
        expected_doors.add(door["key"])
        directory = root / door["key"]
        if not directory.is_dir() or directory.is_symlink():
            raise BackupError("Door outbound component is missing a door's receipt directory.")
        expected_names = set()
        for name in door["receipts"]:
            if not isinstance(name, str) or not _is_capturable_receipt_name(name, RESULT_SUFFIX):
                raise BackupError("Invalid door outbound receipt name in manifest.")
            receipt = directory / name
            if (not receipt.is_file() or receipt.is_symlink()
                    or receipt.stat().st_size > _DOOR_OUTBOUND_MAX_RECEIPT_BYTES):
                raise BackupError("Door outbound receipt is not a regular file within "
                                  f"{_DOOR_OUTBOUND_MAX_RECEIPT_BYTES} bytes: {receipt}")
            if f"{_DOOR_OUTBOUND_DIRNAME}/{door['key']}/{name}" not in manifest.get("checksums", {}):
                raise BackupError("Door outbound receipt has no checksum.")
            expected_names.add(name)
        _refuse_unlisted_entries(directory, expected_names, "Door outbound receipts")
    _refuse_unlisted_entries(root, expected_doors, "Door outbound directories")


def _discard_incomplete_backup(destination: Path, exc: BaseException) -> None:
    """Remove the destination this run created; the caller re-raises `exc`.

    No prior backup is ever removed here: `create_backup` refuses a
    destination that already exists, so this only ever discards what this run
    made. A rejected session, an unreadable file or a failed self-check must
    leave the path free, or the retry the operator is about to make cannot use
    it either.
    """
    try:
        shutil.rmtree(destination)
    except OSError as cleanup:
        raise BackupError(f"{exc} Incomplete backup at {destination} could not be removed: {cleanup}. "
                          "Remove it manually before retrying.") from exc


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

    if voidrunner_save_dir is not None:
        # Not "node" (Codex review): the operator named this path, and an
        # archive that claims the node confirmed a directory it never
        # recorded defeats the point of writing the provenance down.
        game_source, game_source_provenance = voidrunner_save_dir.resolve(), "operator"
    else:
        game_source, game_source_provenance = voidrunner_save_directory(db_path)
    if destination.resolve().is_relative_to(game_source):
        raise BackupError("A backup destination cannot be inside the Voidrunner save directory.")
    destination.mkdir(parents=True)

    checksums = {}
    try:
        game_metadata = _capture_voidrunner(game_source, destination, checksums)
        if game_metadata is not None:
            # Whether this component's source was the node's own answer or
            # this process's guess, recorded in the archive rather than
            # only in the terminal scrollback of whoever ran the command
            # (Codex review). A restore months later is exactly when
            # "was that the right directory?" becomes unanswerable.
            # Informational, and the manifest version stays 1: nothing
            # about the required shape changed, and both older readers
            # and this module's own validator ignore unknown keys.
            game_metadata["source_provenance"] = game_source_provenance
        war_metadata = _capture_war_dialer(db_path, destination, checksums)
        # Before the database snapshot, like the game components above and for
        # a related reason: a receipt must never name a post the snapshot
        # beside it does not contain (issue #556).
        receipt_metadata = _capture_door_outbound(db_path, destination, checksums)
        door_metadata = _capture_door_installs(db_path, destination)
    except BaseException as exc:
        _discard_incomplete_backup(destination, exc)
        raise
    if ((game_metadata is not None and database_filename.casefold() == _VOIDRUNNER_DIRNAME)
            or (war_metadata is not None and database_filename.casefold() == _WAR_DIALER_DIRNAME)
            or (door_metadata is not None and database_filename.casefold() == _DOOR_INSTALLS_DIRNAME)
            or (receipt_metadata is not None and database_filename.casefold() == _DOOR_OUTBOUND_DIRNAME)):
        # The live custom filename remains valid. Only its archive name changes;
        # the manifest and explicit restore --db already separate those paths.
        database_filename = _LEGACY_DB_FILENAME
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

    for extra_path in _extra_artifact_paths(db_path):
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
        # Where this run *looked*, and whether the node told it where to
        # look -- recorded whether or not anything was found (Codex
        # review). A backup is live-safe, so the node may start while one
        # is running: a caller that re-resolves the location afterwards
        # can be told the service path by a database that recorded it
        # half a second ago, and report "no saves at" a directory nothing
        # ever opened. The capture-time answer is the only one that
        # describes this archive, so it travels with it.
        "voidrunner_source": {
            "directory": str(game_source),
            "provenance": game_source_provenance,
        },
        "war_dialer": war_metadata,
        "door_installs": door_metadata,
        "door_outbound": receipt_metadata,
    }
    try:
        # Checked against what was actually written, and inside the same
        # cleanup this function's capture phase uses: a self-check that fails
        # leaves no destination behind, or the next run cannot even retry at
        # the same path.
        _validate_war_dialer_component(destination, manifest)
        _validate_door_outbound_component(destination, manifest)
        (destination / _MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    except BaseException as exc:
        _discard_incomplete_backup(destination, exc)
        raise

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
    _validate_war_dialer_component(source, manifest)
    _validate_door_outbound_component(source, manifest)

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
    payload = json.dumps({
        "started_at": utc_now_iso(),
        "staging_dir": str(staging_dir),
        "rollback_dir": str(rollback_dir),
        "pending_artifacts": pending,
        "external_components": external or {},
    }, indent=2).encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=state_path.parent, prefix=".restore-state-",
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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

    # Planned whether or not this archive has the component (issue #556):
    # a point-in-time restore must restore the absence of receipts too, or a
    # later generation's receipts survive beside an older database and claim
    # post IDs it never issued. `is_dir()` settles the one archive where this
    # name is not the component -- a node database called `door-outbound`,
    # kept under that name when nothing was captured.
    staged_receipts = staging_dir / _DOOR_OUTBOUND_DIRNAME
    live_receipts = _door_outbound_root_for(db_path)
    if live_receipts == db_path:
        # A node database named `door-outbound` occupies the exact path its
        # own receipts would live at, so that node has none and cannot have
        # any -- but planning the artifact anyway would rename the database
        # this restore just put there into the rollback directory.
        if staged_receipts.is_dir():
            raise BackupError(
                "This backup carries door outbound receipts, which live at the same path as a "
                f"database named {db_path.name!r}. Restore the database under another name.")
    else:
        plan.append((_DOOR_OUTBOUND_DIRNAME,
                     staged_receipts if staged_receipts.is_dir() else None,
                     live_receipts))

    for entry in sorted(staging_dir.iterdir()):
        if entry.name in (*_RESERVED_BACKUP_ENTRIES, _VOIDRUNNER_DIRNAME, _WAR_DIALER_DIRNAME,
                          _DOOR_OUTBOUND_DIRNAME, database_filename):
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

    _refuse_overlapping_live_paths(plan)
    return plan


def _refuse_overlapping_live_paths(plan: list[tuple[str, Path | None, Path]]) -> None:
    """No two artifacts may restore onto the same live path, or into each other.

    Every live path is derived from `db_path` except `identity_dir`, which the
    caller supplies and nothing checks against the rest. A node pointed at its
    own receipts root or blob tree for identity would have had its keys
    switched aside by whichever artifact is planned after them, and the restore
    would have reported success. The artifacts are independent, so "the last
    one wins" is never the answer; refuse before the first switch instead.
    """
    seen: list[tuple[str, Path]] = []
    for name, _staged_path, live_path in plan:
        resolved = live_path.resolve()
        for other_name, other in seen:
            if resolved == other or resolved.is_relative_to(other) or other.is_relative_to(resolved):
                raise BackupError(
                    f"Restore targets overlap: {name!r} at {resolved} and {other_name!r} at "
                    f"{other} cannot both be restored. Give the node's database, identity "
                    "directory and derived paths separate locations.")
        seen.append((name, resolved))


class _SwitchRollbackError(BackupError):
    """The failing artifact could not be put back; retain the restore journal."""


def _game_data_entries(directory: Path) -> list[Path]:
    """Keep the directory and lock inodes stable while maintenance excludes play."""
    return [path for path in sorted(directory.iterdir())
            if path.name != ".maintenance.lock" and not re.fullmatch(r"\.[0-9]+\.lock", path.name)]


def _switch_game(staged_path: Path, live_path: Path, rollback_dir: Path) -> None:
    old = rollback_dir / "voidrunner"
    old.mkdir(parents=True)
    live_path.mkdir(parents=True, exist_ok=True)
    moved = []
    try:
        for source, destination in [(path, old / path.name) for path in _game_data_entries(live_path)] + [
                (path, live_path / path.name) for path in _game_data_entries(staged_path)]:
            source.rename(destination)
            moved.append((source, destination))
    except Exception:
        try:
            for source, destination in reversed(moved):
                destination.rename(source)
        except Exception as exc:
            raise _SwitchRollbackError("Could not roll back failing Voidrunner data switch.") from exc
        raise


def _switch_one(name: str, staged_path: Path | None, live_path: Path, rollback_dir: Path) -> None:
    """Roll back even the artifact whose second rename failed."""
    if name == "voidrunner":
        _switch_game(staged_path, live_path, rollback_dir)
        return
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
        if name == "voidrunner":
            for path in _game_data_entries(live_path):
                path.rename(_staged_path / path.name)
            for path in _game_data_entries(rollback_dir / name):
                path.rename(live_path / path.name)
            continue
        if live_path.is_dir():
            shutil.rmtree(live_path)
        elif live_path.exists():
            live_path.unlink()
        rolled_back = rollback_dir / name
        if rolled_back.exists():
            rolled_back.rename(live_path)


def _parse_war_dialer_destinations(values: list[str] | None) -> dict[str, Path] | None:
    if values is None:
        return None
    result = {}
    for value in values:
        key, separator, path = value.partition("=")
        if not separator or not key.isdigit() or not path.strip() or key in result:
            raise BackupError("War Dialer destinations must be unique KEY=PATH entries.")
        result[key] = Path(path)
    return result


def _war_dialer_restore_targets(source: Path, manifest: dict, destinations, protected_paths: list[Path]) -> dict[str, Path]:
    metadata = manifest.get("war_dialer")
    if metadata is None:
        if destinations is not None:
            raise BackupError("This backup has no War Dialer component; no world will be changed.")
        return {}
    keys = {world["key"] for world in metadata["worlds"]}
    if isinstance(destinations, Path) and len(keys) == 1:
        destinations = {next(iter(keys)): destinations}
    if not isinstance(destinations, dict) or set(destinations) != keys:
        raise BackupError("Specify --war-dialer-to KEY=PATH for every archived world: " + ", ".join(sorted(keys)))
    result = {}
    claimed_paths = []
    for key in sorted(keys):
        target = Path(destinations[key]).expanduser().resolve()
        footprint = [Path(str(target) + suffix) for suffix in ("", "-wal", "-shm", "-journal", ".sessions")]
        for protected in [*protected_paths, *claimed_paths]:
            protected = protected.resolve()
            for candidate in footprint:
                if candidate.is_relative_to(protected) or protected.is_relative_to(candidate):
                    raise BackupError("War Dialer restore destination overlaps another world, sidecar, component or backup path.")
        claimed_paths.extend(footprint)
        if target.exists():
            _inspect_war_dialer(target, metadata["owner"])
        result[key] = target
    return result


def restore_backup(*, source: Path, db_path: Path, identity_dir: Path,
                   voidrunner_to: Path | None = None,
                   war_dialer_to: Path | dict[str, Path] | None = None) -> Path | None:
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
    if manifest.get("war_dialer") is None:
        node = db_path.resolve()
        existing_worlds = (_war_dialer_sources(node) if node.exists() else
                           [path for path in [node.parent / (node.name + ".doors") / "war-dialer.db"] if path.exists()])
        if existing_worlds:
            raise BackupError("This archive does not cover existing War Dialer worlds. Preserve the current node and worlds, "
                              "then move uncovered worlds and their sidecars aside before restoring this legacy archive. "
                              "Do not reattach them without verifying the restored user-ID namespace.")
    has_game = manifest.get("voidrunner") is not None
    target = None
    if has_game:
        if voidrunner_to is None:
            raise BackupError("This backup contains Voidrunner careers; specify --voidrunner-to for the restored service.")
        target = voidrunner_to.resolve()
        protected_paths = [source, db_path, identity_dir, _storage_root_for(db_path),
                           _restore_state_path_for(db_path), _pid_file_path_for(db_path)]
        protected_paths.extend(_extra_artifact_paths(db_path))
        protected_paths.extend(_runtime_reserved_paths(db_path))
        protected_paths.extend(Path(str(db_path) + suffix) for suffix in ("-wal", "-shm", "-journal"))
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
    war_protected = [source, db_path, identity_dir, _storage_root_for(db_path),
                     _restore_state_path_for(db_path), _pid_file_path_for(db_path),
                     *_extra_artifact_paths(db_path), *_runtime_reserved_paths(db_path)]
    war_protected.extend(Path(str(db_path) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    war_protected.extend(live for _, _, live in _restore_switch_plan(
        source, db_path, identity_dir, _database_filename_from_manifest(manifest)))
    if target:
        war_protected.append(target)
    war_targets = _war_dialer_restore_targets(source, manifest, war_dialer_to, war_protected)
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

    war_stages = {key: path.parent / f".{path.name}.netbbs-stage-{token}" for key, path in war_targets.items()}
    war_rollbacks = {key: path.parent / f".{path.name}.netbbs-rollback-{token}" for key, path in war_targets.items()}
    external.update({"war-dialer-" + key: {"target": str(path), "staging": str(war_stages[key]),
                                          "rollback": str(war_rollbacks[key])}
                     for key, path in war_targets.items()})

    def component_rollback(name):
        if name.startswith("war-dialer-"):
            return war_rollbacks[name.split("-")[2]]
        return game_rollback if name == "voidrunner" else rollback_dir

    with contextlib.ExitStack() as leases:
        for path in war_targets.values():
            leases.enter_context(_war_dialer_maintenance(path))
            if path.exists():
                _require_not_in_use(path)
        if target:
            leases.enter_context(_voidrunner_maintenance(target))
        staging_dir.mkdir(parents=True)
        try:
            # `door-installs` is capture-only: restore never writes it back,
            # so staging it buys nothing and costs plenty. It also actively
            # breaks restore -- the archive stores symlinks as symlinks, and
            # this copy follows them by default, so one dangling absolute link
            # inside a game installation would fail an otherwise valid node
            # restore, and a live one would drag unrelated host data into
            # staging. Excluded at the archive root only.
            # Only when the manifest says this archive actually has the
            # capture-only component. A node whose database is *named*
            # `door-installs` keeps that basename when no installations were
            # captured, and skipping it unconditionally would drop the
            # database snapshot and make a good backup unrestorable.
            skip = ({_DOOR_INSTALLS_DIRNAME}
                    if manifest.get("door_installs") is not None else set())
            shutil.copytree(source, staging_dir, dirs_exist_ok=True,
                            ignore=lambda directory, names:
                            skip if Path(directory).resolve() == source.resolve() else set())
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
            for key, path in war_targets.items():
                staged = war_stages[key]
                shutil.copy2(staging_dir / _WAR_DIALER_DIRNAME / (key + ".db"), staged)
                expected_hash = staged_manifest["checksums"][f"{_WAR_DIALER_DIRNAME}/{key}.db"]
                if _sha256_of_file(staged) != expected_hash:
                    raise BackupError("War Dialer local staging checksum mismatch.")
                _inspect_war_dialer(staged, staged_manifest["war_dialer"]["owner"])
                # Preserve a live world's WAL generation in the rollback set;
                # it must never be replayed against the restored snapshot.
                for suffix in ("-wal", "-shm", "-journal"):
                    plan.append(("war-dialer-" + key + suffix, None, Path(str(path) + suffix)))
                plan.append(("war-dialer-" + key, staged, path))
            _write_restore_state(state_path, staging_dir=staging_dir, rollback_dir=rollback_dir,
                                 pending=[name for name, _, _ in plan], external=external)
            switched = []
            for name, staged_path, live_path in plan:
                try:
                    _switch_one(name, staged_path, live_path, component_rollback(name))
                    switched.append((name, staged_path, live_path))
                    remaining = [n for n, _, _ in plan if n not in {item[0] for item in switched}]
                    _write_restore_state(state_path, staging_dir=staging_dir, rollback_dir=rollback_dir,
                                         pending=remaining, external=external)
                except _SwitchRollbackError as exc:
                    raise BackupError(f"Restore switch and rollback failed; see {state_path} for manual recovery.") from exc
                except Exception as exc:
                    try:
                        for entry in reversed(switched):
                            root = component_rollback(entry[0])
                            _rollback_switched([entry], root)
                    except Exception:
                        raise BackupError(f"Restore and automatic rollback failed; see {state_path} for manual recovery.") from exc
                    state_path.unlink(missing_ok=True)
                    raise BackupError(f"restore failed while switching {name!r}, automatically rolled back to "
                                      f"the previous generation: {exc}") from exc
            if war_targets:
                try:
                    rollback_dir.mkdir(parents=True, exist_ok=True)
                    (rollback_dir / "war-dialer-rollback.json").write_text(json.dumps(
                        {name: value for name, value in external.items() if name.startswith("war-dialer-")}, indent=2))
                except OSError as exc:
                    raise BackupError(f"Restored data is in place, but rollback metadata could not be written; "
                                      f"see {state_path} before restarting.") from exc
            if game_rollback and game_rollback.exists():
                try:
                    rollback_dir.mkdir(parents=True, exist_ok=True)
                    (rollback_dir / "voidrunner-rollback.json").write_text(json.dumps(external["voidrunner"], indent=2))
                except OSError as exc:
                    raise BackupError(f"Restored data is in place, but rollback metadata could not be written; "
                                      f"see {state_path} for manual recovery before restarting.") from exc
        finally:
            if not state_path.exists():
                # On unresolved failure, retain staging named by the journal.
                for path in war_stages.values():
                    path.unlink(missing_ok=True)
                for path in (staging_dir, game_stage):
                    if path is not None and path.exists():
                        shutil.rmtree(path, ignore_errors=True)
        state_path.unlink(missing_ok=True)
        for path in war_stages.values():
            path.unlink(missing_ok=True)
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
    restore_parser.add_argument("--war-dialer-to", action="append", metavar="KEY=PATH",
                                help="explicit destination for each War Dialer world key shown by create")
    args = parser.parse_args(argv)

    if args.command == "create":
        try:
            destination = create_backup(db_path=args.db, identity_dir=args.identity_dir, destination=args.destination,
                                        voidrunner_save_dir=args.voidrunner_save_dir)
        except BackupError as exc:
            raise SystemExit(terminal_wrapped(f"backup failed: {exc}", stream=sys.stderr)) from exc
        print_wrapped(f"Backup created at {destination}")
        war = json.loads((destination / _MANIFEST_FILENAME).read_text()).get("war_dialer")
        for world in war["worlds"] if war else []:
            print_wrapped(f"War Dialer world {world['key']}: included {world['source_path']}.")
        receipts = json.loads((destination / _MANIFEST_FILENAME).read_text()).get("door_outbound")
        if receipts is not None:
            total = sum(len(door["receipts"]) for door in receipts["doors"])
            doors = len(receipts["doors"])
            left = receipts.get("skipped") or 0
            gone = receipts.get("pruned") or 0
            print_wrapped(
                f"Door outbound: included {total} receipt{'' if total == 1 else 's'} for "
                f"{doors} door{'' if doors == 1 else 's'}."
                + (f" {left} other entr{'y' if left == 1 else 'ies'} left in place, not "
                   "receipts this node wrote." if left else "")
                + (f" {gone} receipt{'' if gone == 1 else 's'} the node pruned while this "
                   "backup ran." if gone else ""))
        created = json.loads((destination / _MANIFEST_FILENAME).read_text())
        coverage = created.get("voidrunner")
        # From the manifest this run just wrote, never a fresh lookup
        # (Codex review): the database is mutable and the node may have
        # started since, so asking again can describe a directory this
        # backup never inspected.
        looked_in = created.get("voidrunner_source") or {}
        source_directory = looked_in.get("directory", "(unrecorded)")
        provenance = looked_in.get("provenance", "guess")
        if coverage is not None and provenance != "guess":
            print_wrapped(f"Voidrunner: included {len(coverage['files'])} retained files from {source_directory}.")
        elif coverage is not None:
            # Codex review: the nastiest case of all, because it reads as
            # success. The node has recorded nothing, and the fallback
            # path *happens to exist* -- a SysOp who once ran a node from
            # their own shell before setting up the service has exactly
            # this directory, holding exactly the wrong careers. Captured
            # anyway rather than refused: on a single-user node with no
            # service account the guess is simply correct, and taking that
            # away would turn an upgrade into data loss. But it is never
            # allowed to read as a finding.
            print_wrapped(
                f"Voidrunner: included {len(coverage['files'])} retained files from {source_directory} "
                f"-- WARNING, GUESSED LOCATION."
            )
            print_wrapped(
                "  This node has recorded no save directory (it has not started since the version that "
                "records one), so that path came from this shell's own home directory. If the node runs "
                "with a different HOME -- examples/netbbs.rc sets it to the state directory -- these are "
                "not its careers. Start the node once, or re-run with --voidrunner-save-dir."
            )
        elif provenance != "guess":
            # The node named this directory, or the operator did. Either
            # way "nothing there" is a fact about the node rather than
            # about this shell.
            print_wrapped(f"Voidrunner: no saves at {source_directory}.")
        else:
            # Issue #555: the case that used to read exactly like the one
            # above and meant something entirely different. Nothing in the
            # database says where this node keeps its careers -- it has not
            # started since this version -- so the directory below was
            # derived from *this* process's home, which is not necessarily
            # the node's. Saying so is the whole point: the old wording
            # sent an operator away believing a rollback point was complete.
            print_wrapped(
                f"Voidrunner: NOT CAPTURED. This node has recorded no save directory (it has not "
                f"started since the version that records one), so {source_directory} was derived "
                f"from this shell's own home directory, and nothing is there."
            )
            print_wrapped(
                "  If the node runs with a different HOME -- examples/netbbs.rc sets it to the "
                "state directory -- this backup is missing every Voidrunner career. Start the node "
                "once, or re-run with --voidrunner-save-dir pointing at the node's own "
                ".netbbs/voidrunner_saves."
            )
    else:
        try:
            rollback_dir = restore_backup(source=args.source, db_path=args.db, identity_dir=args.identity_dir,
                                          voidrunner_to=args.voidrunner_to,
                                          war_dialer_to=_parse_war_dialer_destinations(args.war_dialer_to))
        except BackupError as exc:
            raise SystemExit(terminal_wrapped(f"restore failed: {exc}", stream=sys.stderr)) from exc
        print_wrapped(f"Restored {args.source} into {args.db} / {args.identity_dir}")
        if args.war_dialer_to:
            print_wrapped("War Dialer restored. MANUAL: configure each restored door's world path before restarting; "
                          "the source service must remain stopped. The archived node owns these user IDs.")
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
