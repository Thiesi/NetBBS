"""Every session keeps a copy of its caller's screen (issue #764): the
shared output layer in `Session` feeds it before any transport sends."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from netbbs.net import zmodem
from netbbs.net.local_cli import LocalCLISession
from netbbs.net.session import Session
from netbbs.net.ssh import SSHSession
from netbbs.net.telnet import TelnetSession
from netbbs.net.web import WebSession


class _Writer:
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


class _Stdout:
    def __init__(self):
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


class _Process:
    term_size = (80, 24, 0, 0)
    env: dict = {}

    def __init__(self):
        self.stdout = _Stdout()

    def get_extra_info(self, name, default=None):
        return default


def _telnet():
    return TelnetSession(asyncio.StreamReader(), _Writer())


def _ssh():
    return SSHSession(_Process())


def _web():
    return WebSession(_Socket())


def _local():
    return LocalCLISession(read_byte_fn=lambda: b"", read_byte_with_timeout_fn=lambda timeout: None)


@pytest.mark.parametrize("make", [_telnet, _ssh, _web, _local], ids=["telnet", "ssh", "web", "local"])
def test_every_transport_feeds_the_copy(make, capsys):
    # capsys holds what the local CLI writes to the real stdout.
    async def scenario():
        session = make()
        await session.write("hello\nworld")
        rows = session.screen_copy().text_rows()
        # Bare LF arrives as CRLF on the wire, and the copy agrees.
        assert rows[0].rstrip() == "hello"
        assert rows[1].rstrip() == "world"

    asyncio.run(scenario())


def test_telnet_still_sends_what_it_always_sent():
    async def scenario():
        writer = _Writer()
        session = TelnetSession(asyncio.StreamReader(), writer)
        await session.write("a\nb")
        assert writer.data == b"a\r\nb"

    asyncio.run(scenario())


def test_door_output_is_copied_as_utf8_even_split_mid_character():
    async def scenario():
        session = _ssh()
        encoded = "é█".encode()
        await session.write_raw(encoded[:1])
        await session.write_raw(encoded[1:])
        assert session.screen_copy().text_rows()[0].startswith("é█")

    asyncio.run(scenario())


def test_zmodem_frames_stay_out_of_the_copy(monkeypatch, tmp_path):
    seen = []

    async def fake_send(session, filename, data):
        seen.append(session.binary_transfer_active)
        await session.write_raw(b"\x18B00000000000000\r\x8a\x11" + b"garbage" * 50)

    async def fake_receive(session, *, max_bytes, dest_path):
        seen.append(session.binary_transfer_active)
        await session.write_raw(b"\x18B0100000023be50\r\x8a\x11")
        return None

    monkeypatch.setattr(zmodem, "_send_file", fake_send)
    monkeypatch.setattr(zmodem, "_receive_file", fake_receive)

    async def scenario():
        session = _ssh()
        await session.write("Starting Zmodem send...")
        before = session.screen_copy().snapshot()
        await zmodem.send_file(session, "f.txt", b"data")
        await zmodem.receive_file(session, max_bytes=10, dest_path=Path(tmp_path) / "x")
        assert session.screen_copy().snapshot() == before
        assert not session.binary_transfer_active
        await session.write_raw("after".encode())
        assert "after" in session.screen_copy().text_rows()[0]

    asyncio.run(scenario())
    assert seen == [True, True]


def test_the_copy_follows_the_terminal_size():
    async def scenario():
        session = _ssh()
        await session.write("x")
        session.terminal_width, session.terminal_height = 100, 30
        copy = session.screen_copy()
        assert (copy.width, copy.height) == (100, 30)

    asyncio.run(scenario())


def test_a_broken_copy_never_costs_the_caller_their_output(monkeypatch):
    async def scenario():
        writer = _Writer()
        session = TelnetSession(asyncio.StreamReader(), writer)
        copy = session.screen_copy()

        def explode(text):
            raise RuntimeError("emulator bug")

        monkeypatch.setattr(copy, "feed", explode)
        await session.write("still sent")
        assert writer.data == b"still sent"
        assert session.screen_copy() is not copy

    asyncio.run(scenario())


def test_a_test_double_overriding_write_still_works():
    class Double(Session):
        def __init__(self):
            self.out = []

        async def write(self, text):
            self.out.append(text)

        async def read_line(self, *args, **kwargs):
            return ""

        async def read_key(self, echo=True):
            return ""

        async def read_editor_key(self, **kwargs):
            raise NotImplementedError

        async def close(self):
            pass

        async def read_byte(self):
            return None

    async def scenario():
        double = Double()
        await double.write_line("x")
        assert double.out

    asyncio.run(scenario())


def test_a_fixed_size_web_door_sets_the_session_size_and_gives_it_back():
    async def scenario():
        session = _web()
        session.terminal_width, session.terminal_height = 120, 40
        await session.enter_door_mode(encoding="cp437", width=80, height=25)
        assert (session.terminal_width, session.terminal_height) == (80, 25)
        # The browser resizing meanwhile doesn't move a fixed door...
        await session._handle_event({"type": "resize", "cols": 100, "rows": 30})
        assert (session.terminal_width, session.terminal_height) == (80, 25)
        await session.leave_door_mode()
        # ...but is what the session has once the door ends.
        assert (session.terminal_width, session.terminal_height) == (100, 30)

    asyncio.run(scenario())


def test_a_door_without_a_fixed_size_leaves_the_session_size_alone():
    async def scenario():
        session = _web()
        session.terminal_width, session.terminal_height = 120, 40
        await session.enter_door_mode(encoding="utf-8")
        await session._handle_event({"type": "resize", "cols": 100, "rows": 30})
        assert (session.terminal_width, session.terminal_height) == (100, 30)
        await session.leave_door_mode()

    asyncio.run(scenario())


def test_a_door_that_dies_mid_character_leaves_the_same_replacement_in_the_copy():
    async def scenario():
        session = _ssh()
        await session.write_raw("é".encode()[:1])  # the door stops mid-character
        await session.write("\r\nBack at the menu")
        rows = session.screen_copy().text_rows()
        assert rows[0].startswith("�")
        assert rows[1].startswith("Back at the menu")

    asyncio.run(scenario())
