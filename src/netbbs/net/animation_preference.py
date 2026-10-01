"""
Per-user "Animations" preference (issue #929, step 6): whether SysOp art a
SysOp gave a speed plays as an animation for this caller
(`netbbs.net.art_pacing`), or is drawn at once. On by default: the art is
the SysOp's choice and any key ends it. A thin typed wrapper over
`netbbs.user_preferences`, the same shape as `netbbs.net.redraw_preference`.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "animations"


def animations_enabled(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _PREFERENCE_KEY, default="on") == "on"


def set_animations_enabled(db: Database, user: User, enabled: bool) -> None:
    set_user_preference(db, user, _PREFERENCE_KEY, "on" if enabled else "off")
