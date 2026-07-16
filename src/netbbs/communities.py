"""
Communities (design doc §16, rounds 71/83/84/86): a topic-oriented
navigation/container layer above boards, chat channels, and file areas.
Local Communities only in this module -- Link Communities (round 86)
become a property layered onto this same `communities` row once Phase
6's signed-event/DAG governance machinery exists, not a separate object
type or table.

A Community is a coordination/container object, not a unified content
type (round 87's wording fix) -- `netbbs.boards`, `netbbs.chat`, and
`netbbs.files` remain exactly what they are today, independent,
separately-packaged resource types with their own behavior. A board/
channel/file_area optionally belongs to at most one Community
(`community_id`, nullable FK) -- zero-or-one, never several (round 83;
see the design doc for why many-to-many and a mandatory-with-a-default-
"Uncategorized" shape were both rejected). "No Community" (`community_id
IS NULL`) is a real, common, distinct state, not a fallback synthetic
Community.

Two different inheritance mechanics, matching the two different kinds
of data §13 already has (round 84):
- **Scalar defaults** (`get_effective_*` below): a resource's own
  explicit value wins if set (including an explicit `0`), else its
  Community's default if it belongs to one, else the hardcoded system
  default. `min_read_level`/`min_write_level` (boards/file areas) and
  `min_age`/`name_requirement` (all three resource types) all follow
  this shape. `channels.min_level` deliberately does NOT -- see the
  round-104 migration note in `netbbs.storage.migrations` for why that
  field was left out of Community inheritance rather than inventing a
  `default_min_level` field the design doc never specified.
- **Moderator grant authority** (Community-blanket tier) lives in
  `netbbs.moderation.roles` instead -- see that module's docstring.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class CommunityError(Exception):
    """Raised for Community creation/lookup/update failures."""


@dataclass(frozen=True)
class Community:
    id: int
    name: str
    description: str | None
    hidden: bool
    default_min_read_level: int | None
    default_min_write_level: int | None
    default_min_age: int | None
    default_name_requirement: str | None  # None | "verified" | "verified_and_displayed"
    created_at: str


def create_community(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    hidden: bool = False,
    default_min_read_level: int | None = None,
    default_min_write_level: int | None = None,
    default_min_age: int | None = None,
    default_name_requirement: str | None = None,
    creator: User,
) -> Community:
    """
    Create a new local Community.

    Every default field is nullable and `None` by default -- an
    unassigned default imposes no floor of its own, the same "resource's
    own explicit value, else Community default, else system default"
    chain described in this module's docstring just resolves one level
    further down (to 0, or "no gate") when the Community itself doesn't
    set one either. `create`/`update` deliberately stay lean here
    (design doc round 84: "Create stays lean, Edit carries the rest,
    same split boards already use") -- callers building an admin "create"
    screen are free to prompt for only `name`/`description` and leave
    every default field to its own default, following up with a
    separate `update_community` call for the rest.

    No permission check on *creating* a Community here -- same
    reasoning as `netbbs.boards.boards.create_board`: an admin-level
    action with no SysOp/moderator concept baked into the data layer
    itself.
    """
    if default_name_requirement not in (None, "verified", "verified_and_displayed"):
        raise CommunityError(f"invalid default_name_requirement: {default_name_requirement!r}")
    created_at = utc_now_iso()
    try:
        db.connection.execute(
            """
            INSERT INTO communities
                (name, description, hidden, default_min_read_level, default_min_write_level,
                 default_min_age, default_name_requirement, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                description,
                int(hidden),
                default_min_read_level,
                default_min_write_level,
                default_min_age,
                default_name_requirement,
                created_at,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise CommunityError(f"could not create Community {name!r} — name already in use?") from exc

    new_community = get_community_by_name(db, name)
    record_action(
        db, actor=creator, action="create_community", object_type="community", object_id=new_community.id,
        detail=f"created Community {name!r}",
    )
    return new_community


def get_community(db: Database, community_id: int | None) -> Community | None:
    """Non-raising lookup by ID -- returns `None` for `community_id is
    None` too, so callers can pass a resource's own (possibly-`None`)
    `community_id` straight through without an extra guard, matching
    how `get_effective_*` below use this internally."""
    if community_id is None:
        return None
    row = db.connection.execute("SELECT * FROM communities WHERE id = ?", (community_id,)).fetchone()
    return _row_to_community(row) if row is not None else None


def get_community_by_name(db: Database, name: str) -> Community:
    row = db.connection.execute("SELECT * FROM communities WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise CommunityError(f"no such Community: {name!r}")
    return _row_to_community(row)


def list_communities(db: Database) -> list[Community]:
    """Every Community, alphabetical -- no activity/volume sort the way
    `list_boards`/`list_file_areas` have, since a Community has no
    content or timestamped activity of its own to rank by."""
    rows = db.connection.execute("SELECT * FROM communities ORDER BY name COLLATE NOCASE ASC").fetchall()
    return [_row_to_community(row) for row in rows]


def update_community(
    db: Database,
    community: Community,
    *,
    name: str,
    description: str | None,
    hidden: bool,
    default_min_read_level: int | None,
    default_min_write_level: int | None,
    default_min_age: int | None,
    default_name_requirement: str | None,
    changed_by: User,
) -> Community:
    """Replace `community`'s editable settings with the given full
    state -- mirrors `netbbs.boards.boards.update_board`'s "every field
    required, not partial/PATCH-style" shape exactly."""
    if default_name_requirement not in (None, "verified", "verified_and_displayed"):
        raise CommunityError(f"invalid default_name_requirement: {default_name_requirement!r}")
    try:
        db.connection.execute(
            """
            UPDATE communities
            SET name = ?, description = ?, hidden = ?, default_min_read_level = ?,
                default_min_write_level = ?, default_min_age = ?, default_name_requirement = ?
            WHERE id = ?
            """,
            (
                name, description, int(hidden), default_min_read_level,
                default_min_write_level, default_min_age, default_name_requirement, community.id,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise CommunityError(f"could not update Community {community.name!r} — name already in use?") from exc

    updated = get_community_by_name(db, name)
    record_action(
        db, actor=changed_by, action="update_community", object_type="community", object_id=community.id,
        detail=f"updated Community {community.name!r}",
    )
    return updated


def delete_community(db: Database, community: Community, *, deleted_by: User) -> None:
    """
    Permanently remove `community`.

    Every referencing board/channel/file_area reverts to `community_id
    = NULL` (Uncategorized) -- the reverse of assignment, not a
    cascading delete of the resources themselves, matching design doc
    round 83's "migration is a non-event" framing: losing a Community
    should never destroy the boards/channels/areas that were in it. Any
    Community-blanket moderator grant scoped to this Community is
    revoked outright (`netbbs.moderation.roles`'s Community-blanket
    tier has no meaning once its Community is gone) rather than left
    dangling. Logged before deleting, matching `delete_board`/
    `delete_user`'s own "log first" precedent (design doc round 57).

    Computing and confirming the blast radius (how many resources/
    grants this affects) before calling this function is the admin UI's
    job, same "detail screen owns the confirmation prompt" split
    already used for board/channel/area deletion.
    """
    record_action(
        db, actor=deleted_by, action="delete_community", object_type="community", object_id=community.id,
        detail=f"deleted Community {community.name!r} (id {community.id})",
    )
    db.connection.execute("UPDATE boards SET community_id = NULL WHERE community_id = ?", (community.id,))
    db.connection.execute("UPDATE channels SET community_id = NULL WHERE community_id = ?", (community.id,))
    db.connection.execute("UPDATE file_areas SET community_id = NULL WHERE community_id = ?", (community.id,))
    db.connection.execute("DELETE FROM moderator_grants WHERE community_id = ?", (community.id,))
    db.connection.execute("DELETE FROM communities WHERE id = ?", (community.id,))
    db.connection.commit()


# -- scalar-default resolution (design doc §16, round 84) -------------------


def get_effective_min_read_level(db: Database, resource) -> int:
    """`resource`'s own `min_read_level` if explicitly set (including
    `0`), else its Community's `default_min_read_level` if it belongs
    to one and that Community sets one, else the hardcoded system
    default of `0`. Works for any resource exposing `min_read_level`/
    `community_id` -- boards and file areas today."""
    if resource.min_read_level is not None:
        return resource.min_read_level
    community = get_community(db, resource.community_id)
    if community is not None and community.default_min_read_level is not None:
        return community.default_min_read_level
    return 0


def get_effective_min_write_level(db: Database, resource) -> int:
    """Symmetric counterpart to `get_effective_min_read_level` for
    `min_write_level`/`default_min_write_level`."""
    if resource.min_write_level is not None:
        return resource.min_write_level
    community = get_community(db, resource.community_id)
    if community is not None and community.default_min_write_level is not None:
        return community.default_min_write_level
    return 0


def get_effective_min_age(db: Database, resource) -> int | None:
    """`resource`'s own `min_age` if explicitly set, else its
    Community's `default_min_age` if it belongs to one (which may
    itself be `None`, meaning the Community imposes no age gate
    either), else `None` (no gate). Works for boards, channels, and file
    areas -- all three carry `min_age`/`community_id`."""
    if resource.min_age is not None:
        return resource.min_age
    community = get_community(db, resource.community_id)
    if community is not None:
        return community.default_min_age
    return None


def get_effective_name_requirement(db: Database, resource) -> str | None:
    """Symmetric counterpart to `get_effective_min_age` for
    `name_requirement`/`default_name_requirement`."""
    if resource.name_requirement is not None:
        return resource.name_requirement
    community = get_community(db, resource.community_id)
    if community is not None:
        return community.default_name_requirement
    return None


def _row_to_community(row: sqlite3.Row) -> Community:
    return Community(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        hidden=bool(row["hidden"]),
        default_min_read_level=row["default_min_read_level"],
        default_min_write_level=row["default_min_write_level"],
        default_min_age=row["default_min_age"],
        default_name_requirement=row["default_name_requirement"],
        created_at=row["created_at"],
    )
