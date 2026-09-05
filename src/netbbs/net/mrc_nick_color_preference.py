"""
Per-user MRC nick colour (issue #304): the CGA colour a caller's handle
wears in the house-style body every relayed line carries
(`netbbs.mrc.protocol.format_room_body`). A thin typed wrapper over
`netbbs.user_preferences`, the same shape `netbbs.net.
mrc_color_preference` has; the default is the house yellow (CGA 14).

The bridge knows callers by username, not `User`, and reads this on
the lane when it announces someone -- `mrc_nick_color_for_username` is
that lookup. A change applies on the caller's next join.
"""

from __future__ import annotations

from netbbs.auth.users import User, get_user_by_username
from netbbs.mrc.protocol import DEFAULT_NICK_COLOR
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "mrc_nick_color"


def mrc_nick_color(db: Database, user: User) -> int:
    raw = get_user_preference(db, user, _PREFERENCE_KEY, default=None)
    try:
        value = int(raw) if raw is not None else DEFAULT_NICK_COLOR
    except ValueError:
        return DEFAULT_NICK_COLOR
    return value if 0 <= value <= 15 else DEFAULT_NICK_COLOR


def set_mrc_nick_color(db: Database, user: User, color: int) -> None:
    if not 0 <= color <= 15:
        raise ValueError(f"MRC nick colour must be a CGA colour 0-15, got {color!r}")
    set_user_preference(db, user, _PREFERENCE_KEY, str(color))


def mrc_nick_color_for_username(db: Database, username: str) -> int:
    try:
        user = get_user_by_username(db, username)
    except Exception:
        return DEFAULT_NICK_COLOR
    if user is None:
        return DEFAULT_NICK_COLOR
    return mrc_nick_color(db, user)
