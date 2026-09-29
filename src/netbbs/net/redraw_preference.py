"""
Per-user "redraw menus in place instead of scrolling" preference
(dogfood feature request): whether a menu-driven screen clears the
terminal before reprinting itself -- both for a state-change redraw of
the *same* screen (e.g. moving the cursor in `netbbs.net.
resource_editor.edit_resource_draft`) and for moving to a genuinely
*different* screen. A thin typed wrapper over `netbbs.user_preferences`'
generic per-user key-value store, the same shape
`netbbs.net.editor_preference`/`netbbs.net.color_depth_preference`/
`netbbs.net.menu_description_preference` already established.

Defaults to off, unlike `menu_description_preference`'s own "on by
default" choice: this trades away scrollback continuity (anything
above a cleared screen -- a save confirmation, chat output -- is gone
the moment the next screen clears), a real, not merely cosmetic,
behavior change every existing account should explicitly opt into
rather than wake up to. Not asked during registration either, for the
same reason: a brand-new caller who has never seen either behavior
can't meaningfully judge the trade-off yet -- discovered later via
"Your profile," the same place every other display preference already
lives, with a one-time contextual hint (`redraw_in_place_ever_set`
below) pointing at it the first time it would actually matter for an
account that has never touched the setting.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "redraw_in_place"


def redraw_in_place_enabled(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _PREFERENCE_KEY, default="off") == "on"


def redraw_in_place_ever_set(db: Database, user: User) -> bool:
    """Whether this account has ever explicitly touched the setting --
    distinct from the stored *value* happening to be "off" (its own
    default) -- gates the one-time contextual hint pointing new callers
    at this preference without repeating it forever."""
    return get_user_preference(db, user, _PREFERENCE_KEY, default=None) is not None


def set_redraw_in_place_enabled(db: Database, user: User, enabled: bool) -> None:
    set_user_preference(db, user, _PREFERENCE_KEY, "on" if enabled else "off")


def start_new_account_redrawing_in_place(db: Database, user: User) -> None:
    """A new account starts with in-place redraw on, however it was made:
    signed up, created in the console, or the first SysOp created at
    install (issue #840). The first SysOp was left out, so the field test's SysOp
    got the scrolling screens every one of her callers did not. Existing
    accounts keep the resolve default above."""
    set_redraw_in_place_enabled(db, user, True)
