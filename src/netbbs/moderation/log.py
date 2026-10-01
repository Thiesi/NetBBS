"""
Generic moderation audit log (design doc §13: "All actions logged").

One shared table for every moderation action, rather than a
bespoke log for each feature: moderator-grant/revoke,
and mute/ban/kick and moderated-board approval once those
exist. Built now, ahead of most of its consumers, for the same
anti-retrofit reason `netbbs.permissions.levels` was built ahead of a
menu/command dispatch layer to plug into (design doc) —
better to design this against two real, if not-yet-all-built,
consumers than have each later consumer invent its own logging.

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
    # Nullable: the account that performed an action can later be
    # hard-deleted (ON DELETE SET NULL) without destroying the audit
    # trail that names it.
    actor_user_id: int | None
    action: str
    object_type: str | None
    object_id: int | None
    target_user_id: int | None
    detail: str | None
    created_at: str


def record_action(
    db: Database,
    *,
    actor: User | None,
    action: str,
    object_type: str | None = None,
    object_id: int | None = None,
    target_user_id: int | None = None,
    detail: str | None = None,
) -> ModerationLogEntry:
    """Append one entry and commit immediately. Log entries are
    append-only — there is deliberately no update/delete API; an audit
    trail that can be edited after the fact isn't one."""
    entry = record_action_without_commit(
        db,
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=object_id,
        target_user_id=target_user_id,
        detail=detail,
    )
    db.connection.commit()
    return entry


def record_action_without_commit(
    db: Database,
    *,
    actor: User | None,
    action: str,
    object_type: str | None = None,
    object_id: int | None = None,
    target_user_id: int | None = None,
    detail: str | None = None,
) -> ModerationLogEntry:
    """
    Append one entry without committing (GitHub issue #49) — for a
    caller that needs this insert to be part of a larger, already-open
    explicit transaction (e.g. `netbbs.auth.users.set_user_level`'s own
    `BEGIN IMMEDIATE` block, atomic with the SysOp-count check and the
    row mutation it guards), rather than committing this insert on its
    own the instant it runs. The caller is responsible for eventually
    committing (or rolling back) the transaction this participates in;
    `record_action` above is the auto-committing convenience wrapper
    every caller that doesn't need that should keep using.
    """
    created_at = utc_now_iso()
    cursor = db.connection.execute(
        """
        INSERT INTO moderation_log
            (actor_user_id, action, object_type, object_id, target_user_id, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        # `None` is the node itself acting on a SysOp's standing rule (an
        # automatic promotion, issue #992): the log shows it as "(system)".
        (actor.id if actor is not None else None, action, object_type, object_id, target_user_id, detail,
         created_at),
    )
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


def list_recent_actions(
    db: Database, *, limit: int = 200, object_type: str | None = None, object_id: int | None = None
) -> list[ModerationLogEntry]:
    """
    The most recent `limit` entries across every user/object -- the
    site-wide audit trail (`netbbs.net.admin_flow`'s Operations
    `[A]udit log`, dogfood follow-up: `list_actions_for_object`/
    `list_actions_for_target_user` only ever answer "what happened to
    this specific user/board/channel", leaving a SysOp with no way to
    ask "what has happened on this node recently" without already
    knowing who or what to check).

    Most recent first, mirroring `netbbs.link.diagnostics.
    list_diagnostic_log_entries`'s own reading order and `limit`
    convention -- callers wanting oldest-first reverse this same
    window rather than a separate query.

    With `object_type` and `object_id`, only what was done to that one
    board, file area or channel -- its moderation history, bounded the
    same way (issue #678), unlike `list_actions_for_object`, which reads
    all of it. Two things differ from the node-wide trail (Codex review
    on #797):

    - It is read newest first through `idx_moderation_log_object`, which
      ends in `created_at` (and, as every SQLite index does, the row id
      that breaks a tie between actions in one microsecond), so the limit
      bounds the work as well as the rows.
    - It starts after the object's last deletion (`delete_<object_type>`,
      or `purge_link_resource` for a hidden carried one). A deleted board's
      integer id can be reused by the next one created, and the log keeps
      the deleted one's rows.
    """
    if object_type is not None:
        rows = db.connection.execute(
            """
            SELECT * FROM moderation_log
            WHERE object_type = ? AND object_id = ?
              AND id > COALESCE((
                  SELECT MAX(id) FROM moderation_log
                  WHERE object_type = ? AND object_id = ?
                    AND action IN ('delete_' || ?, 'purge_link_resource')
              ), 0)
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (object_type, object_id, object_type, object_id, object_type, limit),
        ).fetchall()
    else:
        rows = db.connection.execute(
            "SELECT * FROM moderation_log ORDER BY id DESC LIMIT ?", (limit,)
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
