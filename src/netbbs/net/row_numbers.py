"""
Two-digit row numbers on lists (issue #1158, design doc §3.5).

A row is chosen by its number, always typed as two digits: `05`, never a
`5` that might yet become `57`, so no timeout guesses whether a second
digit is coming. A digit and Enter picks that row too (`5` Enter), the
picker's own rule since issue #840. `netbbs.net.picker` reads its numbers
the same way; this is that rule for the lists that draw their own screen
(a board's posts, the mailbox, a file area).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from netbbs.digits import is_ascii_number
from netbbs.net.char_input import CANCEL_KEY, HELP_KEY, REDRAW_KEY, REFRESH_KEY, EditorKey, EditorKeyKind
from netbbs.net.session import Session
from netbbs.rendering.ansi import reject_keystroke

# What `read_key` returns without drawing it (see `reject_unhandled_key`):
# a reader that "echoed" one of these drew nothing to erase.
_UNECHOED_KEYS = (REDRAW_KEY, REFRESH_KEY, HELP_KEY, CANCEL_KEY)


ROW_NUMBER_WIDTH = 2
"""The columns a row number takes: `01`-`99`."""


def row_number_label(number: int) -> str:
    """How a row's number is drawn: `01`-`99`."""
    return f"{number:0{ROW_NUMBER_WIDTH}d}"


def row_range_label(count: int) -> str:
    """What an action bar offers for `count` rows: `01`, or `01-12`."""
    return row_number_label(1) if count <= 1 else f"{row_number_label(1)}-{row_number_label(min(count, 99))}"


async def read_row_number(
    session: Session,
    first: str,
    *,
    row_count: int,
    first_echoed: bool,
    read: Callable[[], Awaitable[tuple[EditorKey, bool]]],
) -> int | None:
    """The row number that starts with the digit `first`, 1-based, or
    `None` once the attempt was refused with a bell.

    Reads one more key with `read` (which says whether it echoed it):
    a second digit completes the number, Enter takes `first` alone.
    Every digit is shown on the prompt line, and a refusal erases
    exactly what was shown, so the prompt is as it was.

    A plain `read_key` reader never returns Enter (it skips CR and LF),
    so through one a number is two digits only; every transport has
    `read_editor_key`, which does."""
    shown = 1
    if not first_echoed:
        await session.write(first)
    key, echoed = await read()
    if key.kind == EditorKeyKind.ENTER or (key.kind == EditorKeyKind.CHAR and key.char in ("\r", "\n")):
        number = int(first)
    else:
        second = key.char if key.kind == EditorKeyKind.CHAR and key.char else None
        if second is not None:
            if not echoed:
                await session.write(second)
                shown = 2
            elif second not in _UNECHOED_KEYS:
                shown = 2
        if second is None or not is_ascii_number(second):
            await session.write(reject_keystroke(shown))
            return None
        number = int(first + second)
    if 1 <= number <= row_count:
        return number
    await session.write(reject_keystroke(shown))
    return None
