"""
Per-user choice whether the MRC hub may remember when they were last
seen (`STATUS LASTSEEN ON|OFF`, MRCDoc rev 1.26; issue #378). On by
default because that is the hub's own default; a caller who turns it
off has the bridge send `STATUS LASTSEEN OFF` whenever it announces
them. The thin typed wrapper shape of `netbbs.net.mrc_private_preference`.
"""

from __future__ import annotations

from netbbs.auth.users import AuthError, User, get_user_by_username
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "mrc_lastseen"


def mrc_lastseen_recorded(db: Database, user: User) -> bool:
    return mrc_lastseen_choice(db, user) is not False


def mrc_lastseen_choice(db: Database, user: User) -> bool | None:
    """The caller's explicit choice, or `None` when they never made one
    -- the bridge sends `STATUS LASTSEEN` only for an explicit choice,
    ON as well as OFF, since the hub remembers an opt-out across
    sessions and only an explicit ON undoes it."""
    value = get_user_preference(db, user, _PREFERENCE_KEY, default=None)
    if value == "off":
        return False
    if value == "on":
        return True
    return None


def set_mrc_lastseen_recorded(db: Database, user: User, recorded: bool) -> None:
    set_user_preference(db, user, _PREFERENCE_KEY, "on" if recorded else "off")


def mrc_lastseen_for_username(db: Database, username: str) -> bool | None:
    """By username, for the bridge: the explicit choice, `None` for no
    choice or an unknown account (the hub's default applies); a
    database failure raises so the bridge can tell."""
    try:
        user = get_user_by_username(db, username)
    except AuthError:
        return None
    return mrc_lastseen_choice(db, user)
