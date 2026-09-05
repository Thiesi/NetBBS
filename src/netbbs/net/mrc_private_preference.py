"""
Per-user opt-in to private messages from MRC users (issue #305, design
doc §16 Decision 3 as amended). Off by default: an MRC private line
carries no confidentiality the network can promise -- the hub and any
client can read or spoof it -- so a caller has to ask for them, and
asking is also what allows sending. A thin typed wrapper over
`netbbs.user_preferences`, the shape `netbbs.net.mrc_color_preference`
established; the bridge knows callers by username and reads this on the
lane when it announces someone (`mrc_private_messages_for_username`).
"""

from __future__ import annotations

from netbbs.auth.users import User, get_user_by_username
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "mrc_private_messages"


def mrc_private_messages_enabled(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _PREFERENCE_KEY, default="off") == "on"


def set_mrc_private_messages_enabled(db: Database, user: User, enabled: bool) -> None:
    set_user_preference(db, user, _PREFERENCE_KEY, "on" if enabled else "off")


def mrc_private_messages_for_username(db: Database, username: str) -> bool:
    try:
        user = get_user_by_username(db, username)
    except Exception:
        return False
    if user is None:
        return False
    return mrc_private_messages_enabled(db, user)
