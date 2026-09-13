"""Tests for netbbs.net.door_flow — the picker/launch wiring, not the
sandbox itself (see tests/test_doors_runtime.py for that). FakeSession
combines test_admin_flow.py's scripted read_key/read_line double (for
picker navigation) with a real asyncio.Queue-backed read_byte (for the
brief real door launch these tests exercise)."""

from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from netbbs.auth.users import create_user
from netbbs.doors import create_door
from netbbs.net.door_flow import browse_doors, has_visible_doors
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.session import Session
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession(Session):
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.peer_address = None
        self._byte_queue: asyncio.Queue[int] = asyncio.Queue()

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_editor_key)")
        raw = self._inputs.pop(0)
        if raw == "":
            return EditorKey(EditorKeyKind.ENTER)
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        # Never fed in these tests -- the doors launched here exit
        # immediately without reading stdin, so this just sits until the
        # relay's own FIRST_COMPLETED logic cancels it. See
        # tests/test_doors_runtime.py for real stdin-relay coverage.
        return await self._byte_queue.get()

    async def write_raw(self, data: bytes) -> None:
        self.written.append(data.decode(errors="replace"))


def _text(session: FakeSession) -> str:
    return "".join(session.written)


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


@pytest.fixture
def player(db):
    return create_user(db, "keeper", password="hunter2", user_level=10)


def _quick_exit_script(tmp_path):
    path = tmp_path / "quick_exit.py"
    path.write_text(textwrap.dedent("pass\n"), encoding="utf-8")
    return path


def test_has_visible_doors_false_when_none_registered(db, player):
    assert has_visible_doors(db, player) is False


def test_has_visible_doors_true_once_one_is_registered(db, player, tmp_path):
    script = _quick_exit_script(tmp_path)
    create_door(db, "Quick", sys.executable, args=(str(script),), creator=player)
    assert has_visible_doors(db, player) is True


def test_has_visible_doors_respects_min_play_level(db, player, tmp_path):
    script = _quick_exit_script(tmp_path)
    create_door(db, "Elite Only", sys.executable, args=(str(script),), min_play_level=250, creator=player)
    assert has_visible_doors(db, player) is False


def test_browsing_with_no_doors_shows_empty_message_then_backs_out(db, lane, player):
    session = FakeSession(inputs=[])
    asyncio.run(browse_doors(session, lane, player))
    assert "No doors are available" in _text(session)


def test_picking_a_door_launches_it_and_returns_to_the_picker(db, lane, player, tmp_path):
    script = _quick_exit_script(tmp_path)
    create_door(db, "Quick", sys.executable, args=(str(script),), creator=player)
    # picker highlights nothing until an arrow press -- select via the
    # 2-digit numeric path instead, then any key to dismiss the result
    # message, then back out of the now-empty-again picker.
    session = FakeSession(inputs=["0", "1", "x", "b"])
    asyncio.run(browse_doors(session, lane, player))
    text = _text(session)
    assert "Launching Quick" in text
    assert "Left Quick." in text


def test_a_door_below_the_callers_level_is_not_offered(db, lane, player, tmp_path):
    script = _quick_exit_script(tmp_path)
    create_door(db, "Elite Only", sys.executable, args=(str(script),), min_play_level=250, creator=player)
    session = FakeSession(inputs=[])
    asyncio.run(browse_doors(session, lane, player))
    assert "No doors are available" in _text(session)
