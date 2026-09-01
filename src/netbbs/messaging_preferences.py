"""
Per-user preference for whether other callers may send an unsolicited
one-off direct message via the caller-facing Who's-online screen
(issue #99, `netbbs.net.directory_flow._caller_who_screen`).

Thin wrapper over `netbbs.user_preferences`'s generic store, the same
pattern `netbbs.directory`'s bio-visibility preference already
establishes -- except defaulted the *other* way. Bio visibility
defaults private (opt-in to share) because it's about disclosing
personal content; this defaults to accepting messages (opt-out to
block) because most callers presumably want to stay reachable, and the
feature this gates is unsolicited-but-visible (the sender always knows
who they're messaging, unlike, say, unsolicited chat invites) rather
than a disclosure risk.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_ACCEPTS_DIRECT_MESSAGES_KEY = "accepts_direct_messages"


def accepts_direct_messages(db: Database, user: User) -> bool:
    """Default `True` (opt-out, not opt-in) -- see module docstring."""
    return get_user_preference(db, user, _ACCEPTS_DIRECT_MESSAGES_KEY, default="1") == "1"


def set_accepts_direct_messages(db: Database, user: User, accepts: bool) -> None:
    set_user_preference(db, user, _ACCEPTS_DIRECT_MESSAGES_KEY, "1" if accepts else "0")
