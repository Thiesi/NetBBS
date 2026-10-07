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
from netbbs.rendering import (
    ALERT_COLOR,
    ERROR_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    WARNING_COLOR,
    highlight_result,
    sanitize_text,
    status_result,
)
from netbbs.rendering.reflow import wrap_terminal_text

_pending: "weakref.WeakKeyDictionary[Session, list[str]]" = weakref.WeakKeyDictionary()

# A leading blank row spaced a line off the keypress echo above it; carried
# into the next screen it would only push the outcome away from the prompt.
_LEADING_BREAK = re.compile(r"^((?:\x1b\[[0-9;]*m)*)(?:\r\n)+")

_TONE_COLORS = {"success": SUCCESS_COLOR, "error": ERROR_COLOR, "muted": MUTED_COLOR}

# A result says how it went with a mark in front and keeps the usual colours
# for the rest (issue #1109). A muted outcome (nothing changed) keeps its
# muted line, and any other colour a caller asks for (new mail's) is left as
# it is: those are not a success, a warning or a failure.
_STATUS_OF_TONE = {"success": "success", "error": "error"}
_STATUS_OF_COLOR = {SUCCESS_COLOR: "success", ERROR_COLOR: "error", WARNING_COLOR: "warning", ALERT_COLOR: "warning"}
# A colour asked for by name: good news (new mail) shares the success green
# but is not the outcome of an action, so only a warning or a failure marks.
_STATUS_OF_ASKED_COLOR = {ERROR_COLOR: "error", WARNING_COLOR: "warning", ALERT_COLOR: "warning"}

# A whole line in one colour, as `colored(text, fg_color=..., bold=...)`
# writes it: bold, when asked for, comes before the colour.
_ONE_COLOR_LINE = re.compile(r"(?:\x1b\[1m)?\x1b\[38;5;(\d+)m([^\x1b]*)\x1b\[0m", re.DOTALL)


def _as_result(line: str) -> str:
    """`line` with a leading mark instead of its colour, when the whole of it
    is one status colour: the many outcomes written as
    `colored("Fetch failed: ...", fg_color=ERROR_COLOR)` read like the rest."""
    match = _ONE_COLOR_LINE.fullmatch(line)
    if match is None:
        return line
    status = _STATUS_OF_COLOR.get(int(match.group(1)))
    if status is None or not match.group(2).strip():
        return line
    return status_result(match.group(2), status)


def _owner(session: Session) -> Session:
    return getattr(session, "notice_session", session)


def announce(session: Session, text: str, *, tone: str = "success", color: int | None = None) -> None:
    """Queue one plain outcome line for the next screen drawn on `session`.
    `tone` is `success`, `error` or `muted` (an outcome that changed
    nothing); `color` overrides it. A key it mentions ("Use [P]review")
    is highlighted like a menu key (issue #1083), a path it names reads as
    a value (issue #1103), and a success, warning or failure starts with its
    mark rather than being drawn all in its colour (issue #1109)."""
    text = sanitize_text(text)
    status = _STATUS_OF_ASKED_COLOR.get(color) if color is not None else _STATUS_OF_TONE.get(tone)
    if status is not None:
        line = status_result(text, status)
    else:
        line = highlight_result(text, color=color if color is not None else _TONE_COLORS[tone])
    _pending.setdefault(_owner(session), []).append(line)


def announce_styled(session: Session, line: str, *, first: bool = False, mark: bool = True) -> None:
    """Queue a line that is already sanitized and styled; with `first`,
    ahead of whatever is already queued (login's Welcome line, issue #949,
    which mail arriving during the login questions must not precede).

    A line wholly in a status colour is the outcome of an action and gets
    its mark (issue #1109). `mark=False` says the line is not an outcome
    but news or a standing state, told in its own colour: waiting mail in
    the good-news green (the success green's own index), a node drain in
    the alert colour. It is queued as it is (#1109 review)."""
    queue = _pending.setdefault(_owner(session), [])
    line = _LEADING_BREAK.sub(r"\1", line)
    if mark:
        line = _as_result(line)
    if first:
        queue.insert(0, line)
    else:
        queue.append(line)


# An outcome that changed nothing reads muted, not as a green success.
NEUTRAL_OUTCOMES = ("Cancelled", "No change", "Already ", "No ", "Nothing ")
# A failure that was never styled must not turn success-green merely by being
# announced. A site that knows it is reporting a failure can say so with
# `announce(..., tone="error")`.
FAILED_OUTCOMES = ("Error", "Could not", "Cannot ", "Can't ", "Failed", "Unable ", "Not a valid")


def announce_line(session: Session, line: str) -> None:
    """Queue a line exactly as an action would have written it -- the SysOp
    console's and the caller's screens alike (issue #1124). One that is
    already styled keeps its colours; a plain one reads as a success, as a
    failure when it says it could not do something, or muted when it says
    nothing was done. A leading blank row is dropped -- it spaced the line
    off a keypress echo that is no longer above it."""
    line = _LEADING_BREAK.sub(r"\1", line)
    if "\x1b[" not in line:
        # A key the outcome mentions stands out as a menu key (issue #1083),
        # a path it names reads as a value (issue #1103), and a success or
        # failure says so with a leading mark (issue #1109).
        if line.startswith(FAILED_OUTCOMES):
            line = status_result(line, "error")
        elif line.startswith(NEUTRAL_OUTCOMES):
            line = highlight_result(line, color=MUTED_COLOR)
        else:
            line = status_result(line, "success")
    announce_styled(session, line)


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
