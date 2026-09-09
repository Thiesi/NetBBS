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
    return get_user_preference(db, user, _PREFERENCE_KEY, default="on") != "off"


def set_mrc_lastseen_recorded(db: Database, user: User, recorded: bool) -> None:
    set_user_preference(db, user, _PREFERENCE_KEY, "on" if recorded else "off")


def mrc_lastseen_for_username(db: Database, username: str) -> bool:
    """By username, for the bridge: an unknown account is on (the hub's
    default); a database failure raises so the bridge can tell."""
    try:
        user = get_user_by_username(db, username)
    except AuthError:
        return True
    return mrc_lastseen_recorded(db, user)
