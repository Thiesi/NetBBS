"""
Bounded, disk-backed chat scrollback per channel.

Design doc round 19/20: revisits round 10's "chat isn't persisted"
decision, scoped specifically to the *local* problem of a channel looking
empty after a node restart — not the separate, harder question of a
newly-joined Link node needing catch-up scrollback from peers, which stays
explicitly deferred to whenever Phase 5 starts. Bounded by message count
rather than a time window (round 19, point 4): predictable storage size
and scrollback length regardless of how chatty a given channel is.

Join/leave presence events are recorded here too, not just chat text —
without them, a replayed message from someone who left the channel long
ago carries no indication of that, and reads as if they're still present.
`kind` is a discriminator on one table rather than two, since messages and
presence events share the same channel/ordering/trimming concerns and
there's no case where a replay would want one without the other.

Deliberately returns structured `ChannelMessage` rows rather than
pre-rendered ANSI text — same separation `netbbs.boards.boards.list_boards`
keeps between storage and display. See `netbbs.net.chat_flow` for how
these get turned into colored terminal output; keeping that here would
mean a future theme change (or a non-ANSI client) needing a data
migration instead of just a rendering-layer change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from netbbs.chat.channels import Channel
from netbbs.config import get_config, set_config
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

MessageKind = Literal["message", "join", "leave"]

# Config key for the node-wide scrollback retention limit, stored via
# netbbs.config — same pattern as netbbs.timeutil's display format/
# timezone settings.
SCROLLBACK_LIMIT_CONFIG_KEY = "chat_scrollback_limit"

# Confirmed with Thiesi: enough to catch up on a conversation without
# excessive per-channel storage. Node-wide default, overridable via
# node_config; per-channel/per-user tuning is out of scope for the same
# reason list_boards' sort order is node-wide only for now (design doc
# round 18) — no per-user preference system exists yet.
_DEFAULT_SCROLLBACK_LIMIT = 100


@dataclass(frozen=True)
class ChannelMessage:
    id: int
    channel_id: int
    kind: MessageKind
    author_label: str
    author_fingerprint: str | None
    body: str | None
    created_at: str


def get_scrollback_limit(db: Database) -> int:
    """
    Node-wide scrollback retention limit (messages + join/leave events
    kept per channel).

    Falls back to the hardcoded default if unset or malformed — same
    defense-in-depth reasoning as
    `netbbs.timeutil.format_for_display` falling back on an invalid
    stored format/timezone rather than raising deep inside a display
    path.
    """
    raw = get_config(db, SCROLLBACK_LIMIT_CONFIG_KEY, default=str(_DEFAULT_SCROLLBACK_LIMIT))
    try:
        limit = int(raw)
    except ValueError:
        return _DEFAULT_SCROLLBACK_LIMIT
    return limit if limit > 0 else _DEFAULT_SCROLLBACK_LIMIT


def set_scrollback_limit(db: Database, limit: int) -> None:
    """Set the node-wide scrollback retention limit, validating first —
    same immediate-feedback reasoning as
    `netbbs.timeutil.set_display_format`."""
    if limit <= 0:
        raise ValueError(f"scrollback limit must be positive, got {limit!r}")
    set_config(db, SCROLLBACK_LIMIT_CONFIG_KEY, str(limit))


def record_message(
    db: Database,
    channel: Channel,
    *,
    kind: MessageKind,
    author_label: str,
    author_fingerprint: str | None = None,
    body: str | None = None,
) -> ChannelMessage:
    """
    Append an event to `channel`'s scrollback and trim it back down to the
    configured limit.

    `body` is required for `kind="message"`; for `"join"`/`"leave"` the
    kind alone carries the whole meaning of the event (see module
    docstring), so `body` is ignored. Validated here rather than left to
    the DB's CHECK constraint so a caller gets an immediate, specific
    error instead of a generic `IntegrityError`.
    """
    if kind == "message" and body is None:
        raise ValueError("body is required for kind='message'")

    created_at = utc_now_iso()
    db.connection.execute(
        """
        INSERT INTO channel_messages
            (channel_id, kind, author_label, author_fingerprint, body, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (channel.id, kind, author_label, author_fingerprint, body, created_at),
    )
    limit = get_scrollback_limit(db)
    db.connection.execute(
        """
        DELETE FROM channel_messages
        WHERE channel_id = ? AND id NOT IN (
            SELECT id FROM channel_messages
            WHERE channel_id = ?
            ORDER BY id DESC
            LIMIT ?
        )
        """,
        (channel.id, channel.id, limit),
    )
    db.connection.commit()

    row = db.connection.execute(
        "SELECT * FROM channel_messages WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
        (channel.id,),
    ).fetchone()
    return _row_to_message(row)


def get_scrollback(db: Database, channel: Channel) -> list[ChannelMessage]:
    """Return `channel`'s retained scrollback, oldest first — the order a
    user reading back through history would expect, matching
    `netbbs.boards.posts.list_posts`."""
    rows = db.connection.execute(
        "SELECT * FROM channel_messages WHERE channel_id = ? ORDER BY id ASC",
        (channel.id,),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


def _row_to_message(row: sqlite3.Row) -> ChannelMessage:
    return ChannelMessage(
        id=row["id"],
        channel_id=row["channel_id"],
        kind=row["kind"],
        author_label=row["author_label"],
        author_fingerprint=row["author_fingerprint"],
        body=row["body"],
        created_at=row["created_at"],
    )
