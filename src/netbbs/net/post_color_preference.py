"""
Per-user "show the color authors put in board posts" preference (issue
#711). A thin typed wrapper over `netbbs.user_preferences`' generic
per-user key-value store, the same shape `netbbs.net.mrc_color_
preference` established.

Matters on a board whose SysOp allows color in posts, and in mail,
which always allows it (issue #809). Defaults to
on, the codebase's "rich default, easy opt-out" posture: a colored body
is filtered before it is drawn (`netbbs.rendering.post_body`), so the
downside of the default is taste, not safety. Off shows those posts as
plain text, with the codes removed.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "post_colors"


def post_colors_enabled(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _PREFERENCE_KEY, default="on") == "on"


def set_post_colors_enabled(db: Database, user: User, enabled: bool) -> None:
    set_user_preference(db, user, _PREFERENCE_KEY, "on" if enabled else "off")
