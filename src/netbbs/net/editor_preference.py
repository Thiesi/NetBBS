"""
Per-user "compose with the fullscreen prose editor" preference (design
doc -- prose editor round B2): whether composing/editing a board post
or bio opens `netbbs.net.prose_editor.edit_prose` instead of the plain
line-based flow. A thin typed wrapper over `netbbs.user_preferences`'
generic per-user key-value store, the exact same shape
`netbbs.chat.timestamps` already established for `/timestamps`.

Defaults to off: the plain `read_line()` flow remains what every
existing account sees until they explicitly opt in, matching design
doc §15's "Editor implementation notes" -- the fullscreen editor is a
convenience layer, never the only path.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "fullscreen_editor"


def fullscreen_editor_enabled(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _PREFERENCE_KEY, default="off") == "on"


def set_fullscreen_editor_enabled(db: Database, user: User, enabled: bool) -> None:
    set_user_preference(db, user, _PREFERENCE_KEY, "on" if enabled else "off")
