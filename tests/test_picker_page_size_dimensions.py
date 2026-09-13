"""`_page_size` must honour the dimensions it is handed.

#528 froze the terminal's dimensions for the length of one render, so a
browser resize arriving mid-render could not leave the page size and the
row layout disagreeing. `_page_size` grew `width`/`height` parameters for
that -- and then ignored them, reading `session.terminal_width` and
`session.terminal_height` straight back out. Half the fix was live.

The patch script that was supposed to rewrite the body matched nothing
and said nothing, because its anchor count was never asserted. These
tests are the thing that would have caught it.
"""

from __future__ import annotations

from netbbs.net.picker import _page_size


class FakeSession:
    def __init__(self, width: int = 80, height: int = 24):
        self.terminal_width = width
        self.terminal_height = height


def test_the_height_argument_is_what_sizes_the_page():
    """The defect in its simplest form: a taller `height` has to produce
    a bigger page, whatever the session currently says."""
    session = FakeSession(height=24)
    small = _page_size(session, None, "off", height=20)
    large = _page_size(session, None, "off", height=40)
    assert large > small


def test_a_passed_height_wins_over_the_live_session():
    session = FakeSession(height=24)
    assert _page_size(session, None, "off", height=40) == _page_size(
        FakeSession(height=40), None, "off"
    )


def test_a_passed_width_wins_over_the_live_session():
    """Width reaches the calculation through the nav row's rendered
    height, which is why a narrow width can cost the page a line."""
    narrow = FakeSession(width=200)
    assert _page_size(narrow, None, "brief", width=40) == _page_size(
        FakeSession(width=40), None, "brief"
    )


def test_omitting_them_still_reads_the_session():
    """Every caller outside a render passes nothing and must keep
    getting live values."""
    assert _page_size(FakeSession(height=24), None, "off") == _page_size(
        FakeSession(height=24), None, "off", height=24
    )


def test_the_header_reservation_still_costs_exactly_one_row():
    session = FakeSession()
    assert _page_size(session, None, "off", header_lines=1) == _page_size(
        session, None, "off", header_lines=0
    ) - 1
