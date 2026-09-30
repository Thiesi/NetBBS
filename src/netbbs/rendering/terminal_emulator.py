"""
A server-side copy of what a caller's terminal shows (issue #764, tracker
#761): a small streaming VT100/ANSI emulator fed everything NetBBS writes
to a session.

Two things read it: the SysOp's snoop view, which draws the grid, and the
break-in chat (#765), which repaints a caller's terminal from it
(`restore_ansi`) when the chat ends. It is fed on every write for every
session, so it is deliberately small: a grid of `Cell`s, a cursor, a pen,
a scroll region and a parser that survives a sequence split across
writes.

What it understands is what NetBBS and the bundled doors emit, plus the
common ANSI-BBS repertoire external doors use: cursor addressing and
movement, erase in display and line, insert and delete of lines and
characters, scrolling and scroll regions, cursor save and restore, SGR
colours (16, 256 and truecolor) with bold, underline and reverse, and
cursor visibility. Anything else is consumed and ignored: an unknown
sequence may leave the copy imperfect, never raise.

Pure: no I/O, no `Session`. Rendering the grid goes through
`netbbs.rendering.screen_buffer`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from typing import NamedTuple

from netbbs.digits import is_ascii_number
from netbbs.rendering.ansi import CSI, ESC, colored, move_cursor, set_scroll_region
from netbbs.rendering.screen_buffer import Cell, Snapshot, full_render_ansi
from netbbs.rendering.width import char_width

# Same defensive ceiling as ScreenBuffer: the size comes from a
# client-reported terminal size.
_MAX_WIDTH = 500
_MAX_HEIGHT = 200

#: A CSI longer than this is garbage, not a sequence; it is dropped rather
#: than buffered without bound.
_MAX_SEQUENCE = 64

_GROUND, _ESCAPE, _CSI, _OSC, _OSC_ESCAPE, _CHARSET, _CSI_IGNORE = range(7)

_BLANK = Cell()

#: The most code points one cell keeps: a base plus its combining marks.
#: A stream of marks after one base must not grow a cell without bound.
_MAX_CELL_CHARS = 8

#: Printable ASCII: one column each, no lookup needed. Most output is runs
#: of these between escape sequences, so they take a fast path.
_ASCII_RUN = re.compile("[\\x20-\\x7e]+")

#: A complete CSI sequence in one piece -- the common case, parsed in one
#: step. One split across two writes falls back to the state machine.
_WHOLE_CSI = re.compile("\\x1b\\[([\\x30-\\x3f]{0,%d})([\\x40-\\x7e])" % 64)

#: `apply_sgr` results by (pen, parameters): a door recolours nearly every
#: character with the same few sequences.
_SGR_CACHE: dict[tuple[object, tuple[int, ...]], object] = {}
_SGR_CACHE_LIMIT = 4096


class Pen(NamedTuple):
    """The attributes the next printed character gets. A tuple, because it
    is a cache key on the per-character path."""

    fg: int | tuple[int, int, int] | None = None
    bg: int | tuple[int, int, int] | None = None
    bold: bool = False
    underline: bool = False
    reverse: bool = False


def _clamp_byte(value: int) -> int:
    return max(0, min(255, value))


def apply_sgr(pen: Pen, params: list[int]) -> Pen:
    """`pen` after one SGR sequence."""
    key = (pen, tuple(params))
    cached = _SGR_CACHE.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    result = _apply_sgr(pen, params)
    if len(_SGR_CACHE) >= _SGR_CACHE_LIMIT:
        _SGR_CACHE.clear()
    _SGR_CACHE[key] = result
    return result


def _apply_sgr(pen: Pen, params: list[int]) -> Pen:
    if not params:
        params = [0]
    fg, bg, bold, underline, reverse = pen.fg, pen.bg, pen.bold, pen.underline, pen.reverse
    i = 0
    while i < len(params):
        code = params[i]
        if code == 0:
            fg, bg, bold, underline, reverse = None, None, False, False, False
        elif code == 1:
            bold = True
        elif code == 22:
            bold = False
        elif code == 4:
            underline = True
        elif code == 24:
            underline = False
        elif code == 7:
            reverse = True
        elif code == 27:
            reverse = False
        elif 30 <= code <= 37:
            fg = code - 30
        elif 90 <= code <= 97:
            fg = code - 90 + 8
        elif code == 39:
            fg = None
        elif 40 <= code <= 47:
            bg = code - 40
        elif 100 <= code <= 107:
            bg = code - 100 + 8
        elif code == 49:
            bg = None
        elif code in (38, 48) and i + 2 < len(params) and params[i + 1] == 5:
            color = _clamp_byte(params[i + 2])
            fg, bg = (color, bg) if code == 38 else (fg, color)
            i += 2
        elif code in (38, 48) and i + 4 < len(params) and params[i + 1] == 2:
            color = (_clamp_byte(params[i + 2]), _clamp_byte(params[i + 3]), _clamp_byte(params[i + 4]))
            fg, bg = (color, bg) if code == 38 else (fg, color)
            i += 4
        i += 1
    return Pen(fg, bg, bold, underline, reverse)


def _sgr_params_with_colons(body: str) -> list[int]:
    """SGR parameters in ISO 8613-6 colon form (`38:2::255:0:0`,
    `38:5:196`), turned into the semicolon form `apply_sgr` reads."""
    params: list[int] = []
    for part in body.split(";"):
        if ":" not in part:
            params.append(int(part) if is_ascii_number(part) else 0)
            continue
        fields = part.split(":")
        numbers = [int(field) if is_ascii_number(field) else 0 for field in fields]
        if numbers[0] in (38, 48) and len(numbers) >= 3 and numbers[1] == 5:
            params.extend([numbers[0], 5, numbers[2]])
        elif numbers[0] in (38, 48) and len(numbers) >= 5 and numbers[1] == 2:
            # 38:2:<colour space>:r:g:b[:...], or 38:2:r:g:b without the
            # colour space; anything after blue is ignored, as xterm does.
            rgb = numbers[3:6] if len(numbers) >= 6 else numbers[2:5]
            params.extend([numbers[0], 2, *rgb])
        else:
            params.append(numbers[0])  # e.g. 4:3, a curly underline: underline
    return params


def pen_sgr(pen: Pen) -> str:
    """The SGR sequence that sets exactly `pen`, from any state."""
    codes = ["0"]
    if pen.bold:
        codes.append("1")
    if pen.underline:
        codes.append("4")
    if pen.reverse:
        codes.append("7")
    for base, color in ((38, pen.fg), (48, pen.bg)):
        if isinstance(color, tuple):
            codes.append(f"{base};2;{color[0]};{color[1]};{color[2]}")
        elif color is not None:
            codes.append(f"{base};5;{color}")
    return f"{CSI}{';'.join(codes)}m"


class TerminalEmulator:
    """A `width` x `height` terminal, fed text as it was sent.

    `wraps_immediately` models a terminal that moves to the next line as soon
    as it writes the last column (SyncTERM and DOS ANSI.SYS, issue #964),
    rather than waiting for the next character the way xterm does: a copy of
    such a caller's screen has to wrap, and scroll at the bottom-right cell,
    exactly where theirs does."""

    def __init__(self, width: int, height: int, *, wraps_immediately: bool = False) -> None:
        self.width = max(1, min(width, _MAX_WIDTH))
        self.height = max(1, min(height, _MAX_HEIGHT))
        self.wraps_immediately = wraps_immediately
        self._reset()

    def _reset(self) -> None:
        self._rows: list[list[Cell]] = [self._blank_row() for _ in range(self.height)]
        self.row = 0
        self.col = 0
        self.pen = Pen()
        self.cursor_visible = True
        self.top = 0
        self.bottom = self.height - 1
        self._wrap_pending = False
        self._saved: tuple[int, int, Pen] | None = None
        # The main screen while the alternate one is shown, else None.
        self._main_rows: list[list[Cell]] | None = None
        self._main_saved: tuple[int, int, Pen] | None = None
        self._main_margins: tuple[int, int] = (0, self.height - 1)
        self._state = _GROUND
        self._sequence = ""
        # Cells by pen, then character: frozen, so shared freely.
        self._cells_by_pen: dict[Pen, dict[str, Cell]] = {}

    # -- inspection --------------------------------------------------------

    def snapshot(self) -> Snapshot:
        return tuple(tuple(row) for row in self._rows)

    def text_rows(self) -> list[str]:
        """The visible characters, one string per row (for tests)."""
        return ["".join(cell.char for cell in row) for row in self._rows]

    def restore_ansi(self) -> str:
        """What repaints a terminal to exactly this state: every cell, then
        the scroll region, the cursor position, the pen and the cursor's
        visibility, so the program that owns the screen carries on as if
        nothing had been drawn over it."""
        parts = [f"{CSI}r", full_render_ansi(self.snapshot())]
        if (self.top, self.bottom) != (0, self.height - 1):
            parts.append(set_scroll_region(self.top + 1, self.bottom + 1))
        parts.append(move_cursor(self.row + 1, self.col + 1))
        if self._wrap_pending:
            # Output ended in the last column. A cursor move clears a real
            # terminal's pending wrap, so reprint that last cell: the
            # terminal is then waiting to wrap, as this copy is.
            col = self.col
            if col > 0 and not self._rows[self.row][col].char:
                col -= 1  # a wide glyph ended the row: reprint it whole
                parts.append(move_cursor(self.row + 1, col + 1))
            cell = self._rows[self.row][col]
            if cell.char:
                parts.append(colored(
                    cell.char, fg_color=cell.fg, bg_color=cell.bg, bold=cell.bold,
                    underline=cell.underline, reverse=cell.reverse,
                ))
        parts.append(pen_sgr(self.pen))
        parts.append(f"{CSI}?25h" if self.cursor_visible else f"{CSI}?25l")
        return "".join(parts)

    # -- size --------------------------------------------------------------

    def resize(self, width: int, height: int) -> None:
        """Keep what fits, top-left anchored, as a terminal does."""
        width = max(1, min(width, _MAX_WIDTH))
        height = max(1, min(height, _MAX_HEIGHT))
        if (width, height) == (self.width, self.height):
            return
        def reshape(grid: list[list[Cell]]) -> list[list[Cell]]:
            rows = []
            for row in grid[:height]:
                kept = row[:width] + [_BLANK] * (width - len(row))
                if width < len(row) and not row[width].char and kept[-1].char:
                    kept[-1] = _BLANK  # the new edge cuts a wide glyph in two
                rows.append(kept)
            return rows + [[_BLANK] * width for _ in range(height - len(rows))]

        self._rows = reshape(self._rows)
        if self._main_rows is not None:
            self._main_rows = reshape(self._main_rows)
        self.width, self.height = width, height
        self.top, self.bottom = 0, height - 1
        # A parked main screen's margins were sized for the old height; a
        # resize resets them, as it resets the active ones.
        self._main_margins = (0, height - 1)
        self.row = min(self.row, height - 1)
        self.col = min(self.col, width - 1)
        self._wrap_pending = False

    # -- feeding -----------------------------------------------------------

    def feed(self, text: str) -> None:
        index, length = 0, len(text)
        while index < length:
            if self._state == _GROUND:
                run = _ASCII_RUN.match(text, index)
                if run is not None:
                    self._print_ascii(run.group())
                    index = run.end()
                    continue
                whole = _WHOLE_CSI.match(text, index)
                if whole is not None:
                    self._csi(whole.group(1), whole.group(2))
                    index = whole.end()
                    continue
            ch = text[index]
            index += 1
            state = self._state
            if state == _GROUND:
                if ch == ESC:
                    self._state = _ESCAPE
                elif ch < " " or ch == "\x7f":
                    self._control(ch)
                else:
                    self._print(ch)
            elif state == _ESCAPE:
                self._escape(ch)
            elif state == _CSI:
                if "\x40" <= ch <= "\x7e":
                    self._state = _GROUND
                    self._csi(self._sequence, ch)
                elif len(self._sequence) >= _MAX_SEQUENCE:
                    # Too long to be real: swallow it up to its final byte,
                    # as a terminal does, rather than print its tail.
                    self._state = _CSI_IGNORE
                else:
                    self._sequence += ch
            elif state == _CSI_IGNORE:
                if "@" <= ch <= "~":
                    self._state = _GROUND
            elif state == _OSC:
                if ch == "\x07":
                    self._state = _GROUND
                elif ch == ESC:
                    self._state = _OSC_ESCAPE
            elif state == _OSC_ESCAPE:
                # ESC \ ends it; anything else, give up on it.
                self._state = _GROUND
            elif state == _CHARSET:
                # ESC ( B, ESC % G, ESC # 8 and friends: intermediate bytes,
                # then the final byte, all consumed and ignored. Another ESC
                # cancels the sequence and starts a new one.
                if ch == ESC:
                    self._state = _ESCAPE
                elif not "\x20" <= ch <= "\x2f":
                    self._state = _GROUND

    def _control(self, ch: str) -> None:
        if ch == "\r":
            self.col = 0
            self._wrap_pending = False
        elif ch in "\n\x0b\x0c":
            self._linefeed()
        elif ch == "\b":
            if self.col > 0:
                self.col -= 1
            self._wrap_pending = False
        elif ch == "\t":
            self.col = min(self.width - 1, (self.col // 8 + 1) * 8)
            self._wrap_pending = False
        # BEL and the rest: nothing to draw.

    def _escape(self, ch: str) -> None:
        self._state = _GROUND
        if ch == "[":
            self._state = _CSI
            self._sequence = ""
        elif ch in "]PX^_":
            # OSC, DCS, SOS, PM, APC: a control string the terminal consumes
            # whole, up to BEL or ST; its payload is never drawn.
            self._state = _OSC
        elif "\x20" <= ch <= "\x2f":
            self._state = _CHARSET
        elif ch == "7":
            self._saved = (self.row, self.col, self.pen)
        elif ch == "8":
            self._restore_cursor()
        elif ch == "D":
            self._linefeed()
        elif ch == "E":
            self.col = 0
            self._linefeed()
        elif ch == "M":
            self._reverse_index()
        elif ch == "c":
            self._reset()

    # -- printing ----------------------------------------------------------

    def _print(self, ch: str) -> None:
        if not ch.isprintable() and not unicodedata.combining(ch):
            # C1 controls (U+0080-U+009F, CSI among them in 8-bit terminals),
            # bidi overrides and other format characters: a caller could
            # otherwise plant them on their own screen and have snoop replay
            # them to the SysOp's terminal. Dropped, never stored.
            return
        width = char_width(ch)
        if width == 0:
            # A combining mark joins the character before it.
            col = self.col if self._wrap_pending else self.col - 1
            if col > 0 and not self._rows[self.row][col].char:
                col -= 1  # past a wide glyph's continuation cell
            if 0 <= col < self.width:
                cell = self._rows[self.row][col]
                if cell.char and len(cell.char) < _MAX_CELL_CHARS:
                    self._rows[self.row][col] = replace(cell, char=cell.char + ch)
            return
        if self._wrap_pending:
            self.col = 0
            self._linefeed()
            self._wrap_pending = False
        if width == 2 and self.col == self.width - 1:
            # A wide glyph that doesn't fit wraps whole, and the terminal
            # blanks the edge cell it leaves behind.
            if self.width < 2:
                return
            self._split_wide(self.row, self.col, self.col + 1)
            self._rows[self.row][self.col] = self._erased()
            self.col = 0
            self._linefeed()
        self._split_wide(self.row, self.col, self.col + width)
        cell = self._pen_cell(ch)
        self._rows[self.row][self.col] = cell
        if width == 2:
            self._rows[self.row][self.col + 1] = replace(cell, char="")
        self.col += width
        if self.col >= self.width:
            self._last_column_written()

    def _last_column_written(self) -> None:
        """The cursor has passed the last column: xterm waits there for the
        next character; a terminal that wraps immediately is already on the
        next line."""
        if self.wraps_immediately:
            self.col = 0
            self._linefeed()
            self._wrap_pending = False
        else:
            self.col = self.width - 1
            self._wrap_pending = True

    def _pen_cell(self, ch: str) -> Cell:
        pen = self.pen
        cells = self._cells_by_pen.get(pen)
        if cells is None:
            if len(self._cells_by_pen) >= 256:
                self._cells_by_pen.clear()
            cells = self._cells_by_pen[pen] = {}
        cell = cells.get(ch)
        if cell is None:
            cell = Cell(char=ch, fg=pen.fg, bg=pen.bg, bold=pen.bold, underline=pen.underline, reverse=pen.reverse)
            if len(cells) < 512:
                cells[ch] = cell
        return cell

    def _print_ascii(self, run: str) -> None:
        """`_print` for a run of one-column characters, a row slice at a
        time."""
        width = self.width
        start = 0
        while start < len(run):
            if self._wrap_pending:
                self.col = 0
                self._linefeed()
                self._wrap_pending = False
            take = min(len(run) - start, width - self.col)
            self._split_wide(self.row, self.col, self.col + take)
            cells = [self._pen_cell(ch) for ch in run[start : start + take]]
            self._rows[self.row][self.col : self.col + take] = cells
            self.col += take
            start += take
            if self.col >= width:
                self._last_column_written()

    def _split_wide(self, row: int, start: int, end: int) -> None:
        """Before cells `start`..`end` are overwritten, blank the other half
        of any wide glyph they cut through, as a terminal does: a leading
        half left of `start`, a continuation right of `end`."""
        line = self._rows[row]
        blank = self._erased()
        for edge in (start, end):
            # A continuation at `edge` with its leading half just left of it
            # is a pair the edge cuts through: blank both halves.
            if 0 < edge < self.width and not line[edge].char and line[edge - 1].char:
                line[edge - 1] = blank
                line[edge] = blank

    def _blank_row(self) -> list[Cell]:
        return [_BLANK] * self.width

    def _erased(self) -> Cell:
        # Erasing fills with the pen's background, as terminals do.
        return Cell(bg=self.pen.bg) if self.pen.bg is not None else _BLANK

    # -- movement and scrolling ------------------------------------------

    def _linefeed(self) -> None:
        self._wrap_pending = False
        if self.row == self.bottom:
            self._scroll_up(1)
        elif self.row < self.height - 1:
            self.row += 1

    def _reverse_index(self) -> None:
        self._wrap_pending = False
        if self.row == self.top:
            self._scroll_down(1)
        elif self.row > 0:
            self.row -= 1

    def _scroll_up(self, count: int, top: int | None = None) -> None:
        top = self.top if top is None else top
        count = max(0, min(count, self.bottom - top + 1))
        if not count:
            return
        blank = [self._erased()] * self.width
        del self._rows[top : top + count]
        for _ in range(count):
            self._rows.insert(self.bottom - count + 1, list(blank))

    def _scroll_down(self, count: int, top: int | None = None) -> None:
        top = self.top if top is None else top
        count = max(0, min(count, self.bottom - top + 1))
        if not count:
            return
        blank = [self._erased()] * self.width
        del self._rows[self.bottom - count + 1 : self.bottom + 1]
        for _ in range(count):
            self._rows.insert(top, list(blank))

    def _restore_cursor(self) -> None:
        if self._saved is None:
            self.row, self.col = 0, 0
        else:
            row, col, self.pen = self._saved
            self.row, self.col = min(row, self.height - 1), min(col, self.width - 1)
        self._wrap_pending = False

    def _move_to(self, row: int, col: int) -> None:
        self.row = max(0, min(row, self.height - 1))
        self.col = max(0, min(col, self.width - 1))
        self._wrap_pending = False

    # -- CSI ---------------------------------------------------------------

    def _csi(self, sequence: str, final: str) -> None:
        if any("\x20" <= ch <= "\x2f" for ch in sequence):
            # An intermediate byte makes it a different command (CSI SP A is
            # scroll right, not cursor up): not one this copy models.
            return
        private = sequence[:1] in ("?", ">", "<", "=")
        body = sequence[1:] if private else sequence
        if ":" in body:
            if final != "m" or private:
                return  # subparameters on anything but SGR: not modelled
            params = _sgr_params_with_colons(body)
        else:
            params = []
            for part in body.split(";") if body else ():
                if is_ascii_number(part):
                    params.append(int(part))
                else:
                    digits = "".join(c for c in part if "0" <= c <= "9")
                    params.append(int(digits) if digits else 0)
        if final == "m" and not private:
            # By far the most frequent sequence: straight to the pen.
            self.pen = apply_sgr(self.pen, params)
            return

        def arg(index: int = 0, default: int = 1) -> int:
            value = params[index] if index < len(params) else 0
            return value if value else default

        if private:
            if final in "hl" and 25 in params:
                self.cursor_visible = final == "h"
            if final in "hl" and {47, 1047, 1049} & set(params):
                self._alternate_screen(final == "h", save_cursor=1049 in params)
            return
        if final in "Hf":
            self._move_to(arg(0) - 1, arg(1) - 1)
        elif final == "A":
            self._move_to(max(self.top if self.row >= self.top else 0, self.row - arg()), self.col)
        elif final == "B":
            self._move_to(min(self.bottom if self.row <= self.bottom else self.height - 1, self.row + arg()), self.col)
        elif final == "C":
            self._move_to(self.row, self.col + arg())
        elif final == "D":
            self._move_to(self.row, self.col - arg())
        elif final == "E":
            self._move_to(self.row + arg(), 0)
        elif final == "F":
            self._move_to(self.row - arg(), 0)
        elif final == "G":
            self._move_to(self.row, arg() - 1)
        elif final == "d":
            self._move_to(arg() - 1, self.col)
        elif final == "J":
            self._erase_display(arg(default=0))
        elif final == "K":
            self._erase_line(arg(default=0))
        elif final == "L":
            if self.top <= self.row <= self.bottom:
                self._scroll_down(arg(), top=self.row)
                self._move_to(self.row, 0)
        elif final == "M":
            if self.top <= self.row <= self.bottom:
                self._scroll_up(arg(), top=self.row)
                self._move_to(self.row, 0)
        elif final == "@":
            self._insert_chars(arg())
        elif final == "P":
            self._delete_chars(arg())
        elif final == "X":
            count = min(arg(), self.width - self.col)
            self._split_wide(self.row, self.col, self.col + count)
            self._rows[self.row][self.col : self.col + count] = [self._erased()] * count
        elif final == "S":
            self._scroll_up(arg())
        elif final == "T":
            self._scroll_down(arg())
        elif final == "r":
            top, bottom = arg(0) - 1, arg(1, self.height) - 1
            if 0 <= top < bottom < self.height:
                self.top, self.bottom = top, bottom
            else:
                self.top, self.bottom = 0, self.height - 1
            self._move_to(0, 0)
        elif final == "s":
            self._saved = (self.row, self.col, self.pen)
        elif final == "u":
            self._restore_cursor()
        elif final == "m":
            self.pen = apply_sgr(self.pen, params)

    def _alternate_screen(self, enter: bool, *, save_cursor: bool) -> None:
        """xterm's alternate buffer, which full-screen programs (native
        doors) switch to and back from: the main screen is kept aside
        untouched and comes back when the program leaves."""
        if enter and self._main_rows is None:
            # The main screen's own save is kept aside with it: a save made
            # on the alternate screen must not replace it.
            self._main_saved = (self.row, self.col, self.pen) if save_cursor else self._saved
            self._main_margins = (self.top, self.bottom)
            self._main_rows = self._rows
            self._rows = [self._blank_row() for _ in range(self.height)]
            self.top, self.bottom = 0, self.height - 1
            self._wrap_pending = False
        elif not enter and self._main_rows is not None:
            self._rows = self._main_rows
            self._main_rows = None
            self._saved, self._main_saved = self._main_saved, None
            self.top, self.bottom = self._main_margins
            if save_cursor:
                self._restore_cursor()

    def _erase_display(self, mode: int) -> None:
        blank = self._erased()
        if mode == 0:
            self._erase_line(0)
            for row in range(self.row + 1, self.height):
                self._rows[row] = [blank] * self.width
        elif mode == 1:
            self._erase_line(1)
            for row in range(0, self.row):
                self._rows[row] = [blank] * self.width
        elif mode == 2:
            self._rows = [[blank] * self.width for _ in range(self.height)]
        self._wrap_pending = False

    def _erase_line(self, mode: int) -> None:
        blank = self._erased()
        line = self._rows[self.row]
        if mode == 0 and self._wrap_pending:
            # The real cursor sits past the last column, waiting to wrap:
            # the range to erase is empty, and the wrap stays pending.
            return
        if mode == 0:
            self._split_wide(self.row, self.col, self.width)
            line[self.col :] = [blank] * (self.width - self.col)
        elif mode == 1:
            self._split_wide(self.row, 0, self.col + 1)
            line[: self.col + 1] = [blank] * (self.col + 1)
        elif mode == 2:
            self._rows[self.row] = [blank] * self.width
        self._wrap_pending = False

    def _insert_chars(self, count: int) -> None:
        line = self._rows[self.row]
        count = min(count, self.width - self.col)
        # Splitting a pair at the cursor, or pushing one off the edge,
        # leaves half a glyph: blank it whole first.
        self._split_wide(self.row, self.col, self.col)
        self._split_wide(self.row, self.width - count, self.width)
        line[self.col : self.col] = [self._erased()] * count
        del line[self.width :]

    def _delete_chars(self, count: int) -> None:
        line = self._rows[self.row]
        count = min(count, self.width - self.col)
        self._split_wide(self.row, self.col, self.col + count)
        del line[self.col : self.col + count]
        line.extend([self._erased()] * count)
