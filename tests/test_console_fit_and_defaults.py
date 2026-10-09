"""A first-time SysOp's console at 80x24, and every new account's defaults
(issue #840).

At 80x24, the size of every terminal the field test's SysOp had, the console
said "Descriptions hidden -- terminal too short" exactly where its one-word
entries (Content? Operations?) needed them. And an account the SysOp made --
her own first one included -- started without the in-place redraw every
signed-up caller got.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user, get_user_by_username
from netbbs.net.admin_flow import _degrade_description_level, admin_menu
from netbbs.net.menu_description_preference import set_menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled, redraw_in_place_ever_set
from netbbs.rendering import MenuEntry, menu_key
from netbbs.rendering.layout import menu_grid
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_detail_view import ScriptedSession, _Exhausted

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def test_an_inline_description_sits_on_its_entrys_line():
    text = _ANSI.sub("", menu_grid(
        [("", [MenuEntry(label=menu_key("C", "ontent"), brief="Boards, areas and channels")])],
        width=80, description_level="inline",
    ))
    assert text.strip().splitlines() == ["[C]ontent  Boards, areas and channels"]


def test_a_short_screen_falls_back_to_inline_before_hiding_descriptions():
    level, _, degraded = _degrade_description_level(
        panel=["x"] * 12, unicode_style=True, description_level="brief",
        entry_count=8, terminal_width=80, terminal_height=24,
    )
    assert (level, degraded) == ("inline", False)


def test_a_small_panel_does_not_hide_what_a_large_one_shows():
    """Two lines per entry fit, but under `menu_grid`'s height floor they
    would be hidden: the one-line form instead (review on #872)."""
    level, _, degraded = _degrade_description_level(
        panel=["x"] * 8, unicode_style=True, description_level="brief",
        entry_count=8, terminal_width=80, terminal_height=24,
    )
    assert (level, degraded) == ("inline", False)


def test_the_console_landing_keeps_descriptions_at_80x24(db, lane):
    sysop = create_user(db, "InkWell", password="hunter2", user_level=SYSOP_LEVEL)
    set_menu_description_level(db, sysop, "brief")
    session = ScriptedSession([], width=80, height=24)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    screen = "\n".join(session.on_terminal())

    assert "Descriptions hidden" not in screen
    assert len(session.on_terminal()) <= 24


def test_an_account_made_in_the_console_starts_redrawing_in_place(db, lane):
    sysop = create_user(db, "InkWell", password="hunter2", user_level=SYSOP_LEVEL)
    session = ScriptedSession(["u", "c", "0", "1", "OldNib", "0", "2", "y", "hunter22", "hunter22", "c"])
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    made = get_user_by_username(db, "OldNib")

    assert redraw_in_place_ever_set(db, made) and redraw_in_place_enabled(db, made)
