"""
The WYSIWYG ANSI art editor (design doc -- welcome banner round B1) --
the first real consumer of the screen-buffer/diff abstraction (design
doc round 26).

Deliberately generic/reusable: this module knows nothing about
"welcome banner" specifically -- see `netbbs.net.admin_flow`'s
`[X] edit` screen for the one concrete caller today. A later round can
reuse `edit_ansi_art` against a different save target without any
changes here; the only file this module writes to directly is the
autosave draft, never a caller's real save target.

First-version scope, confirmed with Thiesi: cursor movement, typing
(with a glyph picker for CP437's block/line-drawing characters no
keyboard can type directly, and a 16-color palette picker for
foreground/background), save/quit, and periodic autosave with
crash/disconnect recovery. No undo/redo, no block copy/fill/select, no
line/box-drawing tools, no canvas resize -- a real, planned later
phase, not abandoned.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.rendering import (
    MUTED_COLOR,
    ScreenBuffer,
    Snapshot,
    clear_line,
    colored,
    decode_ansi_bytes,
    diff_ansi,
    encode_ansi_bytes,
    full_render_ansi,
    move_cursor,
    parse_ansi_into_buffer,
    truncate,
)

_logger = logging.getLogger(__name__)

# Overridable so tests don't wait 30 real seconds; production callers
# get a real, sensible default (design doc -- welcome banner round B1,
# confirmed with Thiesi: periodic autosave, not explicit-save-only).
DEFAULT_AUTOSAVE_INTERVAL_SECONDS = 30.0

# The status line lives one row below the fixed canvas, addressed
# directly (not part of the diffed ScreenBuffer) -- simple ordinary
# writes, since it's a single line rewritten in full each time, not
# something worth diffing.
_STATUS_ROW_OFFSET = 2  # one blank line, then the status line

# The 16 classic ANSI colors -- xterm 256-color palette indices 0-15,
# exactly the classic set real scene ANSI art overwhelmingly targets.
# Deliberately not the full 256-color range `colored()` elsewhere
# supports (design doc -- welcome banner round B1: a stated V1
# restriction, also keeps this a single unpaginated picker screen).
_PALETTE = [
    "Black", "Red", "Green", "Yellow", "Blue", "Magenta", "Cyan", "White",
    "Bright Black", "Bright Red", "Bright Green", "Bright Yellow",
    "Bright Blue", "Bright Magenta", "Bright Cyan", "Bright White",
]

# A representative set of CP437 block/shade/line-drawing glyphs -- the
# signature look of ANSI art, and something no ordinary keyboard can
# type directly (this picker's whole reason for existing).
_GLYPHS: list[tuple[str, str]] = [
    ("Full block", "█"),
    ("Dark shade", "▓"),
    ("Medium shade", "▒"),
    ("Light shade", "░"),
    ("Upper half block", "▀"),
    ("Lower half block", "▄"),
    ("Left half block", "▌"),
    ("Right half block", "▐"),
    ("Box light: horizontal", "─"),
    ("Box light: vertical", "│"),
    ("Box light: top-left", "┌"),
    ("Box light: top-right", "┐"),
    ("Box light: bottom-left", "└"),
    ("Box light: bottom-right", "┘"),
    ("Box light: cross", "┼"),
    ("Box double: horizontal", "═"),
    ("Box double: vertical", "║"),
    ("Box double: top-left", "╔"),
    ("Box double: top-right", "╗"),
    ("Box double: bottom-left", "╚"),
    ("Box double: bottom-right", "╝"),
    ("Plain space", " "),
]


@dataclass
class _EditorState:
    buffer: ScreenBuffer
    row: int = 0
    col: int = 0
    current_fg: int | None = 7  # white
    current_bg: int | None = None
    dirty: bool = False


async def edit_ansi_art(
    session: Session,
    *,
    initial_bytes: bytes | None,
    draft_path: Path,
    width: int = 80,
    height: int = 24,
    autosave_interval_seconds: float = DEFAULT_AUTOSAVE_INTERVAL_SECONDS,
) -> bytes | None:
    """
    Run a WYSIWYG ANSI art editing session against `session`, returning
    the saved bytes on a real save, or `None` if the SysOp quit without
    saving.

    `draft_path` is a periodic autosave target: a pre-existing draft
    there (left behind by a prior disconnect/crash) is offered for
    recovery on entry, instead of `initial_bytes`. Saving or explicitly
    discarding deletes the draft; a genuine disconnect
    (`SessionClosedError` propagating out of a key read) leaves it in
    place -- that's the recovery path working as intended, not a bug
    to catch.
    """
    buffer = ScreenBuffer(width, height)

    loaded_bytes: bytes | None = None
    if draft_path.exists() and await _offer_draft_recovery(session):
        loaded_bytes = draft_path.read_bytes()
    else:
        if draft_path.exists():
            draft_path.unlink()
        loaded_bytes = initial_bytes

    if loaded_bytes is not None:
        parse_ansi_into_buffer(decode_ansi_bytes(loaded_bytes), buffer)

    state = _EditorState(buffer=buffer)
    autosave_task = asyncio.create_task(
        _autosave_loop(state, draft_path, autosave_interval_seconds)
    )
    try:
        previous = buffer.snapshot()
        await session.write(full_render_ansi(previous))
        await _flush(session, state)

        while True:
            key = await session.read_editor_key()

            if key.kind == EditorKeyKind.ESCAPE:
                if not state.dirty:
                    return None
                outcome = await _confirm_quit(session)
                if outcome == "save":
                    result = encode_ansi_bytes(buffer)
                    _delete_draft(draft_path)
                    return result
                if outcome == "discard":
                    _delete_draft(draft_path)
                    return None
                previous = await _redraw(session, state, previous)
                continue

            if key.kind == EditorKeyKind.CTRL and key.char == "s":
                result = encode_ansi_bytes(buffer)
                _delete_draft(draft_path)
                return result

            if key.kind == EditorKeyKind.CTRL and key.char == "g":
                # A chosen glyph is painted immediately, like a typed
                # character would be -- CP437's block/line-drawing
                # glyphs are this picker's whole reason for existing
                # precisely because no real keyboard key ever sends
                # them as a literal character, so there's no ordinary
                # "typing" event a glyph choice could otherwise wait
                # to apply to.
                choice = await _pick_glyph(session)
                if choice is not None:
                    _paint(state, choice)
                previous = await _redraw(session, state, previous)
                continue

            if key.kind == EditorKeyKind.CTRL and key.char == "p":
                choice = await _pick_color(session, "Foreground")
                if choice != "unchanged":
                    state.current_fg = choice
                previous = await _redraw(session, state, previous)
                continue

            if key.kind == EditorKeyKind.CTRL and key.char == "b":
                choice = await _pick_color(session, "Background")
                if choice != "unchanged":
                    state.current_bg = choice
                previous = await _redraw(session, state, previous)
                continue

            _dispatch(state, key)
            previous = await _redraw(session, state, previous)
    finally:
        autosave_task.cancel()
        try:
            await autosave_task
        except asyncio.CancelledError:
            pass


def _dispatch(state: _EditorState, key: EditorKey) -> None:
    buffer = state.buffer
    if key.kind == EditorKeyKind.UP:
        state.row = max(0, state.row - 1)
    elif key.kind == EditorKeyKind.DOWN:
        state.row = min(buffer.height - 1, state.row + 1)
    elif key.kind == EditorKeyKind.LEFT:
        state.col = max(0, state.col - 1)
    elif key.kind == EditorKeyKind.RIGHT:
        state.col = min(buffer.width - 1, state.col + 1)
    elif key.kind == EditorKeyKind.HOME:
        state.col = 0
    elif key.kind == EditorKeyKind.END:
        state.col = buffer.width - 1
    elif key.kind == EditorKeyKind.PAGE_UP:
        state.row = 0
    elif key.kind == EditorKeyKind.PAGE_DOWN:
        state.row = buffer.height - 1
    elif key.kind == EditorKeyKind.ENTER:
        state.row = min(buffer.height - 1, state.row + 1)
        state.col = 0
    elif key.kind == EditorKeyKind.DELETE:
        buffer.write_cell(state.row, state.col, " ", fg=None, bg=None, bold=False)
        state.dirty = True
    elif key.kind == EditorKeyKind.BACKSPACE:
        if state.col > 0:
            state.col -= 1
        elif state.row > 0:
            state.row -= 1
            state.col = buffer.width - 1
        buffer.write_cell(state.row, state.col, " ", fg=None, bg=None, bold=False)
        state.dirty = True
    elif key.kind == EditorKeyKind.CHAR and key.char is not None:
        _paint(state, key.char)
    # TAB and unrecognized kinds: no-op in this round's scope.


def _paint(state: _EditorState, char: str) -> None:
    """Writes `char` at the cursor with the current fg/bg, then
    advances the cursor (wrapping to the next row, typewriter-style,
    clamped at the bottom-right corner rather than wrapping past the
    canvas). Shared by ordinary typing and a glyph picker selection --
    the latter behaves exactly like typing that glyph would, since
    that's the whole reason the picker exists (see its call site)."""
    buffer = state.buffer
    buffer.write_cell(state.row, state.col, char, fg=state.current_fg, bg=state.current_bg, bold=False)
    state.dirty = True
    if state.col < buffer.width - 1:
        state.col += 1
    elif state.row < buffer.height - 1:
        state.col = 0
        state.row += 1


async def _redraw(session: Session, state: _EditorState, previous: Snapshot) -> Snapshot:
    current = state.buffer.snapshot()
    diff = diff_ansi(previous, current)
    if diff:
        await session.write(diff)
    await _flush(session, state)
    return current


async def _flush(session: Session, state: _EditorState) -> None:
    """Redraws the status line and repositions the terminal's real
    cursor to the logical edit position -- called after every action,
    so the SysOp always sees exactly where they are and what they'll
    paint with next, matching how a real terminal editor behaves."""
    fg_label = _PALETTE[state.current_fg] if state.current_fg is not None else "default"
    bg_label = _PALETTE[state.current_bg] if state.current_bg is not None else "default"
    status = (
        f"Row {state.row + 1}/{state.buffer.height}  Col {state.col + 1}/{state.buffer.width}  "
        f"fg={fg_label} bg={bg_label}  "
        f"Ctrl+G glyph  Ctrl+P fg  Ctrl+B bg  Ctrl+S save  Esc quit"
    )
    # Must never exceed the canvas width: a status line long enough to
    # wrap (the palette names alone push this well past 80 columns,
    # e.g. "Bright Magenta") corrupts every subsequent redraw -- the
    # wrapped remainder lands on the row below, which this function's
    # single clear_line() never touches, so it accumulates garbage
    # there on every redraw instead of being overwritten in place.
    status = truncate(status, state.buffer.width)
    await session.write(move_cursor(state.buffer.height + _STATUS_ROW_OFFSET, 1))
    await session.write(clear_line())
    await session.write(colored(status, fg_color=MUTED_COLOR))
    await session.write(move_cursor(state.row + 1, state.col + 1))


async def _offer_draft_recovery(session: Session) -> bool:
    await session.write_line(
        colored(
            "\r\nA draft from a previous session was found here (likely left behind by a "
            "dropped connection).",
            fg_color=MUTED_COLOR,
        )
    )
    await session.write("Resume it? [y/N]: ")
    answer = (await session.read_line()).strip().lower()
    return answer == "y"


async def _confirm_quit(session: Session) -> str:
    """Returns `"save"`, `"discard"`, or `"cancel"`.

    A single keystroke, like every other editor command -- Esc to get
    here in the first place already didn't need Enter, so requiring it
    only for this one sub-prompt would be the odd one out. Any key
    other than S/D defaults to "cancel" (dropping the SysOp back into
    the editor with nothing lost), same fallback `read_line`'s
    startswith-based check used before."""
    await session.write("\r\nUnsaved changes. [S]ave, [D]iscard, or [C]ancel? ")
    answer = (await session.read_key()).lower()
    if answer == "s":
        return "save"
    if answer == "d":
        return "discard"
    return "cancel"


async def _pick_glyph(session: Session) -> str | None:
    selected = await pick_item(
        session, _GLYPHS,
        name_of=lambda item: item[0],
        stable_id_of=lambda item: _GLYPHS.index(item),
        description_of=lambda item: item[1],
        title="Glyph",
        empty_message="No glyphs available.",
    )
    return selected[1] if selected is not None else None


async def _pick_color(session: Session, label: str) -> int | None | str:
    """Returns the chosen palette index, `None` for "default" (no
    color), or the sentinel `"unchanged"` if the picker was cancelled
    without a choice."""
    named = list(enumerate(_PALETTE))
    selected = await pick_item(
        session, named,
        name_of=lambda item: item[1],
        stable_id_of=lambda item: item[0],
        title=f"{label} color",
        empty_message="No colors available.",
    )
    return selected[0] if selected is not None else "unchanged"


async def _autosave_loop(state: _EditorState, draft_path: Path, interval_seconds: float) -> None:
    """
    A genuine independent background task, not a "check between
    keystrokes" approximation -- the latter wouldn't help if a long
    pause precedes a disconnect. Runs until cancelled by `edit_ansi_art`
    returning, regardless of reason (including a `SessionClosedError`
    propagating out of the main loop) -- autosave surviving past the
    interactive session dying is exactly the point.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        if not state.dirty:
            continue
        try:
            draft_path.write_bytes(encode_ansi_bytes(state.buffer))
        except OSError:
            _logger.warning("could not write ANSI editor autosave draft to %s", draft_path, exc_info=True)


def _delete_draft(draft_path: Path) -> None:
    try:
        draft_path.unlink(missing_ok=True)
    except OSError:
        _logger.warning("could not delete ANSI editor draft at %s", draft_path, exc_info=True)
