"""
Transparent chat display aliases (design doc round 32, points 7-10;
sign-off round 41): `/nick` sets a persistent, node-wide presentation
alias — not identity. Every chat rendering keeps the canonical
authenticated username plainly visible alongside it (`nick|username`);
moderation, permissions, blocking, reputation, and auditing always
operate on canonical identity and never look at this module at all.

Stored via `netbbs.user_preferences` (the generic per-user store
introduced in round 38), not a dedicated table — one more small typed
wrapper around it, same as `netbbs.directory`'s bio fields.

Deliberately its own module, not folded into `netbbs.directory`:
design doc round 32 discusses `/nick` alongside `/me`/`/away` as chat
presentation, not as part of the user-directory/vCard feature — a
different concern, even though both happen to sit on the same generic
storage underneath.
"""

from __future__ import annotations

from netbbs.auth.users import User, list_users
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_NICK_KEY = "nick"

MAX_NICK_LENGTH = 32


class NickError(Exception):
    """Raised when a requested alias fails validation (length, or
    colliding with another account's canonical username)."""


def set_nick(db: Database, user: User, nick: str) -> None:
    """
    Set `user`'s display alias. An empty string clears it (see
    `get_nick`) — `/nick off` in the chat command maps to this.

    Validates length and that `nick` doesn't exactly match another
    account's canonical username, case-insensitively (design doc round
    32, point 8: "preserves freedom of presentation without allowing
    an alias to impersonate an authenticated local identity"). Setting
    your own username as your own nick is harmless and allowed — only
    *other* accounts' usernames are rejected. Character content itself
    is deliberately not validated here — sanitized on output, same as
    every other piece of user-generated text in this codebase (bios,
    post bodies, chat messages).
    """
    if not nick:
        set_user_preference(db, user, _NICK_KEY, "")
        return

    if len(nick) > MAX_NICK_LENGTH:
        raise NickError(f"alias cannot exceed {MAX_NICK_LENGTH} characters")

    for other in list_users(db):
        if other.id != user.id and other.username.lower() == nick.lower():
            raise NickError("alias cannot match another account's username")

    set_user_preference(db, user, _NICK_KEY, nick)


def get_nick(db: Database, user: User) -> str | None:
    """`user`'s current alias, or `None` if unset/cleared. An empty
    stored value (from `set_nick(db, user, "")`) is treated the same
    as never having been set."""
    value = get_user_preference(db, user, _NICK_KEY)
    return value if value else None


def display_label(db: Database, user: User) -> str:
    """
    `nick|username` if `user` has an alias set, else just `username` —
    the one place this "which form to show" decision is made, so every
    chat rendering call site (join/leave/message/action) stays
    consistent. Deliberately not used for moderation transparency
    notices (mute/ban/kick/etc.) or command targeting — those always
    show/resolve canonical identity only (design doc round 32, point
    7/9), by calling `user.username` directly instead of this
    function.
    """
    nick = get_nick(db, user)
    return f"{nick}|{user.username}" if nick else user.username
