"""A picker page fits the terminal it was sized for (issue #538).

At 50-60 columns the picker drew one line more than the terminal had, so
the top of the page scrolled off. `_page_size` budgeted for the nav
block and stopped; the trailer is folded onto the nav's last line only
when it fits there, and otherwise takes its own wrapped line -- and
nothing told the budget which had happened.

80 columns was fine, which is why this went unnoticed: the standard
terminal never hit it. The band that did is the one a phone SSH client
or a split pane lands in.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.net.picker import pick_item

_SGR = re.compile(r"\x1b\[[0-9;]*m")


class FakeSession:
    def __init__(self, width: int, height: int):
        self._keys = iter(["b"])
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

    def drawn_rows(self) -> int:
        plain = _SGR.sub("", "".join(self.written)).replace("\r\n", "\n")
        return len([line for line in plain.split("\n") if line.strip()])


def _render(width: int, height: int, *, description_level="off", sort=False, refresh=False):
    session = FakeSession(width, height)
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


# -- The reported band, and everything around it ----------------------


@pytest.mark.parametrize("width", [40, 50, 60, 70, 80, 100])
@pytest.mark.parametrize("height", [10, 16, 24, 40])
def test_a_page_never_outgrows_its_terminal(width, height):
    """50 and 60 are the reported failures; the rest are here so a fix
    that trades one width for another cannot pass."""
    session = _render(width, height)
    assert session.drawn_rows() <= height, f"{session.drawn_rows()} rows on a {height}-row terminal"


@pytest.mark.parametrize("width", [50, 60, 80])
def test_a_page_fits_with_a_sort_label(width):
    """The sort label lengthens the trailer, which is what pushes it off
    the nav line at widths that would otherwise have been fine."""
    session = _render(width, 24, sort=True)
    assert session.drawn_rows() <= 24


@pytest.mark.parametrize("width", [50, 60, 80])
def test_a_page_fits_with_descriptions_on(width):
    """The `menu_grid` nav branch gives the trailer its own line
    unconditionally, so the accounting gap existed there by
    construction even where it did not visibly overflow."""
    session = _render(width, 24, description_level="brief")
    assert session.drawn_rows() <= 24


def test_a_page_fits_with_both_a_sort_label_and_refresh():
    session = _render(50, 24, sort=True, refresh=True)
    assert session.drawn_rows() <= 24


# -- The measurement itself -------------------------------------------


def test_a_trailer_that_fits_beside_the_nav_costs_nothing():
    from netbbs.net.picker import _trailer_rows

    nav = "[S]earch  [B]ack"
    trailer = "Ctrl-H"
    assert _trailer_rows(nav, trailer, width=80, unicode_style=False, description_level="off") == 0


def test_a_trailer_that_does_not_fit_costs_its_wrapped_rows():
    from netbbs.net.picker import _trailer_rows, _trailer_text

    nav = "[N]ext  [P]rev  [S]earch  [G]oto #  [B]ack"
    trailer = _trailer_text("", False)
    rows = _trailer_rows(nav, trailer, width=50, unicode_style=False, description_level="off")
    assert rows >= 1


def test_only_the_wrapping_of_a_trailer_costs_extra_rows():
    """`_RESERVED_LINES` already budgets one line for the trailer, which
    is why 80 columns never overflowed even though the trailer has its
    own line there. Only the *wrapping* is unpaid for, so that is what
    this counts -- returning the full row count instead would take an
    item off every page at 80 to fix a bug that only exists at 50."""
    from netbbs.net.picker import _trailer_rows, _trailer_text

    trailer = _trailer_text("", False)
    nav = "[N]ext  [P]rev  [S]earch  [G]oto #  [B]ack"

    # One line at 80: already reserved, so it costs nothing extra.
    assert _trailer_rows(nav, trailer, width=80, unicode_style=False, description_level="off") == 0
    # Two lines at 50: one of them is not.
    assert _trailer_rows(nav, trailer, width=50, unicode_style=False, description_level="off") == 1
