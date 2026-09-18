"""The outcome of a SysOp action is on the screen the SysOp lands on.

With redraw-in-place on -- the default for a new account -- a line written just
before a screen returns is never seen: the screen it returns to clears the
terminal in the same burst of output. `'General' deleted.`, `Created 'Retro'.`,
a rejected field value, an empty list's "No message boards yet." were all
written that way. They are announced instead (`admin_flow._announce`) and the
next console screen drawn -- a menu, a detail panel, a draft editor, a picker --
shows them directly above its prompt. No keypress is asked for.

Every test here turns the preference on and reads what is on the terminal
*after the last clear*. Asserting that the text was written somewhere in the
transcript passes against the bug, which is how it went unnoticed.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board, list_boards
from netbbs.net.admin_flow import admin_menu
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_detail_view import ScriptedSession, _Exhausted


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    user = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_redraw_in_place_enabled(db, user, True)
    return user


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def _screen(lane, sysop, keys) -> list[str]:
    """The rows on the terminal once `keys` have been typed and the console is
    waiting for the next one."""
    session = ScriptedSession(keys)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    return session.on_terminal()


def _above_the_prompt(rows: list[str]) -> str:
    assert rows[-1].startswith("Choice:"), rows[-3:]
    return rows[-2]


def test_creating_a_board_says_so_on_the_menu_it_returns_to(db, lane, sysop):
    rows = _screen(lane, sysop, ["c", "m", "c", "n", "Retro", "s"])
    assert [board.name for board in list_boards(db)] == ["Retro"]
    assert rows[0].endswith("Message boards")
    assert _above_the_prompt(rows) == "Created message board 'Retro'."


def test_deleting_a_board_says_so_on_the_menu_it_returns_to(db, lane, sysop):
    create_board(db, "General", creator=sysop)
    rows = _screen(lane, sysop, ["c", "m", "l", "0", "1", "d", "General"])
    assert list_boards(db) == []
    assert _above_the_prompt(rows) == "'General' deleted."


def test_a_cancelled_delete_says_so_on_the_detail_screen_it_returns_to(db, lane, sysop):
    create_board(db, "General", creator=sysop)
    rows = _screen(lane, sysop, ["c", "m", "l", "0", "1", "d", "not the name"])
    assert [board.name for board in list_boards(db)] == ["General"]
    assert rows[0].endswith("General")  # still the board's own screen
    assert "Cancelled." in _above_the_prompt(rows)


def test_a_changed_user_level_is_confirmed_on_the_redrawn_user_screen(db, lane, sysop):
    alice = create_user(db, "alice", password="hunter2")
    rows = _screen(lane, sysop, ["u", "l", "g", str(alice.id), "l", "10"])
    assert rows[0].endswith("alice")
    assert "'alice' is now level 10." in rows
    assert any("Level: 10" in " ".join(row.split()) for row in rows)


def test_an_empty_list_explains_itself_on_the_menu_instead_of_flashing_past(db, lane, sysop):
    rows = _screen(lane, sysop, ["c", "m", "l"])
    assert rows[0].endswith("Message boards")
    assert _above_the_prompt(rows) == "No message boards yet."


def test_a_field_prompts_refusal_is_shown_by_the_editor_it_returns_to(db, lane, sysop):
    # Node > Drain: the delay field rejects a non-number and hands back to the
    # draft editor, whose redraw used to erase the reason.
    from netbbs.net.maintenance import MaintenanceMode
    from netbbs.net.session_registry import ActiveSessionRegistry
    from netbbs.net.shutdown import NodeControls

    controls = NodeControls(
        session_registry=ActiveSessionRegistry(), maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(), graceful_delay_seconds=60.0, backup_identity_dir=None,
    )
    session = ScriptedSession(["n", "d", "d", "soon"])
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop, node_controls=controls))
    rows = session.on_terminal()
    assert rows[0].endswith("Schedule drain")
    assert "Not a number." in rows


def test_an_outcome_is_shown_once(db, lane, sysop):
    rows = _screen(lane, sysop, ["c", "m", "c", "n", "Retro", "s", "z"])  # "z": a key the menu rejects
    assert "Created message board 'Retro'." in rows
    rows = _screen(lane, sysop, ["c", "m", "l", "b", "b", "m"])  # leave, and come back to the menu
    assert not any("Created" in row for row in rows)


def test_an_outcome_never_reaches_another_sessions_console(db, lane, sysop):
    from netbbs.net import admin_flow

    first, second = ScriptedSession([]), ScriptedSession([])
    admin_flow._announce(first, "Created 'Retro'.")
    assert admin_flow._take_notices(second) == []
    assert len(admin_flow._take_notices(first)) == 1
    assert admin_flow._take_notices(first) == []
