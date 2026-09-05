"""
Per-user "render the colour codes MRC users put in their lines"
preference (issue #298). A thin typed wrapper over
`netbbs.user_preferences`' generic per-user key-value store, the same
shape `netbbs.net.unicode_style_preference` established.

Defaults to on, this codebase's "rich default, easy opt-out" posture:
colour is part of how the MRC rooms talk, and an inbound line is
sanitized before any code is turned into a colour (see
`netbbs.rendering.pipe_codes`), so the downside of the default is taste,
not safety. Off shows the same lines with every code stripped.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "mrc_colors"


def mrc_colors_enabled(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _PREFERENCE_KEY, default="on") == "on"


def set_mrc_colors_enabled(db: Database, user: User, enabled: bool) -> None:
    set_user_preference(db, user, _PREFERENCE_KEY, "on" if enabled else "off")
