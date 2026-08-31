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
remote nodes/traffic") — which is why `blocklist.fingerprint` still
exists at all in the schema. A local account is always keyed by
`local_user_id` instead (see `block_user`'s own docstring for why): once
an account could register more than one SSH key
(`netbbs.auth.users.add_ssh_key`), fingerprint-keying a local block
became a real bypass, since blocking one registered key left every
other one on the same account unblocked. `fingerprint` is reserved for
whenever remote-node blocking is actually built — a different code path
than `block_user(User)`, which only ever takes a local account.

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

    Always keyed by `local_user_id`, never by fingerprint, for `target`
    (always a local account here). Code review follow-up: an earlier
    version keyed by fingerprint whenever `target` had a keypair, on the
    reasoning that fingerprint is "the form this mechanism extends to
    remote nodes/users later" -- true for a hypothetical future remote-
    blocking path, but every actual caller of `block_user` passes a
    local `User`, which always has a stable `id` regardless of which (or
    how many) keys it holds. Once an account can register more than one
    SSH/public key (`netbbs.auth.users.add_ssh_key`), fingerprint-keying
    a *local* block became a real bypass: blocking whichever fingerprint
    happened to be current at block time left every other registered key
    on the same account unblocked. `local_user_id` has no such gap --
    it's account-level, invariant across however many keys exist.
    Fingerprint-based blocklist rows still exist in the schema/CHECK
    constraint for that future remote-node case, and old databases may
    still carry pre-existing fingerprint-keyed rows from before this fix
    (see `migrate_blocklist_key_to_local_user`, which still re-keys them
    on removal) -- this function just never *creates* new ones anymore.
    """
    created_at = utc_now_iso()
    try:
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
    shape as most unblock/unban operations.

    Deletes by both `local_user_id` and `fingerprint` — `local_user_id`
    covers every block `block_user` creates today; `fingerprint` covers
    a pre-existing fingerprint-keyed row from before that function
    stopped creating them (see `block_user`'s own docstring), so an old
    database's legacy block is still fully removable here even if it was
    never migrated via `migrate_blocklist_key_to_local_user`."""
    db.connection.execute(
        "DELETE FROM blocklist WHERE local_user_id = ? OR fingerprint = ?",
        (target.id, target.fingerprint),
    )
    db.connection.commit()


def is_blocked(db: Database, user: User) -> bool:
    """
    Check whether `user` is currently on the local blocklist.

    Checks both `local_user_id` and `fingerprint` unconditionally, not
    only whichever `user` currently has — `local_user_id` alone already
    covers every block `block_user` creates today (see its own
    docstring), but a legacy fingerprint-keyed row from an old database
    (pre-multi-key, or never migrated) must still be found even for an
    account that currently has no fingerprint at all, e.g. right after
    its last key was removed and before `remove_ssh_key`'s own
    `migrate_blocklist_key_to_local_user` call (same transaction,
    already committed by the time this could run, but defense in depth
    against any other future path that removes a key without going
    through that function).
    """
    row = db.connection.execute(
        "SELECT 1 FROM blocklist WHERE local_user_id = ? OR fingerprint = ?",
        (user.id, user.fingerprint),
    ).fetchone()
    return row is not None


def migrate_blocklist_key_to_local_user(db: Database, *, old_fingerprint: str, local_user_id: int) -> None:
    """
    Re-key an existing fingerprint-based blocklist entry to `local_user_id`
    instead, in place, preserving the block across an identity change
    rather than silently orphaning it.

    GitHub issue #212's own follow-up (code review, PR #213): `block_user`
    used to key a block by fingerprint whenever the target had one at
    block time; if that fingerprint was later removed, `is_blocked`
    stopped finding the entry the moment the account no longer had a
    fingerprint to check, and the account read as unblocked on its next
    password login, silently bypassing an active restriction.
    `block_user` no longer creates fingerprint-keyed rows at all for
    local accounts (see its own docstring), so this now exists purely
    for a *pre-existing* row from before that fix -- `netbbs.auth.users.
    remove_ssh_key` still calls this whenever a fingerprint is removed,
    inside the same transaction, not as optional cleanup, so an old
    database's legacy block still survives its key being removed. A
    no-op if no entry exists for `old_fingerprint` (most accounts were
    never blocked, and any block created after this fix was never
    fingerprint-keyed to begin with).
    """
    db.connection.execute(
        "UPDATE blocklist SET fingerprint = NULL, local_user_id = ? WHERE fingerprint = ?",
        (local_user_id, old_fingerprint),
    )


def list_blocklist(db: Database) -> list[BlocklistEntry]:
    rows = db.connection.execute("SELECT * FROM blocklist ORDER BY created_at").fetchall()
    return [_row_to_entry(row) for row in rows]


def _get_entry_for_user(db: Database, user: User) -> BlocklistEntry:
    # Only ever called right after block_user's own insert, which is
    # always local_user_id-keyed now (see its docstring) -- the
    # fingerprint fallback exists purely for a legacy pre-existing row
    # a fresh block_user call would refuse to duplicate (its own UNIQUE
    # index), the same "OR fingerprint" pattern is_blocked/unblock_user
    # use.
    row = db.connection.execute(
        "SELECT * FROM blocklist WHERE local_user_id = ? OR fingerprint = ?",
        (user.id, user.fingerprint),
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
