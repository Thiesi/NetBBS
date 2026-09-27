"""
What each connected caller is doing right now, for the SysOp's live
session monitor (issue #762, tracker #761).

A session's activity is a short trail of place names -- ``("Boards",
"Retro")``, ``("Doors", "Voidrunner")`` -- kept on the `Session` itself
as `Session.activity`. Screens never set it by hand. The entry point of
each area pushes its own segment for exactly as long as it runs, through
`records_activity` (a decorator on the function every caller of that
area goes through) or the `activity` context manager. Both restore the
previous trail on the way out, however the screen ends -- a return, a
disconnect, a level unwind -- so a trail can never outlive the screen
that set it. The main menu, the root of every trail, uses
`set_root_activity` instead.

Segments name places, never content: a board, a channel or a door by
name, "Mail", "Writing". Never a message subject, a mail or direct-chat
partner, a file name or a search string. The SysOp console is not a
reason to show one caller's private business to another screen.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

#: A segment longer than this is cut. Place names are SysOp-chosen and
#: already bounded, but the monitor must not trust that.
MAX_SEGMENT_LENGTH = 40


def _clean(label: object) -> str:
    # Control characters would reach the SysOp's terminal verbatim.
    text = "".join(ch if ch.isprintable() else " " for ch in str(label)).strip()
    return text[:MAX_SEGMENT_LENGTH]


@contextmanager
def activity(session: Any, label: str | None) -> Iterator[None]:
    """Append `label` to `session`'s activity trail while the block runs.

    `None` or an empty label leaves the trail unchanged, so a dispatcher
    can pass a lookup result straight through. The previous trail is put
    back in a `finally`, not popped, so an inner screen that returned
    abnormally cannot leave a stale segment behind for an outer one."""
    previous = getattr(session, "activity", ())
    cleaned = _clean(label) if label else ""
    if cleaned:
        session.activity = (*previous, cleaned)
    try:
        yield
    finally:
        session.activity = previous


#: The (entry function, segment) pairs already on this task's trail. A
#: screen that redraws itself by calling itself again (`file_flow.
#: _show_area` after offering an upload link) is still one place and must
#: not grow the trail on every redraw -- while a file area that happens to
#: be named "Files" is a real second level and must show as one. Keyed by
#: function as well as label so only the first case is collapsed. Each
#: connection runs in its own task, so the set is per caller.
_ENTERED: contextvars.ContextVar[frozenset] = contextvars.ContextVar("netbbs_activity_entered", default=frozenset())


def records_activity(label: str | Callable[[Mapping[str, Any]], str | None]):
    """Decorate an async screen entry point taking `session` so it runs
    inside `activity(session, ...)`.

    `label` is either a fixed segment or a function of the call's bound
    arguments by name, for a segment naming the place being entered,
    e.g. ``records_activity(lambda args: args["board"].name)``. Putting
    this on the entry function rather than at its call sites covers
    every caller, including ones added later."""

    def decorate(function):
        signature = inspect.signature(function)

        @functools.wraps(function)
        async def wrapper(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            segment = label if isinstance(label, str) else label(bound.arguments)
            key = (wrapper, _clean(segment) if segment else "")
            entered = _ENTERED.get()
            if key in entered:
                return await function(*args, **kwargs)
            token = _ENTERED.set(entered | {key})
            try:
                with activity(bound.arguments["session"], segment):
                    return await function(*args, **kwargs)
            finally:
                _ENTERED.reset(token)

        return wrapper

    return decorate


def set_root_activity(session: Any, label: str | None) -> None:
    """Replace the whole trail with `label` alone, or empty it.

    For the main menu only: it is the root every trail starts from, so it
    empties the trail each time it reads a key and names the branch it is
    about to dispatch to. Its dispatch is one long chain of branches, and
    this spares wrapping all of them in `activity`."""
    cleaned = _clean(label) if label else ""
    session.activity = (cleaned,) if cleaned else ()


def describe(trail: tuple[str, ...], *, authenticated: bool, separator: str = " › ") -> str:
    """The one-line form the monitor shows. `separator` is " > " for a
    SysOp who turned Unicode styling off."""
    if trail:
        return separator.join(trail)
    return "Main menu" if authenticated else "Logging in"
