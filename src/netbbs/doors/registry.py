"""
The door registry: SysOp-facing catalogue of external programs a caller
may launch and play. Mirrors `netbbs.files.areas`' shape closely (see
that module's own docstring) -- both are "a thing granted to a caller,
gated by level, optionally scoped to a Community" -- but deliberately
narrower: a single `min_play_level` gate rather than a read/write split
(launching a door is one action, not two), no categories (v1 keeps the
catalogue flat; easy to add later without disturbing this shape, unlike
retrofitting a permission split would be), and no content-addressed ID
(see the schema migration's own comment for why: doors have no stated
Link future in the locked design, issue #63/#167).

`args`, if set, is a JSON-encoded list of strings -- always launched via
`asyncio.create_subprocess_exec`'s argv-list form (see `netbbs.doors.
runtime`), never a shell, so nothing a SysOp types here can reopen
shell-metacharacter injection.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from netbbs.auth.users import User
from netbbs.doors.profiles import DoorProfile
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class DoorError(Exception):
    """Raised for door registration/lookup failures."""


def custom_doors_dir(db: Database) -> Path:
    """The conventional location for a SysOp's *own* door scripts --
    `netbbs.net.admin_flow`'s door `[F]rom disk` picker browses exactly
    this directory, bounded to it the same way issue #170's welcome-
    banner/masthead filesystem picker is bounded to `banner_path(db)`'s
    own parent: a real, narrow, already-established location under the
    node's own state directory, not open-ended traversal from `/`. A
    subdirectory rather than `db.path.parent` itself (unlike a single
    banner file, there can reasonably be many custom door scripts, and
    keeping them out of the same flat directory as the database/identity
    keys/banner files is worth the one extra path segment). Doors here
    are unfiltered by extension -- unlike banners' `.ans`-only picker, a
    door can legitimately be any executable, not one well-known format
    -- and this directory is never created automatically; it simply
    doesn't exist (and the picker reports nothing found) until a SysOp
    places something there."""
    return db.path.parent / "doors"


@dataclass(frozen=True)
class Door:
    id: int
    name: str
    description: str | None
    executable_path: str
    args: tuple[str, ...]
    min_play_level: int
    pinned: bool
    created_at: str
    community_id: int | None
    profile: DoorProfile | None = None


def create_door(
    db: Database,
    name: str,
    executable_path: str,
    *,
    description: str | None = None,
    args: tuple[str, ...] = (),
    min_play_level: int = 0,
    pinned: bool = False,
    community_id: int | None = None,
    creator: User,
    profile: DoorProfile | None = None,
) -> Door:
    """Register a new door. No permission check here -- same reasoning
    as `create_file_area`/`create_board`: an admin-level action, gated by
    the calling screen (SysOp console), not this function."""
    profile_json = profile.to_json() if profile else None
    created_at = utc_now_iso()
    try:
        db.connection.execute(
            """
            INSERT INTO doors
                (name, description, executable_path, args, min_play_level,
                 pinned, created_at, community_id, profile_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                description,
                executable_path,
                json.dumps(list(args)) if args else None,
                min_play_level,
                int(pinned),
                created_at,
                community_id,
                profile_json,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise DoorError(f"could not register door {name!r} — name already in use?") from exc

    new_door = get_door_by_name(db, name)
    record_action(
        db, actor=creator, action="create_door", object_type="door", object_id=new_door.id,
        detail=f"registered door {name!r} ({executable_path})",
    )
    return new_door


def get_door_by_name(db: Database, name: str) -> Door:
    row = db.connection.execute("SELECT * FROM doors WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise DoorError(f"no such door: {name!r}")
    return _row_to_door(row)


def list_doors(db: Database, *, community_id: int | None = None) -> list[Door]:
    """List doors, pinned first then alphabetical -- same ordering
    convention as `list_file_areas`'/`list_boards`' own default. `None`
    lists every door regardless of Community; pass an explicit
    `community_id` to scope to one. Deliberately does not filter by any
    requesting user's level -- same reasoning as `list_file_areas`: left
    to the caller (`netbbs.permissions.meets_level` against
    `min_play_level`)."""
    if community_id is None:
        rows = db.connection.execute(
            "SELECT * FROM doors ORDER BY pinned DESC, name COLLATE NOCASE ASC"
        ).fetchall()
    else:
        rows = db.connection.execute(
            "SELECT * FROM doors WHERE community_id = ? ORDER BY pinned DESC, name COLLATE NOCASE ASC",
            (community_id,),
        ).fetchall()
    return [_row_to_door(row) for row in rows]


def update_door(
    db: Database,
    door: Door,
    *,
    name: str,
    description: str | None,
    executable_path: str,
    args: tuple[str, ...],
    min_play_level: int,
    pinned: bool,
    community_id: int | None,
    changed_by: User,
    profile: DoorProfile | None = None,
) -> Door:
    """Replace `door`'s editable settings with the given full state --
    mirrors `update_file_area`'s own full-replace shape."""
    try:
        db.connection.execute(
            """
            UPDATE doors
            SET name = ?, description = ?, executable_path = ?, args = ?,
                min_play_level = ?, pinned = ?, community_id = ?, profile_json = ?
            WHERE id = ?
            """,
            (
                name, description, executable_path,
                json.dumps(list(args)) if args else None,
                min_play_level, int(pinned), community_id,
                (profile or door.profile).to_json() if (profile or door.profile) else None, door.id,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise DoorError(f"could not update door {door.name!r} — name already in use?") from exc

    updated = get_door_by_name(db, name)
    record_action(
        db, actor=changed_by, action="update_door", object_type="door", object_id=door.id,
        detail=f"updated door {door.name!r}",
    )
    return updated


def delete_door(db: Database, door: Door, *, deleted_by: User) -> None:
    record_action(
        db, actor=deleted_by, action="delete_door", object_type="door", object_id=door.id,
        detail=f"deleted door {door.name!r} (id {door.id})",
    )
    db.connection.execute("DELETE FROM doors WHERE id = ?", (door.id,))
    db.connection.commit()


def _row_to_door(row: sqlite3.Row) -> Door:
    raw_args = row["args"]
    return Door(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        executable_path=row["executable_path"],
        args=tuple(json.loads(raw_args)) if raw_args else (),
        min_play_level=row["min_play_level"],
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
        community_id=row["community_id"],
        profile=DoorProfile.from_json(row["profile_json"]) if row["profile_json"] else None,
    )
