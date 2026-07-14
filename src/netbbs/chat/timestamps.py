"""
Per-user chat timestamp preference (design doc round 32 point 3, round
42 point 6, sign-off round 62): whether chat lines are prefixed with a
display timestamp, defaulting to off. A thin typed wrapper over
`netbbs.user_preferences`' generic per-user key-value store — the same
pattern `netbbs.timeutil` already uses for the node-wide display
format/timezone settings.

`format_with_preference` is the single place that combines the
preference check, `netbbs.timeutil.format_for_display` (so this reuses
the existing per-user/node display-timezone and display-format system
rather than inventing chat-specific formatting rules, per round 32
point 3), and the muted-color styling — reused identically by both
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
from netbbs.rendering import MUTED_COLOR, colored
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display
from netbbs.user_preferences import get_user_preference, set_user_preference

_PREFERENCE_KEY = "chat_timestamps"


def timestamps_enabled(db: Database, user: User) -> bool:
    return get_user_preference(db, user, _PREFERENCE_KEY, default="off") == "on"


def set_timestamps_enabled(db: Database, user: User, enabled: bool) -> None:
    set_user_preference(db, user, _PREFERENCE_KEY, "on" if enabled else "off")


def format_with_preference(db: Database, user: User, text: str, created_at: str) -> str:
    """Prefix `text` with a muted-color display timestamp if `user` has
    chat timestamps enabled, otherwise return `text` unchanged."""
    if not timestamps_enabled(db, user):
        return text
    stamp = colored(f"[{format_for_display(created_at, db)}]", fg_color=MUTED_COLOR)
    return f"{stamp} {text}"
