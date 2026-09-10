"""Local SysOp commands for owned War Dialer worlds; no service activation."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from netbbs import backup
from netbbs.doors.bundled import war_dialer as wd
from netbbs.rendering.reflow import print_wrapped, terminal_wrapped

AUDIT_LIMIT = 100


def _target(db_path: Path, world: Path) -> Path:
    world = world.expanduser().resolve()
    if world not in backup._war_dialer_sources(db_path):
        raise backup.BackupError("World is not a configured path for this node. Check the service/profile override.")
    backup._inspect_war_dialer(world, backup._war_dialer_owner(db_path))
    return world


def _audit(conn, action, *, reason="", backup_path=None, before=None, after=None):
    row = conn.execute("SELECT value FROM meta WHERE key='sysop_audit'").fetchone()
    entries = json.loads(row[0]) if row else []
    if not isinstance(entries, list):
        raise backup.BackupError("World audit data is invalid; preserve the world for recovery.")
    entry = {"at": wd.to_iso(wd.now_utc()), "action": action,
             "operator": (os.environ.get("USERNAME") or os.environ.get("USER") or "local SysOp")[:80],
             "reason": reason[:240], "before_season": before, "after_season": after}
    if backup_path is not None:
        entry["backup"] = str(backup_path.resolve())[:1024]
        entry["manifest_sha256"] = backup._sha256_of_file(backup_path / "manifest.json")
    entries = [*entries[-(AUDIT_LIMIT - 1):], entry]
    conn.execute("INSERT INTO meta (key,value) VALUES ('sysop_audit',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(entries),))


def world_status(db_path: Path, world: Path) -> dict:
    """Read current stored state without settling clocks, creating files or rows."""
    world = _target(db_path, world)
    with contextlib.closing(sqlite3.connect(world.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        meta = dict(conn.execute("SELECT key,value FROM meta WHERE key IN "
                                 "('node_owner','active_season','maintenance','sysop_audit')"))
        result = {"world": str(world), "schema": conn.execute("PRAGMA user_version").fetchone()[0],
                  "owner": meta.get("node_owner"), "maintenance": meta.get("maintenance", "off"),
                  "stored_season": meta.get("active_season", "not started")}
        for table in ("players", "exchanges", "events"):
            result[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        audit = json.loads(meta.get("sysop_audit", "[]"))
        if not isinstance(audit, list):
            raise backup.BackupError("World audit data is invalid; preserve the world for recovery.")
        result["recent_operations"] = audit[-10:]
        return result


def set_maintenance(db_path: Path, world: Path, enabled: bool) -> None:
    world = _target(db_path, world)
    with backup._war_dialer_maintenance(world), contextlib.closing(wd.connect(world)) as conn:
        with wd._write_transaction(conn):
            value = "on" if enabled else "off"
            conn.execute("INSERT INTO meta (key,value) VALUES ('maintenance',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
            _audit(conn, "maintenance " + value)


def change_competition(db_path: Path, world: Path, *, identity_dir: Path, backup_to: Path,
                       confirm: str, reason: str, reset: bool = False) -> dict:
    world = _target(db_path, world)
    if confirm != world.name or not reason.strip() or len(reason) > 240:
        raise backup.BackupError("Confirm the exact world filename and supply a reason of 1-240 characters.")
    backup._require_node_not_running(db_path)
    # Maintenance is an explicit preceding operation, and remains on afterward.
    if world_status(db_path, world)["maintenance"] != "on":
        raise backup.BackupError("Enable maintenance first and close game sessions; no season was changed.")
    if not identity_dir.is_dir():
        raise backup.BackupError("Node identity directory is unavailable. Check --identity-dir; no season was changed.")
    backup.create_backup(db_path=db_path, identity_dir=identity_dir, destination=backup_to)
    backup._validate_backup_source(backup_to, allow_migrate=False)
    with backup._war_dialer_maintenance(world), contextlib.closing(wd.connect(world)) as conn:
        with wd._write_transaction(conn):
            if conn.execute("SELECT value FROM meta WHERE key='maintenance'").fetchone()[0] != "on":
                raise backup.BackupError("Maintenance was disabled; no season was changed.")
            now = wd.now_utc()
            # Honor already-published natural cutoffs before moving the anchor.
            # Otherwise an overdue active marker would use the new calendar
            # to archive old territory earnings and permanently change awards.
            wd._settle_world(conn, now)
            before = wd.current_world_season(conn, now)
            season = before + 1
            anchor = wd.to_iso(now - (season - 1) * wd.SEASON)
            conn.execute("UPDATE meta SET value=? WHERE key='season_anchor'", (anchor,))
            wd._settle_world(conn, now)
            if reset:
                conn.execute("DELETE FROM events")
                if wd._world_schema_version(conn) >= 9:
                    conn.execute("DELETE FROM scene")
            _audit(conn, "reset competition" if reset else "advance season", reason=reason,
                   backup_path=backup_to, before=before, after=season)
    return world_status(db_path, world)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Inspect or maintain an owned War Dialer world.")
    parser.add_argument("--db", type=Path, required=True, help="owning node database")
    parser.add_argument("--world", type=Path, required=True, help="configured world database")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="read-only status and the ten latest operations")
    maintenance = commands.add_parser("maintenance", help="exclude new sessions; refuses active sessions")
    maintenance.add_argument("state", choices=("on", "off"))
    for name, help_text in (("season", "start the next season, retaining receipts"),
                            ("reset", "reset competition and receipts, retaining identities and account age")):
        command = commands.add_parser(name, help=help_text + "; requires a verified complete node backup")
        command.add_argument("--identity-dir", type=Path, required=True)
        command.add_argument("--backup-to", type=Path, required=True, help="fresh node backup directory")
        command.add_argument("--reason", required=True)
        command.add_argument("--confirm", required=True, help="exact world filename, including extension")
    args = parser.parse_args(argv)
    try:
        if args.command == "maintenance":
            set_maintenance(args.db, args.world, args.state == "on")
        elif args.command in ("season", "reset"):
            change_competition(args.db, args.world, identity_dir=args.identity_dir, backup_to=args.backup_to,
                               confirm=args.confirm, reason=args.reason, reset=args.command == "reset")
        status = world_status(args.db, args.world)
        for key, value in status.items():
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value)
            print_wrapped(f"{key}: {wd._event_plain(rendered)}")
    except (backup.BackupError, wd.WorldStateError, OSError, sqlite3.Error, ValueError) as exc:
        raise SystemExit(terminal_wrapped(f"War Dialer operation failed: {wd._event_plain(str(exc))[:500]}",
                                         stream=sys.stderr)) from exc


if __name__ == "__main__":
    main()
