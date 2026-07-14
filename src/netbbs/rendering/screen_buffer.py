"""
The TUI screen-buffer/diff abstraction (design doc round 26, built now
alongside its first real consumer per that round's own reasoning — see
`netbbs.net.ansi_editor`). Pure, no I/O — matches this package's
existing boundary (nothing else in `netbbs.rendering` touches
`Session`/`Database`).

`ScreenBuffer` is a mutable 2D grid of `Cell`s a caller paints onto;
`diff_ansi`/`full_render_ansi` turn two `Snapshot`s (or one, for the
first draw) into the actual ANSI string to send to a `Session`, built
entirely on the existing `netbbs.rendering.ansi` primitives
(`move_cursor`, `colored`, `clear_screen`) rather than inventing new
escape-sequence handling. Both group consecutive same-style cells on a
row into one `colored()` call rather than one per cell — on the web
transport, every `Session.write()` is one full WebSocket message with
no server-side batching (confirmed by direct investigation), so
minimizing both the number of writes and the bytes per write is a
real, not cosmetic, saving.

Note: `netbbs.rendering.ansi.move_cursor(row, col)` (absolute
positioning, used here) and `netbbs.net.char_input.move_cursor(count,
*, forward)` (relative, single-axis, used by line editing) share a
name with incompatible signatures across two modules — they're never
imported into the same namespace, so no rename was needed, but don't
confuse the two.
"""

from __future__ import annotations

from dataclasses import dataclass

from netbbs.rendering.ansi import clear_screen, colored, move_cursor

Snapshot = tuple[tuple["Cell", ...], ...]


@dataclass(frozen=True)
class Cell:
    char: str = " "
    fg: int | None = None
    bg: int | None = None
    bold: bool = False


class ScreenBuffer:
    """A mutable `width` x `height` grid of `Cell`s, addressed
    `[row][col]`, both 0-indexed (unlike `netbbs.rendering.ansi.
    move_cursor`'s 1-indexed terminal coordinates -- `diff_ansi`/
    `full_render_ansi` are the only places that conversion happens)."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self._rows: list[list[Cell]] = [[Cell() for _ in range(width)] for _ in range(height)]

    def write_cell(
        self, row: int, col: int, char: str, *, fg: int | None = None, bg: int | None = None, bold: bool = False
    ) -> None:
        self._check_bounds(row, col)
        self._rows[row][col] = Cell(char=char, fg=fg, bg=bg, bold=bold)

    def get_cell(self, row: int, col: int) -> Cell:
        self._check_bounds(row, col)
        return self._rows[row][col]

    def clear(self) -> None:
        self._rows = [[Cell() for _ in range(self.width)] for _ in range(self.height)]

    def snapshot(self) -> Snapshot:
        return tuple(tuple(row) for row in self._rows)

    def _check_bounds(self, row: int, col: int) -> None:
        if not (0 <= row < self.height and 0 <= col < self.width):
            raise ValueError(f"cell ({row}, {col}) out of bounds for a {self.width}x{self.height} buffer")


def _style(cell: Cell) -> tuple[int | None, int | None, bool]:
    return (cell.fg, cell.bg, cell.bold)


def diff_ansi(previous: Snapshot, current: Snapshot) -> str:
    """
    The ANSI string that turns a terminal already showing `previous`
    into one showing `current` -- only the cells that actually
    changed, each same-style consecutive run on a row as one
    `move_cursor` + `colored` pair.
    """
    parts: list[str] = []
    for row_index, (prev_row, cur_row) in enumerate(zip(previous, current)):
        width = len(cur_row)
        col = 0
        while col < width:
            if cur_row[col] == prev_row[col]:
                col += 1
                continue
            start_col = col
            style = _style(cur_row[col])
            chars: list[str] = []
            while col < width and cur_row[col] != prev_row[col] and _style(cur_row[col]) == style:
                chars.append(cur_row[col].char)
                col += 1
            parts.append(move_cursor(row_index + 1, start_col + 1))
            parts.append(colored("".join(chars), fg_color=style[0], bg_color=style[1], bold=style[2]))
    return "".join(parts)


def full_render_ansi(current: Snapshot) -> str:
    """A full first-draw (or forced-redraw) render of `current`:
    `clear_screen()` (which already blanks and homes the cursor) plus
    only the non-blank, non-default-style runs -- a freshly cleared
    terminal already shows blank default-style cells everywhere, so
    there's nothing to gain by re-sending them."""
    parts: list[str] = [clear_screen()]
    for row_index, cur_row in enumerate(current):
        width = len(cur_row)
        col = 0
        while col < width:
            if cur_row[col] == Cell():
                col += 1
                continue
            start_col = col
            style = _style(cur_row[col])
            chars: list[str] = []
            while col < width and cur_row[col] != Cell() and _style(cur_row[col]) == style:
                chars.append(cur_row[col].char)
                col += 1
            parts.append(move_cursor(row_index + 1, start_col + 1))
            parts.append(colored("".join(chars), fg_color=style[0], bg_color=style[1], bold=style[2]))
    return "".join(parts)
