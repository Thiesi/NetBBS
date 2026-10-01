"""
The Levels screen (design doc §5.7, issue #1007): `Users ▸ Le[v]els` lists
every level in use with what it first opens, and a level's own screen lists
its gates with where each level comes from.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.access_map import level_ladder
from netbbs.auth.users import SYSOP_LEVEL, create_user, set_user_disabled
from netbbs.boards.boards import create_board
from netbbs.chat.channels import create_channel
from netbbs.communities import create_community
from netbbs.config import set_mail_min_level
from netbbs.doors.registry import create_door
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


@pytest.fixture
def node(db, sysop):
    market = create_community(db, "Market", default_min_read_level=10, creator=sysop)
    create_board(db, "General", min_read_level=0, min_write_level=10, creator=sysop)
    create_board(db, "Trading Post", min_read_level=None, min_write_level=50, community_id=market.id, creator=sysop)
    create_board(db, "Staff Room", min_read_level=100, min_write_level=100, creator=sysop)
    create_channel(db, "lobby", creator=sysop)
    create_door(db, "Voidrunner", "vr.py", min_play_level=10, creator=sysop)
    set_mail_min_level(db, 10)
    for name in ("alice", "bob"):
        create_user(db, name, password="hunter2", user_level=10)
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    set_user_disabled(db, carol, True, changed_by=sysop)
    return market


def _screen(lane, sysop, keys) -> str:
    session = ScriptedSession(keys)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    return "\n".join(" ".join(row.split()) for row in session.on_terminal())


def test_the_ladder_counts_usable_accounts_and_what_each_level_first_opens(db, node):
    ladder = {step.level: step for step in level_ladder(db)}

    assert sorted(ladder) == [0, 10, 50, 100, SYSOP_LEVEL]
    assert ladder[10].users == 2  # carol is disabled
    assert {(g.kind.value, g.name) for g in ladder[10].opens} == {
        ("board_write", "General"), ("board_read", "Trading Post"), ("door", "Voidrunner"), ("mail", "Mail"),
    }
    assert ladder[SYSOP_LEVEL].users == 1


def test_the_levels_screen_lists_each_level_with_a_summary(lane, sysop, node):
    text = _screen(lane, sysop, ["u", "v"])

    assert "Levels" in text
    assert "1 read · 1 post · 1 door · Mail" in text
    assert "everything" in text


def test_a_levels_own_screen_shows_where_each_level_comes_from(lane, sysop, node):
    text = _screen(lane, sysop, ["u", "v", "0", "2"])

    assert "Level 10" in text
    assert "Trading Post" in text and "Community Market" in text
    assert "Voidrunner" in text
    assert "Staff Room" not in text
    assert "New at this level" in text and "2 accounts at 10" in text


def test_view_steps_to_what_stays_closed(lane, sysop, node):
    text = _screen(lane, sysop, ["u", "v", "0", "2", "v", "v"])

    assert "Still closed" in text
    assert "Staff Room" in text and "General" not in text


def test_picking_a_board_opens_its_own_screen(lane, sysop, node):
    text = _screen(lane, sysop, ["u", "v", "0", "2", "0", "2"])

    # The second row of "new at 10", in list order: Trading Post's read gate.
    assert text.splitlines()[0].endswith("Trading Post")


def test_go_to_level_shows_a_level_nobody_holds(lane, sysop, node):
    text = _screen(lane, sysop, ["u", "v", "g", "60"])

    # Nothing first opens at 60; the empty view still says which level.
    assert "Nothing in this view of level 60." in text
    assert "0 accounts at 60" in text


def test_go_to_level_lists_what_a_level_nobody_holds_opens(lane, sysop, node):
    text = _screen(lane, sysop, ["u", "v", "g", "60", "v"])

    assert "Level 60" in text and "Trading Post" in text and "Staff Room" not in text


def test_a_narrow_terminal_gets_the_columns_as_text(lane, sysop, node):
    session = ScriptedSession(["u", "v"], width=50)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    ladder = "\n".join(" ".join(row.split()) for row in session.on_terminal())

    assert "2 users; opens 1 read" in ladder

    session = ScriptedSession(["u", "v", "0", "2"], width=50)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    detail = "\n".join(" ".join(row.split()) for row in session.on_terminal())

    assert "read at 10, Community Market" in detail


class _HookedSession(ScriptedSession):
    """Runs a callable placed among the inputs when the screen reaches it."""

    def _next(self) -> str:
        while self._inputs and callable(self._inputs[0]):
            self._inputs.pop(0)()
        return super()._next()


def test_the_ladder_is_counted_again_after_go_to_level(db, lane, sysop, node):
    """What happens on a level's own screen -- a gate's level changed on a
    board's screen -- shows on the ladder it returns to (review of #1018)."""
    def raise_staff_room():
        db.connection.execute("UPDATE boards SET min_read_level = 60, min_write_level = 60 WHERE name = 'Staff Room'")
        db.connection.commit()

    session = _HookedSession(["u", "v", "g", "60", raise_staff_room, "b"])
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    text = "\n".join(" ".join(row.split()) for row in session.on_terminal())

    assert ". 60 " in text and ". 100 " not in text


def test_a_resource_opened_from_levels_gets_the_content_menus_services(db, lane, sysop, node, monkeypatch):
    """The chat hub guards renaming an occupied channel and moves callers out
    of a deleted one, the door supervisor follows a door's edits, the
    transfer grants offer downloads: opened from here, the screens get them
    as from the Content menu (review of #1018)."""
    from types import SimpleNamespace

    from netbbs.access_map import GateKind, list_gates
    from netbbs.files.areas import create_file_area
    from netbbs.net import admin_flow

    create_file_area(db, "Uploads", creator=sysop)
    seen = {}

    def recorder(name):
        async def screen(session, lane, actor, resource, **kwargs):
            seen[name] = kwargs
        return screen

    for name in ("_channel_detail_screen", "_door_detail_screen", "_area_detail_screen"):
        monkeypatch.setattr(admin_flow, name, recorder(name))
    controls = SimpleNamespace(
        chat_hub="hub", mrc_bridge="bridge", door_services="doors", transfers="grants", backup_identity_dir="ids",
    )
    gates = list_gates(db)
    for kind in (GateKind.CHANNEL, GateKind.DOOR, GateKind.AREA_READ):
        gate = next(g for g in gates if g.kind is kind)
        asyncio.run(admin_flow._open_gate_resource(
            ScriptedSession([]), lane, sysop, gate, node_controls=controls, link_context=None,
        ))

    assert seen["_channel_detail_screen"]["chat_hub"] == "hub"
    assert seen["_channel_detail_screen"]["mrc_bridge"] == "bridge"
    assert seen["_door_detail_screen"]["door_services"] == "doors"
    assert seen["_door_detail_screen"]["backup_identity_dir"] == "ids"
    assert seen["_area_detail_screen"]["transfers"] == "grants"
