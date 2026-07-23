"""
Node backup and restore (design doc §13.4, issue #60's first
operational slice).

A node's recoverable state is five `db_path`-relative artifacts, not
just the database: content blobs (`netbbs.files.storage`), node
identity (`netbbs.link.node_identity`), the SSH host key
(`netbbs.net.ssh`), and the welcome banner
(`netbbs.net.welcome_banner`) all live at derived paths alongside the
database, each with no independent config field of its own. A backup
covering only the database silently loses the SSH host key (every
client gets a MITM warning after restore) and, far more seriously, the
Link node identity -- root-key custody is explicitly "part of ordinary
node backup and restore" (design doc §4.5), not a separate ceremony.
This module treats all five as one atomic backup operation, never a
DB-only one.

Deliberately path-based, not `Database`-based: a backup must be safely
takeable against a live, running node, and opening a second `Database`
handle (migration-check side effects, a second long-lived WAL-mode
connection) is unnecessary work this module has no need for -- it only
ever needs `sqlite3.Connection.backup()` (via
`netbbs.selfupdate.snapshot_database`, reused rather than reinvented)
and plain filesystem copies.

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

Standalone `python -m netbbs.backup {create,restore}` CLI, deliberately
not an interactive SysOp-menu action, since backups need to be
cron-schedulable -- this project has no background scheduler anywhere
and won't grow one just for this, matching `netbbs.boards.posts.
_sweep_expired_posts`'s and `netbbs.files.gc`'s own precedent of "the
operator/an external trigger drives it, not a built-in timer."

**Explicitly deferred, not part of this slice**: encrypting backup
contents at rest (identity material is already unencrypted-by-default
on a live node -- see §4.5 -- and this tool preserves whatever it finds
rather than changing that policy); off-site/remote transport of a
completed backup directory; retention/rotation of old backups; and any
form of automatic scheduling. Also deferred: disaster-recovery drills
proving this mechanism against crash-mid-transfer, corrupt-snapshot,
and stale-backup scenarios (design doc §13.6) -- this module is the
mechanism itself, not that separate proof.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

from netbbs import __version__
from netbbs.config import get_config, set_config
from netbbs.selfupdate import snapshot_database
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

_MANIFEST_FILENAME = "manifest.json"
_DB_FILENAME = "netbbs.db"
_FILES_DIRNAME = "files"
_IDENTITY_DIRNAME = "identity"
_RESERVED_BACKUP_ENTRIES = (_MANIFEST_FILENAME, _DB_FILENAME, _FILES_DIRNAME, _IDENTITY_DIRNAME)

# node_config keys (netbbs.config's generic key-value store) -- same
# reasoning as netbbs.selfupdate's own last-check bookkeeping: purely
# for a future read-only SysOp status line, never required for restore.
_LAST_BACKUP_AT_CONFIG_KEY = "last_backup_at"
_LAST_BACKUP_PATH_CONFIG_KEY = "last_backup_path"


class BackupError(Exception):
    """Raised for any backup/restore failure."""


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


def create_backup(*, db_path: Path, identity_dir: Path, destination: Path) -> Path:
    """
    Create a complete, self-contained backup of one node's recoverable
    state at `destination` (created fresh -- refuses if it already
    exists, rather than silently merging into or overwriting a
    previous backup).

    Safe to run against a live, running node: the database step uses
    SQLite's own online backup API (`netbbs.selfupdate.
    snapshot_database`), and every other artifact is either static once
    created or already rewritten via its own atomic-replace pattern --
    see the module docstring for the one accepted exception (the
    welcome banner has no atomicity guarantee on its own writes; a
    backup landing mid-edit could capture a half-written one, purely
    cosmetic, no correctness consequence).

    Returns `destination`.
    """
    if destination.exists():
        raise BackupError(f"backup destination already exists: {destination}")
    if not db_path.exists():
        raise BackupError(f"no database found at {db_path}")

    destination.mkdir(parents=True)

    snapshot_database(db_path, destination / _DB_FILENAME)

    storage_root = _storage_root_for(db_path)
    if storage_root.is_dir():
        shutil.copytree(
            storage_root, destination / _FILES_DIRNAME, ignore=shutil.ignore_patterns(".incoming")
        )

    if identity_dir.is_dir():
        shutil.copytree(identity_dir, destination / _IDENTITY_DIRNAME)

    for extra_path in (_ssh_host_key_path_for(db_path), _welcome_banner_path_for(db_path)):
        if extra_path.exists():
            shutil.copy2(extra_path, destination / extra_path.name)

    manifest = {
        "created_at": utc_now_iso(),
        "netbbs_version": __version__,
        "db_user_version": _read_user_version(db_path),
        "source_db_path": str(db_path),
        "source_identity_dir": str(identity_dir),
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
    except Exception:
        return
    try:
        set_config(db, _LAST_BACKUP_AT_CONFIG_KEY, utc_now_iso())
        set_config(db, _LAST_BACKUP_PATH_CONFIG_KEY, str(destination))
    finally:
        db.close()


def get_last_backup_summary(db: Database) -> tuple[str | None, str | None]:
    """`(last_backup_at_iso, last_backup_path)`, either possibly `None`
    if no backup has ever been taken via `create_backup` on this node."""
    return get_config(db, _LAST_BACKUP_AT_CONFIG_KEY), get_config(db, _LAST_BACKUP_PATH_CONFIG_KEY)


def restore_backup(*, source: Path, db_path: Path, identity_dir: Path) -> None:
    """
    Restore a backup created by `create_backup` into `db_path`/
    `identity_dir` (and their derived sibling paths), overwriting
    whatever is currently there.

    Refuses, before writing anything, if `db_path` already exists and
    appears to be in active use (see `_require_not_in_use`) -- catches
    a live write actually in flight at this exact instant, which is a
    narrow but real window; it does *not* reliably detect an idle-but-
    running node (SQLite's WAL-mode locking holds the write lock only
    for a transaction's duration, not between them). The primary
    guarantee remains the documented operator precondition: stop the
    node before restoring. Restoration always resumes the same node
    identity; there is still no supported way to run an old and a
    restored instance simultaneously (design doc §13.4).
    """
    manifest_path = source / _MANIFEST_FILENAME
    if not manifest_path.exists():
        raise BackupError(f"not a backup directory (no {_MANIFEST_FILENAME}): {source}")

    if db_path.exists():
        _require_not_in_use(db_path)

    db_snapshot = source / _DB_FILENAME
    if not db_snapshot.exists():
        raise BackupError(f"backup is missing its database snapshot: {db_snapshot}")
    shutil.copy2(db_snapshot, db_path)

    backed_up_files = source / _FILES_DIRNAME
    if backed_up_files.is_dir():
        storage_root = _storage_root_for(db_path)
        if storage_root.exists():
            shutil.rmtree(storage_root)
        shutil.copytree(backed_up_files, storage_root)

    backed_up_identity = source / _IDENTITY_DIRNAME
    if backed_up_identity.is_dir():
        if identity_dir.exists():
            shutil.rmtree(identity_dir)
        shutil.copytree(backed_up_identity, identity_dir)

    # Everything else in the backup directory (the SSH host key, the
    # welcome banner, and forward-compatibly whatever a future version
    # of create_backup might add) restores generically to db_path's own
    # directory, keeping its filename -- symmetric with how those two
    # were captured in create_backup, without restore needing its own
    # copy of their derived-path formulas.
    for entry in source.iterdir():
        if entry.name in _RESERVED_BACKUP_ENTRIES:
            continue
        if entry.is_file():
            shutil.copy2(entry, db_path.parent / entry.name)


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

    args = parser.parse_args(argv)

    if args.command == "create":
        try:
            destination = create_backup(db_path=args.db, identity_dir=args.identity_dir, destination=args.destination)
        except BackupError as exc:
            raise SystemExit(f"backup failed: {exc}") from exc
        print(f"Backup created at {destination}")
    else:
        try:
            restore_backup(source=args.source, db_path=args.db, identity_dir=args.identity_dir)
        except BackupError as exc:
            raise SystemExit(f"restore failed: {exc}") from exc
        print(f"Restored {args.source} into {args.db} / {args.identity_dir}")


if __name__ == "__main__":
    main()
