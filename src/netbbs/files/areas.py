"""
File areas: local-only (design doc §9, §15 phasing) — no Link yet.
"Area" always means *file* area, never "board" (design doc §1's strict
terminology rule). Area IDs are content-addressed (§7) for the same
reason board/channel IDs are: no ID-scheme migration needed when file
areas can become Linked in a later phase.

Categories, pinning, and sort order are built in from the start here,
unlike boards/channels — those got this shape retrofitted in design doc
round 18, after shipping without it first. Doing it up front for file
areas avoids repeating that same later migration, consistent with the
project's broader anti-retrofit principle (§2/§13).

Moderator/permission grants (`netbbs.moderation.roles`) and per-area
moderation settings (`moderated`, `max_file_age_days`) layer on top of
the coarse `min_read_level`/`min_write_level` gate here, mirroring
`netbbs.boards.boards` — see `netbbs.files.entries` for where those
settings actually change file behavior (design doc sign-off round 36).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.boards.content_id import compute_content_id
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

# Mirrors netbbs.boards.boards._VALID_SORT_ORDERS exactly — same three
# signals are meaningful for file areas as for boards: activity (most
# recent upload), alphabetical, and volume (file count).
_VALID_SORT_ORDERS = ("activity", "alphabetical", "volume")


class FileAreaError(Exception):
    """Raised for file area creation/lookup failures."""


@dataclass(frozen=True)
class FileArea:
    id: int
    area_id: str
    name: str
    description: str | None
    min_read_level: int
    min_write_level: int
    category_id: int | None
    pinned: bool
    created_at: str
    moderated: bool
    max_file_age_days: int | None


def create_file_area(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    min_read_level: int = 0,
    min_write_level: int = 0,
    category_id: int | None = None,
    pinned: bool = False,
    moderated: bool = False,
    max_file_age_days: int | None = None,
    creator: User,
) -> FileArea:
    """
    Create a new local file area.

    `min_read_level`/`min_write_level` mirror `netbbs.boards.boards.
    create_board`'s coarse level-gate exactly — design doc §13 confirms
    file areas get the same separate read/write split as boards (unlike
    chat, where access is binary). The richer per-area moderator model
    (`netbbs.moderation.roles`) layers on top, same as boards.

    `moderated` gates whether new uploads start `'pending'` (requiring a
    holder of `BoardPermission.APPROVE` to approve them) or go straight
    to `'approved'` — see `netbbs.files.entries.upload_file`.
    `max_file_age_days` is this area's own maintenance/expiry threshold;
    `None` means retain indefinitely, the default.

    No permission check on *creating* an area here — same reasoning as
    board/channel creation: an admin-level action with no SysOp/moderator
    concept defined yet in Phase 1.
    """
    created_at = utc_now_iso()
    area_id = compute_content_id(
        {
            "type": "file_area",
            "name": name,
            "creator": creator.fingerprint or creator.username,
            "created_at": created_at,
        }
    )

    try:
        db.connection.execute(
            """
            INSERT INTO file_areas
                (area_id, name, description, min_read_level, min_write_level,
                 category_id, pinned, created_at, moderated, max_file_age_days)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                area_id,
                name,
                description,
                min_read_level,
                min_write_level,
                category_id,
                int(pinned),
                created_at,
                int(moderated),
                max_file_age_days,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise FileAreaError(f"could not create file area {name!r} — name already in use?") from exc

    return get_file_area_by_name(db, name)


def get_file_area_by_name(db: Database, name: str) -> FileArea:
    row = db.connection.execute("SELECT * FROM file_areas WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise FileAreaError(f"no such file area: {name!r}")
    return _row_to_file_area(row)


def list_file_areas(db: Database, *, order_by: str = "activity") -> list[FileArea]:
    """
    List all file areas. Pinned areas always sort first, then the rest in
    the chosen `order_by` — identical semantics to
    `netbbs.boards.boards.list_boards`:

      - "activity" (default): most recent upload first (an area with no
        files yet falls back to its own creation time).
      - "alphabetical": by name, case-insensitive.
      - "volume": total file count, highest first.

    Deliberately does *not* filter by any requesting user's level here —
    same reasoning as `list_boards`: "list every area for an admin view"
    and "list areas a given user can actually read" are both legitimate,
    different needs built on this same function; filtering (via
    `netbbs.permissions.meets_level` against each area's
    `min_read_level`) is left to the caller.
    """
    if order_by not in _VALID_SORT_ORDERS:
        raise ValueError(f"order_by must be one of {_VALID_SORT_ORDERS}, got {order_by!r}")

    if order_by == "alphabetical":
        rows = db.connection.execute(
            "SELECT * FROM file_areas ORDER BY pinned DESC, name COLLATE NOCASE ASC"
        ).fetchall()
    elif order_by == "volume":
        rows = db.connection.execute(
            """
            SELECT a.*, COUNT(f.id) AS file_count
            FROM file_areas a
            LEFT JOIN files f ON f.area_id = a.id
            GROUP BY a.id
            ORDER BY a.pinned DESC, file_count DESC, a.name COLLATE NOCASE ASC
            """
        ).fetchall()
    else:  # "activity"
        rows = db.connection.execute(
            """
            SELECT a.*, COALESCE(MAX(f.created_at), a.created_at) AS last_activity
            FROM file_areas a
            LEFT JOIN files f ON f.area_id = a.id
            GROUP BY a.id
            ORDER BY a.pinned DESC, last_activity DESC
            """
        ).fetchall()

    return [_row_to_file_area(row) for row in rows]


def _row_to_file_area(row: sqlite3.Row) -> FileArea:
    return FileArea(
        id=row["id"],
        area_id=row["area_id"],
        name=row["name"],
        description=row["description"],
        min_read_level=row["min_read_level"],
        min_write_level=row["min_write_level"],
        category_id=row["category_id"],
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
        moderated=bool(row["moderated"]),
        max_file_age_days=row["max_file_age_days"],
    )
