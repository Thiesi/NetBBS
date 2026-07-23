"""
The fullscreen prose editor (design doc -- prose editor): a
nano-keybound, word-wrapping, scrolling text composer, built on the
same screen-buffer/diff foundation and control-loop shape as
`netbbs.net.ansi_editor` -- but with a genuinely different
editing core underneath. The ANSI editor is a fixed-grid paint tool
(arrows move a cursor over a canvas, typing overwrites a cell, no
insertion, no wrap); this is real insert-mode text editing over
`netbbs.rendering.prose_buffer.ProseBuffer`, soft-wrapped for display
via that module's `wrap_lines`, with none of the ANSI editor's
glyph/color-picker machinery (nothing here has a CP437/SGR concept at
all -- this saves and loads plain text).

Deliberately generic/reusable, same posture as `ansi_editor.edit_ansi_art`:
knows nothing about "board post" or "bio" specifically -- see
`netbbs.net.login_flow` for both concrete callers. The only file this
module writes to directly is the autosave draft, never a caller's real
save target.

Viewport size is the session's *actual negotiated terminal size*
(`session.terminal_width`/`terminal_height`, minus a reserved status
row), not a fixed canvas -- unlike the ANSI editor, where 80x24 is the
*content's* own dimensions, prose has no fixed size of its own, so this
follows the same session-adaptive sizing `netbbs.net.picker` already
uses (design doc) rather than inventing a fixed default.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.session import Session, SessionClosedError
from netbbs.rendering import (
    MUTED_COLOR,
    ScreenBuffer,
    Snapshot,
    clear_line,
    clear_screen,
    colored,
    diff_ansi,
    full_render_ansi,
    move_cursor,
    truncate,
)
from netbbs.rendering.prose_buffer import ProseBuffer, logical_position, visual_position, wrap_lines

_logger = logging.getLogger(__name__)

DEFAULT_AUTOSAVE_INTERVAL_SECONDS = 30.0

# One blank line, then the status line -- same layout convention as
# netbbs.net.ansi_editor's _STATUS_ROW_OFFSET.
_STATUS_ROW_OFFSET = 2

# The viewport is never narrower/shorter than this even against a
# client that negotiated (or defaulted to) something smaller -- a
# genuinely unusable editor is worse than a floor that's occasionally
# larger than the real terminal, the same "must degrade gracefully
# above 40x24" floor design doc §4 already sets for rendering generally.
_MIN_WIDTH = 40
_MIN_HEIGHT = 10


@dataclass
class _EditorState:
    buffer: ProseBuffer
    max_bytes: int
    scroll_row: int = 0  # index into the current wrap_lines() result
    dirty: bool = False


def _byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


async def edit_prose(
    session: Session,
    *,
    initial_text: str | None,
    draft_path: Path,
    max_bytes: int,
    autosave_interval_seconds: float = DEFAULT_AUTOSAVE_INTERVAL_SECONDS,
) -> str | None:
    """
    Run a fullscreen prose editing session against `session`, returning
    the saved text on a real save, or `None` if quit without saving.

    `draft_path` is a periodic autosave target, same recovery contract
    as `netbbs.net.ansi_editor.edit_ansi_art`: a pre-existing draft
    there is offered for recovery on entry instead of `initial_text`;
    saving or explicitly discarding deletes it; a genuine disconnect
    leaves it in place for next time.

    `max_bytes` (GitHub issue #32) is a required content ceiling, not
    given a generic default -- this module stays deliberately unaware
    of "board post" vs. "bio" (see module docstring), and those two
    have genuinely different limits (`netbbs.boards.posts.
    MAX_BODY_BYTES` vs. `netbbs.directory.MAX_BIO_BYTES`), so every
    caller must decide rather than silently inheriting one meant for
    the other. Enforced here, not only at save time in the domain layer
    -- the DoS this closes is the unbounded *editing* work (full-
    document rewrap per keystroke, periodic full autosave writes)
    itself, which already happens before a save is ever attempted.
    Additional input at the ceiling is refused with a bell and a status
    line indicator, rather than silently dropped with no feedback.
    """
    width = max(_MIN_WIDTH, session.terminal_width)
    height = max(_MIN_HEIGHT, session.terminal_height) - _STATUS_ROW_OFFSET - 1

    loaded_text: str | None
    if draft_path.exists() and await _offer_draft_recovery(session):
        loaded_text = draft_path.read_text(encoding="utf-8")
    else:
        if draft_path.exists():
            draft_path.unlink()
        loaded_text = initial_text

    state = _EditorState(buffer=ProseBuffer.from_text(loaded_text or ""), max_bytes=max_bytes)
    autosave_task = asyncio.create_task(_autosave_loop(state, draft_path, autosave_interval_seconds))
    try:
        await session.write(clear_screen())
        previous = _render(state, width, height)
        await session.write(full_render_ansi(previous))
        await _flush(session, state, width, height)

        while True:
            key = await session.read_editor_key()

            if key.kind == EditorKeyKind.CTRL and key.char == "x":
                if not state.dirty:
                    return None
                outcome = await _confirm_quit(session)
                if outcome == "save":
                    result = state.buffer.to_text()
                    _delete_draft(draft_path)
                    return result
                if outcome == "discard":
                    _delete_draft(draft_path)
                    return None
                previous = await _redraw(session, state, previous, width, height)
                continue

            if key.kind == EditorKeyKind.CTRL and key.char == "o":
                result = state.buffer.to_text()
                _delete_draft(draft_path)
                return result

            rejected = _dispatch(state, key, width, height)
            if rejected:
                await session.write("\a")
            previous = await _redraw(session, state, previous, width, height)
    finally:
        # GitHub issue #38: same fix as netbbs.net.ansi_editor's -- every
        # exit path above returns from mid-loop with the status line
        # still painted on its own row and the real cursor left at the
        # last edit position, neither of which the caller cleans up
        # before drawing its own next screen. A `finally` block runs on
        # every exit (including an unhandled exception), so clearing
        # here once covers all of them.
        #
        # GitHub issue #43: cancellation must not depend on that write
        # succeeding -- a genuine disconnect makes session.write() raise
        # SessionClosedError, which used to skip the two lines below
        # entirely and leak the autosave task running forever against a
        # dead session. Task cleanup now always runs; the screen clear
        # is best-effort and simply skipped for a transport that's
        # already gone (there's no terminal left to clean up).
        try:
            await session.write(clear_screen())
        except SessionClosedError:
            pass
        autosave_task.cancel()
        try:
            await autosave_task
        except asyncio.CancelledError:
            pass


def _dispatch(state: _EditorState, key: EditorKey, width: int, height: int) -> bool:
    """Applies `key` to `state`. Returns True if the keystroke was
    refused for being at `state.max_bytes` (GitHub issue #32) rather
    than applied -- the caller sounds the bell for this, adapting
    `netbbs.rendering.ansi.reject_keystroke`'s "that key doesn't do
    anything here" convention to an editor that draws its own screen
    rather than relying on real terminal echo to visibly undo."""
    buffer = state.buffer
    if key.kind == EditorKeyKind.LEFT:
        buffer.move_left()
    elif key.kind == EditorKeyKind.RIGHT:
        buffer.move_right()
    elif key.kind == EditorKeyKind.UP:
        _move_visual_row(state, width, delta=-1)
    elif key.kind == EditorKeyKind.DOWN:
        _move_visual_row(state, width, delta=1)
    elif key.kind == EditorKeyKind.PAGE_UP:
        _move_visual_row(state, width, delta=-height)
    elif key.kind == EditorKeyKind.PAGE_DOWN:
        _move_visual_row(state, width, delta=height)
    elif key.kind == EditorKeyKind.HOME:
        buffer.move_home()
    elif key.kind == EditorKeyKind.END:
        buffer.move_end()
    elif key.kind == EditorKeyKind.ENTER:
        if _byte_length(buffer.to_text()) + 1 > state.max_bytes:
            return True
        buffer.insert_newline()
        state.dirty = True
    elif key.kind == EditorKeyKind.BACKSPACE:
        buffer.backspace()
        state.dirty = True
    elif key.kind == EditorKeyKind.DELETE:
        buffer.delete()
        state.dirty = True
    elif key.kind == EditorKeyKind.CHAR and key.char is not None:
        if _byte_length(buffer.to_text()) + len(key.char.encode("utf-8")) > state.max_bytes:
            return True
        buffer.insert_char(key.char)
        state.dirty = True
    # TAB and unrecognized kinds: no-op, matching
    # netbbs.net.ansi_editor._dispatch's own precedent -- no tab-width
    # convention has been settled on, so inserting a raw tab character
    # (unpredictable rendering width across terminals) is deliberately
    # not attempted rather than guessed at.
    buffer.clamp_cursor()
    _scroll_into_view(state, width, height)
    return False


def _move_visual_row(state: _EditorState, width: int, *, delta: int) -> None:
    """Move the cursor up/down by `delta` *visual* (soft-wrapped) rows,
    not logical lines -- pressing Down from the middle of a long
    wrapped paragraph moves one screen line, the way every real text
    editor behaves, not one keypress per logical line regardless of
    how many screen rows it spans."""
    buffer = state.buffer
    pos = visual_position(buffer.lines, width, buffer.cursor_line, buffer.cursor_col)
    rows = wrap_lines(buffer.lines, width)
    buffer.cursor_line, buffer.cursor_col = logical_position(rows, pos.row_index + delta, pos.col)


def _scroll_into_view(state: _EditorState, width: int, height: int) -> None:
    pos = visual_position(state.buffer.lines, width, state.buffer.cursor_line, state.buffer.cursor_col)
    if pos.row_index < state.scroll_row:
        state.scroll_row = pos.row_index
    elif pos.row_index >= state.scroll_row + height:
        state.scroll_row = pos.row_index - height + 1


def _render(state: _EditorState, width: int, height: int) -> Snapshot:
    """Paint the current viewport (scroll_row .. scroll_row+height) of
    soft-wrapped text into a `ScreenBuffer`, reusing the same diffed-
    render primitives `netbbs.net.ansi_editor` does -- the only place
    this module touches `ScreenBuffer` at all; every actual edit
    operation works purely on `ProseBuffer`, wrap-unaware."""
    canvas = ScreenBuffer(width, height)
    rows = wrap_lines(state.buffer.lines, width)
    for viewport_row in range(height):
        row_index = state.scroll_row + viewport_row
        if row_index >= len(rows):
            break
        text = rows[row_index].text
        for col, char in enumerate(text[:width]):
            canvas.write_cell(viewport_row, col, char, fg=None, bg=None, bold=False)
    return canvas.snapshot()


async def _redraw(session: Session, state: _EditorState, previous: Snapshot, width: int, height: int) -> Snapshot:
    current = _render(state, width, height)
    diff = diff_ansi(previous, current)
    if diff:
        await session.write(diff)
    await _flush(session, state, width, height)
    return current


async def _flush(session: Session, state: _EditorState, width: int, height: int) -> None:
    """Redraws the status line and repositions the terminal's real
    cursor to the logical edit position, mirroring
    `netbbs.net.ansi_editor._flush` exactly in shape."""
    pos = visual_position(state.buffer.lines, width, state.buffer.cursor_line, state.buffer.cursor_col)
    screen_row = pos.row_index - state.scroll_row
    status = (
        f"Line {state.buffer.cursor_line + 1}  Col {state.buffer.cursor_col + 1}  "
        f"Ctrl+O save  Ctrl+X quit"
    )
    # GitHub issue #32: visible even outside the instant a keystroke
    # gets rejected at the ceiling, not just a flash-on-reject bell --
    # once at the limit it stays a standing fact about this document
    # until something is deleted.
    if _byte_length(state.buffer.to_text()) >= state.max_bytes:
        status += "  AT LENGTH LIMIT"
    status = truncate(status, width)
    await session.write(move_cursor(height + _STATUS_ROW_OFFSET, 1))
    await session.write(clear_line())
    await session.write(colored(status, fg_color=MUTED_COLOR))
    await session.write(move_cursor(max(1, screen_row + 1), pos.col + 1))


async def _offer_draft_recovery(session: Session) -> bool:
    await session.write_line(
        colored(
            "\r\nA draft from a previous session was found here (likely left behind by a "
            "dropped connection).",
            fg_color=MUTED_COLOR,
        )
    )
    return await prompt_yes_no(session, "Resume it?", default=False)


async def _confirm_quit(session: Session) -> str:
    """Returns `"save"`, `"discard"`, or `"cancel"` -- single keystroke,
    same shape as `netbbs.net.ansi_editor._confirm_quit`."""
    await session.write("\r\nUnsaved changes. [S]ave, [D]iscard, or [C]ancel? ")
    answer = (await session.read_key()).lower()
    if answer == "s":
        return "save"
    if answer == "d":
        return "discard"
    return "cancel"


async def _autosave_loop(state: _EditorState, draft_path: Path, interval_seconds: float) -> None:
    """Same independent-background-task shape as
    `netbbs.net.ansi_editor._autosave_loop` -- runs until cancelled by
    `edit_prose` returning, surviving past the interactive session
    dying on purpose."""
    while True:
        await asyncio.sleep(interval_seconds)
        if not state.dirty:
            continue
        try:
            draft_path.write_text(state.buffer.to_text(), encoding="utf-8")
        except OSError:
            _logger.warning("could not write prose editor autosave draft to %s", draft_path, exc_info=True)


def _delete_draft(draft_path: Path) -> None:
    try:
        draft_path.unlink(missing_ok=True)
    except OSError:
        _logger.warning("could not delete prose editor draft at %s", draft_path, exc_info=True)
