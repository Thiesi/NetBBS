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
import re
from typing import Awaitable, Callable

from netbbs.net.char_input import REDRAW_KEY, EditorKey, EditorKeyKind
from netbbs.net.session import Session, SessionClosedError
from netbbs.rendering.ansi import clear_screen
from netbbs.rendering.theme import MENU_KEY_COLOR
from netbbs.rendering.screen_buffer import ScreenBuffer, Snapshot, diff_ansi, full_render_ansi
from netbbs.rendering.width import char_width

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"


async def write_quietly(session: Session, text: str) -> None:
    """Best-effort terminal housekeeping (cursor visibility) on a path that
    may already be unwinding from a closed connection: a failure here must
    not replace the error that is already propagating."""
    try:
        await session.write(text)
    except (SessionClosedError, OSError):
        pass


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
    the limit is dropped rather than half-drawn. A zero-width combining
    mark joins the cell before it, so decomposed text keeps its accents."""
    if not 0 <= row < buffer.height:
        return col
    col = max(0, col)
    limit = buffer.width if width is None else min(buffer.width, col + max(0, width))
    last: int | None = None
    for ch in text:
        if not ch.isprintable():
            ch = " "
        cells = char_width(ch)
        if cells <= 0:
            if last is not None:
                cell = buffer.get_cell(row, last)
                buffer.write_cell(row, last, cell.char + ch, fg=cell.fg, bg=cell.bg, bold=cell.bold)
            continue
        if col + cells > limit:
            break
        last = col
        if cells == 2:
            buffer.write_wide_cell(row, col, ch, fg=fg, bg=bg, bold=bold)
        else:
            buffer.write_cell(row, col, ch, fg=fg, bg=bg, bold=bold)
        col += cells
    return col


_PAINTED_KEY = re.compile(r"\[([A-Za-z0-9]|Enter)\]")


def paint_keyed_text(
    buffer: ScreenBuffer,
    row: int,
    col: int,
    text: str,
    *,
    fg: int | tuple[int, int, int] | None = None,
    bg: int | tuple[int, int, int] | None = None,
    bold: bool = False,
) -> int:
    """`paint_text` with each bracketed key (`[S]`, `[Enter]`) in the menu
    key colour, as `highlight_hotkeys` draws it in running text (issue
    #1083): a painted row has one colour per cell, not nested escapes."""
    position = 0
    for match in _PAINTED_KEY.finditer(text):
        col = paint_text(buffer, row, col, text[position:match.start() + 1], fg=fg, bg=bg, bold=bold)
        col = paint_text(buffer, row, col, match.group(1), fg=MENU_KEY_COLOR, bg=bg, bold=True)
        position = match.end() - 1
    return paint_text(buffer, row, col, text[position:], fg=fg, bg=bg, bold=bold)


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
    on_notice: Callable[[str], None],
    interval: float,
) -> None:
    """Show `paint`'s frame, repainted every `interval` seconds and after
    every key, until `on_key` returns `KeyOutcome.EXIT`.

    Ctrl-L repaints everything, as on every other screen. A change of
    terminal size does too, since every cell may have moved. The screen
    is cleared on the way out, so whatever the caller draws next starts
    clean.

    An out-of-band notice (a SysOp's message, a shutdown broadcast) would
    otherwise be written wherever the last frame left the cursor, and a
    diff against a frame the terminal no longer shows would never repair
    it. While this screen runs, such notices go to `on_notice` instead,
    through `Session.pinned_notice_hook`, and the hook repaints before it
    returns: a notice counts as delivered only once it is on screen, which
    matters when a disconnect follows it at once (Kick with a message).
    One lock serialises that repaint with the loop's own. While `on_key`
    runs, the previous hook is back in place: the handler may be showing a
    prompt or a whole other screen."""
    previous: Snapshot | None = None
    size: tuple[int, int] | None = None
    key_task: asyncio.Task | None = None
    drawing = asyncio.Lock()

    async def render(notice: str | None = None) -> None:
        nonlocal previous, size
        async with drawing:
            if notice is not None:
                # Inside the lock, so no key handled between the notice's
                # arrival and this frame can clear it unseen.
                on_notice(notice)
            current_size = (session.terminal_width, session.terminal_height)
            buffer = ScreenBuffer(*current_size)
            paint(buffer)
            snapshot = buffer.snapshot()
            if previous is None or current_size != size:
                # A nested screen (snoop, a draft) shows the cursor on its way
                # out; every full repaint hides it again.
                frame = HIDE_CURSOR + full_render_ansi(snapshot)
            else:
                frame = diff_ansi(previous, snapshot)
            if frame:
                await session.write(frame)
            previous, size = snapshot, current_size

    async def take_notice(text: str) -> None:
        await render(text)

    outer_hook = session.pinned_notice_hook
    session.pinned_notice_hook = take_notice
    try:
        await session.write(HIDE_CURSOR)
        while True:
            await render()
            if key_task is None:
                key_task = asyncio.create_task(_read_key(session))
            done, _pending = await asyncio.wait({key_task}, timeout=interval)
            if key_task not in done:
                continue
            key, key_task = key_task.result(), None
            if _is_redraw(key):
                previous = None
                continue
            # A key handler may hand the terminal to a prompt or another
            # screen. While it does, notices take the ordinary route, so
            # that screen shows them as it would anywhere else, instead of
            # this screen swallowing them into state the handler is about
            # to overwrite with its own outcome.
            session.pinned_notice_hook = outer_hook
            try:
                outcome = await on_key(key)
            finally:
                session.pinned_notice_hook = take_notice
            if outcome is KeyOutcome.EXIT:
                return
            if outcome is KeyOutcome.REPAINT:
                previous = None
    finally:
        session.pinned_notice_hook = outer_hook
        if key_task is not None:
            key_task.cancel()
            await asyncio.gather(key_task, return_exceptions=True)
        await write_quietly(session, SHOW_CURSOR + clear_screen())
