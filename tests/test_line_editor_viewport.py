"""Editing a value wider than the terminal row (issue #546).

`move_cursor` emits `CSI D`/`CSI C`, which move within one physical row.
A buffer wider than the terminal soft-wraps onto a second row, and from
then on Home, Left, Backspace and every tail redraw clamp at the row
they are on while the logical cursor walks into text a row above: what
is on screen and what will be saved diverge, with nothing to say so.

Issue #529 gated on it -- a value that fit one row was edited inline,
one that did not kept the old "blank = keep" prompt -- which left the
longest descriptions, the exact case that feature was asked for, still
unamendable without retyping. This is the fix underneath: a one-row
window over the buffer that scrolls to follow the cursor.

The claim these tests make is the one that matters for correctness:
**the editor never emits more columns than the row has.** Assert on the
written bytes rather than on an imagined terminal, because that is what
a real terminal is reacting to.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.net.char_input import LineViewport, read_line
from netbbs.rendering.width import display_width

_CSI = re.compile(r"\x1b\[[0-9]*[A-Za-z]")

_WIDTH = 40
_LONG = "The quick brown fox jumps over the lazy dog, twice, and then once more."


class FakeSource:
    """A `ByteSource` over a scripted byte string."""

    def __init__(self, script: bytes):
        self._bytes = list(script)

    async def read_byte(self) -> int | None:
        return self._bytes.pop(0) if self._bytes else ord("\r")

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        return self._bytes.pop(0) if self._bytes else None


class Recorder:
    """Collects what was written, and replays it onto one row."""

    def __init__(self, width: int = _WIDTH):
        self.width = width
        self.chunks: list[str] = []

    async def write(self, text: str) -> None:
        self.chunks.append(text)

    @property
    def raw(self) -> str:
        return "".join(self.chunks)

    def widest_run(self) -> int:
        """The most columns written between cursor repositionings.

        A real terminal wraps when a row fills. Nothing here should ever
        hand it enough to do that.
        """
        return max(
            (display_width(segment) for segment in _CSI.split(self.raw)),
            default=0,
        )


def _edit(script: bytes, *, initial: str = _LONG, width: int = _WIDTH) -> tuple[str, Recorder]:
    recorder = Recorder(width)
    result = asyncio.run(
        read_line(
            FakeSource(script), recorder.write,
            initial=initial, viewport=width,
        )
    )
    return result, recorder


_LEFT = b"\x1b[D"
_HOME = b"\x1b[H"
_END = b"\x1b[F"
_BACKSPACE = b"\x7f"
_ENTER = b"\r"


# -- The row is never overrun -----------------------------------------


def test_opening_a_long_value_draws_one_row_at_most():
    _, recorder = _edit(_ENTER)
    assert recorder.widest_run() <= _WIDTH


@pytest.mark.parametrize("keys", [
    _HOME,
    _END,
    _LEFT * 30,
    _HOME + _LEFT * 5,
    _HOME + _END,
    _LEFT * 20 + _BACKSPACE * 10,
    _BACKSPACE * 40,
])
def test_no_edit_ever_draws_more_than_one_row(keys):
    _, recorder = _edit(keys + _ENTER)
    assert recorder.widest_run() <= _WIDTH


def test_a_value_typed_past_the_row_stays_on_one_row():
    """Not a regression of #529's: a *typed* over-wide line has had this
    from the beginning, and is fixed by the same change."""
    _, recorder = _edit(b"x" * 200 + _ENTER, initial="")
    assert recorder.widest_run() <= _WIDTH


# -- The value is the value -------------------------------------------


def test_an_untouched_value_comes_back_unchanged():
    result, _ = _edit(_ENTER)
    assert result == _LONG


def test_backspace_at_the_end_removes_the_last_character():
    result, _ = _edit(_BACKSPACE + _ENTER)
    assert result == _LONG[:-1]


def test_home_then_backspace_removes_nothing():
    """The defect in one line: Home used to leave the terminal cursor on
    the wrong row, so what the caller then deleted was not what they
    were looking at."""
    result, _ = _edit(_HOME + _BACKSPACE + _ENTER)
    assert result == _LONG


def test_typing_at_the_start_inserts_there():
    result, _ = _edit(_HOME + b"NEW " + _ENTER)
    assert result == "NEW " + _LONG


def test_deleting_from_the_middle_takes_the_right_character():
    result, _ = _edit(_END + _LEFT * 6 + _BACKSPACE + _ENTER)
    assert result == _LONG[:-7] + _LONG[-6:]


# -- The window itself ------------------------------------------------


def test_a_value_that_fits_is_not_scrolled_and_wears_no_markers():
    window = LineViewport(_WIDTH)
    left, visible, right, column = window._layout(list("short"), 5)
    assert (left, visible, right) == ("", "short", "")
    assert column == 5


def test_a_longer_value_shows_where_it_continues():
    window = LineViewport(_WIDTH)
    left, visible, right, _ = window._layout(list(_LONG), 0)
    assert left == " " and right == ">"
    assert display_width(left + visible + right) <= _WIDTH


def test_scrolling_right_shows_where_it_came_from():
    window = LineViewport(_WIDTH)
    left, visible, right, column = window._layout(list(_LONG), len(_LONG))
    assert left == "<"
    assert column <= _WIDTH
    assert visible.endswith(_LONG[-1])


def test_a_narrow_window_scrolls_without_spending_columns_on_markers():
    """At six columns the two markers would be a third of the field."""
    window = LineViewport(6)
    left, visible, right, _ = window._layout(list(_LONG), len(_LONG))
    assert (left, right) == ("", "")
    assert display_width(visible) <= 6


def test_a_wide_character_is_never_split_across_the_edge():
    window = LineViewport(20)
    line = list("東京" * 30)
    left, visible, right, _ = window._layout(line, 40)
    assert display_width(left + visible + right) <= 20


def test_the_cursor_stays_inside_the_window():
    window = LineViewport(_WIDTH)
    for cursor in range(0, len(_LONG) + 1, 7):
        _, _, _, column = window._layout(list(_LONG), cursor)
        assert 0 <= column <= _WIDTH
