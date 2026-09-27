"""SysOp break-in chat (issue #765): the caller's own task keeps waiting
where it was, their keys go to the chat, their screen's output is held,
and afterwards their terminal is repainted and they carry on."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.net import break_in, sysop_monitor
from netbbs.net.break_in import ChatState, paint_chat, run_break_in
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.telnet import TelnetSession
from netbbs.net.web import WebSession
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.screen_buffer import ScreenBuffer
from netbbs.rendering.terminal_emulator import TerminalEmulator
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_sysop_monitor import QueueSession, _connect, _controls, _rows, _select, _until


class _Wire:
    """The caller's end of the connection: everything the node sends."""

    def __init__(self):
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass

    def screen(self, width: int = 80, height: int = 24) -> TerminalEmulator:
        """What the caller's terminal shows, replaying every byte sent."""
        terminal = TerminalEmulator(width, height)
        terminal.feed(self.data.decode("utf-8", errors="replace"))
        return terminal


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
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _caller():
    reader, wire = asyncio.StreamReader(), _Wire()
    return TelnetSession(reader, wire), reader, wire


async def _prompt_task(registry, session, lines):
    """A caller sitting at a line prompt, the way any screen does."""
    registry.enter(session)
    registry.mark_authenticated(session, "alice")
    try:
        await session.write("\x1b[2J\x1b[HWhat is your quest? ")
        lines.append(await session.read_line())
        await session.write("\r\nYou said: " + lines[-1])
        await asyncio.Event().wait()
    finally:
        registry.leave(session)


def test_the_caller_is_back_where_they_were_with_their_half_typed_line(sysop):
    async def scenario():
        registry = ActiveSessionRegistry()
        caller, reader, wire = _caller()
        lines: list[str] = []
        task = asyncio.create_task(_prompt_task(registry, caller, lines))
        reader.feed_data(b"to seek")
        await _until(lambda: "to seek" in wire.screen().text_rows()[0])

        sysop_session = QueueSession()
        chat = asyncio.create_task(run_break_in(sysop_session, sysop, registry, caller, "alice"))
        await _until(lambda: "opened a chat with you" in wire.screen().text_rows()[0])
        before_chat_bytes = len(wire.data)

        # The caller types into the chat, not into their prompt.
        reader.feed_data(b"hi sysop\r")
        await _until(lambda: any("hi sysop" in row for row in wire.screen().text_rows()))
        for key in "hello":
            sysop_session.inputs.put_nowait(key)
        await _until(lambda: any("hello_" in row for row in wire.screen().text_rows()))
        assert not lines, "the chat's Enter must never reach the caller's own prompt"
        assert len(wire.data) > before_chat_bytes

        sysop_session.inputs.put_nowait("ESCAPE")
        await chat
        assert not caller.in_break_in
        screen = wire.screen()
        assert screen.text_rows()[0].rstrip() == "What is your quest? to seek"
        assert (screen.row, screen.col) == (0, len("What is your quest? to seek"))

        # And the prompt carries on as if nothing happened.
        reader.feed_data(b" the grail\r")
        await _until(lambda: lines == ["to seek the grail"])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_output_during_the_chat_is_held_then_shown(sysop):
    async def scenario():
        registry = ActiveSessionRegistry()
        caller, reader, wire = _caller()
        lines: list[str] = []
        task = asyncio.create_task(_prompt_task(registry, caller, lines))
        await _until(lambda: "quest" in wire.screen().text_rows()[0])
        sysop_session = QueueSession()
        chat = asyncio.create_task(run_break_in(sysop_session, sysop, registry, caller, "alice"))
        await _until(lambda: caller.in_break_in)
        await registry.notify_one(caller, "*** Node going down in 5 minutes ***")
        await asyncio.sleep(0.05)
        assert b"going down" not in wire.data, "held output must not reach the caller mid-chat"
        sysop_session.inputs.put_nowait("ESCAPE")
        await chat
        assert any("going down in 5 minutes" in row for row in wire.screen().text_rows())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_caller_who_disconnects_during_the_chat_ends_it_cleanly(sysop):
    async def scenario():
        registry = ActiveSessionRegistry()
        caller, reader, wire = _caller()
        task = asyncio.create_task(_prompt_task(registry, caller, []))
        await _until(lambda: "quest" in wire.screen().text_rows()[0])
        sysop_session = QueueSession()
        chat = asyncio.create_task(run_break_in(sysop_session, sysop, registry, caller, "alice"))
        await _until(lambda: caller.in_break_in)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _until(lambda: "has disconnected" in strip_ansi("".join(sysop_session.written)))
        sysop_session.inputs.put_nowait("x")
        await asyncio.wait_for(chat, 2)

    asyncio.run(scenario())


def test_the_sysop_leaving_abruptly_still_puts_the_caller_back(sysop):
    async def scenario():
        registry = ActiveSessionRegistry()
        caller, reader, wire = _caller()
        task = asyncio.create_task(_prompt_task(registry, caller, []))
        await _until(lambda: "quest" in wire.screen().text_rows()[0])
        chat = asyncio.create_task(run_break_in(QueueSession(), sysop, registry, caller, "alice"))
        await _until(lambda: caller.in_break_in)
        chat.cancel()  # the SysOp's connection dropped
        await asyncio.gather(chat, return_exceptions=True)
        assert not caller.in_break_in
        assert wire.screen().text_rows()[0].startswith("What is your quest?")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_web_keys_go_to_the_chat_during_a_break_in():
    class _Socket:
        closed = False

        def __init__(self):
            self.sent = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

        async def send_json(self, value):
            self.sent.append(value)

    async def scenario():
        session = WebSession(_Socket())
        keys = session.begin_break_in()
        await session._handle_event({"type": "key", "data": "hé"})
        assert session._char_queue.empty()
        assert [keys.get_nowait() for _ in range(keys.qsize())] == list("hé".encode())
        await session.end_break_in()
        await session._handle_event({"type": "key", "data": "x"})
        assert not session._char_queue.empty()

    asyncio.run(scenario())


def test_a_transfer_refuses_a_break_in():
    async def scenario():
        caller, _reader, _wire = _caller()
        assert break_in.refusal(caller) is None
        with caller.binary_transfer():
            assert "file transfer" in break_in.refusal(caller)
        caller.begin_break_in()
        assert "already" in break_in.refusal(caller)

    asyncio.run(scenario())


def test_the_chat_layout_on_both_sides():
    state = ChatState(sysop_name="sysop", caller_name="alice")
    state.sysop.type("hello")
    state.caller.lines.append("hi!")
    for for_sysop, title in ((True, "Break-in chat with alice"), (False, "The SysOp (sysop) has opened a chat")):
        buffer = ScreenBuffer(40, 12)
        paint_chat(buffer, state, for_sysop=for_sysop)
        text = _rows(buffer)
        assert text[0].startswith(title)
        assert any("hello_" in row for row in text)
        assert any("hi!" in row for row in text)


# -- from the Monitor --------------------------------------------------------


def test_chat_from_the_monitor_and_back(db, lane, sysop):
    async def scenario():
        controls = _controls()
        registry = controls.session_registry
        viewer = QueueSession()
        viewer_task = await _connect(registry, viewer, "sysop")
        caller, reader, wire = _caller()
        caller_task = asyncio.create_task(_prompt_task(registry, caller, []))
        await _until(lambda: "quest" in wire.screen().text_rows()[0])
        monitor = asyncio.create_task(sysop_monitor.monitor_screen(
            viewer, lane, sysop, controls, disconnect=lambda entry: asyncio.sleep(0),
        ))
        _select(viewer, controls, "alice")
        viewer.inputs.put_nowait("c")
        await _until(lambda: caller.in_break_in)
        viewer.inputs.put_nowait("ESCAPE")
        await _until(lambda: "they are back where they were" in strip_ansi("".join(viewer.written)))
        assert not caller.in_break_in
        events = [e.text for e in registry.recent_events()]
        assert "chat by sysop: alice" in events
        viewer.inputs.put_nowait("q")
        await monitor
        for task in (viewer_task, caller_task):
            task.cancel()
        await asyncio.gather(viewer_task, caller_task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_door_player_gets_a_warning_first(db, lane, sysop):
    class _Presence:
        def door_of(self, session):
            return (1, "Voidrunner", "2026-09-28")

    async def scenario():
        controls = dataclasses.replace(_controls(), presence=_Presence())
        registry = controls.session_registry
        viewer = QueueSession()
        viewer_task = await _connect(registry, viewer, "sysop")
        caller, reader, wire = _caller()
        caller_task = asyncio.create_task(_prompt_task(registry, caller, []))
        await _until(lambda: "quest" in wire.screen().text_rows()[0])
        monitor = asyncio.create_task(sysop_monitor.monitor_screen(
            viewer, lane, sysop, controls, disconnect=lambda entry: asyncio.sleep(0),
        ))
        _select(viewer, controls, "alice")
        for key in ("c", "n"):
            viewer.inputs.put_nowait(key)
        await _until(lambda: "No chat opened." in strip_ansi("".join(viewer.written)))
        assert "Voidrunner, which keeps running" in strip_ansi("".join(viewer.written))
        assert not caller.in_break_in
        viewer.inputs.put_nowait("q")
        await monitor
        for task in (viewer_task, caller_task):
            task.cancel()
        await asyncio.gather(viewer_task, caller_task, return_exceptions=True)

    asyncio.run(scenario())
