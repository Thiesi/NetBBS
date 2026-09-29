"""Telling a caller that mail arrived while they are online (issue #823).

A letter can land in a signed-in caller's Inbox from another caller on this
node, over Link, or from the BBS itself (issue #819). Before #823 nothing said
so: the main menu's unread count changed on its next redraw, and that was all.

One watcher task per signed-in session (`watch_for_mail`, started and
cancelled by `netbbs.net.login_flow.run_authenticated_session` next to the
account watcher) compares the Inbox's unread letters with those it has already
seen, every few seconds. Polling rather than a hook in each delivery path, so
every way a letter arrives is covered -- a Link delivery on the background
lane, a system notice, even one written by `python -m netbbs.admin` in another
process -- and none of them has to know about sessions. A letter sent from
this process wakes the watchers of its recipient at once (`nudge`), the same
early wake the account watcher gets.

What the caller sees follows `/msg` (`netbbs.chat.mailbox`): a screen that
can take a line at any moment gets it now, and every other screen gets it at
its next safe point, never written into whatever it is drawing.

- Chat, and the SysOp's live monitor, install `Session.pinned_notice_hook`;
  the line goes through it and appears at once, above chat's input row.
- Everywhere else it is queued as a notice (`netbbs.net.notices.announce`),
  shown above the prompt of the next screen drawn. A door or an editor draws
  no notices, so the line waits until the caller leaves it; nothing is ever
  written into a door's screen (`Session.door_active`).
- The main menu and the mailbox list race `arrival_event` against their key
  read, so while one of them sits idle it redraws at once: the menu's unread
  count and the Inbox's rows are brought up to date with the notice above
  the prompt.

Only for a caller mail is open to (`caller_mail_refusal`, issue #816): the
guest and callers below the mail level are told nothing. A letter from a
blocked sender (issue #817) never arrives, so it is never announced.
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass, field

from netbbs.auth.users import User, current_account
from netbbs.mail import MailKey, get_inbox_letters, inbox_mail_keys
from netbbs.net.notices import announce
from netbbs.net.session import Session, SessionClosedError
from netbbs.rendering import WARNING_COLOR, colored, sanitize_text
from netbbs.storage.database import Database

#: How often a watcher looks at the Inbox when nothing woke it. Mail is not
#: chat: a few seconds is live enough, and the query reads only unread rows.
POLL_SECONDS = 5.0

#: At most this many arrivals are named one to a line; more at once are
#: counted in one line instead.
MAX_NAMED = 3

#: What the new-mail notice is drawn in: the colour of the main menu's
#: "N unread messages".
NOTICE_COLOR = WARNING_COLOR


@dataclass(eq=False)
class _Watch:
    username: str
    # Set by `nudge` to look now rather than at the next poll.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when a notice was queued, for an idle main menu or mailbox.
    arrived: asyncio.Event = field(default_factory=asyncio.Event)


_watches: "weakref.WeakKeyDictionary[Session, _Watch]" = weakref.WeakKeyDictionary()


def arrival_event(session: Session) -> asyncio.Event | None:
    """Set when mail was announced to `session` and is waiting to be shown;
    `None` when no watcher runs for it (a test calling a screen directly).
    The screen that shows it clears it."""
    watch = _watches.get(session)
    return watch.arrived if watch is not None else None


def nudge(username: str) -> None:
    """Wake the watcher of every session signed in as `username`: a letter
    sent from this process is announced now, not at the next poll."""
    for watch in list(_watches.values()):
        if watch.username == username:
            watch.wake.set()


def new_mail_notice_lines(labels_and_subjects: list[tuple[str, str]]) -> list[str]:
    """The plain lines that announce these arrivals: one per letter, or a
    count when there are more than `MAX_NAMED`."""
    if len(labels_and_subjects) > MAX_NAMED:
        return [f"{len(labels_and_subjects)} new messages in your mailbox."]
    return [
        f"New mail from {sanitize_text(label)}: {sanitize_text(subject)}"
        for label, subject in labels_and_subjects
    ]


def login_mail_notice(unread: int) -> str | None:
    """The line the first main menu after login shows about waiting mail
    (issue #823), or `None` when nothing is unread."""
    if not unread:
        return None
    return (
        f"You have {unread} unread message{'' if unread == 1 else 's'} -- "
        f"[E]-mail to read {'it' if unread == 1 else 'them'}."
    )


def _arrivals(session: Session, db: Database, user: User, known: set[MailKey]) -> list[tuple[str, str]]:
    """`(sender, subject)` of each unread letter not seen before, oldest
    first; every one is then counted as seen. Nothing for a caller mail is
    closed to."""
    # Imported here: mail_flow imports this module to nudge after a send.
    from netbbs.net.mail_flow import caller_mail_refusal, sender_display_label

    fresh = current_account(db, user) or user
    unread = inbox_mail_keys(db, fresh, unread_only=True)
    new = unread - known
    if not new:
        return []
    known |= new
    if caller_mail_refusal(session, db, fresh) is not None:
        return []
    letters = get_inbox_letters(db, fresh, sorted(mail_id for mail_id, _created in new))
    return [(sender_display_label(db, letter), letter.subject) for letter in letters]


async def _tell(session: Session, watch: _Watch, lines: list[str]) -> None:
    hook = getattr(session, "pinned_notice_hook", None)
    if hook is not None and not getattr(session, "door_active", False):
        for line in lines:
            await hook(colored(line, fg_color=NOTICE_COLOR))
        return
    for line in lines:
        announce(session, line, color=NOTICE_COLOR)
    watch.arrived.set()


async def watch_for_mail(session: Session, db: Database, user: User, *, poll_seconds: float = POLL_SECONDS) -> None:
    """Announce each letter that reaches `user`'s Inbox while `session`
    lasts. Runs until cancelled; what was already there when it started is
    the login notice's business, not this one's."""
    watch = _Watch(user.username)
    _watches[session] = watch
    try:
        known = inbox_mail_keys(db, user)
        while True:
            try:
                await asyncio.wait_for(watch.wake.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass
            watch.wake.clear()
            arrivals = _arrivals(session, db, user, known)
            if arrivals:
                try:
                    await _tell(session, watch, new_mail_notice_lines(arrivals))
                except SessionClosedError:
                    return
    finally:
        if _watches.get(session) is watch:
            del _watches[session]
