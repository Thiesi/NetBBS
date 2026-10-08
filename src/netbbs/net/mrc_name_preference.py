"""
Per-user choice of how an MRC sender's decorated handle is shown (issue
#1156). Some MRC clients put a styled handle such as `+Nick+[CASTLE BBS]`
in front of a line. Since #1152 it is peeled off the text and kept beside
it, and each caller picks one of three ways to see it:

- `combined` (the default): the styled name takes the place of the plain
  one in NetBBS's label, the site stays, and the rest of the handle
  follows as a tag: `<+Nick+@Castle_BBS (CASTLE BBS)>`;
- `both`: the plain label, then the handle as sent (what v7.17.0 showed);
- `label`: the plain label alone (what v7.17.1 showed).

A plain handle, and a line recorded before the handle was kept, looks the
same in all three. A thin typed wrapper over `netbbs.user_preferences`,
the same shape as `netbbs.net.mrc_color_preference`.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "mrc_names"

MRC_NAME_STYLES = ("combined", "both", "label")
DEFAULT_MRC_NAME_STYLE = "combined"


def mrc_name_style(db: Database, user: User) -> str:
    value = get_user_preference(db, user, _PREFERENCE_KEY, default=DEFAULT_MRC_NAME_STYLE)
    return value if value in MRC_NAME_STYLES else DEFAULT_MRC_NAME_STYLE


def set_mrc_name_style(db: Database, user: User, style: str) -> None:
    if style not in MRC_NAME_STYLES:
        raise ValueError(f"MRC name style must be one of {', '.join(MRC_NAME_STYLES)}, got {style!r}")
    set_user_preference(db, user, _PREFERENCE_KEY, style)
