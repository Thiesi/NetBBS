"""A picker page fits the terminal it was sized for (issue #538).

The page budget and the render disagreed about how many rows a page
costs, so the top of the page scrolled off. Two separate errors, found
in two passes:

1. The trailer is folded onto the nav's last line only when it fits
   there, and otherwise takes its own wrapped line -- and nothing told
   the budget which had happened. Reported at 50-60 columns, where the
   boilerplate alone is 63 columns wide.
2. The budget was also one short of the chrome it always draws, and the
   first fix charged the trailer's *wrapping* only, on the belief that
   one trailer line was already paid for. Measuring said otherwise: with
   descriptions off the picker drew 26 rows on a 24-row terminal at
   every width, 80 included.

With descriptions on it looked fine on page 1, because the nav block is
reserved at its worst case (both Next and Prev) while page 1 renders
without Prev -- two rows of slack that covered the shortfall until page
2, where the nav really is that tall.

**These tests count every physical row**, blank ones included, and wrap
long lines the way a terminal does. The first version of this file
discarded whitespace-only rows, which is exactly how it passed while the
page was still two rows over.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.net.picker import pick_item
from netbbs.rendering.width import display_width

_SGR = re.compile("\x1b" + r"\[[0-9;]*m")
_ANSI = re.compile("\x1b" + r"\[[0-9;]*[A-Za-z]")
_CLEAR = "\x1b" + "[2J"


class FakeSession:
    def __init__(self, width: int, height: int, keys: list[str] | None = None):
        self._keys = iter(keys or ["b"])
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

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return next(self._keys, "b")

    def rows_on_screen(self) -> int:
        """Every row the last drawn page occupies.

        Blank rows count -- they take a line of the terminal like any
        other. So does wrapping: a logical line wider than the terminal
        becomes two rows. Everything before the last screen clear does
        not count, being no longer on screen.
        """
        raw = "".join(self.written).rsplit(_CLEAR, 1)[-1]
        plain = _ANSI.sub("", _SGR.sub("", raw)).replace("\r\n", "\n")
        # Without redraw-in-place a new page is appended below the last
        # one rather than replacing it, so count from where the final
        # page's own title begins. The row above it is that page's
        # leading blank, which belongs to it too.
        title = plain.rfind(self.node_display_name + " /")
        if title > 0:
            plain = "\n" + plain[plain.rfind("\n", 0, title) :].lstrip("\n")
        rows = sum(
            max(1, -(-display_width(line) // self.terminal_width))
            for line in plain.split("\n")
        )
        # A trailing newline ends the last row rather than opening a new one.
        return rows - 1 if plain.endswith("\n") else rows


def _render(width, height, *, description_level="off", sort=False, refresh=False, keys=None):
    session = FakeSession(width, height, keys)
    kwargs = {}
    if sort:
        kwargs["sort_label"] = lambda: "Activity"
        kwargs["on_sort"] = None
    if refresh:
        async def _refresh():
            return list(range(1, 80))
        kwargs["refresh"] = _refresh
    asyncio.run(
        pick_item(
            session, list(range(1, 80)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
            description_level=description_level, **kwargs,
        )
    )
    return session


# -- Every width, not just the reported band ---------------------------


@pytest.mark.parametrize("width", [40, 50, 60, 63, 70, 80, 100])
@pytest.mark.parametrize("height", [10, 16, 20, 24, 40])
def test_a_page_never_outgrows_its_terminal(width, height):
    session = _render(width, height)
    assert session.rows_on_screen() <= height, (
        f"{session.rows_on_screen()} rows on a {height}-row terminal"
    )


@pytest.mark.parametrize("width", [40, 50, 60, 63, 80])
@pytest.mark.parametrize("height", [16, 20, 24])
def test_a_page_fits_with_descriptions_on(width, height):
    session = _render(width, height, description_level="brief")
    assert session.rows_on_screen() <= height


@pytest.mark.parametrize("width", [50, 60, 80])
def test_a_page_fits_with_a_sort_label(width):
    """The sort label lengthens the trailer, which is what pushes it off
    the nav line at widths that would otherwise have been fine."""
    session = _render(width, 24, sort=True)
    assert session.rows_on_screen() <= 24


def test_a_page_fits_with_both_a_sort_label_and_refresh():
    session = _render(50, 24, sort=True, refresh=True)
    assert session.rows_on_screen() <= 24


# -- The page that hid the bug -----------------------------------------


@pytest.mark.parametrize("level", ["off", "brief"])
@pytest.mark.parametrize("width", [50, 80])
def test_the_second_page_fits_too(level, width):
    """Page 1 draws no `[P]rev`, so its nav is one entry shorter than the
    worst case the budget reserves. That slack hid the shortfall with
    descriptions on until somebody pressed [N]."""
    session = _render(width, 24, description_level=level, keys=["n", "b"])
    assert session.rows_on_screen() <= 24


# -- The measurement itself --------------------------------------------


def test_a_trailer_that_fits_beside_the_nav_costs_nothing():
    from netbbs.net.picker import _trailer_rows

    assert _trailer_rows(
        "[S]earch  [B]ack", "Ctrl-H",
        width=80, unicode_style=False, description_level="off",
    ) == 0


def test_a_trailer_on_its_own_line_costs_every_row_it_takes():
    """Including the first. The earlier version of this returned rows
    beyond the first, on the reasoning that the reserve already paid for
    one -- it did not, and that was two of the rows the page was over."""
    from netbbs.net.picker import _trailer_rows, _trailer_text

    trailer = _trailer_text("", False)
    nav = "[N]ext  [P]rev  [S]earch  [G]oto #  [B]ack"
    assert _trailer_rows(nav, trailer, width=80, unicode_style=False, description_level="off") == 1
    assert _trailer_rows(nav, trailer, width=50, unicode_style=False, description_level="off") == 2


def test_the_descriptive_nav_floor_counts_what_the_page_budget_counts():
    """`_render_nav` takes the taller nav only while at least
    `_MIN_PAGE_SIZE_FOR_DESCRIPTIVE_NAV` items still fit. Answering that
    with a different sum than the one that sizes the page meant accepting
    it on the promise of five items and delivering three."""
    from netbbs.net.picker import (
        _MIN_PAGE_SIZE_FOR_DESCRIPTIVE_NAV, _page_size, _render_nav, _trailer_text,
    )

    trailer = _trailer_text("", False)
    for width in (40, 50, 63, 80):
        for height in (16, 20, 24):
            session = FakeSession(width, height)
            nav = _render_nav(
                session, None, "brief", width=width, height=height,
                trailer=trailer, unicode_style=False,
            )
            size = _page_size(
                session, None, "brief", width=width, height=height,
                trailer=trailer, unicode_style=False,
            )
            # Not "does it span rows": `action_bar` wraps to two rows at
            # 40 columns all by itself. The descriptive form is the one
            # that carries each entry's brief under it.
            descriptive = "Return without picking" in nav
            if descriptive and size > 1:
                assert size >= _MIN_PAGE_SIZE_FOR_DESCRIPTIVE_NAV, (
                    f"{width}x{height}: descriptive nav left only {size} items"
                )


# -- What the second review round found --------------------------------


@pytest.mark.parametrize("width,height", [(120, 16), (100, 16), (120, 20), (80, 16)])
def test_the_tallest_nav_is_the_one_reserved_for(width, height):
    """"Worst case" is the tallest *layout*, not the longest entry list
    (Codex review). `menu_grid` packs into more columns as the list
    grows, so at 120x16 the six-entry form is three columns and four
    rows while the five-entry one is a single column of ten -- the
    fuller list is the shorter layout. Reserving from entry count alone
    accepted the descriptive nav on a four-row estimate and then drew
    ten."""
    session = _render(width, height, description_level="brief", keys=["n", "b"])
    assert session.rows_on_screen() <= height


def test_create_counts_toward_the_reservation_too():
    """The floor measured a nav without `[C]reate` while the render drew
    one with it."""
    from netbbs.net.picker import _nav_entries

    with_create = _nav_entries(None, on_create=lambda: None)
    without = _nav_entries(None)
    assert len(with_create) == len(without) + 1


def test_opening_on_a_stored_item_lands_on_a_page_that_holds_it():
    """`start_stable_id` measured a page size in one generation and then
    rendered in the next, re-reading a `sort_label` whose text can
    differ between reads -- and a label that wraps differently gives a
    different page size, so a target placed at index 15 could be
    highlighted on a page that turned out to hold 14. Enter then raised
    `IndexError` (Codex review)."""
    labels = iter(["Activity", "Activity, newest first, including every archived entry"])

    session = FakeSession(80, 24, ["\r"])
    asyncio.run(
        pick_item(
            session, list(range(1, 80)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
            sort_label=lambda: next(labels, "Activity"), on_sort=None,
            start_stable_id=16,
        )
    )
    # No IndexError, and the page it opened on is the page it drew.
    assert session.rows_on_screen() <= 24


# -- What the third review round found ---------------------------------


@pytest.mark.parametrize("width,height", [(120, 24), (120, 20), (100, 24), (80, 20)])
@pytest.mark.parametrize("count", [5, 79])
def test_the_budget_and_the_floor_measure_the_same_nav(width, height, count):
    """They used to measure different things: the floor took the tallest
    shape, the page budget took the both-Next-and-Prev default. At
    120x24 that difference was six rows off the bottom (Codex review).

    `count=5` is a single-page list, whose neither-Next-nor-Prev nav was
    excluded from "tallest" on the assumption it must be shorter --
    `menu_grid` is non-monotonic across its column threshold, so it can
    be ten rows where the six-entry form is four."""
    session = FakeSession(width, height)
    asyncio.run(
        pick_item(
            session, list(range(1, count + 1)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
            description_level="brief", sort_label=lambda: "Activity", on_sort=None,
        )
    )
    assert session.rows_on_screen() <= height


def test_a_shrinking_page_does_not_keep_a_highlight_it_lost():
    """`sort_label` is read fresh per render by contract, so a label
    that wraps differently shrinks the page under a highlight taken from
    the previous one -- and `page_items[highlighted]` then raised
    `IndexError` on Enter (Codex review)."""
    labels = iter(["Activity", "Activity, newest first, with every archived entry included too"])
    session = FakeSession(80, 24, [_UP := "\x1b[A", "\r"])
    asyncio.run(
        pick_item(
            session, list(range(1, 80)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
            sort_label=lambda: next(labels, "Activity"), on_sort=None,
        )
    )
    assert session.rows_on_screen() <= 24
