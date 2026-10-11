"""Bot identity and this node's channel policy (design doc §16, #1210).

This module neither launches bots nor grants permission to publish. Policy
controls local presentation; it never rewrites or deletes signed Link events.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from netbbs.auth.users import BOT_LABEL_SUFFIX, AuthError, User, get_user_by_username
from netbbs.chat.channels import Channel
from netbbs.moderation import ChannelPermission, has_permission, record_action
from netbbs.storage.database import Database

if TYPE_CHECKING:
    from netbbs.chat.scrollback import ChannelMessage

BotOrigin = Literal["local", "remote"]
BOT_POLICIES = {"all": "all bots", "local": "only this node's own bots", "none": "no bots"}


class BotPolicyError(Exception):
    """Invalid or unauthorized channel policy change."""


def bot_label_origin(db: Database, label: str, *, remote: bool = False) -> BotOrigin | None:
    """Classify a canonical author, retaining pre-reservation local accounts.

    A peer's identity is its canonical user half, never a display alias.
    Older peers may have human accounts ending in .bot; design §16 accepts
    their classification as bots, just as it does for .door.
    """
    local_part, at, _node = label.partition("@")
    if not local_part.lower().endswith(BOT_LABEL_SUFFIX):
        return None
    if remote or at:
        return "remote"
    try:
        get_user_by_username(db, label)
    except AuthError:
        return "local"
    return None


def bot_origin(db: Database, message: ChannelMessage) -> BotOrigin | None:
    if message.external_source is not None:
        return None
    if message.link_content_id is not None and message.link_event_json is None:
        row = db.connection.execute(
            "SELECT envelope_json FROM link_events WHERE content_id = ?", (message.link_content_id,)
        ).fetchone()
        if row is not None:
            try:
                author = json.loads(row["envelope_json"])["envelope"]["payload"]["author"]["local_user_id"]
                if isinstance(author, str):
                    return bot_label_origin(db, author, remote=True)
            except (KeyError, TypeError, ValueError):
                pass
    # Live Link and origin snapshots carry a node-qualified canonical label;
    # this node's own signed row has link_event_json and remains local.
    return bot_label_origin(db, message.author_label)


def channel_bot_policy(db: Database, channel: Channel) -> str:
    row = db.connection.execute("SELECT * FROM channels WHERE id = ?", (channel.id,)).fetchone()
    if row is None:
        return "none"
    # MRC mappings forbid bots even while paused. Existing historical-schema
    # migration rehearsals may not yet have either optional column.
    if "mrc_room" in row.keys() and row["mrc_room"]:
        return "none"
    return row["bot_policy"] if "bot_policy" in row.keys() else "all"


def bot_origin_allowed(db: Database, channel: Channel, origin: BotOrigin | None) -> bool:
    if origin is None:
        return True
    policy = channel_bot_policy(db, channel)
    return policy == "all" or (policy == "local" and origin == "local")


def bot_message_visible(db: Database, channel: Channel, message: ChannelMessage) -> bool:
    return bot_origin_allowed(db, channel, bot_origin(db, message))


def set_channel_bot_policy(db: Database, channel: Channel, policy: str, *, changed_by: User) -> None:
    if not has_permission(
        db, changed_by, object_type="channel", object_id=channel.id, permission=ChannelPermission.MODERATE
    ):
        raise BotPolicyError("You do not have moderation permission on this channel.")
    if policy not in BOT_POLICIES:
        raise BotPolicyError("Choose all, local or none.")
    row = db.connection.execute("SELECT bot_policy, mrc_room FROM channels WHERE id = ?", (channel.id,)).fetchone()
    if row is None:
        raise BotPolicyError("This channel no longer exists.")
    if row["mrc_room"] and policy != "none":
        raise BotPolicyError("Bots are not permitted in MRC-bridged channels, including paused mappings.")
    if row["bot_policy"] == policy:
        return
    with db.connection:
        db.connection.execute("UPDATE channels SET bot_policy = ? WHERE id = ?", (policy, channel.id))
        record_action(
            db, actor=changed_by, action="channel_bot_policy", object_type="channel", object_id=channel.id,
            detail=f"{row['bot_policy']} -> {policy} (local node only)",
        )
