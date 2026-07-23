"""
Chat channel moderation: mute/ban/kick (design doc §13), gated by
`netbbs.moderation.roles.ChannelPermission.MODERATE` — the single bit
all three are bundled under, since §13 describes them as one
bundled capability of being a chat moderator, not independently
combinable permissions.

Lives here, alongside `channels.py`/`hub.py`/`scrollback.py`, rather
than in `netbbs.moderation` — same layering already established for
boards (`approve_post`/`delete_post` live in `netbbs.boards.posts`,
not the generic moderation package, which stays limited to the
cross-feature grant/log primitives).

Mute and ban share one table (`channel_restrictions`, discriminated by
`kind`) since they're structurally identical: same duration/expiry
shape, same "is there a live, non-expired row for (channel, user)"
check. No cleanup sweep for expired rows — unlike board/file expiry
(where deleting the row was the point of the feature), a stale
expired mute/ban row causes no problem just sitting
there; `is_muted`/`is_banned` simply filter it out at check time.

`kick_user` only handles the permission check and audit trail — it
persists no ongoing state (a kick is a one-time removal, not a
restriction on rejoining, unlike mute/ban). Actually removing a
currently-connected target's live session is `netbbs.net.chat_flow`'s
job: only that module has any notion of live sessions/participant IDs
(via `netbbs.chat.hub.ChatHub`), which this module deliberately knows
nothing about.
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.chat.channels import Channel
from netbbs.moderation import ChannelPermission, has_permission, record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class ChatModerationError(Exception):
    """Raised when the acting user doesn't hold
    `ChannelPermission.MODERATE` on the channel."""


class DurationError(Exception):
    """Raised for an unparseable mute/ban duration argument."""


_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_duration(arg: str | None) -> datetime.timedelta | None:
    """
    Parse a mute/ban duration argument, matching design doc §13
    exactly: no argument (`None` or empty) means indefinite (`None`);
    a bare number means minutes; a number with one of `s/m/h/d/w/y`
    appended means that unit. `y` (years) is treated as a fixed 365
    days — `timedelta` has no calendar-aware year, and a mute/ban
    duration doesn't need that precision.
    """
    if not arg:
        return None

    if arg[-1].isalpha():
        unit_char, number_part = arg[-1].lower(), arg[:-1]
    else:
        unit_char, number_part = "m", arg  # bare numeric = minutes

    if unit_char == "y":
        try:
            years = int(number_part)
        except ValueError:
            raise DurationError(f"invalid duration: {arg!r}") from None
        if years <= 0:
            raise DurationError(f"duration must be positive: {arg!r}")
        return datetime.timedelta(days=365 * years)

    if unit_char not in _DURATION_UNITS:
        raise DurationError(f"unknown duration unit {arg[-1]!r} in {arg!r}")
    try:
        amount = int(number_part)
    except ValueError:
        raise DurationError(f"invalid duration: {arg!r}") from None
    if amount <= 0:
        raise DurationError(f"duration must be positive: {arg!r}")
    return datetime.timedelta(**{_DURATION_UNITS[unit_char]: amount})


@dataclass(frozen=True)
class ChannelRestriction:
    id: int
    channel_id: int
    user_id: int
    kind: str  # "mute" | "ban"
    expires_at: str | None  # None == indefinite
    imposed_by_user_id: int
    reason: str | None
    created_at: str


def mute_user(
    db: Database,
    channel: Channel,
    target: User,
    *,
    duration: datetime.timedelta | None,
    reason: str | None,
    muted_by: User,
) -> ChannelRestriction:
    """Requires `muted_by` to hold `ChannelPermission.MODERATE` on
    `channel`. Re-muting an already-muted user replaces the existing
    restriction's duration/reason rather than erroring or stacking."""
    return _impose(db, channel, target, kind="mute", duration=duration, reason=reason, imposed_by=muted_by)


def ban_user(
    db: Database,
    channel: Channel,
    target: User,
    *,
    duration: datetime.timedelta | None,
    reason: str | None,
    banned_by: User,
) -> ChannelRestriction:
    """Requires `banned_by` to hold `ChannelPermission.MODERATE` on
    `channel`. Only persists the restriction (blocking future joins) —
    removing a currently-present target is the caller's job, see this
    module's docstring."""
    return _impose(db, channel, target, kind="ban", duration=duration, reason=reason, imposed_by=banned_by)


def unmute_user(db: Database, channel: Channel, target: User, *, unmuted_by: User) -> None:
    """Idempotent — not an error if `target` wasn't muted, same shape
    as `netbbs.moderation.blocklist.unblock_user`."""
    _lift(db, channel, target, kind="mute", lifted_by=unmuted_by)


def unban_user(db: Database, channel: Channel, target: User, *, unbanned_by: User) -> None:
    """Idempotent, same reasoning as `unmute_user`."""
    _lift(db, channel, target, kind="ban", lifted_by=unbanned_by)


def kick_user(db: Database, channel: Channel, target: User, *, reason: str | None, kicked_by: User) -> None:
    """Requires `kicked_by` to hold `ChannelPermission.MODERATE` on
    `channel`. See module docstring: persists nothing, only the
    permission check and audit trail."""
    if not has_permission(
        db, kicked_by, object_type="channel", object_id=channel.id, permission=ChannelPermission.MODERATE
    ):
        raise ChatModerationError(f"{kicked_by.username!r} does not hold moderate permission on this channel")
    record_action(
        db,
        actor=kicked_by,
        action="kick",
        object_type="channel",
        object_id=channel.id,
        target_user_id=target.id,
        detail=reason,
    )


def is_muted(db: Database, channel: Channel, user: User) -> ChannelRestriction | None:
    return _active_restriction(db, channel, user, kind="mute")


def is_banned(db: Database, channel: Channel, user: User) -> ChannelRestriction | None:
    return _active_restriction(db, channel, user, kind="ban")


def list_channel_restrictions(db: Database, channel: Channel) -> list[ChannelRestriction]:
    rows = db.connection.execute(
        "SELECT * FROM channel_restrictions WHERE channel_id = ? ORDER BY created_at",
        (channel.id,),
    ).fetchall()
    return [_row_to_restriction(row) for row in rows]


def _impose(
    db: Database,
    channel: Channel,
    target: User,
    *,
    kind: str,
    duration: datetime.timedelta | None,
    reason: str | None,
    imposed_by: User,
) -> ChannelRestriction:
    if not has_permission(
        db, imposed_by, object_type="channel", object_id=channel.id, permission=ChannelPermission.MODERATE
    ):
        raise ChatModerationError(f"{imposed_by.username!r} does not hold moderate permission on this channel")

    expires_at = None
    if duration is not None:
        expires_at = (datetime.datetime.now(datetime.timezone.utc) + duration).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

    created_at = utc_now_iso()
    db.connection.execute(
        """
        INSERT INTO channel_restrictions
            (channel_id, user_id, kind, expires_at, imposed_by_user_id, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id, user_id, kind) DO UPDATE SET
            expires_at = excluded.expires_at,
            imposed_by_user_id = excluded.imposed_by_user_id,
            reason = excluded.reason,
            created_at = excluded.created_at
        """,
        (channel.id, target.id, kind, expires_at, imposed_by.id, reason, created_at),
    )
    db.connection.commit()

    record_action(
        db,
        actor=imposed_by,
        action=kind,
        object_type="channel",
        object_id=channel.id,
        target_user_id=target.id,
        detail=reason,
    )
    return _get_restriction(db, channel, target, kind=kind)


def _lift(db: Database, channel: Channel, target: User, *, kind: str, lifted_by: User) -> None:
    if not has_permission(
        db, lifted_by, object_type="channel", object_id=channel.id, permission=ChannelPermission.MODERATE
    ):
        raise ChatModerationError(f"{lifted_by.username!r} does not hold moderate permission on this channel")

    db.connection.execute(
        "DELETE FROM channel_restrictions WHERE channel_id = ? AND user_id = ? AND kind = ?",
        (channel.id, target.id, kind),
    )
    db.connection.commit()

    record_action(
        db,
        actor=lifted_by,
        action=f"un{kind}",
        object_type="channel",
        object_id=channel.id,
        target_user_id=target.id,
    )


def _active_restriction(db: Database, channel: Channel, user: User, *, kind: str) -> ChannelRestriction | None:
    row = db.connection.execute(
        """
        SELECT * FROM channel_restrictions
        WHERE channel_id = ? AND user_id = ? AND kind = ?
              AND (expires_at IS NULL OR expires_at > ?)
        """,
        (channel.id, user.id, kind, utc_now_iso()),
    ).fetchone()
    return _row_to_restriction(row) if row is not None else None


def _get_restriction(db: Database, channel: Channel, user: User, *, kind: str) -> ChannelRestriction | None:
    row = db.connection.execute(
        "SELECT * FROM channel_restrictions WHERE channel_id = ? AND user_id = ? AND kind = ?",
        (channel.id, user.id, kind),
    ).fetchone()
    return _row_to_restriction(row) if row is not None else None


def _row_to_restriction(row: sqlite3.Row) -> ChannelRestriction:
    return ChannelRestriction(
        id=row["id"],
        channel_id=row["channel_id"],
        user_id=row["user_id"],
        kind=row["kind"],
        expires_at=row["expires_at"],
        imposed_by_user_id=row["imposed_by_user_id"],
        reason=row["reason"],
        created_at=row["created_at"],
    )
