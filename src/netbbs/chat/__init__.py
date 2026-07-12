"""
Local real-time chat: channels + broadcast hub.

Local-only in Phase 1 (design doc §15) — no Link, no moderators yet.
Channel IDs are content-addressed from day one (§7), same reasoning as
boards.
"""

from netbbs.chat.channels import (
    Channel,
    ChannelError,
    create_channel,
    get_channel_by_name,
    list_channels,
)
from netbbs.chat.hub import ChatHub
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
    "ChannelMessage",
    "get_scrollback",
    "get_scrollback_limit",
    "record_message",
    "set_scrollback_limit",
]
