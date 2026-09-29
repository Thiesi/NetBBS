"""
A caller's character set preference (design doc §3.2, "Character set
per session", issue #929): Auto (the default), Unicode, CP437 or ASCII.
Auto follows what the terminal said when it connected
(`netbbs.net.terminal_detect`); the other three override it. An explicit
choice always wins over detection, so one caller can use SyncTERM at
home and a modern terminal at work under Auto.

It replaced a two-way "Unicode decorative style" preference, stored
under the same per-user store with its own key: an account that had
switched that off reads as ASCII, every other account as Auto, until the
caller chooses. Screens that vary their decoration still ask
`unicode_style_enabled`, which is now simply "not ASCII".
"""

from __future__ import annotations

from typing import Literal

from netbbs.auth.users import User
from netbbs.rendering.charset import ASCII, CP437, UTF8, Charset
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

CharsetPreference = Literal["auto", "unicode", "cp437", "ascii"]

CHARSET_PREFERENCES: tuple[CharsetPreference, ...] = ("auto", "unicode", "cp437", "ascii")

_CHARSET_KEY = "charset"
_LEGACY_STYLE_KEY = "unicode_style"

_FIXED: dict[str, Charset] = {"unicode": UTF8, "cp437": CP437, "ascii": ASCII}


def charset_preference(db: Database, user: User) -> CharsetPreference:
    value = get_user_preference(db, user, _CHARSET_KEY, default=None)
    if value in CHARSET_PREFERENCES:
        return value
    legacy = get_user_preference(db, user, _LEGACY_STYLE_KEY, default=None)
    return "ascii" if legacy == "off" else "auto"


def charset_preference_ever_set(db: Database, user: User) -> bool:
    """Whether the caller has chosen, now or under the old style
    preference by switching it off: such a caller is not asked which
    sample line looks right."""
    if get_user_preference(db, user, _CHARSET_KEY, default=None) is not None:
        return True
    return get_user_preference(db, user, _LEGACY_STYLE_KEY, default=None) == "off"


def set_charset_preference(db: Database, user: User, value: CharsetPreference) -> None:
    if value not in CHARSET_PREFERENCES:
        raise ValueError(f"unknown character set preference {value!r}")
    set_user_preference(db, user, _CHARSET_KEY, value)


def effective_charset(preference: CharsetPreference, session: object) -> Charset:
    """The character set `session` gets under `preference`. The browser
    terminal reads UTF-8 whatever the account says, so there only ASCII
    changes anything."""
    if getattr(session, "transport_name", None) == "web":
        return ASCII if preference == "ascii" else UTF8
    if preference == "auto":
        return getattr(session, "detected_charset", UTF8)
    return _FIXED[preference]


def apply_charset_preference(session: object, preference: CharsetPreference) -> None:
    session.output_charset = effective_charset(preference, session)


def unicode_style_enabled(db: Database, user: User) -> bool:
    """Whether screens use their decorated variants: everything but an
    ASCII preference. CP437 terminals get the decoration mapped."""
    return charset_preference(db, user) != "ascii"


def set_unicode_style_enabled(db: Database, user: User, enabled: bool) -> None:
    """The two-way view: on is Auto, off is ASCII."""
    set_charset_preference(db, user, "auto" if enabled else "ascii")
