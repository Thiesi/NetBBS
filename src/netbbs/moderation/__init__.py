"""
Moderation: the local blocklist (design doc §15 Phase 1), plus the
richer §13 moderator-grant model (round 34) — per-object and
local-blanket grants across `read/write/edit/delete/approve`
(boards/file areas) and `edit/moderate/manage_members` (channels),
and a generic moderation-action audit log shared by grants now and by
mute/ban/kick and moderated-board approval once those later Phase 2
tracks land.
"""

from netbbs.moderation.blocklist import (
    BlocklistEntry,
    BlocklistError,
    block_user,
    is_blocked,
    list_blocklist,
    unblock_user,
)
from netbbs.moderation.log import (
    ModerationLogEntry,
    list_actions_for_object,
    list_actions_for_target_user,
    record_action,
)
from netbbs.moderation.roles import (
    BoardPermission,
    ChannelPermission,
    ModeratorGrant,
    ModeratorGrantError,
    get_grant,
    grant_permissions,
    has_permission,
    list_grants_for_community,
    list_grants_for_object,
    list_grants_for_user,
    revoke_permissions,
)

__all__ = [
    "BlocklistEntry",
    "BlocklistError",
    "block_user",
    "is_blocked",
    "list_blocklist",
    "unblock_user",
    "ModerationLogEntry",
    "list_actions_for_object",
    "list_actions_for_target_user",
    "record_action",
    "BoardPermission",
    "ChannelPermission",
    "ModeratorGrant",
    "ModeratorGrantError",
    "get_grant",
    "grant_permissions",
    "has_permission",
    "list_grants_for_community",
    "list_grants_for_object",
    "list_grants_for_user",
    "revoke_permissions",
]
