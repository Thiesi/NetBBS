"""Integration tests for the chat-channel-picker masthead (GitHub issue
#176) actually being prepended by `netbbs.net.chat_flow._pick_channel`
-- distinct from tests/test_chat_channel_picker_banner.py's own isolated
loader/status tests. Mirrors tests/test_board_list_masthead_
integration.py, adapted to `browse_channels`'s own hub/presence/mailbox/
history wiring the way tests/test_chat_flow_picker_sort.py already does
for the `[O]rder` command. Every scenario backs out with "b" rather than
selecting a channel -- selecting one enters the live chat loop, a
categorically different screen this masthead deliberately never reaches
(see chat_channel_picker_banner.py's own module docstring)."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.categories import create_category
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.chat_channel_picker_banner import (
    chat_channel_picker_banner_path,
    set_chat_channel_picker_banner_enabled,
)
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
        if not self._inputs:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        return self._inputs.pop(0)

    async def read_line(
        self, echo: bool = True, history=None, completer=None, *,
        live_buffer=None, lock=None, list_candidates=None, viewport=None, **_ignored,
    ) -> str:
        if not self._inputs:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
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
def hub():
    return ChatHub()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


async def _run(lane, hub, presence, user, inputs):
    session = FakeSession(inputs)
    history = InputHistory()
    mailbox = MessageMailbox()
    await asyncio.wait_for(
        chat_flow.browse_channels(session, lane, hub, presence, mailbox, history, user), timeout=2
    )
    return session


def test_disabled_masthead_leaves_channel_picker_byte_for_byte_unchanged(db, lane, hub, presence, alice):
    create_channel(db, "lobby", creator=alice)
    with_module = asyncio.run(_run(lane, hub, presence, alice, ["b"]))
    without_reference = asyncio.run(_run(lane, hub, presence, alice, ["b"]))
    assert _written_text(with_module) == _written_text(without_reference)
    assert "MY CUSTOM CHANNEL MASTHEAD" not in _written_text(with_module)


def test_masthead_shown_above_the_top_level_channel_picker(db, lane, hub, presence, alice):
    create_channel(db, "lobby", creator=alice)
    chat_channel_picker_banner_path(db).write_bytes(b"MY CUSTOM CHANNEL MASTHEAD")
    set_chat_channel_picker_banner_enabled(db, True)

    session = asyncio.run(_run(lane, hub, presence, alice, ["b"]))
    text = _written_text(session)
    assert "MY CUSTOM CHANNEL MASTHEAD" in text
    assert text.index("MY CUSTOM CHANNEL MASTHEAD") < text.index("Available chat channels")


def test_masthead_also_shown_when_drilling_into_a_category(db, lane, hub, presence, alice):
    vintage = create_category(db, "Vintage", created_by=alice)
    create_channel(db, "retro-computing", creator=alice, category_id=vintage.id)
    chat_channel_picker_banner_path(db).write_bytes(b"MY CUSTOM CHANNEL MASTHEAD")
    set_chat_channel_picker_banner_enabled(db, True)

    session = asyncio.run(_run(lane, hub, presence, alice, ["0", "1", "b"]))
    text = _written_text(session)
    assert text.count("MY CUSTOM CHANNEL MASTHEAD") == 2


def test_masthead_with_redraw_in_place_clears_before_the_masthead_not_after(db, lane, hub, presence, alice):
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled

    create_channel(db, "lobby", creator=alice)
    chat_channel_picker_banner_path(db).write_bytes(b"MY CUSTOM CHANNEL MASTHEAD")
    set_chat_channel_picker_banner_enabled(db, True)
    set_redraw_in_place_enabled(db, alice, True)

    session = asyncio.run(_run(lane, hub, presence, alice, ["b"]))
    text = _written_text(session)
    assert text.index(clear_screen()) < text.index("MY CUSTOM CHANNEL MASTHEAD")
    assert text.count(clear_screen()) == 1
