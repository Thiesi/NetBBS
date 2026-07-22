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
    hidden: bool
    members_only: bool
    allow_member_invites: bool
    # Age/name-gating (design doc §18, rounds 85/86/101/102) -- nullable,
    # NULL means no gate *and* (since round 86/§16) "inherit this
    # Community's default" if this channel belongs to one, same shape
    # and enforcement point as netbbs.boards.boards.Board's own fields;
    # see netbbs.net.chat_flow's join/message checks.
    min_age: int | None
    name_requirement: str | None  # None | "verified" | "verified_and_displayed"
    # Zero-or-one, nullable FK (design doc §16, round 83), same shape as
    # Board.community_id. Deliberately does NOT gain Community-level
    # inheritance for `min_level` itself -- see the round-104 migration
    # note in netbbs.storage.migrations for why that field was left out
    # of scope rather than invented.
    community_id: int | None


def create_channel(
    db: Database,
    name: str,
    *,
    description: str | None = None,
    min_level: int = 0,
    category_id: int | None = None,
    pinned: bool = False,
    hidden: bool = False,
    members_only: bool = False,
    allow_member_invites: bool = False,
    min_age: int | None = None,
    name_requirement: str | None = None,
    community_id: int | None = None,
    creator: User,
) -> Channel:
    """Create a new local channel. No permission check on creation here —
    same reasoning as `netbbs.boards.create_board`: an admin-level action
    with no SysOp/moderator concept defined yet in Phase 1.

    `category_id` optionally places the channel under a
    `netbbs.chat.categories.Category`. `pinned` channels always sort
    first — see `list_channels`. `hidden`/`members_only`/
    `allow_member_invites` (design doc §8/round 33 points 8/9/11, Phase
    2 Track 5h) default to `False` — an invite-only or hidden channel is
    always an explicit opt-in, never accidental. No `/createchannel`
    command exists yet (matches every earlier track's precedent — no
    SysOp channel/board-creation UI), so these are seeded here directly,
    e.g. by test fixtures, the same way round 21's file-area equivalents
    already are.

    `min_age`/`name_requirement` (design doc §18, rounds 85/86/101/102)
    are the same nullable-means-no-gate shape as
    `netbbs.boards.boards.create_board`'s own fields — see that
    function's docstring.

    `community_id` (design doc §16, round 83) optionally places the
    channel under a `netbbs.communities.Community` -- same zero-or-one
    shape as `Board.community_id`. Unlike boards/file areas,
    `min_level` itself is NOT nullable/inheritable here -- see
    `Channel.community_id`'s own docstring comment.
    """
    if name_requirement not in (None, "verified", "verified_and_displayed"):
        raise ChannelError(f"invalid name_requirement: {name_requirement!r}")
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
                (channel_id, name, description, min_level, category_id, pinned, created_at,
                 hidden, members_only, allow_member_invites, min_age, name_requirement, community_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id, name, description, min_level, category_id, int(pinned), created_at,
                int(hidden), int(members_only), int(allow_member_invites), min_age, name_requirement,
                community_id,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise ChannelError(f"could not create channel {name!r} — name already in use?") from exc

    new_channel = get_channel_by_name(db, name)
    record_action(
        db, actor=creator, action="create_channel", object_type="channel", object_id=new_channel.id,
        detail=f"created channel {name!r}",
    )
    return new_channel


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


def update_channel(
    db: Database,
    channel: Channel,
    *,
    name: str,
    description: str | None,
    min_level: int,
    category_id: int | None,
    pinned: bool,
    hidden: bool,
    members_only: bool,
    allow_member_invites: bool,
    min_age: int | None,
    name_requirement: str | None,
    community_id: int | None,
    changed_by: User,
) -> Channel:
    """
    Replace `channel`'s editable settings with the given full state
    (design doc -- channel management round), mirroring
    `netbbs.boards.boards.update_board`: every field is required, not
    partial/PATCH-style; the admin UI pre-fills current values as
    editable defaults. `channel_id`/`created_at` are immutable, not
    accepted here. `topic` is deliberately not settable through this
    function -- it stays gated by `set_topic`'s own
    `ChannelPermission.EDIT` check and audit trail, not folded into
    this SysOp-only full-state replace.

    `min_age`/`name_requirement` follow design doc §18 (rounds 101/102)
    -- see `create_channel`'s docstring. `community_id` follows design
    doc §16 (round 83) -- see that same docstring.
    """
    if name_requirement not in (None, "verified", "verified_and_displayed"):
        raise ChannelError(f"invalid name_requirement: {name_requirement!r}")
    try:
        db.connection.execute(
            """
            UPDATE channels
            SET name = ?, description = ?, min_level = ?, category_id = ?, pinned = ?,
                hidden = ?, members_only = ?, allow_member_invites = ?,
                min_age = ?, name_requirement = ?, community_id = ?
            WHERE id = ?
            """,
            (
                name, description, min_level, category_id, int(pinned),
                int(hidden), int(members_only), int(allow_member_invites),
                min_age, name_requirement, community_id, channel.id,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise ChannelError(f"could not update channel {channel.name!r} — name already in use?") from exc

    updated = get_channel_by_name(db, name)
    record_action(
        db, actor=changed_by, action="update_channel", object_type="channel", object_id=channel.id,
        detail=f"updated channel {channel.name!r}",
    )
    return updated


def delete_channel(db: Database, channel: Channel, *, deleted_by: User) -> None:
    """
    Permanently remove `channel`, along with its scrollback, mute/ban
    restrictions, membership/invitations, any moderator grants scoped
    to it (design doc -- channel management round), and any per-user
    read-cursor/follow rows for it (issue #56) --
    application-level cleanup, same reasoning as
    `netbbs.boards.boards.delete_board`: no `ON DELETE` behavior in the
    schema (a rebuild to add it risks the same silent-cascade hazard
    found and documented in round 60). Logged before deleting, matching
    `delete_board`/`delete_user`'s own "log first" precedent.
    """
    record_action(
        db, actor=deleted_by, action="delete_channel", object_type="channel", object_id=channel.id,
        detail=f"deleted channel {channel.name!r} (id {channel.id})",
    )
    db.connection.execute("DELETE FROM channel_messages WHERE channel_id = ?", (channel.id,))
    db.connection.execute("DELETE FROM channel_restrictions WHERE channel_id = ?", (channel.id,))
    db.connection.execute("DELETE FROM channel_members WHERE channel_id = ?", (channel.id,))
    db.connection.execute("DELETE FROM channel_invitations WHERE channel_id = ?", (channel.id,))
    db.connection.execute(
        "DELETE FROM moderator_grants WHERE object_type = 'channel' AND object_id = ?", (channel.id,)
    )
    db.connection.execute(
        "DELETE FROM user_read_cursors WHERE object_type = 'channel' AND object_id = ?", (channel.id,)
    )
    db.connection.execute(
        "DELETE FROM user_follows WHERE object_type = 'channel' AND object_id = ?", (channel.id,)
    )
    db.connection.execute("DELETE FROM channels WHERE id = ?", (channel.id,))
    db.connection.commit()


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
        hidden=bool(row["hidden"]),
        members_only=bool(row["members_only"]),
        allow_member_invites=bool(row["allow_member_invites"]),
        min_age=row["min_age"],
        name_requirement=row["name_requirement"],
        community_id=row["community_id"],
    )
