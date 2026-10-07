"""
Issue #1119: an outcome is drawn on the screen that follows it, not under
that screen's clear. A type-the-name confirmation that does not match says
what was typed and that nothing happened -- a warning, not a muted
"Cancelled." -- unless the answer was empty, which is the plain cancel the
prompt offers.
"""

from __future__ import annotations

from netbbs.boards.boards import create_board, get_board_by_name
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from tests.test_admin_flow import FakeSession, _run, _visible, _written_text, db, lane, sysop  # noqa: F401 -- fixtures

CLEAR = "\x1b[2J"


def _after_last_clear_before(text: str, marker: str) -> str:
    """The screen drawn after `marker`: from the clear that follows it to
    that screen's prompt."""
    tail = text[text.index(marker):]
    screen = tail[tail.index(CLEAR):]
    return screen[: screen.index("Choice:")]


def test_a_mistyped_delete_confirmation_is_a_warning_on_the_next_screen(db, lane, sysop):
    set_redraw_in_place_enabled(db, sysop, True)
    create_board(db, "Pen Repair", creator=sysop)
    # Content > Message boards > List > 01 > [R]emove, a wrong name, Back out.
    session = FakeSession(["m", "m", "l", "0", "1", "r", "Pen Repar", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    screen = _visible(_after_last_clear_before(_written_text(session), "to confirm"))
    assert "! Cancelled: 'Pen Repar' is not 'Pen Repair'. Nothing was deleted." in " ".join(screen.split())
    assert get_board_by_name(db, "Pen Repair") is not None


def test_an_empty_delete_confirmation_is_a_plain_cancel(db, lane, sysop):
    set_redraw_in_place_enabled(db, sysop, True)
    create_board(db, "Pen Repair", creator=sysop)
    session = FakeSession(["m", "m", "l", "0", "1", "r", "", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    screen = _visible(_after_last_clear_before(_written_text(session), "to confirm"))
    assert "Cancelled." in screen and "is not" not in screen
    assert get_board_by_name(db, "Pen Repair") is not None
