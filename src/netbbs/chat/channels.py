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
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class ChannelError(Exception):
    """Raised for channel creation/lookup failures."""


@dataclass(frozen=True)
class Channel:
    id: int
    channel_id: str
    name: str
    description: str | None
    min_level: int
    created_at: str


def create_channel(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    min_level: int = 0,
    creator: User,
) -> Channel:
    """Create a new local channel. No permission check on creation here —
    same reasoning as `netbbs.boards.create_board`: an admin-level action
    with no SysOp/moderator concept defined yet in Phase 1."""
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
            INSERT INTO channels (channel_id, name, description, min_level, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (channel_id, name, description, min_level, created_at),
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
    """List all channels, in creation order. Same "caller filters by
    level" pattern as `netbbs.boards.list_boards` — see that function's
    docstring for why filtering isn't baked in here."""
    rows = db.connection.execute("SELECT * FROM channels ORDER BY created_at").fetchall()
    return [_row_to_channel(row) for row in rows]


def _row_to_channel(row: sqlite3.Row) -> Channel:
    return Channel(
        id=row["id"],
        channel_id=row["channel_id"],
        name=row["name"],
        description=row["description"],
        min_level=row["min_level"],
        created_at=row["created_at"],
    )
