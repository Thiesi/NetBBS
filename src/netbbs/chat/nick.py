"""
Transparent chat display aliases (design doc, points 7-10):
`/nick` sets a persistent, node-wide
presentation alias — not identity.

An alias is always shown next to the account's canonical username, as
`nick|username` (`display_label`, and `chat_stream_label` for the live
stream). Moderation, permissions, blocking, reputation, and auditing
always operate on canonical identity and never look at this module at
all.

Issue #843: the live stream used to show the alias alone, marked
`~nick~`. A field test showed a newcomer taking `~InkWell[sysop]~` for
the SysOp: nothing explained the tildes, and the SysOp's own status bar
reads "InkWell[sysop]". So the stream now shows the username too, and
`set_nick` refuses aliases that read as a staff title or as another
account's username (`netbbs.auth.users.presentation_name_problem`), and
the characters the chat screen itself uses to frame names and notices.

Stored via `netbbs.user_preferences` (the generic per-user store),
not a dedicated table — one more small typed
wrapper around it, same as `netbbs.directory`'s bio fields.

Deliberately its own module, not folded into `netbbs.directory`:
the design doc discusses `/nick` alongside `/me`/`/away` as chat
presentation, not as part of the user-directory/vCard feature — a
different concern, even though both happen to sit on the same generic
storage underneath.
"""

from __future__ import annotations

from netbbs.auth.users import User, presentation_name_problem
from netbbs.rendering import MUTED_COLOR, NICK_COLOR, colored, sanitize_text
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_NICK_KEY = "nick"

MAX_NICK_LENGTH = 32

#: Joins an alias to the username it stands for: `nick|username`.
NICK_SEPARATOR = "|"

# Characters an alias may not contain (issue #843): the separator itself,
# the brackets of a status-bar tag ("[sysop]"), the angle brackets that
# frame a speaker, and the "*" of `/me` lines and system notices. "~" was
# the old alias marker; an alias holding one would read as two.
_RESERVED_NICK_CHARACTERS = f"{NICK_SEPARATOR}[]<>*~"


class NickError(Exception):
    """Raised when a requested alias fails validation (length, a
    reserved character, or reading as a staff title or another
    account's username)."""


def set_nick(db: Database, user: User, nick: str) -> None:
    """
    Set `user`'s display alias. An empty string clears it (see
    `get_nick`) — a bare `/nick` in the chat command maps to this.

    Validates length and `_RESERVED_NICK_CHARACTERS`, then refuses an
    alias that reads as a staff title (unless `user` is a SysOp) or as
    another account's username once case, spacing, accents and
    look-alike characters are set aside (design doc, point 8:
    "preserves freedom of presentation without allowing an alias to
    impersonate an authenticated local identity"; issue #843). Setting
    your own username as your own nick is harmless and allowed.
    Character content is otherwise deliberately not validated here —
    sanitized on output, same as every other piece of user-generated
    text in this codebase (bios, post bodies, chat messages).
    """
    if not nick:
        set_user_preference(db, user, _NICK_KEY, "")
        return

    if len(nick) > MAX_NICK_LENGTH:
        raise NickError(f"alias cannot exceed {MAX_NICK_LENGTH} characters")

    reserved = sorted({ch for ch in nick if ch in _RESERVED_NICK_CHARACTERS})
    if reserved:
        raise NickError(f"alias cannot contain {' '.join(reserved)}")

    problem = presentation_name_problem(db, nick, owner=user, protect_every_username=True)
    if problem is not None:
        raise NickError(problem)

    set_user_preference(db, user, _NICK_KEY, nick)


def get_nick(db: Database, user: User) -> str | None:
    """`user`'s current alias, or `None` if unset/cleared. An empty
    stored value (from `set_nick(db, user, "")`) is treated the same
    as never having been set."""
    value = get_user_preference(db, user, _NICK_KEY)
    return value if value else None


def display_label(db: Database, user: User) -> str:
    """
    `nick|username` if `user` has an alias set, else just `username`,
    unsanitized and uncolored -- for directory-style listings (`/who`,
    `/whois`, `/names`) and Link direct messages, which sanitize it
    themselves. The live stream uses `chat_stream_label`, the same text
    with the alias colored.
    Deliberately not used for moderation transparency notices (mute/
    ban/kick/etc.) or command targeting -- those always show/resolve
    canonical identity only (design doc, point 7/9), by calling
    `user.username` directly.
    """
    nick = get_nick(db, user)
    return f"{nick}{NICK_SEPARATOR}{user.username}" if nick else user.username


def chat_stream_label(db: Database, user: User) -> str:
    """
    `nick|username` with the alias colored via `NICK_COLOR` and the
    `|username` after it muted, or plain `username` if `user` has no
    alias. The alias leads: the username is there so no alias stands
    alone (issue #843), and at the same weight readers could not tell
    which of the two was the alias (issue #899). Used in the live chat stream
    itself (regular messages, `/me`, join/leave, scrollback replay).

    Sanitizes the underlying nick/username *before* applying
    `NICK_COLOR`, never after — this function owns both concerns itself
    rather than leaving sanitization to the caller the way
    `display_label` does, so a caller never runs `sanitize_text` on
    this function's own output, which would strip the SGR codes just
    applied here. Callers embedding this in a larger colored template
    (e.g. a `MUTED_COLOR`-wrapped join notice) should splice it in as
    its own segment rather than wrapping the whole line in one outer
    `colored` call — nesting a second color inside an already-open one
    resets to the terminal default, not back to the outer color, once
    this function's own trailing reset fires.
    """
    nick = get_nick(db, user)
    if not nick:
        return sanitize_text(user.username)
    return colored(sanitize_text(nick), fg_color=NICK_COLOR) + colored(
        f"{NICK_SEPARATOR}{sanitize_text(user.username)}", fg_color=MUTED_COLOR
    )
