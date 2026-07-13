"""
Persistent channel membership and the invite-then-accept flow (design
doc §8/round 33 points 8/9/11, Phase 2 Track 5h).

Deliberately its own module, distinct from `netbbs.chat.channels`
(channel CRUD/topic) and `netbbs.chat.moderation` (mute/ban/kick):
membership is access/visibility eligibility, not a moderation action or
a permission grant — `channel_members` is its own table rather than
folded into `moderator_grants` for exactly that reason (see the round
50 sign-off note).

Two independent capabilities live here:

- **Direct membership** (`is_member`/`add_member`/`remove_member`,
  backed by `channel_members`) — persistent access to a `members_only`
  channel, granted or revoked outright by `/grantaccess`/`/revokeaccess`.
- **Invitations** (`channel_invitations`) — a pending offer that a
  successful `/join` consumes (see `netbbs.net.chat_flow._handle_join`)
  rather than granting membership directly; there is no separate
  `/accept` command (round 33's reasoning: reuse `/join`'s existing
  "look up, check authorization, switch" flow instead of inventing
  parallel command surface for the same action).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User, list_users
from netbbs.chat.channels import Channel
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
    by anyone already in the channel (design doc round 33 point 8),
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
    invite-then-accept flow entirely (round 33 point 8: "granting or
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


def create_invitation(db: Database, channel: Channel, target: User, *, invited_by: User) -> ChannelInvitation:
    """
    Create (or upsert, same `ON CONFLICT` pattern as `channel_
    restrictions`) a pending invitation for `target`.

    Allowed if `invited_by` holds `MANAGE_MEMBERS`, **or** the channel
    has `allow_member_invites` set and `invited_by` is already a member
    (design doc round 33 point 11's opt-in) — checked here, not left to
    the caller, so this is the one place that authorization decision is
    made.
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
    db.connection.execute(
        """
        INSERT INTO channel_invitations
            (channel_id, invited_user_id, invited_by_user_id, status, created_at, expires_at)
        VALUES (?, ?, ?, 'pending', ?, NULL)
        ON CONFLICT(channel_id, invited_user_id) DO UPDATE SET
            invited_by_user_id = excluded.invited_by_user_id,
            status = 'pending',
            created_at = excluded.created_at,
            expires_at = excluded.expires_at
        """,
        (channel.id, target.id, invited_by.id, created_at),
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


def accept_invitation(db: Database, channel: Channel, user: User) -> None:
    """Marks `user`'s pending invitation (if any) as accepted — called
    by a successful `/join` (`netbbs.net.chat_flow._handle_join`), not
    a separate `/accept` command (round 33's "reuse /join" decision). A
    no-op if there was no pending invitation (e.g. the user was already
    a direct member, or the channel isn't `members_only` at all) —
    joining doesn't require one to have existed."""
    db.connection.execute(
        """
        UPDATE channel_invitations SET status = 'accepted'
        WHERE channel_id = ? AND invited_user_id = ? AND status = 'pending'
        """,
        (channel.id, user.id),
    )
    db.connection.commit()


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
