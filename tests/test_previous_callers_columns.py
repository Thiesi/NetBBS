"""Previous Callers lines its columns up (issue #535).

The name field had no fixed width, so every column after it started
wherever that row's name happened to end:

    01 * kit • 13.09.2026 04:16 • ONLINE NOW
    02 * Bartholomew • 13.09.2026 04:16 • SIGNED OFF

Same defect as #528 on a screen that does not go through `pick_item`
and so was not covered by it -- this one is a framed banner composing
its segments by hand. The sibling screen in the same module
(`_show_logoff_summary_screen`) already padded its own label column, so
this was one spot overlooked rather than a rule nobody had.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.net.profile_flow import _show_previous_callers_screen
from netbbs.rendering import display_width
from netbbs.session_history import (
    record_session_end,
    record_session_start,
    set_previous_callers_enabled,
)
from netbbs.storage.database import Database

_SGR = re.compile(r"\x1b\[[0-9;]*m")


class FakeSession:
    def __init__(self, width: int = 80):
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = 24
        self.node_display_name = "ReLink"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_any_key(self, echo: bool = True) -> str:
        return " "

    @property
    def output(self) -> str:
        return "".join(self.written)


def _rows(session: FakeSession) -> list[str]:
    """The caller rows, stripped of styling, without the frame."""
    plain = _SGR.sub("", session.output)
    return [
        line for line in plain.split("\n")
        if re.search(r"\d\d [◆*] ", line)
    ]


def _column_start(row: str, needle: str) -> int:
    """Where `needle` begins, in display columns -- not characters. A
    CJK handle is two columns per character, so measuring this with
    `str.index` is the very mistake the padding exists to prevent."""
    return display_width(row[: row.index(needle)])


def _setup(tmp_path, names, *, online: set[str] = frozenset()):
    db = Database(tmp_path / "node.db")
    set_previous_callers_enabled(db, True)
    for name in names:
        user = create_user(db, name, password="hunter2", user_level=10)
        history_id = record_session_start(db, user)
        if name not in online:
            record_session_end(db, history_id)
    viewer = create_user(db, "viewer", password="hunter2", user_level=10)
    return db, viewer


def _render(tmp_path, names, width=80, **kwargs):
    db, viewer = _setup(tmp_path, names, **kwargs)
    session = FakeSession(width=width)
    shown = asyncio.run(
        _show_previous_callers_screen(session, db, viewer, current_history_id=None)
    )
    db.close()
    assert shown is True
    return session


# -- The reported defect ----------------------------------------------


def test_the_timestamp_starts_at_the_same_column_on_every_row(tmp_path):
    """The report. Names of wildly different lengths must not move the
    column after them."""
    session = _render(tmp_path, ["al", "Bartholomew", "zoe", "Christopher-Longname"])
    rows = _rows(session)
    assert len(rows) == 4
    starts = {_column_start(row, "•") for row in rows}
    assert len(starts) == 1, [(_column_start(r, "•"), r) for r in rows]


def test_the_status_starts_at_the_same_column_on_every_row(tmp_path):
    """The third column, which only the wide layout draws."""
    session = _render(
        tmp_path, ["al", "Bartholomew", "zoe"], online={"zoe"}
    )
    rows = _rows(session)
    starts = set()
    for row in rows:
        for status in ("SIGNED OFF", "SIGNAL LOST", "ONLINE NOW"):
            if status in row:
                starts.add(_column_start(row, status))
    assert len(starts) == 1, starts


def test_a_hidden_name_occupies_the_same_column_as_a_real_one(tmp_path):
    """"(name hidden)" is rendered in a different colour and is a
    different length; it must still be a cell."""
    db, viewer = _setup(tmp_path, ["visible_one"])
    hidden = create_user(db, "hidden_one", password="hunter2", user_level=10)
    from netbbs.session_history import set_session_history_name_visible

    set_session_history_name_visible(db, hidden, False)
    history_id = record_session_start(db, hidden)
    record_session_end(db, history_id)

    session = FakeSession()
    asyncio.run(_show_previous_callers_screen(session, db, viewer, current_history_id=None))
    db.close()

    rows = _rows(session)
    assert len(rows) == 2
    assert len({_column_start(row, "•") for row in rows}) == 1


# -- Width measurement ------------------------------------------------


# The name shown here is always `username_label` -- `[A-Za-z0-9_.-]`,
# at most 32 characters (`netbbs.auth.users`) -- or the literal
# "(name hidden)". So the wide/CJK case cannot actually arise on this
# screen, and there is no test fabricating one: the padding measures in
# display columns anyway, defensively and for free, but a test that
# forced a CJK handle past `create_user` would be asserting against an
# input the domain refuses.
_LONGEST_LEGAL_NAME = "a" * 32


def test_the_longest_legal_name_still_lines_up(tmp_path):
    session = _render(tmp_path, [_LONGEST_LEGAL_NAME, "bob"])
    rows = _rows(session)
    assert len(rows) == 2
    assert len({_column_start(row, "•") for row in rows}) == 1


def test_a_name_too_long_for_a_narrow_panel_is_truncated(tmp_path):
    """At 58 columns the name column is narrower than a 32-character
    username, so it must lose characters rather than push the columns
    out or break the frame."""
    session = _render(tmp_path, [_LONGEST_LEGAL_NAME, "bob"], width=58)
    rows = _rows(session)
    assert len({_column_start(row, "•") for row in rows}) == 1
    assert _LONGEST_LEGAL_NAME not in _SGR.sub("", session.output)


# -- Both layouts, and the frame --------------------------------------


@pytest.mark.parametrize("width", [58, 62, 80, 100, 132])
def test_columns_align_at_every_supported_width(tmp_path, width):
    """The screen has a wide layout (>= 62 columns, with the status
    column) and a narrow one. Both must line up."""
    session = _render(tmp_path, ["al", "Bartholomew", "zoe"], width=width)
    rows = _rows(session)
    assert rows, f"no rows rendered at width {width}"
    assert len({_column_start(row, "•") for row in rows}) == 1


@pytest.mark.parametrize("width", [58, 62, 80, 100, 132])
def test_the_frame_stays_rectangular(tmp_path, width):
    """Padding a cell must not push a row past the frame it sits in."""
    session = _render(
        tmp_path, ["al", _LONGEST_LEGAL_NAME, "Bartholomew"], width=width
    )
    plain = _SGR.sub("", session.output)
    framed = [line for line in plain.split("\n") if line.startswith(("║", "|"))]
    assert framed
    assert len({display_width(line) for line in framed}) == 1
