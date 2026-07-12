"""
Local blocklist — a basic moderation stub, pre-dating the full
reputation/trust system (design doc §6: local blocklists are the "hard
mechanism" — each node decides who *it* stops relaying to/from, no
network-wide effect from a unilateral decision).

Phase 1 scope: purely local, no Link, no full moderator tiers (§13's
richer model is Phase 2+). This exists to block a specific local account
from logging in — nothing more sophisticated yet. Design doc §15's Phase
3 explicitly extends this same mechanism to remote nodes/traffic once the
Link exists ("the local blocklist mechanism from Phase 1, extended to
remote nodes/traffic") — which is why entries key on `fingerprint`
whenever possible, the same identity concept that'll apply to remote
users/nodes later, rather than anything Phase-1-specific.

No permission check is embedded in `block_user`/`unblock_user` — same
precedent as `netbbs.boards.create_board` and `netbbs.chat.create_channel`:
an admin-level action with no SysOp/moderator concept defined yet in
Phase 1, so gating who's allowed to call this is left to whatever calls
it (a future admin tool), not baked in here.

Deliberately kept separate from `netbbs.auth`: authentication ("are these
credentials correct") and this kind of authorization ("is this correctly-
authenticated account allowed to proceed") are different concerns, same
layering principle already applied to keep `netbbs.permissions` separate
from `netbbs.auth` too. Enforcement lives in the login flow
(`netbbs.net.login_flow`), the actual entry-point orchestration layer,
not inside the auth module itself.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class BlocklistError(Exception):
    """Raised for blocklist entry creation/lookup failures."""


@dataclass(frozen=True)
class BlocklistEntry:
    id: int
    fingerprint: str | None
    local_user_id: int | None
    reason: str | None
    blocked_by_user_id: int
    created_at: str


def block_user(db: Database, target: User, *, blocked_by: User, reason: str | None = None) -> BlocklistEntry:
    """
    Add `target` to the local blocklist.

    Blocks by fingerprint when `target` has a keypair — the form this
    mechanism extends to remote nodes/users later — or by local user ID
    for password-only accounts, which have no fingerprint to block by.
    """
    created_at = utc_now_iso()
    try:
        if target.fingerprint is not None:
            db.connection.execute(
                """
                INSERT INTO blocklist (fingerprint, reason, blocked_by_user_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (target.fingerprint, reason, blocked_by.id, created_at),
            )
        else:
            db.connection.execute(
                """
                INSERT INTO blocklist (local_user_id, reason, blocked_by_user_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (target.id, reason, blocked_by.id, created_at),
            )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise BlocklistError(f"{target.username!r} is already blocked") from exc

    return _get_entry_for_user(db, target)


def unblock_user(db: Database, target: User) -> None:
    """Remove `target` from the blocklist, if present. Not an error if
    they weren't blocked in the first place — same "idempotent removal"
    shape as most unblock/unban operations."""
    if target.fingerprint is not None:
        db.connection.execute("DELETE FROM blocklist WHERE fingerprint = ?", (target.fingerprint,))
    else:
        db.connection.execute("DELETE FROM blocklist WHERE local_user_id = ?", (target.id,))
    db.connection.commit()


def is_blocked(db: Database, user: User) -> bool:
    """
    Check whether `user` is currently on the local blocklist.

    Checks both `fingerprint` and `local_user_id` whenever `user` has a
    fingerprint, not just `fingerprint` alone — covers the edge case of a
    user who was blocked back when they only had a password (blocked by
    `local_user_id`) and later gained a keypair. Adding a keypair to an
    existing password-only account isn't a feature that exists yet, so
    this case isn't reachable today, but it's cheap to handle correctly
    now rather than leave as a landmine for whenever that feature does
    exist.
    """
    if user.fingerprint is not None:
        row = db.connection.execute(
            "SELECT 1 FROM blocklist WHERE fingerprint = ? OR local_user_id = ?",
            (user.fingerprint, user.id),
        ).fetchone()
    else:
        row = db.connection.execute(
            "SELECT 1 FROM blocklist WHERE local_user_id = ?", (user.id,)
        ).fetchone()
    return row is not None


def list_blocklist(db: Database) -> list[BlocklistEntry]:
    rows = db.connection.execute("SELECT * FROM blocklist ORDER BY created_at").fetchall()
    return [_row_to_entry(row) for row in rows]


def _get_entry_for_user(db: Database, user: User) -> BlocklistEntry:
    if user.fingerprint is not None:
        row = db.connection.execute(
            "SELECT * FROM blocklist WHERE fingerprint = ?", (user.fingerprint,)
        ).fetchone()
    else:
        row = db.connection.execute(
            "SELECT * FROM blocklist WHERE local_user_id = ?", (user.id,)
        ).fetchone()
    if row is None:
        raise BlocklistError(f"no blocklist entry found for {user.username!r}")
    return _row_to_entry(row)


def _row_to_entry(row: sqlite3.Row) -> BlocklistEntry:
    return BlocklistEntry(
        id=row["id"],
        fingerprint=row["fingerprint"],
        local_user_id=row["local_user_id"],
        reason=row["reason"],
        blocked_by_user_id=row["blocked_by_user_id"],
        created_at=row["created_at"],
    )
