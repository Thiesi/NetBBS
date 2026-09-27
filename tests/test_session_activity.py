"""Idle time and the activity trail the SysOp monitor shows (issue #762)."""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from netbbs.doors import runtime
from netbbs.net import (
    ansi_editor,
    board_flow,
    chat_flow,
    composition,
    door_flow,
    file_flow,
    main_menu,
    prose_editor,
    session_registry,
)
from netbbs.net.local_cli import LocalCLISession
from netbbs.net.session_activity import (
    MAX_SEGMENT_LENGTH,
    activity,
    describe,
    records_activity,
    set_root_activity,
)
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.ssh import SSHSession
from netbbs.net.telnet import IAC, WILL, TelnetSession
from netbbs.net.web import WebSession


class _Plain:
    """Anything with attributes; the helpers never need more."""

    activity: tuple[str, ...] = ()


# -- the trail ---------------------------------------------------------------


def test_nested_segments_build_a_trail_and_unwind_in_order():
    session = _Plain()
    with activity(session, "Boards"):
        with activity(session, "Retro"):
            assert session.activity == ("Boards", "Retro")
        assert session.activity == ("Boards",)
    assert session.activity == ()


def test_the_trail_is_restored_when_a_screen_raises():
    session = _Plain()
    with activity(session, "Boards"):
        with pytest.raises(RuntimeError):
            with activity(session, "Retro"):
                raise RuntimeError("disconnect")
        assert session.activity == ("Boards",)


def test_the_trail_is_restored_on_cancellation():
    async def scenario():
        session = _Plain()

        @records_activity("Doors")
        async def screen(session):
            await asyncio.Event().wait()

        task = asyncio.create_task(screen(session))
        await asyncio.sleep(0)
        assert session.activity == ("Doors",)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.activity == ()

    asyncio.run(scenario())


def test_an_empty_label_leaves_the_trail_alone():
    session = _Plain()
    with activity(session, "Mail"):
        with activity(session, None):
            assert session.activity == ("Mail",)
        with activity(session, ""):
            assert session.activity == ("Mail",)


def test_segments_are_stripped_of_control_characters_and_capped():
    session = _Plain()
    with activity(session, "\x1b[2Jevil\r\nboard"):
        assert session.activity == ("[2Jevil  board",)
    with activity(session, "x" * 100):
        assert session.activity == ("x" * MAX_SEGMENT_LENGTH,)


def test_records_activity_names_the_place_from_the_call_arguments():
    async def scenario():
        session = _Plain()
        seen = []

        class Board:
            name = "Retro"

        @records_activity(lambda args: args["board"].name)
        async def show(session, db, board, *, flag=False):
            seen.append(session.activity)
            return "done"

        assert await show(session, None, Board()) == "done"
        assert await show(session=session, db=None, board=Board(), flag=True) == "done"
        assert seen == [("Retro",), ("Retro",)]
        assert session.activity == ()

    asyncio.run(scenario())


def test_the_main_menu_root_replaces_the_whole_trail():
    session = _Plain()
    session.activity = ("stale", "trail")
    set_root_activity(session, "Mail")
    assert session.activity == ("Mail",)
    set_root_activity(session, None)
    assert session.activity == ()


def test_describe():
    assert describe(("Boards", "Retro"), authenticated=True) == "Boards › Retro"
    assert describe((), authenticated=True) == "Main menu"
    assert describe((), authenticated=False) == "Logging in"


# -- coverage of the screens -------------------------------------------------


def test_every_main_menu_branch_names_its_activity():
    source = inspect.getsource(main_menu._main_menu_loop)
    dispatched = set(re.findall(r'choice == "([a-z])"', source))
    assert dispatched, "the dispatch chain moved; update this test"
    assert dispatched <= set(main_menu._MENU_ACTIVITY), dispatched - set(main_menu._MENU_ACTIVITY)


@pytest.mark.parametrize(
    "function",
    [
        board_flow._browse_boards,
        board_flow._show_board,
        chat_flow.browse_channels,
        chat_flow._chat_loop,
        chat_flow.run_direct_chat_loop,
        file_flow.browse_file_areas,
        file_flow._show_area,
        file_flow._handle_upload,
        file_flow.send_file_to_caller,
        file_flow._fetch_remote_file,
        door_flow.browse_doors,
        runtime.run_door,
        composition.edit_line_body,
        prose_editor.edit_prose,
        ansi_editor.edit_ansi_art,
    ],
    ids=lambda function: function.__qualname__,
)
def test_area_entry_points_record_their_activity(function):
    # `records_activity` wraps with functools.wraps, which sets __wrapped__.
    assert hasattr(function, "__wrapped__")


# -- idle time: each transport stamps real input only ------------------------


class _UnusedWriter:
    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def test_telnet_stamps_data_but_not_negotiation():
    async def scenario():
        reader = asyncio.StreamReader()
        session = TelnetSession(reader, _UnusedWriter())
        reader.feed_data(bytes([IAC, WILL, 31]))
        assert await session.read_byte() is None
        assert session.last_input_at is None
        reader.feed_data(b"q")
        assert await session.read_byte() == ord("q")
        assert session.last_input_at is not None

    asyncio.run(scenario())


def test_ssh_stamps_data():
    class _Stdin:
        async def read(self, n):
            return b"q"

    class _Process:
        term_size = (80, 24, 0, 0)
        env: dict = {}
        stdin = _Stdin()

        def get_extra_info(self, name, default=None):
            return default

    async def scenario():
        session = SSHSession(_Process())
        assert session.last_input_at is None
        assert await session.read_byte() == ord("q")
        assert session.last_input_at is not None

    asyncio.run(scenario())


def test_local_cli_stamps_data():
    async def scenario():
        session = LocalCLISession(read_byte_fn=lambda: b"q", read_byte_with_timeout_fn=lambda timeout: None)
        assert session.last_input_at is None
        assert await session.read_byte() == ord("q")
        assert session.last_input_at is not None

    asyncio.run(scenario())


class _Socket:
    closed = False

    def __init__(self):
        self.sent = []
        self.done = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.done.wait()
        raise StopAsyncIteration

    async def send_json(self, value):
        self.sent.append(value)

    async def close(self, **kwargs):
        self.closed = True
        self.done.set()


def test_web_stamps_keys_and_door_keys_but_not_resizes():
    async def scenario():
        session = WebSession(_Socket())
        await session._handle_event({"type": "resize", "cols": 100, "rows": 40})
        assert session.last_input_at is None
        await session._handle_event({"type": "key", "data": "q"})
        menu_stamp = session.last_input_at
        assert menu_stamp is not None
        await session.enter_door_mode(encoding="cp437", width=80, height=25)
        session.last_input_at = None
        stream = session._door_stream
        await session._handle_event({"type": "door_key", "stream": stream, "data": "x"})
        assert session.last_input_at is not None
        await session.leave_door_mode()

    asyncio.run(scenario())


# -- the registry snapshot ---------------------------------------------------


def test_the_snapshot_carries_idle_time_and_activity(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(session_registry.time, "monotonic", lambda: clock[0])

    async def scenario():
        registry = ActiveSessionRegistry()
        session = _Plain()
        session.peer_address = "192.0.2.1"
        session.last_input_at = None
        registry.enter(session)
        clock[0] = 1030.0
        (summary,) = registry.list_entries()
        # No input yet: idle since connecting.
        assert summary.idle_seconds == 30.0
        assert summary.activity == ()

        session.last_input_at = 1025.0
        session.activity = ("Boards", "Retro")
        clock[0] = 1040.0
        (summary,) = registry.list_entries()
        assert summary.idle_seconds == 15.0
        assert summary.activity == ("Boards", "Retro")

    asyncio.run(scenario())
