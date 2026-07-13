"""
Generic moderation audit log (design doc §13: "All actions logged").

One shared table for every moderation action, rather than a
bespoke log for each feature: moderator-grant/revoke (this round),
and mute/ban/kick and moderated-board approval once those tracks
exist. Built now, ahead of most of its consumers, for the same
anti-retrofit reason `netbbs.permissions.levels` was built ahead of a
menu/command dispatch layer to plug into (design doc round 34) —
better to design this against two real, if not-yet-all-built,
consumers than have Track 2/3 each invent their own logging.

No action-specific columns: `action` is a short free-text label
("grant", "revoke", ...), `detail` is a human-readable free-text
description of what changed, and `object_id`/`target_user_id` are
nullable so an action that doesn't apply to a specific object or
target user just leaves them NULL.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


@dataclass(frozen=True)
class ModerationLogEntry:
    id: int
    actor_user_id: int
    action: str
    object_type: str | None
    object_id: int | None
    target_user_id: int | None
    detail: str | None
    created_at: str


def record_action(
    db: Database,
    *,
    actor: User,
    action: str,
    object_type: str | None = None,
    object_id: int | None = None,
    target_user_id: int | None = None,
    detail: str | None = None,
) -> ModerationLogEntry:
    """Append one entry. Log entries are append-only — there is
    deliberately no update/delete API; an audit trail that can be
    edited after the fact isn't one."""
    created_at = utc_now_iso()
    cursor = db.connection.execute(
        """
        INSERT INTO moderation_log
            (actor_user_id, action, object_type, object_id, target_user_id, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (actor.id, action, object_type, object_id, target_user_id, detail, created_at),
    )
    db.connection.commit()
    row = db.connection.execute(
        "SELECT * FROM moderation_log WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return _row_to_entry(row)


def list_actions_for_object(db: Database, object_type: str, object_id: int) -> list[ModerationLogEntry]:
    rows = db.connection.execute(
        "SELECT * FROM moderation_log WHERE object_type = ? AND object_id = ? ORDER BY created_at",
        (object_type, object_id),
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def list_actions_for_target_user(db: Database, target_user_id: int) -> list[ModerationLogEntry]:
    rows = db.connection.execute(
        "SELECT * FROM moderation_log WHERE target_user_id = ? ORDER BY created_at",
        (target_user_id,),
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def _row_to_entry(row: sqlite3.Row) -> ModerationLogEntry:
    return ModerationLogEntry(
        id=row["id"],
        actor_user_id=row["actor_user_id"],
        action=row["action"],
        object_type=row["object_type"],
        object_id=row["object_id"],
        target_user_id=row["target_user_id"],
        detail=row["detail"],
        created_at=row["created_at"],
    )
