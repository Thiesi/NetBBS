"""Integration tests for the board-list masthead (GitHub issue #176)
actually being prepended by `netbbs.net.board_flow._browse_boards_in_
category` -- distinct from tests/test_board_list_banner.py's own
isolated loader/status tests.

Mirrors tests/test_main_menu_masthead.py's own two halves: the
disabled-masthead byte-for-byte-unchanged case, and the real
clear_screen()-ordering regression `_draw_main_menu`'s own masthead
handling already guards against. Also proves the "every level" scoping
decision (GitHub issue #176's own discussion): the masthead reappears
when drilling into a category, not only on the very first unfiltered
screen."""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.categories import create_category
from netbbs.net import board_flow
from netbbs.net.board_list_banner import board_list_banner_path, set_board_list_banner_enabled
from netbbs.net.session import Session
from netbbs.rendering.ansi import clear_screen
from netbbs.storage.database import Database


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


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_disabled_masthead_leaves_board_list_byte_for_byte_unchanged(db, alice):
    create_board(db, "general", creator=alice)
    with_module = FakeSession(["b"])
    asyncio.run(board_flow._browse_boards(with_module, db, alice))

    # A second, otherwise-identical run confirms the module existing at
    # all (imported, wired in) doesn't change a single byte when unset --
    # there's no "before this module existed" baseline to diff against
    # directly here, so this instead pins today's own output as the
    # contract: it must not vary run to run with the masthead untouched.
    without_reference = FakeSession(["b"])
    asyncio.run(board_flow._browse_boards(without_reference, db, alice))
    assert _written_text(with_module) == _written_text(without_reference)
    assert "MY CUSTOM BOARD MASTHEAD" not in _written_text(with_module)


def test_masthead_shown_above_the_top_level_board_list(db, alice):
    create_board(db, "general", creator=alice)
    board_list_banner_path(db).write_bytes(b"MY CUSTOM BOARD MASTHEAD")
    set_board_list_banner_enabled(db, True)

    session = FakeSession(["b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    text = _written_text(session)
    assert "MY CUSTOM BOARD MASTHEAD" in text
    assert text.index("MY CUSTOM BOARD MASTHEAD") < text.index("Available message boards")


def test_masthead_also_shown_when_drilling_into_a_category(db, alice):
    vintage = create_category(db, "Vintage", created_by=alice)
    create_board(db, "apple", creator=alice, category_id=vintage.id)
    board_list_banner_path(db).write_bytes(b"MY CUSTOM BOARD MASTHEAD")
    set_board_list_banner_enabled(db, True)

    # "0", "1" (read one keystroke at a time, like a real 2-digit
    # selection) picks the (sole) category from the top-level mixed
    # list; "b" then backs out of the category's own flat board list.
    session = FakeSession(["0", "1", "b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    text = _written_text(session)
    assert text.count("MY CUSTOM BOARD MASTHEAD") == 2


def test_masthead_with_redraw_in_place_clears_before_the_masthead_not_after(db, alice):
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled

    create_board(db, "general", creator=alice)
    board_list_banner_path(db).write_bytes(b"MY CUSTOM BOARD MASTHEAD")
    set_board_list_banner_enabled(db, True)
    set_redraw_in_place_enabled(db, alice, True)

    session = FakeSession(["b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    text = _written_text(session)
    assert text.index(clear_screen()) < text.index("MY CUSTOM BOARD MASTHEAD")
    assert text.count(clear_screen()) == 1
