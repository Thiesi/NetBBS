"""Read-only detail panels: grouped label/value rows, tables, and paging.

The draft editor (`netbbs.net.resource_editor`) already had a readable shape
for a screen full of facts -- an uppercase section heading, a label column in
one colour, a value column in another, long values wrapped under where they
start, and a page per section once the terminal runs out. Every *read-only*
status screen predated it and printed `Label: value` sentences one under the
other in the terminal's default colour, so nothing separated a label from its
value or one group of facts from the next, and a screen with more rows than the
terminal simply scrolled its top away.

This module is that same shape for screens that only show things. It builds
styled rows and nothing else -- no session, no database -- so a screen can
measure exactly what it is about to write before it writes it, which is what
lets `netbbs.net.detail_view` page a panel instead of letting it scroll.

Plain text handed to `Field`/`Note`/`Table` is sanitized here, before any
styling is applied; a value that is already styled (a badge, a gauge) is passed
through `styled=True` and is trusted as it stands.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from netbbs.rendering.ansi import colored
from netbbs.rendering.sanitize import sanitize_text
from netbbs.rendering.theme import (
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    RULE_COLOR,
    VALUE_COLOR,
)
from netbbs.rendering.width import cut_to_width, display_width, wrap_to_width

Color = int | tuple[int, int, int]

_INDENT = "  "
# A label column wider than this pushes every value on the screen to the right
# for the sake of one long label; past it, that one row stacks instead.
_MAX_LABEL_WIDTH = 28
# Below this many columns left for the value, label and value stop sharing a
# row at all (same floor the draft editor keeps).
_MIN_VALUE_WIDTH = 12
_TABLE_GUTTER = 2
_PAIRED_MIN_WIDTH = 72
# A wrapping table column narrower than this is not worth reading: the table
# is laid out one record per row instead (`_record_lines`).
_MIN_FLEX_WIDTH = 16
# A page emptier than this fraction takes the start of the next group rather
# than being left nearly blank for the sake of keeping that group whole.
_MIN_PAGE_FILL = 0.5
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain_width(text: str) -> int:
    return display_width(_SGR_RE.sub("", text))


@dataclass(frozen=True)
class Field:
    """One `Label:  value` row. `value` is plain text unless `styled`."""

    label: str
    value: str
    color: Color = VALUE_COLOR
    bold: bool = False
    styled: bool = False
    # The `>` cursor of a screen whose rows can be arrow-selected, drawn the way
    # the draft editor draws its own: marker and label as one accented run.
    selected: bool = False
    accent: Color = LABEL_COLOR
    # A muted remark about this one value, wrapped under where the value starts
    # so it reads as belonging to it rather than as a row of its own.
    note: str | None = None


@dataclass(frozen=True)
class Note:
    """A sentence of prose that belongs to the section, wrapped to the panel."""

    text: str
    color: Color = MUTED_COLOR


@dataclass(frozen=True)
class Table:
    """Aligned columns under a header row. A cell is text or `(text, color)`.

    `flex` names the column that gives way when the rows are wider than the
    terminal; every other column keeps its natural width."""

    headers: Sequence[str]
    rows: Sequence[Sequence[str | tuple[str, Color]]]
    flex: int = 0
    right_aligned: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Section:
    """A titled group of rows -- one paragraph of the panel."""

    title: str | None
    rows: Sequence[Field | Note | Table]
    # Lay short fields out side by side, two to a row, where the terminal is
    # wide enough and every value fits its half; otherwise one to a row.
    paired: bool = False


def label_width(sections: Sequence[Section]) -> int:
    """The label column every section on one screen shares, so values line up
    down the whole panel rather than per group. Capped: see `_MAX_LABEL_WIDTH`."""
    widths = [
        display_width(sanitize_text(row.label))
        for section in sections for row in section.rows if isinstance(row, Field)
    ]
    return min(_MAX_LABEL_WIDTH, max(widths, default=0))


def _right_label_width(sections: Sequence[Section]) -> int:
    """The label column the right-hand half of every paired section shares."""
    widths = [
        display_width(sanitize_text(row.label))
        for section in sections if section.paired
        for row in list(section.rows)[1::2] if isinstance(row, Field)
    ]
    return min(_MAX_LABEL_WIDTH, max(widths, default=0))


def _field_lines(row: Field, *, column: int, width: int) -> list[str]:
    label = sanitize_text(row.label)
    label_size = display_width(label)
    # The cursor takes the indent's two columns, so selecting a row never
    # moves its value.
    if row.selected:
        head = colored(f"> {label}:", fg_color=row.accent, bold=True)
    else:
        head = colored(f"{_INDENT}{label}:", fg_color=LABEL_COLOR)
    value_column = len(_INDENT) + column + 2
    stacked = label_size > column or value_column > width - _MIN_VALUE_WIDTH
    # A styled value (a badge, a gauge) cannot be wrapped, so one that does not
    # fit beside its label takes a row of its own rather than the terminal's
    # soft wrap -- which would also make the row count paging relies on wrong.
    if row.styled and value_column + _plain_width(row.value) > width:
        stacked = True
    if stacked:
        value_column = len(_INDENT) * 2
    available = max(1, width - value_column)
    if row.styled:
        value_lines = [row.value]
    else:
        value_lines = [
            colored(line, fg_color=row.color, bold=row.bold)
            for line in (wrap_to_width(sanitize_text(row.value), available) or [""])
        ]
    indent = " " * value_column
    if row.note:
        value_lines.extend(
            colored(line, fg_color=MUTED_COLOR) for line in wrap_to_width(sanitize_text(row.note), available)
        )
    if stacked:
        return [head, *(indent + line for line in value_lines)]
    padding = " " * (column - label_size + 1)
    return [head + padding + value_lines[0], *(indent + line for line in value_lines[1:])]


def _paired_lines(
    fields: Sequence[Field], *, column: int, right_column: int, width: int
) -> list[str] | None:
    """`fields` two to a row, or `None` when that would cost a value its
    room: the terminal is too narrow, or some value does not fit its half."""
    if width < _PAIRED_MIN_WIDTH or len(fields) < 2:
        return None
    half = width // 2
    left, right = fields[0::2], fields[1::2]
    cells: list[list[str]] = []
    for group, group_column in ((left, column), (right, right_column)):
        rendered = [_field_lines(f, column=group_column, width=half) for f in group]
        if any(len(lines) != 1 or _plain_width(lines[0]) > half for lines in rendered):
            return None
        cells.append([lines[0] for lines in rendered])
    lines = []
    for index, cell in enumerate(cells[0]):
        if index < len(cells[1]):
            # The right cell's own indent is the gutter between the halves.
            lines.append(cell + " " * (half - _plain_width(cell)) + cells[1][index])
        else:
            lines.append(cell)
    return lines


def _note_lines(row: Note, *, width: int) -> list[str]:
    available = max(1, width - len(_INDENT))
    return [
        _INDENT + colored(line, fg_color=row.color)
        for line in (wrap_to_width(sanitize_text(row.text), available) or [""])
    ]


def _table_lines(table: Table, *, width: int, unicode_style: bool) -> list[str]:
    cells = [
        [(sanitize_text(c), VALUE_COLOR) if isinstance(c, str) else (sanitize_text(c[0]), c[1]) for c in row]
        for row in table.rows
    ]
    headers = [sanitize_text(h) for h in table.headers]
    count = len(headers)
    if not count:
        return []
    flex = min(table.flex, count - 1)
    widths = [
        max([display_width(headers[i]), *(display_width(row[i][0]) for row in cells)]) for i in range(count)
    ]
    budget = max(1, width - len(_INDENT) - _TABLE_GUTTER * (count - 1))
    fixed = sum(widths) - widths[flex]
    if sum(widths) > budget:
        if budget - fixed < min(_MIN_FLEX_WIDTH, widths[flex]):
            # The other columns leave the wrapping one no room worth having
            # (an audit row whose kind alone is 33 columns, on any terminal; any
            # table at all on a 40-column one). Squeezing it further made rows
            # wider than the terminal; each row becomes a short record instead.
            return _record_lines(headers, cells, width=width)
        widths[flex] = budget - fixed

    def _cell(text: str, size: int, color: Color, *, right: bool, bold: bool = False) -> str:
        text = cut_to_width(text, size)
        pad = " " * max(0, size - display_width(text))
        styled = colored(text, fg_color=color, bold=bold) if text else ""
        return pad + styled if right else styled + pad

    gutter = " " * _TABLE_GUTTER

    def _rows(texts: Sequence[tuple[str, Color]], *, bold: bool = False) -> list[str]:
        """One table row, its flex cell wrapped under itself: continuation rows
        leave every other column blank. A table never drops text."""
        wrapped = wrap_to_width(texts[flex][0], widths[flex]) or [""]
        out = []
        for index, piece in enumerate(wrapped):
            parts = [
                (piece, texts[i][1]) if i == flex else (texts[i] if index == 0 else ("", VALUE_COLOR))
                for i in range(count)
            ]
            out.append((_INDENT + gutter.join(
                _cell(parts[i][0], widths[i], parts[i][1], right=i in table.right_aligned, bold=bold)
                for i in range(count)
            )).rstrip())
        return out

    lines = _rows([(header, METADATA_COLOR) for header in headers], bold=True)
    lines.append(_INDENT + colored(
        ("─" if unicode_style else "-") * (sum(widths) + _TABLE_GUTTER * (count - 1)), fg_color=RULE_COLOR
    ))
    for row in cells:
        lines.extend(_rows(row))
    return lines


def _record_lines(
    headers: Sequence[str], cells: Sequence[Sequence[tuple[str, Color]]], *, width: int
) -> list[str]:
    """A table too wide for its terminal, one record per row: each column as a
    `Header: value` field, the values wrapping, a blank row between records."""
    column = min(_MAX_LABEL_WIDTH, max(display_width(header) for header in headers))
    lines: list[str] = []
    for index, row in enumerate(cells):
        if index:
            lines.append("")
        for header, (text, color) in zip(headers, row):
            if text:
                lines.extend(_field_lines(Field(header, text, color=color), column=column, width=width))
    return lines


@dataclass(frozen=True)
class Block:
    """One rendered section: its heading row, if it has one, and its rows."""

    heading: str | None
    rows: list[str]

    @property
    def lines(self) -> list[str]:
        return ([self.heading] if self.heading is not None else []) + self.rows


def render_section(
    section: Section, *, column: int, width: int, unicode_style: bool = False, right_column: int | None = None
) -> Block:
    """One section as styled rows. Every row fits `width`."""
    heading = None
    if section.title:
        heading = colored(
            cut_to_width(sanitize_text(section.title).upper(), width), fg_color=METADATA_COLOR, bold=True
        )
    rows: list[str] = []
    if right_column is None:
        right_column = _right_label_width([section])
    if section.paired and all(isinstance(row, Field) for row in section.rows):
        paired = _paired_lines(list(section.rows), column=column, right_column=right_column, width=width)
        if paired is not None:
            return Block(heading, paired)
    for row in section.rows:
        if isinstance(row, Field):
            rows.extend(_field_lines(row, column=column, width=width))
        elif isinstance(row, Note):
            rows.extend(_note_lines(row, width=width))
        else:
            rows.extend(_table_lines(row, width=width, unicode_style=unicode_style))
    return Block(heading, rows)


def render_sections(sections: Sequence[Section], *, width: int, unicode_style: bool = False) -> list[Block]:
    """Every non-empty section as its own block, sharing one label column.
    Blocks rather than one flat list so `paginate` can keep a group of facts
    together on a page."""
    column = label_width(sections)
    right_column = _right_label_width(sections)
    return [
        render_section(
            section, column=column, width=width, unicode_style=unicode_style, right_column=right_column
        )
        for section in sections if section.rows
    ]


def paginate(blocks: Sequence[Block], *, budget: int) -> list[list[str]]:
    """Pack blocks into pages of at most `budget` rows, one blank row between
    blocks. A group of facts stays together where that costs little: a block
    that does not fit what is left of a page moves whole to the next one --
    unless that would leave the current page under half full (a three-row
    Identity group alone on page 1 of 4, because the group after it was long),
    in which case the block starts here and continues overleaf. A block that
    runs onto another page repeats its heading there, so the rows still say
    what they are."""
    budget = max(3, budget)
    pages: list[list[str]] = []
    current: list[str] = []

    def _continued(block: Block) -> list[str]:
        if block.heading is None:
            return []
        return [block.heading + colored(" (continued)", fg_color=MUTED_COLOR)]

    for block in blocks:
        lines = block.lines
        gap = 1 if current else 0
        if len(current) + gap + len(lines) <= budget:
            current.extend([""] * gap + lines)
            continue
        heading_rows = 1 if block.heading is not None else 0
        room = budget - len(current) - gap - heading_rows
        keep_whole = len(lines) <= budget and len(current) >= budget * _MIN_PAGE_FILL
        rows = list(block.rows)
        if current and (keep_whole or room < 2):
            pages.append(current)
            current = []
        else:
            # Start the block on this page and carry on overleaf.
            current.extend([""] * gap + ([block.heading] if block.heading is not None else []) + rows[:room])
            rows = rows[room:]
            pages.append(current)
            current = _continued(block)
            if not rows:
                current = []
                continue
        if not current:
            current = [block.heading] if block.heading is not None else []
        while rows:
            space = budget - len(current)
            current.extend(rows[:space])
            rows = rows[space:]
            if rows:
                pages.append(current)
                current = _continued(block)
    if current:
        pages.append(current)
    return pages or [[]]
