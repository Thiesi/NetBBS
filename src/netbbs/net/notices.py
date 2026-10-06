"""Outcomes carried into the next redraw (design doc §3.5, issue #680).

With redraw-in-place on (the default for a new account), a line written just
before a screen redraws is never seen: the redraw clears the terminal in the
same burst of output. So an action does not *write* its outcome; it
`announce`s it, and whichever screen is drawn next shows it directly above
its prompt. No keypress is asked for: the outcome is simply on the screen the
caller lands on, where the eye already is.

Started in the SysOp console (`netbbs.net.admin_flow`) and shared from here,
so a caller's board, file area, review screen and picker follow the same
rule. A screen that draws a prompt calls `write_notices` just before it; a
picker reads pending outcomes at each render and draws them above its own
prompt by itself -- including one its own keys announce while it is open
([N]ew scan's [M]ark read, issue #710).

Keyed weakly by session, so a notice can never outlive the connection it was
meant for or reach another caller's screen. A stand-in session that holds a
flow's output for its real session (the console's `_TrailingOutput`) names
that session as `notice_session`, and notices go to it.
"""

from __future__ import annotations

import re
import weakref

from netbbs.net.session import Session
from netbbs.rendering import ERROR_COLOR, MUTED_COLOR, SUCCESS_COLOR, colored, highlight_result, sanitize_text
from netbbs.rendering.reflow import wrap_terminal_text

_pending: "weakref.WeakKeyDictionary[Session, list[str]]" = weakref.WeakKeyDictionary()

# A leading blank row spaced a line off the keypress echo above it; carried
# into the next screen it would only push the outcome away from the prompt.
_LEADING_BREAK = re.compile(r"^((?:\x1b\[[0-9;]*m)*)(?:\r\n)+")

_TONE_COLORS = {"success": SUCCESS_COLOR, "error": ERROR_COLOR, "muted": MUTED_COLOR}


def _owner(session: Session) -> Session:
    return getattr(session, "notice_session", session)


def announce(session: Session, text: str, *, tone: str = "success", color: int | None = None) -> None:
    """Queue one plain outcome line for the next screen drawn on `session`.
    `tone` is `success`, `error` or `muted` (an outcome that changed
    nothing); `color` overrides it. A key it mentions ("Use [P]review")
    is highlighted like a menu key (issue #1083), and a path it names
    reads as a value (issue #1103)."""
    line = highlight_result(sanitize_text(text), color=color if color is not None else _TONE_COLORS[tone])
    _pending.setdefault(_owner(session), []).append(line)


def announce_styled(session: Session, line: str, *, first: bool = False) -> None:
    """Queue a line that is already sanitized and styled; with `first`,
    ahead of whatever is already queued (login's Welcome line, issue #949,
    which mail arriving during the login questions must not precede)."""
    queue = _pending.setdefault(_owner(session), [])
    line = _LEADING_BREAK.sub(r"\1", line)
    if first:
        queue.insert(0, line)
    else:
        queue.append(line)


def has_notices(session: Session) -> bool:
    return bool(_pending.get(_owner(session)))


def take_notices(session: Session) -> list[str]:
    return _pending.pop(_owner(session), [])


def pending_notices(session: Session) -> list[str]:
    """What is queued, without taking it -- what the next screen will show."""
    return list(_pending.get(_owner(session), ()))


def pending_notice_rows(session: Session) -> int:
    """How many terminal rows the pending notices will take, for a screen
    that budgets its height."""
    width = max(1, session.terminal_width)
    return sum(wrap_terminal_text(line, width).count("\r\n") + 1 for line in _pending.get(_owner(session), ()))


async def write_notices(session: Session) -> None:
    """Write every pending outcome, for a screen about to draw its prompt."""
    for line in take_notices(session):
        await session.write_line(line)
