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

from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.picker import pick_item
from netbbs.rendering.width import display_width

#: Scripted keys this fake turns into real editor events rather than
#: characters. Without `read_editor_key`, `pick_item`'s base
#: `Session.read_editor_key` never sees them and "\x1b[A" arrives as an
#: ordinary keystroke -- which is how a highlight regression test can
#: pass while never pressing Up at all (Codex review).
_EDITOR_KEYS = {
    "UP": EditorKeyKind.UP,
    "DOWN": EditorKeyKind.DOWN,
    "ENTER": EditorKeyKind.ENTER,
}

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

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        raw = next(self._keys, "b")
        if raw in _EDITOR_KEYS:
            return EditorKey(_EDITOR_KEYS[raw])
        return EditorKey(EditorKeyKind.CHAR, char=raw)

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

    # "ENTER", not "\r": this fake maps the token, and a bare carriage
    # return arrives as an ordinary character the picker rejects -- so
    # the first version of this never indexed the highlighted row at
    # all, and would have passed with the `IndexError` still there
    # (Codex review).
    session = FakeSession(80, 24, ["ENTER"])
    result = asyncio.run(
        pick_item(
            session, list(range(1, 80)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
            sort_label=lambda: next(labels, "Activity"), on_sort=None,
            start_stable_id=16,
        )
    )
    # It opened highlighted on item 16, so Enter selects exactly that --
    # which is what proves the highlight index was inside the page that
    # was actually drawn.
    assert result == 16, f"selected {result!r}"
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
    session = FakeSession(80, 24, ["UP", "ENTER"])
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


def test_falling_back_to_the_compact_nav_does_not_cost_the_page():
    """A compact `action_bar` wraps to two rows by itself at 40 columns,
    so "does the nav contain a line break" is not a test for "is it the
    descriptive form". Inferring it that way priced a picker that had
    correctly fallen back to compact as if it were ten rows of
    descriptive nav, cutting a page that fits ten choices to two (Codex
    review)."""
    from netbbs.net.picker import (
        _MIN_PAGE_SIZE_FOR_DESCRIPTIVE_NAV, _page_size, _render_nav, _trailer_text,
    )

    session = FakeSession(40, 20)
    trailer = _trailer_text("", False)
    nav = _render_nav(
        session, None, "brief", width=40, height=20,
        trailer=trailer, unicode_style=False,
    )
    assert "Return without picking" not in nav, "this width falls back to the compact bar"
    assert "\r\n" in nav, "and that bar wraps, which is what made the guess wrong"

    size = _page_size(
        session, None, "brief", width=40, height=20,
        trailer=trailer, unicode_style=False,
    )
    assert size >= _MIN_PAGE_SIZE_FOR_DESCRIPTIVE_NAV, f"only {size} choices fit"


@pytest.mark.parametrize("width,height", [(120, 20), (100, 24), (80, 24)])
def test_a_single_page_list_keeps_its_descriptions(width, height):
    """A list that fits one page can never draw Next or Prev, so
    reserving rows for those shapes costs it the descriptive nav for no
    reason -- four items on a 120x20 terminal fit in nineteen rows with
    an eight-row nav, and were priced against a ten-row shape they
    cannot render (Codex review)."""
    session = FakeSession(width, height)
    asyncio.run(
        pick_item(
            session, [1, 2, 3, 4],
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none", description_level="brief",
        )
    )
    text = "".join(session.written)
    assert "Return without picking" in text, "the descriptions the caller asked for"
    assert session.rows_on_screen() <= height


@pytest.mark.parametrize("width,height", [(120, 20), (80, 24), (50, 24), (40, 20)])
def test_a_paginated_list_still_fits(width, height):
    """The other half of the same trade: the moment a list needs pages,
    it is priced against the nav those pages will draw."""
    session = FakeSession(width, height, ["n", "b"])
    asyncio.run(
        pick_item(
            session, list(range(1, 80)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none", description_level="brief",
        )
    )
    assert session.rows_on_screen() <= height


def test_a_shorter_last_page_does_not_keep_a_highlight_beyond_it():
    """The guard compared against the nominal page size, not the slice
    that was actually taken -- and the last page is shorter than a full
    one, so an index inside `page_size` can still be outside
    `page_items`. That is `IndexError` on Enter, which is exactly what
    the guard exists to stop (Codex review)."""
    labels = iter([
        "Activity",
        "Activity, newest first, with every archived entry and every note included as well",
        "Activity",
    ])
    session = FakeSession(80, 24, ["n", "UP", "ENTER"])
    result = asyncio.run(
        pick_item(
            session, list(range(1, 21)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
            sort_label=lambda: next(labels, "Activity"), on_sort=None,
        )
    )
    # No IndexError. Whatever it returned, it returned something on the
    # page that was actually drawn.
    assert result is None or result in range(1, 21)


def test_seven_items_fit_the_terminal_they_were_sized_for():
    """The two-pass could answer inconsistently: a list that fits a
    paginated page but not a single-page one returned the paginated
    size, which put every item on one page -- so the render chose the
    single-page nav that had *just* been measured as not fitting. Seven
    items at 40x20 drew 21 rows (Codex review)."""
    session = FakeSession(40, 20)
    asyncio.run(
        pick_item(
            session, list(range(1, 8)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none", description_level="brief",
        )
    )
    assert session.rows_on_screen() <= 20


def test_paging_never_repeats_or_skips_a_row_when_the_page_size_changes():
    """`page_index * page_size` means one thing only while `page_size`
    holds still, and it does not: a `sort_label` that wraps changes the
    trailer's height and so the page size. At 80x24 with a label
    alternating short and two-line, page 1 held items 1-16, page 2
    started at 16, and page 3 at 33 -- [N]ext twice showed item 16 twice
    and never showed 31 or 32 (Codex review).

    The claim is about the *pages*, so this reads each rendered page's
    rows and asserts each page starts exactly where the last one ended.
    """
    labels = iter([
        "Activity",
        "Activity, newest first, with every archived entry and every note as well",
        "Activity",
        "Activity, newest first, with every archived entry and every note as well",
    ])
    session = FakeSession(80, 24, ["n", "n", "b"])
    asyncio.run(
        pick_item(
            session, list(range(1, 41)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
            sort_label=lambda: next(labels, "Activity"), on_sort=None,
        )
    )

    plain = _ANSI.sub("", _SGR.sub("", "".join(session.written)))
    # One group of rows per render: the title line starts each page.
    pages = [
        [int(n) for n in re.findall(r"area (\d+)", block)]
        for block in plain.split("ReLink /")[1:]
    ]
    pages = [rows for rows in pages if rows]
    assert len(pages) >= 3, f"expected three renders, got {len(pages)}"

    for earlier, later in zip(pages, pages[1:]):
        assert later[0] == earlier[-1] + 1, (
            f"page starting {later[0]} follows a page ending {earlier[-1]}: "
            f"{'repeats' if later[0] <= earlier[-1] else 'skips'} rows"
        )


def _pages_drawn(session) -> list[list[int]]:
    """The rows of each rendered page, in the order they were drawn."""
    plain = _ANSI.sub("", _SGR.sub("", "".join(session.written)))
    pages = [
        [int(n) for n in re.findall(r"area (\d+)", block)]
        for block in plain.split("ReLink /")[1:]
    ]
    return [rows for rows in pages if rows]


def test_next_advances_past_the_page_that_was_actually_drawn():
    """[N]ext recomputed the page size from live geometry instead of
    advancing past the rendered slice, so a terminal that shrank between
    the render and the keypress made the next page start inside the last
    one (Codex review)."""
    class Shrinking(FakeSession):
        async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
            # The caller resizes while looking at the page, before
            # pressing anything.
            #
            # Overriding `read_editor_key`, not `read_key`, because that
            # is what the picker's dispatch actually reads -- the first
            # version of this test overrode the other one and therefore
            # never resized at all, which is how it passed against the
            # code it was written to catch.
            self.terminal_height = 22
            return await FakeSession.read_editor_key(self)

    session = Shrinking(80, 24, ["n", "b"])
    asyncio.run(
        pick_item(
            session, list(range(1, 41)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
        )
    )
    pages = _pages_drawn(session)
    assert len(pages) >= 2
    assert pages[1][0] == pages[0][-1] + 1, (
        f"page 2 starts at {pages[1][0]} after a page ending {pages[0][-1]}"
    )


def test_prev_returns_to_the_page_it_came_from():
    """Subtracting the current page size does not recover the previous
    page's start once the size has changed -- [P]rev landed between two
    pages and redrew rows the caller had already passed (Codex
    review)."""
    labels = iter([
        "Activity",
        "Activity, newest first, with every archived entry and every note as well",
        "Activity",
        "Activity, newest first, with every archived entry and every note as well",
    ])
    session = FakeSession(80, 24, ["n", "p", "b"])
    asyncio.run(
        pick_item(
            session, list(range(1, 41)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
            sort_label=lambda: next(labels, "Activity"), on_sort=None,
        )
    )
    pages = _pages_drawn(session)
    assert len(pages) >= 3, f"expected first, next and prev renders, got {len(pages)}"
    assert pages[2][0] == pages[0][0], (
        f"[P]rev returned to {pages[2][0]}, not to {pages[0][0]} where it started"
    )


def test_the_page_number_counts_pages_walked_not_rows_divided():
    """`page_start // page_size` renames the page under the caller when
    the size changes: an 80x22 picker showing "page 1/2" grew to 80x24,
    and [N]ext then showed the last item as "page 1/1" (Codex review)."""
    class Growing(FakeSession):
        async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
            self.terminal_height = 24
            return await FakeSession.read_editor_key(self)

    session = Growing(80, 22, ["n", "b"])
    asyncio.run(
        pick_item(
            session, list(range(1, 16)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
        )
    )
    plain = _ANSI.sub("", _SGR.sub("", "".join(session.written)))
    labels = re.findall(r"page (\d+)/(\d+)", plain)
    assert labels, "no page label drawn"
    ordinals = [int(current) for current, _ in labels]
    assert ordinals == sorted(ordinals), f"the page number went backwards: {ordinals}"
    assert ordinals[-1] >= 2, f"[N]ext left the ordinal at {ordinals[-1]}"


def test_opening_on_a_stored_item_leaves_prev_a_trail():
    """Placement recorded no history, so [P]rev derived the previous
    boundary from live geometry: a picker opened on item 20 and then
    grown showed rows 15-28, and [P]rev showed 1-16 -- repeating two
    (Codex review)."""
    class Growing(FakeSession):
        async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
            # The terminal grows before [P]rev, which is what made the
            # subtraction land somewhere the caller had never been.
            self.terminal_height = 24
            return await FakeSession.read_editor_key(self)

    session = Growing(80, 22, ["p", "b"])
    asyncio.run(
        pick_item(
            session, list(range(1, 41)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none", start_stable_id=35,
        )
    )
    pages = _pages_drawn(session)
    assert len(pages) >= 2, "expected the opening page and the one [P]rev went to"
    # Placement walked boundaries 0 and 14 to reach 28, so [P]rev returns
    # to 14 -- row 15. Deriving it from the grown geometry gave
    # 28 - 16 = 12 instead: a boundary the caller had never been on.
    assert pages[1][0] == pages[0][0] - 14, (
        f"[P]rev went to row {pages[1][0]}, not to the boundary before {pages[0][0]}"
    )


def test_the_denominator_agrees_with_whether_next_is_offered():
    """The page total was estimated by dividing the whole list by the
    *current* page size, while the ordinal counted the pages actually
    walked -- so after a resize mid-browse the two disagreed (issue
    #558). 31 items at 80x22 page at 14; grown to 80x24 and paged once,
    the label read "page 2/2" while item 31 was still there and [N]ext
    still worked and still went to it.

    Asserted as the invariant rather than as the one arithmetic answer:
    a page that offers [N]ext is not the last page, and a page that is
    the last page does not offer it. Both are drawn from the same state,
    so nothing but a bug can separate them.
    """
    class Growing(FakeSession):
        async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
            self.terminal_height = 24
            return await FakeSession.read_editor_key(self)

    session = Growing(80, 22, ["n", "b"])
    asyncio.run(
        pick_item(
            session, list(range(1, 32)),
            name_of=lambda i: f"area {i}", stable_id_of=lambda i: i,
            description_of=lambda i: "read 0/write 0, open",
            title="File areas", empty_message="none",
        )
    )
    plain = _ANSI.sub("", _SGR.sub("", "".join(session.written)))
    checked = 0
    for block in plain.split("ReLink /")[1:]:
        label = re.search(r"page (\d+)/(\d+)", block)
        if label is None:
            continue
        checked += 1
        current, total = int(label.group(1)), int(label.group(2))
        offers_next = "[N]ext" in block
        assert (current < total) == offers_next, (
            f"page {current}/{total} "
            f"{'offers' if offers_next else 'does not offer'} [N]ext"
        )
    assert checked >= 2, f"expected the opening page and the one [N]ext drew, saw {checked}"
