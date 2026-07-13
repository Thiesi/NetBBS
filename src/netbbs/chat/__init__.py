"""
Local real-time chat: channels + broadcast hub.

Local-only (design doc §15) — no Link yet. Channel IDs are
content-addressed from day one (§7), same reasoning as boards.
Moderator/permission grants and mute/ban/kick (§13, sign-off round 37)
live in `netbbs.chat.moderation`.
"""

from netbbs.chat.channels import (
    Channel,
    ChannelError,
    create_channel,
    get_channel_by_name,
    list_channels,
)
from netbbs.chat.hub import ChatHub
from netbbs.chat.moderation import (
    ChannelRestriction,
    ChatModerationError,
    DurationError,
    ban_user,
    is_banned,
    is_muted,
    kick_user,
    list_channel_restrictions,
    mute_user,
    parse_duration,
    unban_user,
    unmute_user,
)
from netbbs.chat.scrollback import (
    ChannelMessage,
    get_scrollback,
    get_scrollback_limit,
    record_message,
    set_scrollback_limit,
)

__all__ = [
    "Channel",
    "ChannelError",
    "create_channel",
    "get_channel_by_name",
    "list_channels",
    "ChatHub",
    "ChannelRestriction",
    "ChatModerationError",
    "DurationError",
    "ban_user",
    "is_banned",
    "is_muted",
    "kick_user",
    "list_channel_restrictions",
    "mute_user",
    "parse_duration",
    "unban_user",
    "unmute_user",
    "ChannelMessage",
    "get_scrollback",
    "get_scrollback_limit",
    "record_message",
    "set_scrollback_limit",
]
