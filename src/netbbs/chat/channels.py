"""
Chat channels: local-only in Phase 1 — no Link, no moderators
(kick/mute/ban) yet (design doc §15 phasing). Channel IDs are
content-addressed (§7) for the same reason board IDs are: no ID-scheme
migration needed when NetBBS Link Channels arrive in a later phase.

Unlike boards, channels have a single `min_level` rather than separate
read/write levels — chat access doesn't have a meaningful read/write
split (you either can participate or you can't), a design point
confirmed explicitly during the permissions & moderation discussion (see
design doc §13).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.boards.content_id import compute_content_id
from netbbs.moderation import ChannelPermission, has_permission, record_action
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class ChannelError(Exception):
    """Raised for channel creation/lookup failures."""


class TopicError(Exception):
    """Raised when the acting user doesn't hold `ChannelPermission.EDIT`
    on the channel. Deliberately not `netbbs.chat.moderation`'s
    `ChatModerationError` -- that module already imports `Channel` from
    here, so importing back from it would be a circular import; a small
    local exception avoids that without needing a third shared module
    just for this."""


@dataclass(frozen=True)
class Channel:
    id: int
    channel_id: str
    name: str
    description: str | None
    min_level: int
    category_id: int | None
    pinned: bool
    created_at: str
    topic: str | None


def create_channel(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    min_level: int = 0,
    category_id: int | None = None,
    pinned: bool = False,
    creator: User,
) -> Channel:
    """Create a new local channel. No permission check on creation here —
    same reasoning as `netbbs.boards.create_board`: an admin-level action
    with no SysOp/moderator concept defined yet in Phase 1.

    `category_id` optionally places the channel under a
    `netbbs.chat.categories.Category`. `pinned` channels always sort
    first — see `list_channels`."""
    created_at = utc_now_iso()
    channel_id = compute_content_id(
        {
            "type": "channel",
            "name": name,
            "creator": creator.fingerprint or creator.username,
            "created_at": created_at,
        }
    )

    try:
        db.connection.execute(
            """
            INSERT INTO channels
                (channel_id, name, description, min_level, category_id, pinned, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (channel_id, name, description, min_level, category_id, int(pinned), created_at),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise ChannelError(f"could not create channel {name!r} — name already in use?") from exc

    return get_channel_by_name(db, name)


def get_channel_by_name(db: Database, name: str) -> Channel:
    row = db.connection.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ChannelError(f"no such channel: {name!r}")
    return _row_to_channel(row)


def list_channels(db: Database) -> list[Channel]:
    """
    List all channels, pinned first, then alphabetically.

    Deliberately doesn't offer an "activity" sort here the way
    `netbbs.boards.boards.list_boards` does — chat messages aren't
    persisted (see `netbbs.chat.hub`'s module docstring), so there's no
    stored history to compute "most recent activity" from. That signal
    exists only in-memory, via `netbbs.chat.hub.ChatHub.last_activity`,
    which the caller (see `netbbs.net.chat_flow`) combines with this
    function's output — keeping this module free of any dependency on
    the in-memory hub, a cleaner separation than threading `ChatHub`
    through the storage layer.

    Same "caller filters by level" pattern as
    `netbbs.boards.boards.list_boards` — see that function's docstring
    for why filtering isn't baked in here either.
    """
    rows = db.connection.execute(
        "SELECT * FROM channels ORDER BY pinned DESC, name COLLATE NOCASE ASC"
    ).fetchall()
    return [_row_to_channel(row) for row in rows]


def set_topic(db: Database, channel: Channel, topic: str | None, *, set_by: User) -> Channel:
    """
    Set (or clear, if `topic` is `None`/empty) `channel`'s topic.

    Requires `set_by` to hold `ChannelPermission.EDIT` on `channel` --
    already reserved for exactly this in `netbbs.moderation.roles`
    (`"EDIT = auto()  # gates /topic changes (round 33, point 5)"`).
    Logged via the shared `moderation_log` audit trail (design doc round
    33 point 5: "recorded in moderation or metadata history with setter
    identity and timestamp") -- that table's `actor`/`created_at` columns
    already satisfy this, no separate history table needed.
    """
    if not has_permission(
        db, set_by, object_type="channel", object_id=channel.id, permission=ChannelPermission.EDIT
    ):
        raise TopicError(f"{set_by.username!r} does not hold edit permission on this channel")

    db.connection.execute("UPDATE channels SET topic = ? WHERE id = ?", (topic or None, channel.id))
    db.connection.commit()

    record_action(
        db, actor=set_by, action="topic", object_type="channel", object_id=channel.id, detail=topic
    )
    return get_channel_by_name(db, channel.name)


def _row_to_channel(row: sqlite3.Row) -> Channel:
    return Channel(
        id=row["id"],
        channel_id=row["channel_id"],
        name=row["name"],
        description=row["description"],
        min_level=row["min_level"],
        category_id=row["category_id"],
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
        topic=row["topic"],
    )
