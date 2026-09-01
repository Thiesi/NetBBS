"""
End-to-end tests for the message-board picker's `[O]rder` command
(design doc, dogfood feature request) -- drives the real
`_browse_boards`/`_browse_boards_in_category`/`pick_item`/
`prompt_sort_change` chain, same shape
`tests/test_chat_flow_picker_sort.py` established for the channel
picker, adapted for this module's own synchronous-`db` (no
`DatabaseLane`) execution model.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.categories import create_category
from netbbs.net import board_flow
from netbbs.net.session import Session
from netbbs.sort_preferences import get_effective_sort_mode
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


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_text(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _set_created_at(db, board_name, iso_timestamp):
    db.connection.execute("UPDATE boards SET created_at = ? WHERE name = ?", (iso_timestamp, board_name))
    db.connection.commit()


def test_picker_shows_the_current_sort_mode_by_default(db, alice):
    create_board(db, "general", creator=alice)
    session = FakeSession(["b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    # Boards default to "activity" -- unlike channels, this is safe:
    # real, persisted, Link-synced post/creation timestamps every node
    # agrees on (see DEFAULT_SORT_MODE_BY_KIND's own comment).
    assert "Sort: Activity" in _written_text(session)


def test_order_command_resorts_the_flat_board_list_and_persists_globally(db, alice):
    create_board(db, "apple", creator=alice)
    create_board(db, "zebra", creator=alice)
    _set_created_at(db, "apple", "2026-01-01T00:00:00.000000Z")
    _set_created_at(db, "zebra", "2026-01-02T00:00:00.000000Z")

    # Default ("activity", falling back to created_at with no posts):
    # zebra first. Switch to alphabetical: apple first instead.
    session = FakeSession(["o", "l", "g", "b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    text = _visible_text(session)
    assert "Sort: Alphabetical" in text
    assert re.search(r"01\.\s*\(#\d+\)\s*apple", text)
    assert get_effective_sort_mode(db, alice, "board") == "alphabetical"


def test_order_command_choosing_just_this_time_does_not_persist(db, alice):
    create_board(db, "apple", creator=alice)
    create_board(db, "zebra", creator=alice)
    _set_created_at(db, "apple", "2026-01-01T00:00:00.000000Z")
    _set_created_at(db, "zebra", "2026-01-02T00:00:00.000000Z")

    session = FakeSession(["o", "l", "j", "b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    assert get_effective_sort_mode(db, alice, "board") == "activity"  # unchanged


def test_order_command_in_the_mixed_categories_view_only_reorders_boards(db, alice):
    create_category(db, "Vintage", created_by=alice)
    create_board(db, "apple", creator=alice)
    create_board(db, "zebra", creator=alice)
    _set_created_at(db, "apple", "2026-01-01T00:00:00.000000Z")
    _set_created_at(db, "zebra", "2026-01-02T00:00:00.000000Z")

    session = FakeSession(["o", "l", "j", "b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    text = _visible_text(session)
    assert re.search(r"01\.\s*\(#-?\d+\)\s*\[Vintage\]", text)


def test_community_scoped_order_offers_a_whole_community_save_option(db, alice):
    from netbbs.boards.boards import update_board
    from netbbs.communities import create_community

    community = create_community(db, "Retro Computing", creator=alice)
    board = create_board(db, "lobby", creator=alice)
    update_board(
        db, board, name="lobby", description=None, min_read_level=0, min_write_level=0,
        category_id=None, pinned=False, moderated=False, max_post_age_days=None,
        min_age=None, name_requirement=None, community_id=community.id, changed_by=alice,
    )

    session = FakeSession(["o", "l", "w", "b"])
    asyncio.run(
        board_flow._browse_boards(session, db, alice, community_id=community.id, community_scoped=True)
    )
    text = _written_text(session)
    assert "hole Community (Retro Computing)" in text
    assert get_effective_sort_mode(db, alice, "board", community_id=community.id) == "alphabetical"
    assert get_effective_sort_mode(db, alice, "board") == "activity"  # global untouched
