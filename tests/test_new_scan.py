"""
Tests for issue #56's `[N]ew scan` activity summary --
`netbbs.net.login_flow._draw_main_menu`'s always-shown entry and
`_new_scan_screen` itself: never-visited/caught-up/unread status per
board/channel/file area, the cross-board "replies to you" section, and
jumping straight to the first unread post/file. Channel-entry wiring
(`browse_channels`'s own `initial_channel` parameter) is proved for real
in tests/test_chat_flow_join.py -- this file only checks that `[N]ew
scan` calls into it with the right channel, via monkeypatch, since the
chat-capable `FakeSession` there can't script `pick_item`'s digit-based
selection the way this file's simpler session can.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.net import login_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.login_flow import _draw_main_menu, _main_menu
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession:
    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("FakeSession ran out of scripted input (read_key)")
        return self._inputs.pop(0)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


def _visible_text(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


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


def _run_main_menu(db, lane, user, keys):
    session = FakeSession(keys)
    asyncio.run(
        _main_menu(
            session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user, lane=lane
        )
    )
    return session


# -- menu visibility ----------------------------------------------------


def test_new_scan_is_always_shown_regardless_of_level(db, alice):
    session = FakeSession()
    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), alice))
    assert "[N]ew scan" in _visible_text(session)


def test_new_scan_is_not_available_without_a_lane(db, alice):
    session = FakeSession(["n", "l"])
    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice)
    )
    assert "New scan is not available in this context." in _written_text(session)


# -- unread status: never-visited / caught-up / unread -----------------


def test_new_scan_shows_never_visited_for_an_unvisited_board(db, lane, alice):
    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    create_post(db, board, other, "hello", "world")

    session = _run_main_menu(db, lane, alice, ["n", "b", "b", "l"])

    assert "not yet visited" in _written_text(session)


def test_new_scan_shows_caught_up_once_the_board_has_been_visited(db, lane, alice, monkeypatch):
    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: "2026-01-01T00:00:00.000000Z")
    create_post(db, board, other, "hello", "world")

    # First visit: pick the board (only item, "01"), back out of it, back to main menu.
    _run_main_menu(db, lane, alice, ["n", "0", "1", "b", "b", "l"])
    # Second new scan: now caught up.
    session = _run_main_menu(db, lane, alice, ["n", "b", "l"])

    assert "caught up" in _written_text(session)
    assert "not yet visited" not in _written_text(session)


def test_new_scan_shows_unread_count_for_new_activity(db, lane, alice, monkeypatch):
    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    create_post(db, board, other, "first", "1")

    _run_main_menu(db, lane, alice, ["n", "0", "1", "b", "b", "l"])  # visit once, catch up
    create_post(db, board, other, "second", "2")  # new activity after the visit

    session = _run_main_menu(db, lane, alice, ["n", "b", "l"])

    assert "1 unread" in _written_text(session)


# -- replies to you -------------------------------------------------------


def test_new_scan_shows_replies_to_you(db, lane, alice, monkeypatch):
    board = create_board(db, "general", creator=alice)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    alices_post = create_post(db, board, alice, "question", "how do I do X?")
    other = create_user(db, "bob", password="hunter2", user_level=10)
    create_post(db, board, other, "Re: question", "like this", parent_post_id=alices_post.post_id)

    session = _run_main_menu(db, lane, alice, ["n", "b", "l"])

    assert "Replies to you: 1" in _written_text(session)
    assert "Re: question" in _written_text(session)


def test_new_scan_shows_no_replies_when_there_are_none(db, lane, alice):
    session = _run_main_menu(db, lane, alice, ["n", "b", "l"])
    assert "Replies to you: none." in _written_text(session)


# -- jump to first unread -------------------------------------------------


def test_selecting_a_board_jumps_to_the_first_unread_post(db, lane, alice, monkeypatch):
    from netbbs.activity import record_board_seen

    other = create_user(db, "bob", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=other)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(timestamps))
    first = create_post(db, board, other, "first", "1")
    create_post(db, board, other, "second", "2")

    record_board_seen(db, alice, board, first)

    session = _run_main_menu(db, lane, alice, ["n", "0", "1", "b", "l"])

    text = _written_text(session)
    assert "first --" not in text
    assert "second --" in text


def test_selecting_a_file_area_jumps_to_the_first_unread_file(db, lane, alice, monkeypatch):
    from netbbs.activity import record_file_area_seen
    from netbbs.files import entries as entries_module

    other = create_user(db, "bob", password="hunter2", user_level=10)
    area = create_file_area(db, "downloads", creator=other)
    timestamps = iter([f"2026-01-01T00:00:0{i}.000000Z" for i in range(2)])
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    first = upload_file(db, area, other, "a.txt", b"hello")
    upload_file(db, area, other, "b.txt", b"world")

    record_file_area_seen(db, alice, area, first)

    session = _run_main_menu(db, lane, alice, ["n", "0", "1", "b", "l"])

    text = _written_text(session)
    assert "a.txt" not in text
    assert "b.txt" in text


# -- channel dispatch (proved for real in tests/test_chat_flow_join.py) -----


def test_selecting_a_channel_calls_browse_channels_with_that_channel(db, lane, alice, monkeypatch):
    channel = create_channel(db, "lobby", creator=alice)

    calls = []

    async def fake_browse_channels(session, lane, hub, presence, mailbox, history, user, **kwargs):
        calls.append(kwargs.get("initial_channel"))

    monkeypatch.setattr(login_flow, "browse_channels", fake_browse_channels)

    _run_main_menu(db, lane, alice, ["n", "0", "1", "l"])

    assert len(calls) == 1
    assert calls[0].id == channel.id
