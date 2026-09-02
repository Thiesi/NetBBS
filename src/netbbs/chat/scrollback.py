"""
Bounded, disk-backed chat scrollback per channel.

Revisits the earlier "chat isn't persisted"
decision, scoped specifically to the *local* problem of a channel looking
empty after a node restart — not the separate question of a node
live-subscribing to a linked channel getting recent scrollback from its
origin. That question is issue #194: `get_scrollback` is its chosen
source too, reused as-is by `netbbs.link.realtime_channels._handle_
subscribe` to build a `scrollback_snapshot` frame for a freshly-
subscribing peer, no new storage or filtering mechanism of its own.
Bounded by message count rather than a time window: predictable storage
size and scrollback length regardless of how chatty a given channel is.

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
from netbbs.link.enforcement import link_content_visible
from netbbs.search import index_channel_message, prune_channel_message_search
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

MessageKind = Literal[
    "message", "join", "leave", "mute", "unmute", "ban", "unban", "kick", "action", "nick", "daybreak"
]
# "nick" was already a valid DB-level kind (added by a CHECK-widening
# migration; see netbbs.net.chat_flow._announce_nick_change) but had
# drifted out of sync with this Python-side type hint until now --
# fixed in passing while adding "daybreak" below,
# not a separate change.

# Config key for the node-wide scrollback retention limit, stored via
# netbbs.config — same pattern as netbbs.timeutil's display format/
# timezone settings.
SCROLLBACK_LIMIT_CONFIG_KEY = "chat_scrollback_limit"

# Confirmed with Thiesi: enough to catch up on a conversation without
# excessive per-channel storage. Node-wide default, overridable via
# node_config; per-channel/per-user tuning is out of scope for the same
# reason list_boards' sort order is node-wide only for now
# — no per-user preference system exists yet.
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
    link_content_id: str | None = None
    link_event_json: str | None = None
    body_truncated: bool = False


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

    `body` is required for `kind="message"` and `kind="daybreak"`
    -- the announcement text itself, since a
    daybreak event has no author to derive display text from the way
    "join"/"leave" do); for `"join"`/`"leave"` the kind alone carries
    the whole meaning of the event (see module docstring), so `body` is
    ignored. Validated here rather than left to the DB's CHECK
    constraint so a caller gets an immediate, specific error instead of
    a generic `IntegrityError`.
    """
    if kind in ("message", "daybreak") and body is None:
        raise ValueError(f"body is required for kind={kind!r}")

    created_at = utc_now_iso()
    cursor = db.connection.execute(
        """
        INSERT INTO channel_messages
            (channel_id, kind, author_label, author_fingerprint, body, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (channel.id, kind, author_label, author_fingerprint, body, created_at),
    )
    message_id = cursor.lastrowid
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
    # Issue #56's search index: index the new message, then prune
    # whatever the trim step just removed above -- in that order, since
    # a scrollback limit of 1 could otherwise trim the very message just
    # indexed before it's ever pruned back out (harmless either order
    # here, but indexing first keeps this reading top-to-bottom with the
    # inserts/deletes immediately above).
    index_channel_message(db, channel.id, message_id, kind, body)
    prune_channel_message_search(db, channel.id)
    db.connection.commit()

    row = db.connection.execute(
        "SELECT * FROM channel_messages WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
        (channel.id,),
    ).fetchone()
    return _row_to_message(row)


def get_scrollback(db: Database, channel: Channel) -> list[ChannelMessage]:
    """Return `channel`'s retained scrollback, oldest first — the order a
    user reading back through history would expect. Unlike board posts
    (`netbbs.boards.posts.list_posts_page`), this doesn't need its own
    pagination: `record_message` already trims retained scrollback down
    to `get_scrollback_limit(db)` on every insert, so
    what's fetched here is already small and bounded by construction,
    not by a query-time LIMIT.

    A carried (Link-materialized) message is filtered through
    `link_content_visible` (issue #164) the same way
    `netbbs.boards.posts.list_posts_page` already filters board posts —
    keyed on `link_content_id`, the received event's own content ID
    (`materialize_carried_channel_message` stores it precisely so this
    lookup is possible; a purely local message has no `link_content_id`
    and is never filtered). This suppresses only a `BLOCKED`/
    `QUARANTINED` *author's* messages, not a merely-quarantined relay's —
    see `link_content_visible`'s own docstring for why that distinction
    matters. A row's `author_fingerprint` is deliberately not used for
    this check: `materialize_carried_channel_message` always stores it as
    `NULL` for a carried message (the real author identity lives in the
    signed event `link_content_id` points at, not this column)."""
    rows = db.connection.execute(
        "SELECT * FROM channel_messages WHERE channel_id = ? ORDER BY id ASC",
        (channel.id,),
    ).fetchall()
    return [
        _row_to_message(row)
        for row in rows
        if row["link_content_id"] is None or link_content_visible(db, row["link_content_id"])
    ]


def _row_to_message(row: sqlite3.Row) -> ChannelMessage:
    columns = row.keys()
    return ChannelMessage(
        id=row["id"],
        channel_id=row["channel_id"],
        kind=row["kind"],
        author_label=row["author_label"],
        author_fingerprint=row["author_fingerprint"],
        body=row["body"],
        created_at=row["created_at"],
        # Migration tests intentionally exercise historical schemas from
        # before these Link columns existed. Their absent metadata is the
        # same semantic state as NULL on a current local-only row.
        link_content_id=row["link_content_id"] if "link_content_id" in columns else None,
        link_event_json=row["link_event_json"] if "link_event_json" in columns else None,
    )
