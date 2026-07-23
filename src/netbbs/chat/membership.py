"""
Persistent channel membership and the invite-then-accept flow (design
doc §8, points 8/9/11).

Deliberately its own module, distinct from `netbbs.chat.channels`
(channel CRUD/topic) and `netbbs.chat.moderation` (mute/ban/kick):
membership is access/visibility eligibility, not a moderation action or
a permission grant — `channel_members` is its own table rather than
folded into `moderator_grants` for exactly that reason.

Two independent capabilities live here:

- **Direct membership** (`is_member`/`add_member`/`remove_member`,
  backed by `channel_members`) — persistent access to a `members_only`
  channel, granted or revoked outright by `/grantaccess`/`/revokeaccess`.
- **Invitations** (`channel_invitations`) — a pending offer that a
  successful `/join` consumes (see `netbbs.net.chat_flow._handle_join`)
  rather than granting membership directly; there is no separate
  `/accept` command (reusing `/join`'s existing
  "look up, check authorization, switch" flow instead of inventing
  parallel command surface for the same action).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import datetime

from netbbs.auth.users import User, list_users
from netbbs.chat.channels import Channel
from netbbs.config import get_invitation_expiry_days
from netbbs.moderation import ChannelPermission, has_permission, record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class MembershipError(Exception):
    """Raised when the acting user doesn't hold `ChannelPermission.
    MANAGE_MEMBERS` (or, for `create_invitation`, doesn't otherwise
    qualify via `allow_member_invites`) — deliberately not
    `netbbs.chat.moderation.ChatModerationError`, the same reasoning
    `TopicError` already documents: a small local exception avoids a
    circular import back through that module."""


@dataclass(frozen=True)
class ChannelInvitation:
    id: int
    channel_id: int
    invited_user_id: int
    invited_by_user_id: int
    status: str  # 'pending' | 'accepted' | 'revoked'
    created_at: str
    expires_at: str | None  # None == indefinite


# -- direct membership -------------------------------------------------


def is_member(db: Database, channel: Channel, user: User) -> bool:
    row = db.connection.execute(
        "SELECT 1 FROM channel_members WHERE channel_id = ? AND user_id = ?",
        (channel.id, user.id),
    ).fetchone()
    return row is not None


def list_members(db: Database, channel: Channel) -> list[User]:
    """Every user directly granted membership — not the same as
    `netbbs.chat.hub.ChatHub.participant_ids`'s live roster (who's
    actually connected right now); this is the persistent access list a
    `members_only` channel checks. No permission check here — viewable
    by anyone already in the channel (design doc, point 8),
    enforced by the caller (`netbbs.net.chat_flow._handle_members`).

    Filters `list_users`' full roster down to the member set rather
    than joining against `users` directly — `netbbs.auth.users` has no
    public by-ID lookup or row-conversion helper to build on here, and
    total registered users is naturally bounded at this project's
    declared scale (§14), same reasoning `list_users` itself already
    gives for not paginating."""
    member_ids = {
        row["user_id"]
        for row in db.connection.execute(
            "SELECT user_id FROM channel_members WHERE channel_id = ?", (channel.id,)
        ).fetchall()
    }
    return sorted(
        (user for user in list_users(db) if user.id in member_ids),
        key=lambda user: user.username.lower(),
    )


def add_member(db: Database, channel: Channel, target: User, *, granted_by: User) -> None:
    """Grant `target` persistent access to `channel`, bypassing the
    invite-then-accept flow entirely (design doc, point 8: "granting or
    removing persistent access" as its own capability, distinct from
    invitations)."""
    _require_manage_members(db, channel, granted_by)

    db.connection.execute(
        """
        INSERT INTO channel_members (channel_id, user_id, granted_by_user_id, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(channel_id, user_id) DO UPDATE SET
            granted_by_user_id = excluded.granted_by_user_id,
            created_at = excluded.created_at
        """,
        (channel.id, target.id, granted_by.id, utc_now_iso()),
    )
    db.connection.commit()

    record_action(
        db, actor=granted_by, action="grantaccess", object_type="channel", object_id=channel.id,
        target_user_id=target.id,
    )


def remove_member(db: Database, channel: Channel, target: User, *, removed_by: User) -> None:
    _require_manage_members(db, channel, removed_by)

    db.connection.execute(
        "DELETE FROM channel_members WHERE channel_id = ? AND user_id = ?",
        (channel.id, target.id),
    )
    db.connection.commit()

    record_action(
        db, actor=removed_by, action="revokeaccess", object_type="channel", object_id=channel.id,
        target_user_id=target.id,
    )


# -- invitations --------------------------------------------------------


def _expiry_timestamp(days: int) -> str:
    """`days` from now, same fixed format `utc_now_iso` produces --
    comparable directly against stored `expires_at` strings, same as
    `netbbs.boards.posts._cutoff_iso`'s own days-based cutoff (that one
    counts backward from now; this counts forward)."""
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
    return future.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def create_invitation(db: Database, channel: Channel, target: User, *, invited_by: User) -> ChannelInvitation:
    """
    Create (or upsert, same `ON CONFLICT` pattern as `channel_
    restrictions`) a pending invitation for `target`.

    Allowed if `invited_by` holds `MANAGE_MEMBERS`, **or** the channel
    has `allow_member_invites` set and `invited_by` is already a member
    (design doc, point 11's opt-in) — checked here, not left to
    the caller, so this is the one place that authorization decision is
    made.

    `expires_at` (GitHub issue #28) is populated from
    `netbbs.config.get_invitation_expiry_days` -- a node-wide default
    (7 days out of the box) rather than the permanently-`NULL` value
    this used to always write, which left the schema/model's own
    expiry support structurally present but never actually operating.
    `None` remains available (an operator can configure indefinite
    invitations) — see that config function's own docstring.
    """
    if not (
        has_permission(
            db, invited_by, object_type="channel", object_id=channel.id,
            permission=ChannelPermission.MANAGE_MEMBERS,
        )
        or (channel.allow_member_invites and is_member(db, channel, invited_by))
    ):
        raise MembershipError(f"{invited_by.username!r} may not invite users to this channel")

    created_at = utc_now_iso()
    expiry_days = get_invitation_expiry_days(db)
    expires_at = _expiry_timestamp(expiry_days) if expiry_days is not None else None
    db.connection.execute(
        """
        INSERT INTO channel_invitations
            (channel_id, invited_user_id, invited_by_user_id, status, created_at, expires_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(channel_id, invited_user_id) DO UPDATE SET
            invited_by_user_id = excluded.invited_by_user_id,
            status = 'pending',
            created_at = excluded.created_at,
            expires_at = excluded.expires_at
        """,
        (channel.id, target.id, invited_by.id, created_at, expires_at),
    )
    db.connection.commit()

    record_action(
        db, actor=invited_by, action="invite", object_type="channel", object_id=channel.id,
        target_user_id=target.id,
    )
    return _get_invitation(db, channel, target)


def revoke_invitation(db: Database, channel: Channel, target: User, *, revoked_by: User) -> None:
    _require_manage_members(db, channel, revoked_by)

    cursor = db.connection.execute(
        """
        UPDATE channel_invitations SET status = 'revoked'
        WHERE channel_id = ? AND invited_user_id = ? AND status = 'pending'
        """,
        (channel.id, target.id),
    )
    db.connection.commit()
    if cursor.rowcount == 0:
        raise MembershipError(f"no pending invitation for {target.username!r} on this channel")

    record_action(
        db, actor=revoked_by, action="uninvite", object_type="channel", object_id=channel.id,
        target_user_id=target.id,
    )


def has_pending_invitation(db: Database, channel: Channel, user: User) -> bool:
    row = db.connection.execute(
        """
        SELECT 1 FROM channel_invitations
        WHERE channel_id = ? AND invited_user_id = ? AND status = 'pending'
              AND (expires_at IS NULL OR expires_at > ?)
        """,
        (channel.id, user.id, utc_now_iso()),
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class PendingInvitationView:
    """A pending invitation plus the display fields a caller needs
    without a second round-trip — the channel's name and the inviter's
    username, both resolved via a join here rather than requiring
    `netbbs.net.login_flow`/`netbbs.net.chat_flow` to look each one up
    per invitation themselves (GitHub issue #42)."""

    invitation_id: int
    channel_id: int
    channel_name: str
    invited_by_username: str
    created_at: str
    expires_at: str | None


def list_pending_invitations_for_user(db: Database, user: User) -> list[PendingInvitationView]:
    """
    Every currently pending, non-expired invitation addressed to
    `user`, across every channel, oldest first (GitHub issue #42).

    The durable view a user can check regardless of whether they were
    online at `/invite` time: `channel_invitations` already persists
    everything needed, independent of any session/mailbox state, which
    is exactly what makes this usable as an offline-invitee
    notification mechanism where the old `_deliver_private_message`-only
    approach wasn't (that mailbox is deliberately session-addressed and
    ephemeral — see its own module docstring — so it silently reached
    nobody for an invitee with no active session at invite time).
    Same `status = 'pending'` + not-expired filter `has_pending_
    invitation` uses for one specific channel; this is the account-wide
    equivalent, e.g. for `netbbs.net.login_flow`'s post-login notice and
    an on-demand "list my pending invitations" screen.
    """
    rows = db.connection.execute(
        """
        SELECT ci.id AS invitation_id, ci.channel_id, c.name AS channel_name,
               u.username AS invited_by_username, ci.created_at, ci.expires_at
        FROM channel_invitations ci
        JOIN channels c ON c.id = ci.channel_id
        JOIN users u ON u.id = ci.invited_by_user_id
        WHERE ci.invited_user_id = ? AND ci.status = 'pending'
              AND (ci.expires_at IS NULL OR ci.expires_at > ?)
        ORDER BY ci.created_at
        """,
        (user.id, utc_now_iso()),
    ).fetchall()
    return [
        PendingInvitationView(
            invitation_id=row["invitation_id"],
            channel_id=row["channel_id"],
            channel_name=row["channel_name"],
            invited_by_username=row["invited_by_username"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )
        for row in rows
    ]


def accept_invitation(db: Database, channel: Channel, user: User) -> bool:
    """
    Accepts `user`'s pending invitation (if any) — called by a
    successful `/join` (`netbbs.net.chat_flow._handle_join`), not a
    separate `/accept` command ("reuse /join" decision).
    Returns `False`, without error, if there was no pending, non-
    expired invitation left to accept (e.g. the user was already a
    direct member, the channel isn't `members_only` at all, or a
    concurrent revoke/expiry beat this call to it) — joining doesn't
    require one to have existed, but the caller must not treat a no-op
    as a successful acceptance (GitHub issue #28's reopened check-then-
    act gap: `_handle_join` used to call this unconditionally after its
    own separate `has_pending_invitation` check, so a revoke landing
    between the two silently let the join through anyway).

    One atomic state transition (GitHub issue #28), not just marking
    the invitation accepted the way this used to: also inserts (or
    upserts) `channel_members`, using the inviter (`invited_by_user_id`
    on the invitation itself) as `granted_by_user_id` — accepting an
    invitation used to leave the invited user with no actual persistent
    membership row at all, so access silently reverted to "not a
    member" the moment they next left the channel.

    Wrapped in an explicit SAVEPOINT (GitHub issue #28, reopened a
    second time), not just two DML statements followed by `commit()`:
    on `db`'s shared long-lived connection, SQLite's default ABORT
    behavior means a failure partway through (e.g. the membership
    INSERT) would otherwise leave the preceding invitation-status
    UPDATE pending on the connection, vulnerable to being persisted by
    a later, completely unrelated `commit()` elsewhere. The membership
    row is inserted *before* the status flips to `'accepted'`, and the
    UPDATE's own `WHERE status = 'pending'` is checked via `rowcount`
    rather than trusted — closing the same race from the other
    direction, a concurrent `revoke_invitation` between this
    function's own SELECT and UPDATE. A savepoint, not an unconditional
    `BEGIN`, so this stays safe if ever called inside a wider
    transaction.

    Deliberately never calls `conn.commit()` itself (GitHub issue #28,
    reopened a third time): `RELEASE` on an *outermost* savepoint
    already commits it on its own, so an unconditional `commit()`
    afterward was either redundant in that case or, if this function
    is ever called while a caller already has its own transaction open,
    actively wrong — it would commit that whole enclosing transaction
    early, contradicting this very docstring's "stays safe if ever
    called inside a wider transaction" claim. Releasing an outermost
    savepoint persists this function's own work; releasing a nested one
    correctly leaves the enclosing transaction's boundary for its own
    owner to decide.
    """
    conn = db.connection
    conn.execute("SAVEPOINT accept_channel_invitation")
    try:
        row = conn.execute(
            """
            SELECT * FROM channel_invitations
            WHERE channel_id = ? AND invited_user_id = ? AND status = 'pending'
                  AND (expires_at IS NULL OR expires_at > ?)
            """,
            (channel.id, user.id, utc_now_iso()),
        ).fetchone()

        if row is None:
            conn.execute("RELEASE SAVEPOINT accept_channel_invitation")
            return False

        conn.execute(
            """
            INSERT INTO channel_members (channel_id, user_id, granted_by_user_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id, user_id) DO UPDATE SET
                granted_by_user_id = excluded.granted_by_user_id,
                created_at = excluded.created_at
            """,
            (channel.id, user.id, row["invited_by_user_id"], utc_now_iso()),
        )

        cursor = conn.execute(
            "UPDATE channel_invitations SET status = 'accepted' WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )
        if cursor.rowcount != 1:
            raise MembershipError("invitation was no longer pending")

        conn.execute("RELEASE SAVEPOINT accept_channel_invitation")
        return True
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT accept_channel_invitation")
        conn.execute("RELEASE SAVEPOINT accept_channel_invitation")
        raise


def _require_manage_members(db: Database, channel: Channel, user: User) -> None:
    if not has_permission(
        db, user, object_type="channel", object_id=channel.id, permission=ChannelPermission.MANAGE_MEMBERS
    ):
        raise MembershipError(f"{user.username!r} does not hold manage-members permission on this channel")


def _get_invitation(db: Database, channel: Channel, user: User) -> ChannelInvitation:
    row = db.connection.execute(
        "SELECT * FROM channel_invitations WHERE channel_id = ? AND invited_user_id = ?",
        (channel.id, user.id),
    ).fetchone()
    return _row_to_invitation(row)


def _row_to_invitation(row: sqlite3.Row) -> ChannelInvitation:
    return ChannelInvitation(
        id=row["id"],
        channel_id=row["channel_id"],
        invited_user_id=row["invited_user_id"],
        invited_by_user_id=row["invited_by_user_id"],
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )
