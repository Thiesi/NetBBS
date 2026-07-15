"""
Local real-time chat: channels + broadcast hub.

Local-only (design doc §15) — no Link yet. Channel IDs are
content-addressed from day one (§7), same reasoning as boards.
Moderator/permission grants and mute/ban/kick (§13, sign-off round 37)
live in `netbbs.chat.moderation`; transparent display aliases (round
32, sign-off round 41) live in `netbbs.chat.nick`; node-wide account
presence/away state (round 32, sign-off round 42) lives in
`netbbs.chat.presence`.
"""

from netbbs.chat.channels import (
    Channel,
    ChannelError,
    TopicError,
    create_channel,
    get_channel_by_name,
    list_channels,
    set_topic,
)
from netbbs.chat.hub import ChatHub, ParticipantId, QueueOverflowNotice
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.membership import (
    ChannelInvitation,
    MembershipError,
    PendingInvitationView,
    accept_invitation,
    add_member,
    create_invitation,
    has_pending_invitation,
    is_member,
    list_members,
    list_pending_invitations_for_user,
    remove_member,
    revoke_invitation,
)
from netbbs.chat.nick import (
    MAX_NICK_LENGTH,
    NICK_MARKER,
    NickError,
    chat_stream_label,
    display_label,
    get_nick,
    set_nick,
)
from netbbs.chat.presence import PresenceRegistry
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
from netbbs.chat.timestamps import (
    format_with_preference,
    set_timestamps_enabled,
    timestamps_enabled,
)

__all__ = [
    "Channel",
    "ChannelError",
    "TopicError",
    "create_channel",
    "get_channel_by_name",
    "list_channels",
    "set_topic",
    "ChatHub",
    "ParticipantId",
    "QueueOverflowNotice",
    "MessageMailbox",
    "ChannelInvitation",
    "MembershipError",
    "PendingInvitationView",
    "accept_invitation",
    "add_member",
    "create_invitation",
    "has_pending_invitation",
    "is_member",
    "list_members",
    "list_pending_invitations_for_user",
    "remove_member",
    "revoke_invitation",
    "MAX_NICK_LENGTH",
    "NICK_MARKER",
    "NickError",
    "chat_stream_label",
    "display_label",
    "get_nick",
    "set_nick",
    "PresenceRegistry",
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
    "format_with_preference",
    "set_timestamps_enabled",
    "timestamps_enabled",
]
