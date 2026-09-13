"""
Per-user chat timestamp preference (design doc, point 3):
whether chat lines are prefixed with a
display timestamp, defaulting to **on** (dogfood feedback). Knowing when
something was said is most of what makes scrollback readable, and a
caller who joins a quiet channel cannot otherwise tell whether the last
line is a minute or a week old. Off remains one keystroke away on the
chat screen; the default is simply the other way round now. A thin typed wrapper over
`netbbs.user_preferences`' generic per-user key-value store — the same
pattern `netbbs.timeutil` already uses for the node-wide display
format/timezone settings.

`format_with_preference` is the single place that combines the
preference check, `netbbs.timeutil.format_for_display` (so this reuses
the existing per-user/node display-timezone and display-format system
rather than inventing chat-specific formatting rules), and the
timestamp styling — reused identically by both
`netbbs.net.chat_flow` (live chat, scrollback replay) and
`netbbs.net.login_flow` (mailbox-flushed private messages), so the
combination logic lives in exactly one place rather than being
duplicated across both callers. Kept in `netbbs.chat`, not
`netbbs.timeutil`, since it does ANSI coloring — `netbbs.chat.nick`'s
`chat_stream_label`/`display_label` already set the precedent of a
small chat-specific helper combining a data lookup with its own
sanitizing/coloring.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.rendering import METADATA_COLOR, colored
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "chat_timestamps"


def timestamps_enabled(db: Database, user: User) -> bool:
    # The default is the *unset* answer, so flipping it turns timestamps
    # on for every account that never expressed a preference -- which is
    # the intent. An account that switched them off explicitly has "off"
    # stored and keeps it.
    return get_user_preference(db, user, _PREFERENCE_KEY, default="on") == "on"


def set_timestamps_enabled(db: Database, user: User, enabled: bool) -> None:
    set_user_preference(db, user, _PREFERENCE_KEY, "on" if enabled else "off")


def format_with_preference(db: Database, user: User, text: str, created_at: str) -> str:
    """Prefix `text` with a display timestamp if `user` has chat
    timestamps enabled, otherwise return `text` unchanged.

    METADATA_COLOR, not MUTED_COLOR: a timestamp is chrome attached to
    the line beside it, not a system message that is content in its own
    right, and the two constants stopped sharing a value when the grey
    ramp was lifted. On by default now, so this shade is in front of
    every chat line rather than only the ones a caller opted into.

    Deliberately time-only (`override_format="%H:%M"`),
    not the node's full configured display format (which includes
    the date) -- the same reasoning applied elsewhere to the
    status line's own clock: chat is an inherently *now* context, so a
    per-message date is static clutter, not information, for the
    overwhelming majority of a session's messages. Unlike the status
    line, this isn't continuously repainted -- old scrollback still
    replays with only a time, which is an accepted trade-off (the
    surrounding chat context, not the exact date, is what tells you
    "this was from a while ago") rather than a reason to special-case
    scrollback replay with a different format.
    """
    if not timestamps_enabled(db, user):
        return text
    try:
        shown = format_for_display(created_at, db, override_format="%H:%M")
    except ValueError:
        # Belt to the ingest boundary's braces (Codex review). A row
        # stored before that boundary existed still has to render: this
        # runs for every message in scrollback on entering a channel, so
        # raising here would make one unparseable carried timestamp shut
        # a caller out of the channel entirely, every time, with nothing
        # on screen saying why. The line is worth more than its stamp,
        # so the line survives without one.
        return text
    return f"{colored(f'[{shown}]', fg_color=METADATA_COLOR)} {text}"
