"""
The one-time "keys moved" screen (issue #1158, design doc §16 Decision 6).

Paging used to be on letters, and on different letters depending on the
screen (`[N]ext`/`[P]rev`, `[O]lder`/`[N]ewer`). Once it moved to `<` `>`
on every screen, a caller who had learned the old keys would press `N` on
a list and hear a bell. So the migration that shipped with the change
marked every account that existed then as `pending`, and each one sees
this screen once, at its next login. An account made afterwards never
learned the old keys and has no mark. Guests share one account and are
not shown it.
"""

from __future__ import annotations

from netbbs.auth.users import User
from netbbs.net.help_overlay import show_help
from netbbs.net.session import Session
from netbbs.net.shared_account import signed_in_without_credential
from netbbs.rendering import colored
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference

#: Set to "pending" for every account by the migration that shipped with
#: the change, and to "seen" once shown.
PREFERENCE_KEY = "keys_notice_1158"

_LINES = [
    "Some keys work the same on every screen now:",
    "",
    "  < >        previous / next page (also PgUp/PgDn, and Left/Right",
    "             on lists)",
    "  /          find",
    "  ?          help (also F1 and Ctrl-H)",
    "  B or Esc   back (Esc first clears a highlighted row)",
    "",
    "Paging is no longer on letters such as N, P, O. On a message",
    "board, [N]ext post and [P]revious post in the reader are unchanged.",
    "When you write a post or a letter, the review screen's Body is",
    "now [E]dit body, and [B]ack asks before it discards your draft.",
]


async def show_keys_notice_once(
    session: Session, db: Database, user: User, *,
    header_color: int | tuple[int, int, int], unicode_style: bool,
) -> None:
    """Show the screen once to an account the upgrade marked `pending`."""
    if signed_in_without_credential(session):
        return
    if get_user_preference(db, user, PREFERENCE_KEY) != "pending":
        return
    set_user_preference(db, user, PREFERENCE_KEY, "seen")
    await show_help(
        session, "Keys that work everywhere",
        [colored(_LINES[0], fg_color=header_color, bold=True), *_LINES[1:]],
        header_color=header_color, unicode_style=unicode_style,
    )
