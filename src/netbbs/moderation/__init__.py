"""
Moderation: currently just the local blocklist (design doc §15 Phase 1).

Natural home for the richer §13 moderation model (mute/ban/kick, board
moderator roles) once that's built in Phase 2 — kept as its own package
now rather than folding the blocklist into `netbbs.auth` or
`netbbs.permissions`, both different concerns (see `blocklist.py`'s
module docstring for the layering reasoning).
"""

from netbbs.moderation.blocklist import (
    BlocklistEntry,
    BlocklistError,
    block_user,
    is_blocked,
    list_blocklist,
    unblock_user,
)

__all__ = [
    "BlocklistEntry",
    "BlocklistError",
    "block_user",
    "is_blocked",
    "list_blocklist",
    "unblock_user",
]
