"""
Moderator grants: the §13 read/write/edit/delete/approve (boards/file
areas) and edit/moderate/manage_members (channels) permission model,
layered on top of `netbbs.permissions.levels` rather than replacing
it. Level-gating answers "does this account meet a minimum level";
grants answer a narrower, additive question: "does this specific
account also hold this specific permission on this specific object —
or on every local object of this type, if locally-blanket."

See design doc sign-off round 34 for the reasoning behind the choices
this module encodes: a bitmask representation (matching §13's own
"settable individually or combined" phrasing), no partial-exception
blanket grants, and recording every grant/revoke in the shared
`netbbs.moderation.log` audit trail.

Three moderator scope tiers exist per §13: per-object, local-blanket
(`object_id` is `None` here), and Link-blanket ("global"). The last is
unreachable until Phase 6's Link-wide moderation exists and is
deliberately not modeled by a third `object_id` sentinel now — decide
that shape once Phase 6 actually needs it, rather than guessing today.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import IntFlag, auto

from netbbs.auth.users import SYSOP_LEVEL, User
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class BoardPermission(IntFlag):
    """Board/file-area permission bits (design doc §13), settable
    individually or combined, per moderator, per board/area."""

    READ = auto()
    WRITE = auto()
    EDIT = auto()
    DELETE = auto()
    APPROVE = auto()


class ChannelPermission(IntFlag):
    """
    Chat channel permission bits — a deliberately different, smaller
    set than `BoardPermission`. Chat access itself has no read/write
    split (see `netbbs.chat.channels.Channel.min_level`), so there's
    no READ/WRITE bit here. MODERATE bundles kick/mute/ban into one
    bit rather than three separately-grantable ones: §13 describes
    them as a single bundled capability of being a chat moderator
    ("Chat moderators (non-SysOp) can kick/mute/ban within their
    scope"), not as independently combinable permissions the way
    board permissions are.
    """

    EDIT = auto()  # gates /topic changes (round 33, point 5)
    MODERATE = auto()  # kick/mute/ban
    MANAGE_MEMBERS = auto()  # invite-only channel membership admin


_PERMISSION_ENUMS: dict[str, type[IntFlag]] = {
    "board": BoardPermission,
    "file_area": BoardPermission,
    "channel": ChannelPermission,
}


class ModeratorGrantError(Exception):
    """Raised for an unknown `object_type`, or a permission bit that
    doesn't belong to that `object_type`'s enum (e.g. `ChannelPermission
    .MODERATE` passed for a `board`)."""


@dataclass(frozen=True)
class ModeratorGrant:
    id: int
    user_id: int
    object_type: str
    object_id: int | None  # None == local-blanket, see module docstring
    permissions: int
    granted_by_user_id: int
    created_at: str

    def has(self, permission: IntFlag) -> bool:
        return bool(self.permissions & int(permission))


def grant_permissions(
    db: Database,
    target: User,
    *,
    object_type: str,
    object_id: int | None,
    permissions: IntFlag,
    granted_by: User,
) -> ModeratorGrant:
    """
    Grant `permissions` to `target` on one object (or every local
    object of `object_type`, if `object_id` is None — a local-blanket
    grant).

    Additive: repeating this call for the same (user, object_type,
    object_id) combines with whatever bits are already granted, rather
    than requiring the caller to already know and re-specify the
    existing set. Use `revoke_permissions` to remove specific bits.
    """
    enum_type = _validate_permission_type(object_type, permissions)
    row = _get_grant_row(db, target.id, object_type, object_id)

    if row is None:
        new_mask = int(permissions)
        db.connection.execute(
            """
            INSERT INTO moderator_grants
                (user_id, object_type, object_id, permissions, granted_by_user_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (target.id, object_type, object_id, new_mask, granted_by.id, utc_now_iso()),
        )
    else:
        new_mask = row["permissions"] | int(permissions)
        db.connection.execute(
            "UPDATE moderator_grants SET permissions = ? WHERE id = ?",
            (new_mask, row["id"]),
        )
    db.connection.commit()

    record_action(
        db,
        actor=granted_by,
        action="grant",
        object_type=object_type,
        object_id=object_id,
        target_user_id=target.id,
        detail=_describe(enum_type, int(permissions)),
    )
    return _get_grant(db, target.id, object_type, object_id)


def revoke_permissions(
    db: Database,
    target: User,
    *,
    object_type: str,
    object_id: int | None,
    permissions: IntFlag,
    revoked_by: User,
) -> ModeratorGrant | None:
    """
    Remove `permissions` from target's existing grant. If nothing is
    left in the mask afterwards, the grant row is deleted entirely and
    None is returned. A no-op (still logged) if target had no grant to
    begin with — same "idempotent removal" shape as
    `netbbs.moderation.blocklist.unblock_user`.
    """
    enum_type = _validate_permission_type(object_type, permissions)
    row = _get_grant_row(db, target.id, object_type, object_id)

    if row is not None:
        new_mask = row["permissions"] & ~int(permissions)
        if new_mask == 0:
            db.connection.execute("DELETE FROM moderator_grants WHERE id = ?", (row["id"],))
        else:
            db.connection.execute(
                "UPDATE moderator_grants SET permissions = ? WHERE id = ?",
                (new_mask, row["id"]),
            )
        db.connection.commit()

    record_action(
        db,
        actor=revoked_by,
        action="revoke",
        object_type=object_type,
        object_id=object_id,
        target_user_id=target.id,
        detail=_describe(enum_type, int(permissions)),
    )
    return _get_grant(db, target.id, object_type, object_id)


def has_permission(
    db: Database,
    user: User,
    *,
    object_type: str,
    object_id: int,
    permission: IntFlag,
) -> bool:
    """
    Does `user` hold `permission` on this object — either via a grant
    on it specifically, or a local-blanket grant covering every local
    object of `object_type`? (Deliberately no partial-exception
    blanket grants — see design doc sign-off round 34.)

    SysOp-level always satisfies this, with zero grant rows required
    (design doc -- board/area management round, a deliberate, cross-
    cutting decision -- confirmed to also apply to every existing
    consumer of this function, not just new board/area moderation:
    chat's `/mute`/`/ban`/`/kick`, Tab-completion visibility, etc.).
    Input validation still runs first regardless of caller identity, so
    a SysOp passing a nonsensical `object_type`/`permission`
    combination still gets caught rather than silently bypassed.
    `get_grant`/`list_grants_for_object` are deliberately *not* given
    the same treatment -- those answer "what grants actually exist",
    used for admin displays, and must stay literal.
    """
    _validate_permission_type(object_type, permission)
    if user.user_level >= SYSOP_LEVEL:
        return True
    rows = db.connection.execute(
        "SELECT permissions FROM moderator_grants "
        "WHERE user_id = ? AND object_type = ? AND (object_id = ? OR object_id IS NULL)",
        (user.id, object_type, object_id),
    ).fetchall()
    combined = 0
    for row in rows:
        combined |= row["permissions"]
    return bool(combined & int(permission))


def get_grant(db: Database, user: User, *, object_type: str, object_id: int | None) -> ModeratorGrant | None:
    """The grant row exactly matching this (user, object_type,
    object_id) — does NOT fold in a separate blanket grant the way
    `has_permission` does."""
    return _get_grant(db, user.id, object_type, object_id)


def list_grants_for_user(db: Database, user: User) -> list[ModeratorGrant]:
    rows = db.connection.execute(
        "SELECT * FROM moderator_grants WHERE user_id = ? ORDER BY object_type, object_id",
        (user.id,),
    ).fetchall()
    return [_row_to_grant(row) for row in rows]


def list_grants_for_object(db: Database, *, object_type: str, object_id: int) -> list[ModeratorGrant]:
    """Every grant that applies to this object: per-object grants on
    it specifically, plus any local-blanket grant over its
    object_type."""
    rows = db.connection.execute(
        "SELECT * FROM moderator_grants WHERE object_type = ? AND (object_id = ? OR object_id IS NULL)",
        (object_type, object_id),
    ).fetchall()
    return [_row_to_grant(row) for row in rows]


def _validate_permission_type(object_type: str, permissions: IntFlag) -> type[IntFlag]:
    try:
        enum_type = _PERMISSION_ENUMS[object_type]
    except KeyError:
        raise ModeratorGrantError(
            f"unknown object_type {object_type!r}; expected one of {sorted(_PERMISSION_ENUMS)}"
        ) from None
    if not isinstance(permissions, enum_type):
        raise ModeratorGrantError(
            f"{permissions!r} is not a {enum_type.__name__} member, "
            f"which object_type {object_type!r} requires"
        )
    return enum_type


def _describe(enum_type: type[IntFlag], mask: int) -> str:
    names = [flag.name for flag in enum_type if flag.value & mask and flag.name]
    return ",".join(names) if names else "none"


def _get_grant_row(db: Database, user_id: int, object_type: str, object_id: int | None) -> sqlite3.Row | None:
    return db.connection.execute(
        "SELECT * FROM moderator_grants WHERE user_id = ? AND object_type = ? AND object_id IS ?",
        (user_id, object_type, object_id),
    ).fetchone()


def _get_grant(db: Database, user_id: int, object_type: str, object_id: int | None) -> ModeratorGrant | None:
    row = _get_grant_row(db, user_id, object_type, object_id)
    return _row_to_grant(row) if row is not None else None


def _row_to_grant(row: sqlite3.Row) -> ModeratorGrant:
    return ModeratorGrant(
        id=row["id"],
        user_id=row["user_id"],
        object_type=row["object_type"],
        object_id=row["object_id"],
        permissions=row["permissions"],
        granted_by_user_id=row["granted_by_user_id"],
        created_at=row["created_at"],
    )
