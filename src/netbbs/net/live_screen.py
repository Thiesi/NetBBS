"""
A full-screen view that redraws itself on a timer while waiting for a key
(issue #763): the SysOp monitor's engine, and the one place any later
self-refreshing screen should start from.

Every other screen in NetBBS redraws only in answer to a key. Here a
`paint` callback fills a fresh `ScreenBuffer` on every tick and on every
key, and only the cells that changed are sent (`diff_ansi`), so an idle
screen costs a few bytes a tick rather than a repaint.

Two rules keep it safe to leave running:

- **One key read survives every tick.** The pending read is never
  cancelled to make room for a redraw, so a keystroke that arrives
  mid-escape-sequence cannot be torn in half, and no typed key is lost
  between ticks.
- **`paint` is in-memory only.** It runs every tick, so it must not wait
  on the `DatabaseLane`. The worklog's live-dashboard rule forbids adding
  an await on a slow resource to a path a drain or shutdown may be
  unwinding through.
"""

from __future__ import annotations

import asyncio
import enum
from typing import Awaitable, Callable

from netbbs.net.char_input import REDRAW_KEY, EditorKey, EditorKeyKind
from netbbs.net.session import Session, SessionClosedError
from netbbs.rendering.ansi import clear_screen
from netbbs.rendering.screen_buffer import ScreenBuffer, Snapshot, diff_ansi, full_render_ansi
from netbbs.rendering.width import char_width

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"


class KeyOutcome(enum.Enum):
    """What `on_key` tells the loop to do next."""

    #: Repaint as usual: only what changed is sent.
    CONTINUE = enum.auto()
    #: The key's handler wrote to the terminal itself (a prompt), so the
    #: screen no longer shows the last frame: repaint all of it.
    REPAINT = enum.auto()
    #: Leave the screen.
    EXIT = enum.auto()


def paint_text(
    buffer: ScreenBuffer,
    row: int,
    col: int,
    text: str,
    *,
    width: int | None = None,
    fg: int | tuple[int, int, int] | None = None,
    bg: int | tuple[int, int, int] | None = None,
    bold: bool = False,
) -> int:
    """Paint `text` at (`row`, `col`), cut to `width` columns (or the edge
    of the buffer), and return the column after the last cell painted.

    Out-of-range rows paint nothing, so a caller laying out a screen for
    a terminal that turned out too short needs no bounds checks of its
    own. Double-width glyphs take two cells, and one that would straddle
    the limit is dropped rather than half-drawn."""
    if not 0 <= row < buffer.height:
        return col
    limit = buffer.width if width is None else min(buffer.width, col + max(0, width))
    for ch in text:
        if not ch.isprintable():
            ch = " "
        cells = char_width(ch)
        if cells <= 0:
            continue
        if col + cells > limit:
            break
        if cells == 2:
            buffer.write_wide_cell(row, col, ch, fg=fg, bg=bg, bold=bold)
        else:
            buffer.write_cell(row, col, ch, fg=fg, bg=bg, bold=bold)
        col += cells
    return col


def fill_row(buffer: ScreenBuffer, row: int, *, bg: int | tuple[int, int, int] | None) -> None:
    """Give a whole row a background, before its text is painted."""
    if 0 <= row < buffer.height:
        for col in range(buffer.width):
            buffer.write_cell(row, col, " ", bg=bg)


async def _read_key(session: Session) -> EditorKey:
    """A structured key, so arrows arrive as keys, with the plain `read_key`
    fallback lightweight sessions need (`board_flow._read_list_key`)."""
    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            return await read_editor_key(distinguish_ctrl_h=True)
        except NotImplementedError:
            pass
    return EditorKey(EditorKeyKind.CHAR, char=await session.read_key(echo=False))


def _is_redraw(key: EditorKey) -> bool:
    return (key.kind is EditorKeyKind.CTRL and key.char == "l") or (
        key.kind is EditorKeyKind.CHAR and key.char == REDRAW_KEY
    )


async def run_live_screen(
    session: Session,
    *,
    paint: Callable[[ScreenBuffer], None],
    on_key: Callable[[EditorKey], Awaitable[KeyOutcome]],
    interval: float,
) -> None:
    """Show `paint`'s frame, repainted every `interval` seconds and after
    every key, until `on_key` returns `KeyOutcome.EXIT`.

    Ctrl-L repaints everything, as on every other screen. A change of
    terminal size does too, since every cell may have moved. The screen
    is cleared on the way out, so whatever the caller draws next starts
    clean."""
    previous: Snapshot | None = None
    size: tuple[int, int] | None = None
    key_task: asyncio.Task | None = None
    await session.write(HIDE_CURSOR)
    try:
        while True:
            current_size = (session.terminal_width, session.terminal_height)
            buffer = ScreenBuffer(*current_size)
            paint(buffer)
            snapshot = buffer.snapshot()
            if previous is None or current_size != size:
                frame = full_render_ansi(snapshot)
            else:
                frame = diff_ansi(previous, snapshot)
            if frame:
                await session.write(frame)
            previous, size = snapshot, current_size

            if key_task is None:
                key_task = asyncio.create_task(_read_key(session))
            done, _pending = await asyncio.wait({key_task}, timeout=interval)
            if key_task not in done:
                continue
            key, key_task = key_task.result(), None
            if _is_redraw(key):
                previous = None
                continue
            outcome = await on_key(key)
            if outcome is KeyOutcome.EXIT:
                return
            if outcome is KeyOutcome.REPAINT:
                previous = None
    finally:
        if key_task is not None:
            key_task.cancel()
            await asyncio.gather(key_task, return_exceptions=True)
        try:
            await session.write(SHOW_CURSOR + clear_screen())
        except (SessionClosedError, OSError):
            # The connection is already gone: nothing to restore.
            pass
