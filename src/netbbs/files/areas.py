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
from netbbs.moderation.log import record_action
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
    # Nullable (design doc §16, round 84's correction) -- see
    # netbbs.boards.boards.Board's own fields for the full
    # Community-inheritance reasoning; identical here.
    min_read_level: int | None
    min_write_level: int | None
    category_id: int | None
    pinned: bool
    created_at: str
    moderated: bool
    max_file_age_days: int | None
    # Age/name-gating (design doc §18, rounds 85/86/101/102) -- nullable,
    # NULL means no gate *and* (since round 86/§16) "inherit this
    # Community's default" if this area belongs to one, same shape and
    # enforcement point as netbbs.boards.boards.Board's own fields; see
    # netbbs.net.file_flow's browse/upload checks.
    min_age: int | None
    name_requirement: str | None  # None | "verified" | "verified_and_displayed"
    # Zero-or-one, nullable FK (design doc §16, round 83), same shape as
    # Board.community_id.
    community_id: int | None


def create_file_area(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    min_read_level: int | None = 0,
    min_write_level: int | None = 0,
    category_id: int | None = None,
    pinned: bool = False,
    moderated: bool = False,
    max_file_age_days: int | None = None,
    min_age: int | None = None,
    name_requirement: str | None = None,
    community_id: int | None = None,
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

    `min_age`/`name_requirement` (design doc §18, rounds 85/86/101/102)
    are the same nullable-means-no-gate shape as
    `netbbs.boards.boards.create_board`'s own fields — see that
    function's docstring. `min_read_level`/`min_write_level` (nullable,
    §16 round 84) and `community_id` (§16 round 83) follow that same
    docstring's Community-inheritance reasoning.

    No permission check on *creating* an area here — same reasoning as
    board/channel creation: an admin-level action with no SysOp/moderator
    concept defined yet in Phase 1.
    """
    if name_requirement not in (None, "verified", "verified_and_displayed"):
        raise FileAreaError(f"invalid name_requirement: {name_requirement!r}")
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
                 category_id, pinned, created_at, moderated, max_file_age_days,
                 min_age, name_requirement, community_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                min_age,
                name_requirement,
                community_id,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise FileAreaError(f"could not create file area {name!r} — name already in use?") from exc

    new_area = get_file_area_by_name(db, name)
    record_action(
        db, actor=creator, action="create_file_area", object_type="file_area", object_id=new_area.id,
        detail=f"created file area {name!r}",
    )
    return new_area


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

      - "activity" (default): most recent *approved, non-expired*
        upload first (an area with no such files falls back to its own
        creation time) -- pending/expired entries don't count, same
        reasoning as list_boards (GitHub issue #36).
      - "alphabetical": by name, case-insensitive.
      - "volume": count of approved, non-expired files, highest first
        -- not a raw row count (GitHub issue #36).

    "Non-expired" means *effectively* non-expired, not just not yet
    swept into `status = 'expired'` (GitHub issue #36, reopened) —
    mirrors `list_boards`'s own fix exactly, for the identical reason:
    expiry sweeping (`netbbs.files.entries._sweep_expired_files`) is
    lazy, triggered only by something actually browsing that specific
    area, so a file already past `area.max_file_age_days` can otherwise
    sit stored as `'approved'` — and keep counting toward both
    rankings — indefinitely until that happens. Computed inline as a
    read-only predicate rather than by sweeping from here (see
    `list_boards` for the fuller reasoning); excludes the grace period
    for the same reason (governs hard-deletion of already-expired rows,
    not whether a row is still live content); `exempt_from_expiry`
    files are excluded from the check entirely, same as the sweep. One
    `now` value is reused across every placeholder in a single call.

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
        now = utc_now_iso()
        rows = db.connection.execute(
            """
            SELECT a.*, COUNT(f.id) AS file_count
            FROM file_areas a
            LEFT JOIN files f ON f.area_id = a.id
                AND f.status = 'approved'
                AND (
                      f.exempt_from_expiry = 1
                      OR a.max_file_age_days IS NULL
                      OR julianday(f.created_at) >= julianday(?) - a.max_file_age_days
                )
            GROUP BY a.id
            ORDER BY a.pinned DESC, file_count DESC, a.name COLLATE NOCASE ASC
            """,
            (now,),
        ).fetchall()
    else:  # "activity"
        now = utc_now_iso()
        rows = db.connection.execute(
            """
            SELECT a.*, COALESCE(MAX(f.created_at), a.created_at) AS last_activity
            FROM file_areas a
            LEFT JOIN files f ON f.area_id = a.id
                AND f.status = 'approved'
                AND (
                      f.exempt_from_expiry = 1
                      OR a.max_file_age_days IS NULL
                      OR julianday(f.created_at) >= julianday(?) - a.max_file_age_days
                )
            GROUP BY a.id
            ORDER BY a.pinned DESC, last_activity DESC
            """,
            (now,),
        ).fetchall()

    return [_row_to_file_area(row) for row in rows]


def update_file_area(
    db: Database,
    area: FileArea,
    *,
    name: str,
    description: str | None,
    min_read_level: int | None,
    min_write_level: int | None,
    category_id: int | None,
    pinned: bool,
    moderated: bool,
    max_file_age_days: int | None,
    min_age: int | None,
    name_requirement: str | None,
    community_id: int | None,
    changed_by: User,
) -> FileArea:
    """Replace `area`'s editable settings with the given full state --
    mirrors `netbbs.boards.boards.update_board` exactly, see that
    function's docstring for the full reasoning. `min_age`/
    `name_requirement` follow design doc §18 (rounds 101/102).
    `min_read_level`/`min_write_level`/`community_id` follow design doc
    §16 (rounds 83/84)."""
    if name_requirement not in (None, "verified", "verified_and_displayed"):
        raise FileAreaError(f"invalid name_requirement: {name_requirement!r}")
    try:
        db.connection.execute(
            """
            UPDATE file_areas
            SET name = ?, description = ?, min_read_level = ?, min_write_level = ?,
                category_id = ?, pinned = ?, moderated = ?, max_file_age_days = ?,
                min_age = ?, name_requirement = ?, community_id = ?
            WHERE id = ?
            """,
            (
                name, description, min_read_level, min_write_level,
                category_id, int(pinned), int(moderated), max_file_age_days,
                min_age, name_requirement, community_id, area.id,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise FileAreaError(f"could not update file area {area.name!r} — name already in use?") from exc

    updated = get_file_area_by_name(db, name)
    record_action(
        db, actor=changed_by, action="update_file_area", object_type="file_area", object_id=area.id,
        detail=f"updated file area {area.name!r}",
    )
    return updated


def delete_file_area(db: Database, area: FileArea, *, deleted_by: User) -> None:
    """Permanently remove `area`, along with its files and any
    moderator grants scoped to it -- mirrors
    `netbbs.boards.boards.delete_board` exactly, see that function's
    docstring for the full reasoning (including why this is handled at
    the application level rather than via a schema ON DELETE clause)."""
    record_action(
        db, actor=deleted_by, action="delete_file_area", object_type="file_area", object_id=area.id,
        detail=f"deleted file area {area.name!r} (id {area.id})",
    )
    db.connection.execute("DELETE FROM files WHERE area_id = ?", (area.id,))
    db.connection.execute(
        "DELETE FROM moderator_grants WHERE object_type = 'file_area' AND object_id = ?", (area.id,)
    )
    db.connection.execute("DELETE FROM file_areas WHERE id = ?", (area.id,))
    db.connection.commit()


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
        min_age=row["min_age"],
        name_requirement=row["name_requirement"],
        community_id=row["community_id"],
    )
