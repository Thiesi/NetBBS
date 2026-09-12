"""Columnar picker rows (issue #528).

A SysOp resource list used to render as

    02. (#1) Test - read 100/write 100, open

— one flat string in one flat colour, with nothing aligned down the
page and nowhere to put the age and name gates, so a gated area looked
exactly like an open one. These tests hold the table's three promises:
the columns line up, the fields are separately coloured, and the gates
are visible.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.net.picker import (
    _COLUMN_GUTTER,
    _MAX_TABLE_NAME_WIDTH,
    _MIN_TABLE_NAME_WIDTH,
    _SELECTOR_WIDTH,
    ListColumn,
    _pad_cell,
    _page_size,
    _table_header,
    _table_widths,
    pick_item,
)
from netbbs.rendering import GATE_COLOR, MUTED_COLOR, VALUE_COLOR, fg

_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _SGR.sub("", text)


COLUMNS = [
    ListColumn("read", 7, VALUE_COLOR, align_right=True),
    ListColumn("write", 7, VALUE_COLOR, align_right=True),
    ListColumn("status", 9, VALUE_COLOR),
    ListColumn("gates", 9, GATE_COLOR),
]


def _column_starts(columns, reference_width: int, name_width: int) -> list[int]:
    """Where each column begins, counted the way the renderer builds a
    row -- so a test failure means the row disagrees with the layout,
    not that the test recomputed it differently."""
    offset = _SELECTOR_WIDTH + reference_width + _COLUMN_GUTTER + name_width
    starts = []
    for column in columns:
        offset += _COLUMN_GUTTER
        starts.append(offset)
        offset += column.width
    return starts


class FakeSession:
    def __init__(self, keys, width=80, height=24):
        self._keys = iter(keys)
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "ReLink"
        self.node_name_gradient = None
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self) -> str:
        return next(self._keys, "b")

    async def read_line(self, echo: bool = True) -> str:
        return next(self._keys, "b")

    @property
    def output(self) -> str:
        return "".join(self.written)

    def lines(self) -> list[str]:
        return _plain(self.output).split("\n")


class Item:
    def __init__(self, id_, name, cells):
        self.id = id_
        self.name = name
        self.cells = cells


ITEMS = [
    Item(1, "NetBBS releases", ["0", "100", "open", ("-", MUTED_COLOR)]),
    Item(3, "Test", ["100", "100", "open", ("18+ name", GATE_COLOR)]),
    Item(17, "Uploads", ["inherit", "inherit", "moderated", ("name+", GATE_COLOR)]),
]


def _render(items=ITEMS, width=80, height=24, **kwargs):
    session = FakeSession(["b"], width=width, height=height)
    asyncio.run(
        pick_item(
            session,
            items,
            name_of=lambda i: i.name,
            stable_id_of=lambda i: i.id,
            description_of=lambda i: "read 100/write 100, open",
            title="File areas",
            empty_message="none",
            **kwargs,
        )
    )
    return session


def _table(**kwargs):
    return _render(columns=COLUMNS, column_values_of=lambda i: i.cells, **kwargs)


# -- Layout maths -----------------------------------------------------


def test_pad_cell_measures_display_width_not_character_count():
    # Two columns per CJK character: padding by len() would leave this
    # cell three columns too wide and shift everything after it.
    assert _pad_cell("日本", 6, align_right=False) == "日本  "
    assert _pad_cell("ab", 4, align_right=True) == "  ab"


def test_pad_cell_truncates_an_oversized_cell_to_exactly_its_width():
    from netbbs.rendering import display_width

    assert display_width(_pad_cell("a very long name indeed", 8, align_right=False)) == 8


def test_table_widths_reserve_every_column_and_gutter():
    reference, name = _table_widths(80, COLUMNS, 2)
    fixed = _SELECTOR_WIDTH + reference + _COLUMN_GUTTER + sum(c.width + _COLUMN_GUTTER for c in COLUMNS)
    assert fixed + name == 80


def test_table_widths_give_up_rather_than_squeeze_the_name_away():
    assert _table_widths(40, COLUMNS, 2) is None
    # The boundary itself still produces a usable name column.
    fixed = _SELECTOR_WIDTH + 2 + _COLUMN_GUTTER + sum(c.width + _COLUMN_GUTTER for c in COLUMNS)
    assert _table_widths(fixed + _MIN_TABLE_NAME_WIDTH, COLUMNS, 2)[1] == _MIN_TABLE_NAME_WIDTH


def test_table_stops_spanning_a_very_wide_terminal():
    assert _table_widths(200, COLUMNS, 2)[1] == _MAX_TABLE_NAME_WIDTH


def test_reference_column_is_at_least_as_wide_as_its_heading():
    # A list whose ids are all single digits still needs room for "#".
    assert _table_widths(80, COLUMNS, 1)[0] == 1
    assert "#" in _plain(_table_header(COLUMNS, reference_width=1, name_width=20))


# -- The rendered table -----------------------------------------------


def test_header_row_names_every_column():
    header = [line for line in _table().lines() if "NAME" in line][0]
    for column in COLUMNS:
        assert column.header.upper() in header


def test_every_row_puts_its_cells_at_the_same_offset():
    """The promise the report was actually asking for: values form
    columns instead of starting wherever the preceding text ended."""
    session = _table()
    reference, name_width = _table_widths(80, COLUMNS, len("17"))
    starts = _column_starts(COLUMNS, reference, name_width)

    lines = session.lines()
    header = [line for line in lines if "NAME" in line][0]
    rows = [line for line in lines if re.match(r"^\s{2}\d\d\. ", line)]
    assert len(rows) == 3

    for column, start in zip(COLUMNS, starts):
        assert header[start : start + column.width].strip() == column.header.upper()
    for row, item in zip(rows, ITEMS):
        for column, start, cell in zip(COLUMNS, starts, item.cells):
            expected = cell[0] if isinstance(cell, tuple) else cell
            assert row[start : start + column.width].strip() == expected


def test_a_long_name_truncates_instead_of_shifting_the_columns():
    items = [Item(1, "a" * 200, ["0", "0", "open", ("-", MUTED_COLOR)])]
    session = _table(items=items)
    reference, name_width = _table_widths(80, COLUMNS, 1)
    starts = _column_starts(COLUMNS, reference, name_width)
    row = [line for line in session.lines() if re.match(r"^\s{2}\d\d\. ", line)][0]
    assert row[starts[0] : starts[0] + COLUMNS[0].width].strip() == "0"


def test_a_cjk_name_does_not_shift_the_columns_after_it():
    items = [
        Item(1, "日本語掲示板", ["5", "6", "open", ("-", MUTED_COLOR)]),
        Item(2, "plain", ["5", "6", "open", ("-", MUTED_COLOR)]),
    ]
    from netbbs.rendering import display_width

    session = _table(items=items)
    rows = [line for line in session.lines() if re.match(r"^\s{2}\d\d\. ", line)]
    # Compared in display columns, not character offsets: six CJK
    # characters occupy twelve columns, so the two rows correctly
    # disagree by six *characters* while landing on the same column.
    # Measuring this the other way is the same mistake `_pad_cell`
    # exists to avoid.
    assert display_width(rows[0][: rows[0].index("open")]) == display_width(
        rows[1][: rows[1].index("open")]
    )


# -- Colour -----------------------------------------------------------


def test_fields_are_coloured_independently():
    """Item 7 of the report: levels should not be the same colour as
    the labels above them."""
    output = _table().output
    assert fg(VALUE_COLOR) in output  # the level/status cells
    assert fg(GATE_COLOR) in output   # a row that actually carries a gate


def test_an_ungated_row_stays_quiet():
    """Only rows with a real gate light up -- otherwise the column is
    just noise on every line."""
    items = [Item(1, "Open area", ["0", "0", "open", ("-", MUTED_COLOR)])]
    assert fg(GATE_COLOR) not in _table(items=items).output


# -- Falling back, and leaving everyone else alone --------------------


def test_a_narrow_terminal_falls_back_to_the_flat_description():
    session = _table(width=50)
    text = session.output
    assert "NAME" not in _plain(text)
    assert "read 100/write 100, open" in text
    assert "(#1)" in text  # the flat form's own permanent reference


def test_a_caller_without_columns_renders_the_flat_row():
    session = _render()
    plain = _plain(session.output)
    assert "NAME" not in plain
    # Unchanged flat form, including its own id padding (the widest id
    # on this page is "17", so "1" is padded to match).
    assert "  01. (#1)  NetBBS releases - read 100/write 100, open" in plain


def test_columns_and_values_must_be_supplied_together():
    with pytest.raises(ValueError):
        _render(columns=COLUMNS)
    with pytest.raises(ValueError):
        _render(column_values_of=lambda i: i.cells)


def test_short_cell_lists_lose_a_value_not_the_alignment():
    """A caller returning too few cells should not silently drag every
    following row out of its column."""
    items = [Item(1, "Half", ["0", "100"])]
    session = _table(items=items)
    row = [line for line in session.lines() if re.match(r"^\s{2}\d\d\. ", line)][0]
    reference, name_width = _table_widths(80, COLUMNS, 1)
    starts = _column_starts(COLUMNS, reference, name_width)
    assert row[starts[1] : starts[1] + COLUMNS[1].width].strip() == "100"


# -- Page geometry ----------------------------------------------------


def test_the_header_row_is_paid_for_out_of_the_page_size():
    """The header is a real line on a real terminal. Not reserving it
    would push the last item of every full page off the bottom."""
    session = FakeSession(["b"], height=24)
    without = _page_size(session, None, "off", header_lines=0)
    with_header = _page_size(session, None, "off", header_lines=1)
    assert with_header == without - 1


def test_a_highlighted_row_overrides_every_column_colour():
    """`pick_item`'s standing rule, applied to columns too: selection
    state wins over field identity. Distinguishable per-field colours
    are a normal-row affordance; a row under the cursor is already
    unambiguous and should read as one highlighted run."""
    from netbbs.net.char_input import EditorKey, EditorKeyKind

    class Arrowing(FakeSession):
        def __init__(self):
            super().__init__(["b"])
            self._editor = iter([EditorKey(EditorKeyKind.DOWN), EditorKey(EditorKeyKind.CHAR, char="b")])

        async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
            return next(self._editor)

    session = Arrowing()
    asyncio.run(
        pick_item(
            session, ITEMS,
            name_of=lambda i: i.name, stable_id_of=lambda i: i.id,
            columns=COLUMNS, column_values_of=lambda i: i.cells,
            title="File areas", empty_message="none",
        )
    )
    highlighted = [line for line in session.lines() if line.startswith("> ")]
    assert highlighted, "expected the cursor to land on a row"
    # The gate colour belongs to a normal row's gates cell; under the
    # cursor the whole row is the highlight colour instead.
    row_start = session.output.index("> 01.")
    row_end = session.output.index("\n", row_start)
    assert fg(GATE_COLOR) not in session.output[row_start:row_end]


def test_name_segments_and_columns_cannot_both_own_the_name():
    with pytest.raises(ValueError):
        _render(
            columns=COLUMNS,
            column_values_of=lambda i: i.cells,
            name_segments_of=lambda i: [(i.name, VALUE_COLOR)],
        )


def test_a_narrow_terminal_does_not_reserve_a_row_for_a_header_it_never_draws():
    """The fallback renders no heading, so reserving a line for one
    would cost the page an item for nothing."""
    wide = FakeSession(["b"], width=80, height=24)
    narrow = FakeSession(["b"], width=50, height=24)
    fits = _table_widths(wide.terminal_width, COLUMNS, 1) is not None
    does_not = _table_widths(narrow.terminal_width, COLUMNS, 1) is None
    assert fits and does_not
    assert _page_size(narrow, None, "off", header_lines=0) == _page_size(
        wide, None, "off", header_lines=1
    ) + 1
