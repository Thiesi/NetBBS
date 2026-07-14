"""
Pure, I/O-free text-editing core for the fullscreen prose editor (design
doc -- prose editor round B2): a logical line buffer plus cursor,
soft-wrapped for display, with no dependency on a session, a terminal,
or `netbbs.rendering.screen_buffer` -- mirrors that module's own
"pure and testable independent of any I/O" shape.

Deliberately not `netbbs.rendering.reflow`: that module collapses and
rewraps a whole already-finished multi-paragraph text (correct for
*displaying* a saved post), which would reshuffle line breaks out from
under someone actively editing. Here, a logical line is exactly what
the SysOp typed between two real Enter presses -- never rewrapped or
merged automatically -- and word-wrap is a *display-only* concern:
`wrap_lines` computes what to show on screen without ever changing
what's actually stored. Saving hands back the plain logical lines
joined with real `\n`s; `reflow` (unchanged) still does the collapsing
rewrap at *display* time, same as it always has for any post body.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class VisualRow:
    """One on-screen row of the editor's soft-wrapped view.

    `text` is a slice of logical line `line_index`, starting at
    `start_col` -- never a hard line break the user actually typed.
    Every logical line, including an empty one (a blank paragraph
    separator), produces at least one `VisualRow`, so blank lines stay
    visible and navigable rather than silently vanishing the way
    `textwrap.wrap("")` (`[]`, no rows) would produce on its own.
    """

    line_index: int
    start_col: int
    text: str


def wrap_lines(lines: list[str], width: int) -> list[VisualRow]:
    """Soft-wrap every logical line to `width` columns for display."""
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")

    rows: list[VisualRow] = []
    for line_index, line in enumerate(lines):
        segments = textwrap.wrap(line, width=width) or [""]
        col = 0
        for segment in segments:
            # Re-locate the segment's actual start column in the
            # original line rather than assuming `len(segment)`-sized
            # steps -- textwrap collapses runs of whitespace between
            # words, so segment lengths don't sum back to len(line).
            col = line.index(segment, col) if segment else col
            rows.append(VisualRow(line_index=line_index, start_col=col, text=segment))
            col += len(segment)
    return rows


@dataclass(frozen=True)
class VisualPosition:
    row_index: int  # index into the list wrap_lines() returned
    col: int  # offset within that VisualRow's text


def visual_position(lines: list[str], width: int, cursor_line: int, cursor_col: int) -> VisualPosition:
    """Map a logical `(cursor_line, cursor_col)` to where that lands in
    `wrap_lines(lines, width)`'s output -- the row whose slice of
    `cursor_line` covers `cursor_col`, or the line's last row if
    `cursor_col` is exactly at (or past) its end, e.g. a cursor
    freshly advanced by typing the line's very last character."""
    rows = wrap_lines(lines, width)
    candidates = [i for i, row in enumerate(rows) if row.line_index == cursor_line]
    if not candidates:
        # An empty lines list, or an out-of-range cursor_line -- not a
        # normal editing state, but degrade to the first/last row
        # rather than raising, matching this codebase's general "clamp,
        # don't crash" posture for cursor arithmetic elsewhere (see
        # netbbs.net.ansi_editor._dispatch).
        row_index = max(0, min(cursor_line, len(rows) - 1)) if rows else 0
        return VisualPosition(row_index=row_index, col=0)

    for row_index in candidates:
        row = rows[row_index]
        # Inclusive upper bound, not exclusive: textwrap's wrap point
        # consumes the separating whitespace between two segments, so
        # a cursor sitting exactly on that consumed character (a real,
        # reachable position -- typing to the end of a wrapped row
        # lands here) matches no row under an exclusive bound at all.
        # Landing it on the *end* of the earlier row, not the start of
        # the next, is what makes this invertible: logical_position of
        # (this row, len(row.text)) must reproduce the same cursor_col.
        if row.start_col <= cursor_col <= row.start_col + len(row.text):
            return VisualPosition(row_index=row_index, col=cursor_col - row.start_col)

    last_row_index = candidates[-1]
    last_row = rows[last_row_index]
    return VisualPosition(row_index=last_row_index, col=cursor_col - last_row.start_col)


def logical_position(rows: list[VisualRow], row_index: int, col: int) -> tuple[int, int]:
    """The inverse of `visual_position`: given a row from an already-
    computed `wrap_lines()` result plus a column within it, the logical
    `(line, col)` it corresponds to. Clamps `row_index` into range
    rather than raising, so callers moving Up from the first row or
    Down from the last can pass the clamped result straight through
    without a separate bounds check of their own."""
    row_index = max(0, min(row_index, len(rows) - 1))
    row = rows[row_index]
    return row.line_index, row.start_col + max(0, min(col, len(row.text)))


@dataclass
class ProseBuffer:
    """The logical text buffer + cursor -- every edit operation here
    works purely on `lines`/`cursor_line`/`cursor_col`, entirely
    unaware of word-wrap or the screen; `wrap_lines`/`visual_position`
    above are the only place wrapping enters the picture, kept
    deliberately separate so editing logic never has to reason about
    where a soft line break happens to fall."""

    lines: list[str]
    cursor_line: int = 0
    cursor_col: int = 0

    @classmethod
    def from_text(cls, text: str) -> "ProseBuffer":
        return cls(lines=text.split("\n") if text else [""])

    def to_text(self) -> str:
        return "\n".join(self.lines)

    def insert_char(self, char: str) -> None:
        line = self.lines[self.cursor_line]
        self.lines[self.cursor_line] = line[: self.cursor_col] + char + line[self.cursor_col :]
        self.cursor_col += len(char)

    def insert_newline(self) -> None:
        line = self.lines[self.cursor_line]
        before, after = line[: self.cursor_col], line[self.cursor_col :]
        self.lines[self.cursor_line] = before
        self.lines.insert(self.cursor_line + 1, after)
        self.cursor_line += 1
        self.cursor_col = 0

    def backspace(self) -> None:
        if self.cursor_col > 0:
            line = self.lines[self.cursor_line]
            self.lines[self.cursor_line] = line[: self.cursor_col - 1] + line[self.cursor_col :]
            self.cursor_col -= 1
        elif self.cursor_line > 0:
            # Merge into the end of the previous line -- the standard
            # "Backspace at column 0 joins lines" behavior every text
            # editor has, nano included.
            previous = self.lines[self.cursor_line - 1]
            current = self.lines.pop(self.cursor_line)
            self.cursor_line -= 1
            self.cursor_col = len(previous)
            self.lines[self.cursor_line] = previous + current

    def delete(self) -> None:
        line = self.lines[self.cursor_line]
        if self.cursor_col < len(line):
            self.lines[self.cursor_line] = line[: self.cursor_col] + line[self.cursor_col + 1 :]
        elif self.cursor_line < len(self.lines) - 1:
            # Forward-merge: Delete at end-of-line joins the *next*
            # line up, the mirror image of Backspace's join.
            nxt = self.lines.pop(self.cursor_line + 1)
            self.lines[self.cursor_line] = line + nxt

    def move_left(self) -> None:
        if self.cursor_col > 0:
            self.cursor_col -= 1
        elif self.cursor_line > 0:
            self.cursor_line -= 1
            self.cursor_col = len(self.lines[self.cursor_line])

    def move_right(self) -> None:
        if self.cursor_col < len(self.lines[self.cursor_line]):
            self.cursor_col += 1
        elif self.cursor_line < len(self.lines) - 1:
            self.cursor_line += 1
            self.cursor_col = 0

    def move_home(self) -> None:
        self.cursor_col = 0

    def move_end(self) -> None:
        self.cursor_col = len(self.lines[self.cursor_line])

    def clamp_cursor(self) -> None:
        """Keeps the cursor in bounds after any operation that could
        leave it dangling past the end of a now-shorter line (called
        defensively at the end of every dispatched key in the
        interactive editor, matching `netbbs.net.ansi_editor._dispatch`'s
        own clamping posture)."""
        self.cursor_line = max(0, min(self.cursor_line, len(self.lines) - 1))
        self.cursor_col = max(0, min(self.cursor_col, len(self.lines[self.cursor_line])))
