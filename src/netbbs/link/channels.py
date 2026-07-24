"""
Local-origination bridge for linked channels (design doc §9.6, issue
#87) -- turns an existing local channel/message into a signed
`channel_genesis`/`channel_message` Link event, persists it on the
channel's/message's own row, and (for a channel's genesis specifically)
registers it with the running node the same two ways `netbbs.link.
boards.link_board` already does for a board.

Mirrors `netbbs.link.boards` as closely as the underlying local
resources allow -- see that module's own docstring for the "why here,
not in netbbs.chat" reasoning, unchanged for channels. Two real
differences, both inherited from how local channels already differ from
local boards, not invented for Link:

- no edit chain (`channel_message_edit` doesn't exist -- chat messages
  have no local edit concept at all, design doc §9.6);
- no origin-succession event types yet (`channel_origin_transfer_offer`/
  `_accepted` aren't built in this issue -- §9.4's model applies
  unchanged by reference if a future issue ever needs it).

Every function here is plain and synchronous, `db`-first, same calling
convention as `netbbs.link.boards`.
"""

from __future__ import annotations

import json

from netbbs.chat.channels import Channel
from netbbs.chat.scrollback import ChannelMessage as LocalChannelMessage, get_scrollback_limit
from netbbs.link.events import (
    CHANNEL_MESSAGE_OBJECT_TYPE,
    ChannelGenesis,
    ChannelMessage,
    build_channel_genesis,
    build_channel_message,
)
from netbbs.link.node_identity import NodeIdentity
from netbbs.search import index_channel_message
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class LinkChannelsError(Exception):
    """Raised for re-Linking an already-Linked channel."""


class ChannelCarryLimitError(Exception):
    """Raised by `materialize_carried_channel` when this node's own
    `max_carried_channels` quota (design doc §9.6/§13.9) is already
    reached -- the exact channel-side counterpart to `netbbs.link.
    boards.BoardCarryLimitError`; see that exception's own docstring for
    why the caller must treat this differently from every other
    rejection: the underlying `channel_genesis` is already verified,
    accepted, and persisted, and keeps gossiping normally regardless --
    only this node's own local materialization is refused."""


def is_channel_linked(db: Database, channel: Channel) -> bool:
    """Whether `channel` already has a `channel_genesis` on file -- the
    single source of truth for "is this channel Linked," mirroring
    `netbbs.link.boards.is_board_linked` exactly."""
    row = db.connection.execute(
        "SELECT link_genesis_json FROM channels WHERE id = ?", (channel.id,)
    ).fetchone()
    return row is not None and row["link_genesis_json"] is not None


def link_channel(
    db: Database,
    channel: Channel,
    *,
    node_identity: NodeIdentity,
    description: str | None = None,
    default_min_level: int | None = None,
    default_min_age: int | None = None,
    default_name_requirement: str | None = None,
) -> ChannelGenesis:
    """
    Put `channel` into Link scope: build and sign a `channel_genesis`
    event referencing its existing `channel_id` and persist it on the
    channel's own row -- mirrors `netbbs.link.boards.link_board` exactly,
    minus the board-only `default_min_write_level`/`default_moderated`/
    `default_max_post_age_days`/`forked_from` parameters `Channel` has no
    equivalent settings for.

    Raises `LinkChannelsError` if `channel` is already Linked.

    Deliberately does **not** register the result with a live `LinkNode`
    -- same division of responsibility `link_board` already documents;
    callers (the `[L]ink` admin command) do that themselves right after
    this function returns.
    """
    if is_channel_linked(db, channel):
        raise LinkChannelsError(f"channel {channel.name!r} is already Linked")

    genesis = build_channel_genesis(
        signing_identity=node_identity.signing_key,
        origin_fingerprint=node_identity.fingerprint,
        channel_id=channel.channel_id,
        name=channel.name,
        created_at=utc_now_iso(),
        description=description if description is not None else channel.description,
        default_min_level=default_min_level,
        default_min_age=default_min_age,
        default_name_requirement=default_name_requirement,
    )

    db.connection.execute(
        "UPDATE channels SET link_genesis_json = ? WHERE id = ?",
        (json.dumps(genesis.to_dict()), channel.id),
    )
    db.connection.commit()

    return genesis


def _channel_from_row(row) -> Channel:
    """Local row->`Channel` mapping, mirroring `netbbs.link.boards.
    _board_from_row`'s own "duplicate rather than reach into another
    module's private helper" reasoning."""
    return Channel(
        id=row["id"], channel_id=row["channel_id"], name=row["name"], description=row["description"],
        min_level=row["min_level"], category_id=row["category_id"], pinned=bool(row["pinned"]),
        created_at=row["created_at"], topic=row["topic"], hidden=bool(row["hidden"]),
        members_only=bool(row["members_only"]), allow_member_invites=bool(row["allow_member_invites"]),
        min_age=row["min_age"], name_requirement=row["name_requirement"], community_id=row["community_id"],
    )


def carried_channel_count(db: Database, own_fingerprint: str) -> int:
    """How many channels this node currently carries -- mirrors
    `netbbs.link.boards.carried_board_count` exactly."""
    count = 0
    for row in db.connection.execute(
        "SELECT link_genesis_json FROM channels WHERE link_genesis_json IS NOT NULL"
    ):
        genesis = json.loads(row["link_genesis_json"])
        if genesis["envelope"]["payload"].get("origin_fingerprint") != own_fingerprint:
            count += 1
    return count


def materialize_carried_channel(
    db: Database,
    genesis: ChannelGenesis,
    *,
    own_fingerprint: str | None = None,
    max_carried_channels: int | None = None,
) -> Channel:
    """
    Turn a *received* (not self-originated) `channel_genesis` into a
    real, locally browsable `Channel` row -- mirrors `netbbs.link.
    boards.materialize_carried_board` exactly: bypasses `netbbs.chat.
    channels.create_channel` (which mints a fresh content-addressed ID
    from the local creator/timestamp, wrong for carried content) in
    favor of a direct insert using the genesis's own `channel_id`
    verbatim. Idempotent, keyed on `genesis.payload["channel_id"]`.

    `topic` is left `NULL` -- a carried channel starts with no topic set,
    same as a newly-created local one; genesis carries no topic field to
    seed it from (design doc §9.6's cascading-recommendation list is
    `description`/`min_level`/`min_age`/`name_requirement` only, matching
    what `Channel` actually has settable at creation time).
    """
    existing = db.connection.execute(
        "SELECT * FROM channels WHERE channel_id = ?", (genesis.payload["channel_id"],)
    ).fetchone()
    if existing is not None:
        return _channel_from_row(existing)

    if max_carried_channels is not None and carried_channel_count(db, own_fingerprint) >= max_carried_channels:
        raise ChannelCarryLimitError(
            f"cannot carry channel {genesis.payload['channel_id']!r}: already at this node's own "
            f"max_carried_channels limit ({max_carried_channels})"
        )

    payload = genesis.payload
    db.connection.execute(
        """
        INSERT INTO channels
            (channel_id, name, description, min_level, category_id, pinned, created_at,
             hidden, members_only, allow_member_invites, min_age, name_requirement, community_id,
             link_genesis_json)
        VALUES (?, ?, ?, ?, NULL, 0, ?, 0, 0, 0, ?, ?, NULL, ?)
        """,
        (
            payload["channel_id"],
            payload["name"],
            payload.get("description"),
            payload.get("default_min_level", 0),
            payload["created_at"],
            payload.get("default_min_age"),
            payload.get("default_name_requirement"),
            json.dumps(genesis.to_dict()),
        ),
    )
    db.connection.commit()

    return _channel_from_row(
        db.connection.execute("SELECT * FROM channels WHERE channel_id = ?", (payload["channel_id"],)).fetchone()
    )


def _channel_message_from_row(row) -> LocalChannelMessage:
    return LocalChannelMessage(
        id=row["id"], channel_id=row["channel_id"], kind=row["kind"], author_label=row["author_label"],
        author_fingerprint=row["author_fingerprint"], body=row["body"], created_at=row["created_at"],
    )


def materialize_carried_channel_message(
    db: Database, message: ChannelMessage, *, sender_fingerprint: str
) -> LocalChannelMessage | None:
    """
    Turn a *received* `channel_message` into a new `channel_messages`
    row -- mirrors `netbbs.link.boards.materialize_carried_post`'s
    shape (idempotent, same-transaction `link_events` persistence, same
    `local_user_id@home_node_fingerprint` author-label synthesis), using
    `netbbs.chat.scrollback`'s own insert-and-trim SQL directly (not
    `record_message`, which would mint a fresh local identity rather
    than using the event's own `content_id`).

    Idempotency is keyed on `channel_messages.link_content_id` -- the
    event's own `content_id`, stored redundantly for an indexed lookup
    (see the migration's own docstring for why: unlike `posts.post_id`,
    `channel_messages.id` is a plain autoincrement with no existing
    content-addressed column to reuse for dedup, since `netbbs.chat.
    scrollback`'s own trim-to-limit ordering depends on `id` staying a
    simple insertion-order integer).

    Returns `None` (not an error) if `message.payload["channel_id"]`
    names a channel this node does not carry -- the same "not carried on
    this node" honest exclusion §9.3 already establishes for a board_post
    whose board is unknown.

    **A real, worth-repeating consequence of channels' own bounded
    scrollback** (design doc §9.6): this insert is subject to `netbbs.
    chat.scrollback`'s existing trim-to-limit behavior exactly like a
    local message. A materialized message can be trimmed back out by a
    later burst of activity in the same channel -- not a bug, the same
    bound every channel's own local history already has -- in which case
    this function returns `None` rather than a row that no longer exists.
    """
    existing = db.connection.execute(
        "SELECT * FROM channel_messages WHERE link_content_id = ?", (message.content_id,)
    ).fetchone()
    if existing is not None:
        return _channel_message_from_row(existing)

    channel_row = db.connection.execute(
        "SELECT id FROM channels WHERE channel_id = ?", (message.payload["channel_id"],)
    ).fetchone()
    if channel_row is None:
        return None
    channel_local_id = channel_row["id"]

    payload = message.payload
    author = payload["author"]
    author_label = f"{author['local_user_id']}@{author['home_node_fingerprint']}"

    db.connection.execute(
        """
        INSERT INTO link_events (content_id, sender_fingerprint, object_type, envelope_json, received_at, channel_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(content_id) DO NOTHING
        """,
        (
            message.content_id, sender_fingerprint, CHANNEL_MESSAGE_OBJECT_TYPE, json.dumps(message.to_dict()),
            utc_now_iso(), payload["channel_id"],
        ),
    )
    db.connection.execute(
        """
        INSERT INTO channel_messages
            (channel_id, kind, author_label, author_fingerprint, body, created_at, link_content_id)
        VALUES (?, 'message', ?, NULL, ?, ?, ?)
        """,
        (channel_local_id, author_label, payload["body"], payload["created_at"], message.content_id),
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
        (channel_local_id, channel_local_id, limit),
    )
    db.connection.commit()

    row = db.connection.execute(
        "SELECT * FROM channel_messages WHERE link_content_id = ?", (message.content_id,)
    ).fetchone()
    if row is None:
        # Trimmed before this call ever got to read it back -- an
        # already-accepted burst of local activity beat this materialization
        # to the scrollback limit. Not an error (design doc §9.6's own
        # stated bounded-scrollback consequence) -- there is simply
        # nothing left to return.
        return None
    materialized = _channel_message_from_row(row)
    index_channel_message(db, channel_local_id, materialized.id, "message", payload["body"])
    db.connection.commit()
    return materialized


def queue_channel_message_if_linked(
    db: Database,
    message: LocalChannelMessage,
    channel: Channel,
    *,
    node_identity: NodeIdentity,
) -> ChannelMessage | None:
    """
    If `channel` is Linked, build and sign a `channel_message` event for
    `message` and store it on the message's own row for `netbbs.link.
    sync` to push -- a no-op returning `None` otherwise. Mirrors
    `netbbs.link.boards.queue_board_post_if_linked` exactly, minus the
    board-only `parent_post_id` reply-linking logic (channel scrollback
    is flat, never threaded).

    Idempotent: a message that already has a queued event returns it
    as-is. Only `kind="message"` is ever queued -- join/leave/presence
    events (design doc §9.6: "asynchronous channel-message propagation
    only") are never propagated over Link, matching the issue's own
    explicit "Real-time Link chat delivery/presence... is Phase 5"
    exclusion; this queues chat *text*, never presence.
    """
    if message.kind != "message":
        return None
    if not is_channel_linked(db, channel):
        return None

    existing = db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE id = ?", (message.id,)
    ).fetchone()
    if existing is not None and existing["link_event_json"] is not None:
        return ChannelMessage.from_dict(json.loads(existing["link_event_json"]))

    channel_message = build_channel_message(
        signing_identity=node_identity.signing_key,
        home_node_fingerprint=node_identity.fingerprint,
        local_user_id=message.author_label,
        channel_id=channel.channel_id,
        body=message.body or "",
        created_at=message.created_at,
    )

    db.connection.execute(
        "UPDATE channel_messages SET link_event_json = ? WHERE id = ?",
        (json.dumps(channel_message.to_dict()), message.id),
    )
    db.connection.commit()

    return channel_message


def load_own_channel_events(db: Database, own_fingerprint: str) -> list[ChannelGenesis | ChannelMessage]:
    """
    This node's own originated `channel_genesis`/`channel_message`
    events, read directly off the `channels`/`channel_messages` tables'
    own columns -- mirrors `netbbs.link.boards.load_own_board_events`
    exactly, minus the lifecycle-event half (no origin succession for
    channels yet, see module docstring).
    """
    events: list[ChannelGenesis | ChannelMessage] = []
    for row in db.connection.execute(
        "SELECT link_genesis_json FROM channels WHERE link_genesis_json IS NOT NULL"
    ):
        genesis = ChannelGenesis.from_dict(json.loads(row["link_genesis_json"]))
        if genesis.payload["origin_fingerprint"] == own_fingerprint:
            events.append(genesis)
    for row in db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE link_event_json IS NOT NULL"
    ):
        events.append(ChannelMessage.from_dict(json.loads(row["link_event_json"])))
    return events
