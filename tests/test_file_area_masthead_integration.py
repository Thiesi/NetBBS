"""Integration tests for the file-area masthead (GitHub issue #176)
actually being prepended by `netbbs.net.file_flow._browse_areas_in_
category` -- distinct from tests/test_file_area_banner.py's own
isolated loader/status tests. Mirrors tests/test_board_list_masthead_
integration.py exactly, adapted to `netbbs.net.file_flow`'s own
lane-based execution model (see that module's own docstring)."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.files.areas import create_file_area
from netbbs.files.categories import create_category
from netbbs.net import file_flow
from netbbs.net.file_area_banner import file_area_banner_path, set_file_area_banner_enabled
from netbbs.net.session import Session
from netbbs.rendering.ansi import clear_screen
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession(Session):
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        return self._inputs.pop(0)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return self._inputs.pop(0)

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
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
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_disabled_masthead_leaves_file_area_list_byte_for_byte_unchanged(db, lane, alice):
    create_file_area(db, "downloads", creator=alice)
    with_module = FakeSession(["b"])
    asyncio.run(file_flow.browse_file_areas(with_module, lane, alice))

    without_reference = FakeSession(["b"])
    asyncio.run(file_flow.browse_file_areas(without_reference, lane, alice))
    assert _written_text(with_module) == _written_text(without_reference)
    assert "MY CUSTOM FILE AREA MASTHEAD" not in _written_text(with_module)


def test_masthead_shown_above_the_top_level_file_area_list(db, lane, alice):
    create_file_area(db, "downloads", creator=alice)
    file_area_banner_path(db).write_bytes(b"MY CUSTOM FILE AREA MASTHEAD")
    set_file_area_banner_enabled(db, True)

    session = FakeSession(["b"])
    asyncio.run(file_flow.browse_file_areas(session, lane, alice))
    text = _written_text(session)
    assert "MY CUSTOM FILE AREA MASTHEAD" in text
    assert text.index("MY CUSTOM FILE AREA MASTHEAD") < text.index("Available file areas")


def test_masthead_also_shown_when_drilling_into_a_category(db, lane, alice):
    vintage = create_category(db, "Vintage", created_by=alice)
    create_file_area(db, "old-shareware", creator=alice, category_id=vintage.id)
    file_area_banner_path(db).write_bytes(b"MY CUSTOM FILE AREA MASTHEAD")
    set_file_area_banner_enabled(db, True)

    session = FakeSession(["0", "1", "b", "b"])
    asyncio.run(file_flow.browse_file_areas(session, lane, alice))
    text = _written_text(session)
    assert text.count("MY CUSTOM FILE AREA MASTHEAD") == 3


def test_masthead_with_redraw_in_place_clears_before_the_masthead_not_after(db, lane, alice):
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled

    create_file_area(db, "downloads", creator=alice)
    file_area_banner_path(db).write_bytes(b"MY CUSTOM FILE AREA MASTHEAD")
    set_file_area_banner_enabled(db, True)
    set_redraw_in_place_enabled(db, alice, True)

    session = FakeSession(["b"])
    asyncio.run(file_flow.browse_file_areas(session, lane, alice))
    text = _written_text(session)
    assert text.index(clear_screen()) < text.index("MY CUSTOM FILE AREA MASTHEAD")
    assert text.count(clear_screen()) == 1
