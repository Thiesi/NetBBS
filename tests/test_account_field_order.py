"""
Issue #1119: on a user's account screen the fields the cursor walks are one
block, one to a row, in the cursor's own order, and what can't be changed
there is grouped below them -- not paired across two columns, which made
the cursor zigzag between them.
"""

from __future__ import annotations

from netbbs.auth.users import create_user
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from tests.test_admin_flow import FakeSession, _run, _visible, _written_text, db, lane, sysop  # noqa: F401 -- fixtures

_EDITABLE_IN_CURSOR_ORDER = (
    "Level", "Status", "Blocked", "Display name", "Birthdate",
    "Public key", "Password", "Staff", "Can verify identity", "Auto promotion",
)
_READ_ONLY = ("Member since", "Admin actions", "Moderator grants")


def _account_screen(session) -> list[str]:
    text = _written_text(session)
    at = text.index("Member since")
    start = text.rfind("\x1b[2J", 0, at)
    return _visible(text[start: text.index("Choice:", at) + len("Choice:")]).split("\r\n")


def test_editable_fields_are_one_column_in_cursor_order_above_the_read_only_ones(db, lane, sysop):
    set_redraw_in_place_enabled(db, sysop, True)
    create_user(db, "alice", password="hunter2")
    session = FakeSession(["u", "u", "/", "alice", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    rows = [row.strip() for row in _account_screen(session)]

    def row_of(label: str) -> int:
        return next(index for index, row in enumerate(rows) if row.startswith(f"{label}:"))

    editable = [row_of(label) for label in _EDITABLE_IN_CURSOR_ORDER]
    # One to a row, straight down: consecutive rows in the cursor's order.
    assert editable == list(range(editable[0], editable[0] + len(editable)))
    # Nothing read-only shares a row with them, and all of it comes after.
    first_read_only = min(index for index, row in enumerate(rows) if row.startswith(_READ_ONLY))
    assert first_read_only > editable[-1]
    assert not any(label + ":" in rows[index] for index in editable for label in _READ_ONLY)


def test_the_account_screen_still_fits_24_rows(db, lane, sysop):
    set_redraw_in_place_enabled(db, sysop, True)
    create_user(db, "alice", password="hunter2")
    session = FakeSession(["u", "u", "/", "alice", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert len(_account_screen(session)) <= 24
