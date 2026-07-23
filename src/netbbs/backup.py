"""
Node backup and restore (design doc §13.4/§13.10, issue #60's first
operational slice, hardened by issue #75).

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

Standalone `python -m netbbs.backup {create,restore}` CLI, deliberately
not an interactive SysOp-menu action, since backups need to be
cron-schedulable -- this project has no background scheduler anywhere
and won't grow one just for this, matching `netbbs.boards.posts.
_sweep_expired_posts`'s and `netbbs.files.gc`'s own precedent of "the
operator/an external trigger drives it, not a built-in timer."

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
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
from pathlib import Path

from netbbs import __version__
from netbbs.config import get_config, set_config
from netbbs.link.node_identity import NodeIdentity, NodeIdentityError
from netbbs.selfupdate import snapshot_database
from netbbs.storage.database import Database
from netbbs.storage.migrations import MIGRATIONS
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

_RESTORE_STAGING_PREFIX = ".netbbs-restore-staging-"
_RESTORE_ROLLBACK_PREFIX = ".netbbs-restore-rollback-"
_RESTORE_STATE_FILENAME = ".netbbs-restore-state.json"


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

    The manifest's `checksums` (design doc §13.10, issue #75) cover
    every file captured *outside* the content-addressed `files/` tree
    -- the database snapshot, each identity file, the SSH host key, and
    the welcome banner. The blob tree needs no manifest entry at all:
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

    destination.mkdir(parents=True)

    snapshot_database(db_path, destination / _DB_FILENAME)
    checksums = {_DB_FILENAME: _sha256_of_file(destination / _DB_FILENAME)}

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

    for extra_path in (_ssh_host_key_path_for(db_path), _welcome_banner_path_for(db_path)):
        if extra_path.exists():
            shutil.copy2(extra_path, destination / extra_path.name)
            checksums[extra_path.name] = _sha256_of_file(destination / extra_path.name)

    manifest = {
        "created_at": utc_now_iso(),
        "netbbs_version": __version__,
        "db_user_version": _read_user_version(db_path),
        "source_db_path": str(db_path),
        "source_identity_dir": str(identity_dir),
        "checksums": checksums,
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

    db_snapshot = source / _DB_FILENAME
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


def _write_restore_state(state_path: Path, *, staging_dir: Path, rollback_dir: Path, pending: list[str]) -> None:
    state_path.write_text(
        json.dumps(
            {
                "started_at": utc_now_iso(),
                "staging_dir": str(staging_dir),
                "rollback_dir": str(rollback_dir),
                "pending_artifacts": pending,
            },
            indent=2,
        )
    )


def _restore_switch_plan(staging_dir: Path, db_path: Path, identity_dir: Path) -> list[tuple[str, Path, Path]]:
    """`(name, staged_path, live_path)` for every artifact this backup
    actually contains, in the same DB/files/identity/extras order
    `create_backup` captures them (design doc §13.4's own ordering
    reasoning) -- SSH host key, welcome banner, and forward-compatibly
    anything a future `create_backup` adds restore generically, keeping
    their filename, exactly like the pre-issue-#75 restore logic did."""
    plan: list[tuple[str, Path, Path]] = [("db", staging_dir / _DB_FILENAME, db_path)]

    staged_files = staging_dir / _FILES_DIRNAME
    if staged_files.is_dir():
        plan.append(("files", staged_files, _storage_root_for(db_path)))

    staged_identity = staging_dir / _IDENTITY_DIRNAME
    if staged_identity.is_dir():
        plan.append(("identity", staged_identity, identity_dir))

    for entry in sorted(staging_dir.iterdir()):
        if entry.name in _RESERVED_BACKUP_ENTRIES:
            continue
        if entry.is_file():
            plan.append((entry.name, entry, db_path.parent / entry.name))

    return plan


def _switch_one(name: str, staged_path: Path, live_path: Path, rollback_dir: Path) -> None:
    """Atomic (same-filesystem) rename in each direction -- never a
    copy. `rollback_dir` is created lazily, only once something
    actually needs preserving (a fresh target with nothing live yet
    leaves no rollback directory behind at all)."""
    if live_path.exists():
        rollback_dir.mkdir(parents=True, exist_ok=True)
        live_path.rename(rollback_dir / name)
    staged_path.rename(live_path)


def _rollback_switched(switched: list[tuple[str, Path, Path]], rollback_dir: Path) -> None:
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


def restore_backup(*, source: Path, db_path: Path, identity_dir: Path) -> Path | None:
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
    _validate_backup_source(source, allow_migrate=False)

    if db_path.exists():
        _require_not_in_use(db_path)
    _require_node_not_running(db_path)
    _refuse_if_restore_in_progress(db_path)

    token = secrets.token_hex(6)
    staging_dir = db_path.parent / f"{_RESTORE_STAGING_PREFIX}{token}"
    rollback_dir = db_path.parent / f"{_RESTORE_ROLLBACK_PREFIX}{token}"
    state_path = _restore_state_path_for(db_path)

    staging_dir.mkdir(parents=True)
    try:
        shutil.copytree(source, staging_dir, dirs_exist_ok=True)
        _validate_backup_source(staging_dir, allow_migrate=True)

        plan = _restore_switch_plan(staging_dir, db_path, identity_dir)
        _write_restore_state(
            state_path, staging_dir=staging_dir, rollback_dir=rollback_dir, pending=[name for name, _, _ in plan]
        )

        switched: list[tuple[str, Path, Path]] = []
        for name, staged_path, live_path in plan:
            try:
                _switch_one(name, staged_path, live_path, rollback_dir)
            except Exception as exc:
                try:
                    _rollback_switched(switched, rollback_dir)
                except Exception:
                    raise BackupError(
                        f"restore failed while switching {name!r} and the automatic rollback "
                        f"also failed -- see {state_path} for exactly what's where; manual "
                        "recovery needed"
                    ) from exc
                state_path.unlink(missing_ok=True)
                raise BackupError(
                    f"restore failed while switching {name!r}, automatically rolled back to "
                    f"the previous generation: {exc}"
                ) from exc
            switched.append((name, staged_path, live_path))
            remaining = [n for n, _, _ in plan if n not in {s[0] for s in switched}]
            _write_restore_state(state_path, staging_dir=staging_dir, rollback_dir=rollback_dir, pending=remaining)
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

    state_path.unlink(missing_ok=True)
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

    args = parser.parse_args(argv)

    if args.command == "create":
        try:
            destination = create_backup(db_path=args.db, identity_dir=args.identity_dir, destination=args.destination)
        except BackupError as exc:
            raise SystemExit(f"backup failed: {exc}") from exc
        print(f"Backup created at {destination}")
    else:
        try:
            rollback_dir = restore_backup(source=args.source, db_path=args.db, identity_dir=args.identity_dir)
        except BackupError as exc:
            raise SystemExit(f"restore failed: {exc}") from exc
        print(f"Restored {args.source} into {args.db} / {args.identity_dir}")
        if rollback_dir is not None:
            print(
                f"Previous generation preserved at {rollback_dir} -- not deleted automatically, "
                "remove it yourself once you're satisfied the restore is good."
            )


if __name__ == "__main__":
    main()
