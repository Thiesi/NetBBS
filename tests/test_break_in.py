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


# -- review follow-ups (PR #785) ---------------------------------------------


def test_a_password_prompt_refuses_a_break_in():
    async def scenario():
        caller, reader, wire = _caller()
        typing = asyncio.create_task(caller.read_line(echo=False))
        reader.feed_data(b"hunt")
        await _until(lambda: caller.reading_secret)
        assert "password" in break_in.refusal(caller)
        reader.feed_data(b"er2\r")
        assert await typing == "hunter2"
        assert not caller.reading_secret
        assert break_in.refusal(caller) is None

    asyncio.run(scenario())


def test_keys_typed_at_a_password_prompt_during_a_chat_show_as_stars():
    async def scenario():
        caller, _reader, _wire = _caller()
        state = ChatState(sysop_name="sysop", caller_name="alice")
        keys = break_in._CallerKeys(state.caller, caller)
        for value in b"hi ":
            keys.feed(value)
        caller.reading_secret = True
        for value in b"secret":
            keys.feed(value)
        assert state.caller.typing == "hi ******"

    asyncio.run(scenario())


def test_a_transfer_waits_for_the_chat_to_end(sysop, monkeypatch):
    from netbbs.net import zmodem

    async def scenario():
        registry = ActiveSessionRegistry()
        caller, reader, wire = _caller()
        task = asyncio.create_task(_prompt_task(registry, caller, []))
        await _until(lambda: "quest" in wire.screen().text_rows()[0])
        began = []

        async def fake_send(session, filename, data):
            began.append(session.in_break_in)

        monkeypatch.setattr(zmodem, "_send_file", fake_send)
        sysop_session = QueueSession()
        chat = asyncio.create_task(run_break_in(sysop_session, sysop, registry, caller, "alice"))
        await _until(lambda: caller.in_break_in)
        transfer = asyncio.create_task(zmodem.send_file(caller, "f.txt", b"x"))
        await asyncio.sleep(0.05)
        assert not transfer.done() and not began, "the transfer must not start under a chat"
        sysop_session.inputs.put_nowait("ESCAPE")
        await chat
        await asyncio.wait_for(transfer, 2)
        assert began == [False]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_restore_releases_even_under_endless_output():
    async def scenario():
        caller, reader, wire = _caller()
        await caller.write("steady")
        caller.begin_break_in()
        real = caller.write_through

        async def busy_write_through(text):
            await real(text)
            caller._copy_output("more door output ")  # every repaint races new output

        caller.write_through = busy_write_through
        await asyncio.wait_for(caller.end_break_in(), 2)
        assert not caller.in_break_in and not caller._output_held
        await caller.write("after")
        assert b"after" in wire.data

    asyncio.run(scenario())


def test_an_oversized_web_key_event_is_rejected_during_a_chat_too():
    from netbbs.net.session import SessionClosedError
    from netbbs.net import web

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

        async def close(self, **kwargs):
            self.closed = True

    async def scenario():
        session = WebSession(_Socket())
        session.begin_break_in()
        with pytest.raises(SessionClosedError):
            await session._handle_event({"type": "key", "data": "x" * (web._MAX_KEY_EVENT_LENGTH + 1)})

    asyncio.run(scenario())


def test_a_web_door_gets_a_fresh_decoder_when_a_chat_begins():
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
        await session.enter_door_mode(encoding="utf-8", width=80, height=25)
        stream = session._door_stream
        session.begin_break_in()
        await session.break_in_began()
        frame = session._ws.sent[-1]
        assert frame == {"type": "door_mode", "active": True, "stream": stream, "encoding": "utf-8",
                         "cols": 80, "rows": 25}
        await session.end_break_in()
        await session.leave_door_mode()

    asyncio.run(scenario())


def test_a_second_sysop_who_confirms_late_is_told_not_crashed(db, lane, sysop):
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
        viewer.inputs.put_nowait("c")
        await _until(lambda: "Chat anyway?" in strip_ansi("".join(viewer.written)))
        # Another SysOp breaks in while this one reads the warning.
        other = QueueSession()
        first = asyncio.create_task(run_break_in(other, sysop, registry, caller, "alice"))
        await _until(lambda: caller.in_break_in)
        viewer.inputs.put_nowait("y")
        await _until(lambda: "already in a break-in chat" in strip_ansi("".join(viewer.written)))
        assert not monitor.done()
        other.inputs.put_nowait("ESCAPE")
        await first
        viewer.inputs.put_nowait("q")
        await monitor
        for task in (viewer_task, caller_task):
            task.cancel()
        await asyncio.gather(viewer_task, caller_task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_caller_gone_before_the_first_frame_does_not_take_the_sysop_down(sysop, monkeypatch):
    from netbbs.net.session import SessionClosedError

    async def scenario():
        registry = ActiveSessionRegistry()
        caller, reader, wire = _caller()
        task = asyncio.create_task(_prompt_task(registry, caller, []))
        await _until(lambda: "quest" in wire.screen().text_rows()[0])

        async def dead(text):
            raise SessionClosedError("gone")

        monkeypatch.setattr(caller, "write_through", dead)
        sysop_session = QueueSession()
        chat = asyncio.create_task(run_break_in(sysop_session, sysop, registry, caller, "alice"))
        await _until(lambda: "has disconnected" in strip_ansi("".join(sysop_session.written)))
        sysop_session.inputs.put_nowait("x")
        await asyncio.wait_for(chat, 2)  # returns normally: no SessionClosedError for the SysOp
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_notice_for_the_sysop_during_a_chat_is_shown(sysop):
    async def scenario():
        registry = ActiveSessionRegistry()
        caller, reader, wire = _caller()
        task = asyncio.create_task(_prompt_task(registry, caller, []))
        await _until(lambda: "quest" in wire.screen().text_rows()[0])
        sysop_session = QueueSession()
        registry.enter(sysop_session)
        chat = asyncio.create_task(run_break_in(sysop_session, sysop, registry, caller, "alice"))
        await _until(lambda: caller.in_break_in)
        await asyncio.sleep(0.05)
        assert await registry.notify_one(sysop_session, "*** Node going down ***")
        # The diff skips an unchanged blank between words, so match each.
        shown = strip_ansi("".join(sysop_session.written))
        assert "*** Node" in shown and "going down ***" in shown
        sysop_session.inputs.put_nowait("ESCAPE")
        await chat
        registry.leave(sysop_session)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_overflowing_chat_input_is_counted_and_logged(caplog):
    async def scenario():
        caller, reader, wire = _caller()
        keys = caller.begin_break_in()
        for _ in range(keys.maxsize + 5):
            caller._divert(ord("x"))
        assert caller.break_in_dropped == 5
        await caller.end_break_in()

    with caplog.at_level("WARNING", logger="netbbs.net.session"):
        asyncio.run(scenario())
    assert sum("overflowed" in r.getMessage() for r in caplog.records) == 1


def test_a_character_split_across_the_release_reaches_the_caller_whole():
    async def scenario():
        caller, reader, wire = _caller()
        caller.begin_break_in()
        encoded = "é".encode()
        await caller.write_raw(encoded[:1])  # a door, mid-character, during the chat
        await caller.end_break_in()
        await caller.write_raw(encoded[1:])  # the rest, after release
        assert encoded in wire.data
        assert "é" in wire.data.decode("utf-8", errors="replace")

    asyncio.run(scenario())
