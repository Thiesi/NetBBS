"""
Pure, I/O-free text-editing core for the fullscreen prose editor (design
doc -- prose editor): a logical line buffer plus cursor,
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

import re
import unicodedata
from dataclasses import dataclass

from netbbs.rendering.width import char_width, display_width, wrap_to_width

# A quoted line's marker: one or more ">", each maybe followed by a space --
# "> ", "> > " and ">> " as `netbbs.quoting.quote_body` and other mail
# programs write them.
_QUOTE_PREFIX = re.compile(r"(?:>[ \t]?)+")
# What `netbbs.quoting.quote_body` puts where it cut a long quote short.
_ELISION = "[...]"


@dataclass(frozen=True)
class VisualRow:
    """One on-screen row of the editor's soft-wrapped view.

    `text` is a slice of logical line `line_index`, starting at
    `start_col` -- never a hard line break the user actually typed.
    Every logical line, including an empty one (a blank paragraph
    separator), produces at least one `VisualRow`, so blank lines stay
    visible and navigable rather than silently vanishing the way
    `wrap_to_width("", width)` (`[]`, no rows) would produce on its own.
    """

    line_index: int
    start_col: int
    text: str


def cell_width(char: str) -> int:
    """Columns `char` takes in an editor row: `char_width`, except that a
    control character -- a tab in loaded text -- is one blank column, as
    `paint_char` draws it, rather than none."""
    if " " <= char <= "~":
        return 1  # printable ASCII, the common case, without the lookups
    if len(char) == 1 and unicodedata.category(char) == "Cc":
        return 1
    return char_width(char)


def paint_char(char: str) -> str:
    """What an editor row draws for `char`: a control character is a blank,
    never the raw byte, which would move the terminal's cursor."""
    if " " <= char <= "~":
        return char
    return " " if len(char) == 1 and unicodedata.category(char) == "Cc" else char


def cells(text: str) -> int:
    """`text`'s width in editor columns (`cell_width`)."""
    return sum(cell_width(char) for char in text)


def soft_wrap(text: str, width: int) -> list[str]:
    """`text` cut into rows of at most `width` columns, keeping every
    character: the rows joined give back `text` exactly.

    Unlike `wrap_to_width`, which is for showing finished text and drops
    the whitespace at each break and squeezes every run of it to one
    space, this is for text being typed. Dropping a typed space was why a
    space at the end of a line did nothing on screen until the next
    letter arrived, and why a second space looked eaten: the stored text
    had them, the row drawn from it did not, and the cursor, measured
    against the row, stayed put.

    A row breaks after the last run of whitespace that fits, so a word
    that does not fit moves down whole. Where a word ends exactly at the
    edge, the one whitespace character after it stays on the row,
    "hanging" past the edge where nothing draws it -- as a word processor
    does -- so the next row does not start with a stray space; any more
    whitespace starts the next row, where it shows. A word wider than the
    row is cut at the edge. When the last row is full (or ends in a hung
    space), an empty row follows it: the cursor after the text is on it,
    which is where a terminal puts it too."""
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")
    rows: list[str] = []
    start, n = 0, len(text)
    while True:
        used = 0
        i = start
        last_break: int | None = None
        while i < n:
            w = cell_width(text[i])
            if used + w > width:
                break
            used += w
            i += 1
            if i < n and text[i - 1].isspace() and not text[i].isspace():
                last_break = i
        if i >= n:
            rows.append(text[start:])
            if used >= width:
                rows.append("")
            return rows
        if text[i].isspace():
            end = i + 1  # hangs past the edge
            if end >= n:
                rows.append(text[start:])
                rows.append("")
                return rows
        elif last_break is not None:
            end = last_break
        else:
            end = max(i, start + 1)
            while end < n and cell_width(text[end]) == 0:
                end += 1  # a mark stays with its character
        rows.append(text[start:end])
        start = end


def wrap_lines(lines: list[str], width: int, cache: dict[str, list[str]] | None = None) -> list[VisualRow]:
    """Soft-wrap every logical line to `width` columns for display
    (`soft_wrap`). Each line's rows are exact slices of it, so a row's
    `start_col` plus an offset into its `text` is a real position in the
    line.

    `cache` (one per editor and width) keeps each line's rows from the
    last call: a keystroke changes one line, so a long text is not wrapped
    whole again for every key. Afterwards it holds exactly the lines of
    this call, so it is never bigger than the text itself."""
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")

    rows: list[VisualRow] = []
    fresh: dict[str, list[str]] = {}
    for line_index, line in enumerate(lines):
        segments = fresh.get(line)
        if segments is None and cache is not None:
            segments = cache.get(line)
        if segments is None:
            segments = soft_wrap(line, width)
        fresh[line] = segments
        col = 0
        for segment in segments:
            rows.append(VisualRow(line_index=line_index, start_col=col, text=segment))
            col += len(segment)
    if cache is not None:
        cache.clear()
        cache.update(fresh)
    return rows


@dataclass(frozen=True)
class VisualPosition:
    row_index: int  # index into the list wrap_lines() returned
    col: int  # offset within that VisualRow's text


def visual_position(
    lines: list[str], width: int, cursor_line: int, cursor_col: int, rows: list[VisualRow] | None = None
) -> VisualPosition:
    """Map a logical `(cursor_line, cursor_col)` to where that lands in
    `wrap_lines(lines, width)`'s output -- the row whose slice of
    `cursor_line` holds the character at `cursor_col`, or the line's last
    row for a cursor at (or past) its end. `rows`, when the caller has
    already wrapped `lines` at `width`, saves wrapping them again."""
    if rows is None:
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

    # Rows are exact, back-to-back slices of the line (`soft_wrap`), so
    # the offset where one ends is where the next begins, and belongs to
    # the next: a cursor after a row's last character is at the start of
    # the row below, as on a terminal.
    for row_index in candidates[:-1]:
        row = rows[row_index]
        if cursor_col < row.start_col + len(row.text):
            return VisualPosition(row_index=row_index, col=max(0, cursor_col - row.start_col))

    last_row_index = candidates[-1]
    last_row = rows[last_row_index]
    return VisualPosition(row_index=last_row_index, col=max(0, cursor_col - last_row.start_col))


def _quote_parts(line: str) -> tuple[str, int, str] | None:
    """`(prefix, depth, content)` of a quoted line, or None for a line that
    is not quoted. `depth` counts the `>`s, so "> > " and ">> " match."""
    match = _QUOTE_PREFIX.match(line)
    if match is None:
        return None
    prefix = match.group(0)
    return prefix, prefix.count(">"), line[match.end():]


def rewrap_quote(lines: list[str], line_index: int, width: int) -> tuple[int, int, list[str]] | None:
    """The quoted paragraph around `lines[line_index]` rewrapped to `width`
    display columns, every line keeping the quote's prefix (issue #815).

    Returns `(start, end, new_lines)`: `lines[start:end]` is the paragraph
    and `new_lines` replaces it. None when the line is not quoted, or is a
    quote's empty separator line (`>`), which has no paragraph to rewrap.

    The paragraph is the run of quoted lines at the same depth that have
    text; a line at another depth, an empty quote line, or an unquoted
    line ends it, and so does the `[...]` that marks a cut quote. The editor itself never hard-wraps -- a long line is
    wrapped only on screen -- but a quote is different: a quoted line
    longer than the screen shows its continuation without a `>`, and a
    quote trimmed in place is left ragged. Long words are kept whole: a
    URL cut in two would no longer work."""
    parts = _quote_parts(lines[line_index])
    if parts is None or parts[2].strip() in ("", _ELISION):
        return None
    prefix, depth, _ = parts

    def in_paragraph(index: int) -> bool:
        other = _quote_parts(lines[index])
        return other is not None and other[1] == depth and other[2].strip() not in ("", _ELISION)

    start = line_index
    while start > 0 and in_paragraph(start - 1):
        start -= 1
    end = line_index + 1
    while end < len(lines) and in_paragraph(end):
        end += 1
    prefix = prefix.rstrip() + " "
    text = " ".join(_quote_parts(lines[i])[2].strip() for i in range(start, end))  # type: ignore[index]
    body_width = max(1, width - display_width(prefix))
    return start, end, [prefix + segment for segment in wrap_to_width(text, body_width, break_long_words=False)]


def logical_position(rows: list[VisualRow], row_index: int, col: int) -> tuple[int, int]:
    """The inverse of `visual_position`: given a row from an already-
    computed `wrap_lines()` result plus a column within it, the logical
    `(line, col)` it corresponds to. Clamps `row_index` into range
    rather than raising, so callers moving Up from the first row or
    Down from the last can pass the clamped result straight through
    without a separate bounds check of their own.

    A row that continues on the next one ends before its last offset,
    which is the next row's start (see `visual_position`), so a column
    past its end lands on its last character rather than a row further
    down."""
    row_index = max(0, min(row_index, len(rows) - 1))
    row = rows[row_index]
    continues = row_index + 1 < len(rows) and rows[row_index + 1].line_index == row.line_index
    end = len(row.text) - 1 if continues and row.text else len(row.text)
    return row.line_index, row.start_col + max(0, min(col, end))


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

    def insert_text(self, text: str) -> None:
        """Insert `text`, which may hold `\\n`s, at the cursor, leaving the
        cursor after it -- a paste."""
        pieces = text.split("\n")
        line = self.lines[self.cursor_line]
        before, after = line[: self.cursor_col], line[self.cursor_col :]
        if len(pieces) == 1:
            self.lines[self.cursor_line] = before + text + after
            self.cursor_col += len(text)
            return
        new_lines = [before + pieces[0], *pieces[1:-1], pieces[-1] + after]
        self.lines[self.cursor_line : self.cursor_line + 1] = new_lines
        self.cursor_line += len(new_lines) - 1
        self.cursor_col = len(pieces[-1])

    def cut_line(self) -> str:
        """Remove the cursor's whole logical line and return it (issue
        #815, nano's Ctrl-K). The cursor lands at the start of the line
        that took its place. The last line has no line after it to take
        its place, so it is emptied instead; the text still ends where
        it did."""
        cut = self.lines[self.cursor_line]
        if self.cursor_line == len(self.lines) - 1:
            self.lines[self.cursor_line] = ""
        else:
            del self.lines[self.cursor_line]
        self.cursor_col = 0
        return cut

    def delete_word_before(self) -> None:
        """Delete the word before the cursor, and the spaces between it and
        the cursor (issue #815; readline's Ctrl-W). At the start of a line
        it joins the line to the one above, as Backspace does."""
        if self.cursor_col == 0:
            self.backspace()
            return
        line = self.lines[self.cursor_line]
        start = self.cursor_col
        while start > 0 and line[start - 1].isspace():
            start -= 1
        while start > 0 and not line[start - 1].isspace():
            start -= 1
        self.lines[self.cursor_line] = line[:start] + line[self.cursor_col :]
        self.cursor_col = start

    def clamp_cursor(self) -> None:
        """Keeps the cursor in bounds after any operation that could
        leave it dangling past the end of a now-shorter line (called
        defensively at the end of every dispatched key in the
        interactive editor, matching `netbbs.net.ansi_editor._dispatch`'s
        own clamping posture)."""
        self.cursor_line = max(0, min(self.cursor_line, len(self.lines) - 1))
        self.cursor_col = max(0, min(self.cursor_col, len(self.lines[self.cursor_line])))
