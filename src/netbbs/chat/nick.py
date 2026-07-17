"""
Transparent chat display aliases (design doc round 32, points 7-10;
sign-off rounds 41/53): `/nick` sets a persistent, node-wide
presentation alias — not identity.

Two distinct rendering forms exist, deliberately, since round 53:
`display_label` (`nick|username`, both forms together) is used
everywhere a *directory*-style listing needs to unambiguously show who
someone really is alongside how they've chosen to present themselves
(`/who`, `/whois`, `/names`) — moderation, permissions, blocking,
reputation, and auditing always operate on canonical identity and never
look at this module at all, regardless of which rendering form is used
anywhere else. `chat_stream_label` (the alias alone, visually marked)
is used in the live conversational stream itself (regular messages,
`/me`, join/leave, scrollback replay) — Thiesi judged showing both
forms on every single line of live chat added clutter without adding
safety, since the directory commands already provide canonical identity
on demand.

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
from netbbs.rendering import NICK_COLOR, colored, sanitize_text
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

_NICK_KEY = "nick"

MAX_NICK_LENGTH = 32

# Wraps a nick in the live chat stream (`chat_stream_label`) to mark it
# as a stand-in name, distinct from the account's canonical username —
# design doc round 53. Plain ASCII, not a Unicode glyph, for the same
# reason italics was ruled out as a *styling* option there: guaranteed
# to render identically on a CP437-only classic BBS client, not
# font/encoding-dependent. `~` doesn't collide with anything else this
# codebase already uses as a chat-rendering convention (unlike `*`,
# already used for `/me` actions and moderation notices).
NICK_MARKER = "~"


class NickError(Exception):
    """Raised when a requested alias fails validation (length,
    colliding with another account's canonical username, or containing
    the reserved marker character)."""


def set_nick(db: Database, user: User, nick: str) -> None:
    """
    Set `user`'s display alias. An empty string clears it (see
    `get_nick`) — a bare `/nick` in the chat command maps to this.

    Validates length, that `nick` doesn't exactly match another
    account's canonical username, case-insensitively (design doc round
    32, point 8: "preserves freedom of presentation without allowing
    an alias to impersonate an authenticated local identity"), and
    (round 53) that it doesn't contain `NICK_MARKER` — reserved so
    `chat_stream_label`'s marker can never be confused with something a
    user actually typed into their own alias. Setting your own username
    as your own nick is harmless and allowed — only *other* accounts'
    usernames are rejected. Character content is otherwise deliberately
    not validated here — sanitized on output, same as every other piece
    of user-generated text in this codebase (bios, post bodies, chat
    messages).
    """
    if not nick:
        set_user_preference(db, user, _NICK_KEY, "")
        return

    if len(nick) > MAX_NICK_LENGTH:
        raise NickError(f"alias cannot exceed {MAX_NICK_LENGTH} characters")

    if NICK_MARKER in nick:
        raise NickError(f"alias cannot contain {NICK_MARKER!r} — reserved for display")

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
    used by directory-style listings (`/who`, `/whois`, `/names`) that
    need canonical identity unambiguously visible alongside however the
    account has chosen to present itself. The live conversational
    stream itself uses `chat_stream_label` instead (design doc round
    53) — see that function's docstring for why the two diverge.
    Deliberately not used for moderation transparency notices (mute/
    ban/kick/etc.) or command targeting either way — those always show/
    resolve canonical identity only (design doc round 32, point 7/9),
    by calling `user.username` directly instead of either of these two
    functions.
    """
    nick = get_nick(db, user)
    return f"{nick}|{user.username}" if nick else user.username


def chat_stream_label(db: Database, user: User) -> str:
    """
    The alias alone, visually marked (`~nick~`, colored via
    `NICK_COLOR`) if `user` has one set, else plain `username` —
    design doc round 53. Used in the live chat stream itself (regular
    messages, `/me`, join/leave, scrollback replay): showing both forms
    on *every single line* of live conversation was judged cluttered in
    practice, not just in theory, once actually tried — the canonical
    username is still one `/whois` away, and `display_label` still
    shows both forms in every directory-style listing.

    `NICK_MARKER` is rejected by `set_nick` itself, so this can never
    be confused with something a user actually typed into their own
    alias.

    Sanitizes (design doc round 29) the underlying nick/username
    *before* applying `NICK_COLOR`, never after — this function owns
    both concerns itself rather than leaving sanitization to the
    caller the way `display_label` does, specifically so a caller never
    ends up running `sanitize_text` on this function's own *output*,
    which would risk stripping the legitimate SGR codes just applied
    here along with (or instead of) any actually-hostile content.
    Callers embedding this in a larger colored template (e.g. a
    `MUTED_COLOR`-wrapped join notice) should splice it in as its own
    segment rather than wrapping the whole line in one outer `colored`
    call — nesting a second color inside an already-open one resets to
    the terminal default, not back to the outer color, once this
    function's own trailing reset fires; known, accepted for now (the
    remainder of that one line reverts to the terminal's default color
    rather than staying muted) rather than engineered around, since
    this whole feature is still provisional pending Thiesi actually
    seeing it live.
    """
    nick = get_nick(db, user)
    if not nick:
        return sanitize_text(user.username)
    marked = f"{NICK_MARKER}{sanitize_text(nick)}{NICK_MARKER}"
    return colored(marked, fg_color=NICK_COLOR)
