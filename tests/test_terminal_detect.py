"""Choosing a connection's character set from its terminal type (issue #929)."""

from __future__ import annotations

import asyncio
import time

import pytest

from netbbs.net.session import Session
from netbbs.net.ssh import SSHSession
from netbbs.net.telnet import IAC, NAWS, SB, SE, TTYPE, TTYPE_IS, WILL, WONT, TelnetServer
from netbbs.net.terminal_detect import classify_terminal_types
from netbbs.rendering.charset import ASCII, CP437, UTF8
from tests.test_telnet import _FULL_NEGOTIATION_LEN, _TTYPE_SEND, answer_terminal_type


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["syncterm"], (CP437, True)),
        (["ANSI-BBS"], (CP437, True)),
        (["ansi"], (CP437, False)),
        (["xterm-256color"], (UTF8, True)),
        (["VT100"], (UTF8, True)),
        (["tmux-256color"], (UTF8, True)),
        (["dumb"], (None, False)),
        ([], (None, False)),
        (["something", "syncterm"], (CP437, True)),
    ],
)
def test_classify_terminal_types(names, expected):
    assert classify_terminal_types(names) == expected


def _connect(client, *, wait=5.0):
    """Run `client(reader, writer)` against a server; return what the
    session looked like when its handler started, the handler's own
    result, and how long the handler took to start."""
    seen = {}
    started = time.monotonic()

    async def handler(session: Session):
        seen["charset"] = session.output_charset
        seen["certain"] = session.charset_certain
        seen["types"] = session.terminal_types
        seen["width"] = session.terminal_width
        seen["after"] = time.monotonic() - started
        await session.write_line("é╔")

    async def scenario():
        server = TelnetServer(host="127.0.0.1", port=0, session_handler=handler, terminal_type_wait_seconds=wait)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(_FULL_NEGOTIATION_LEN)
            seen["tail"] = await client(reader, writer)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    return seen


def test_syncterm_gets_cp437_from_the_first_screen():
    async def client(reader, writer):
        await answer_terminal_type(reader, writer, "syncterm")
        return await reader.readuntil(b"\r\n")

    seen = _connect(client)
    assert seen["charset"] == CP437 and seen["certain"]
    assert seen["types"] == ("syncterm",)
    assert seen["tail"] == bytes([0x82, 0xC9, 0x0D, 0x0A])


def test_a_refusal_ends_the_wait_and_means_ascii():
    async def client(reader, writer):
        writer.write(bytes([IAC, WONT, TTYPE]))
        await writer.drain()
        return await reader.readuntil(b"\r\n")

    seen = _connect(client, wait=5.0)
    assert seen["charset"] == ASCII and not seen["certain"]
    assert seen["after"] < 2.0
    assert seen["tail"] == b"e+\r\n"


def test_no_answer_waits_out_the_deadline_then_means_ascii():
    async def client(reader, writer):
        return await reader.readuntil(b"\r\n")

    seen = _connect(client, wait=0.3)
    assert seen["charset"] == ASCII and not seen["certain"]
    assert 0.25 <= seen["after"] < 3.0


def test_keystrokes_typed_during_the_wait_are_kept():
    seen = {}

    async def handler(session: Session):
        seen["line"] = await session.read_line()

    async def scenario():
        server = TelnetServer(host="127.0.0.1", port=0, session_handler=handler, terminal_type_wait_seconds=5.0)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(_FULL_NEGOTIATION_LEN)
            writer.write(b"hi\r")
            await writer.drain()
            await answer_terminal_type(reader, writer, "xterm")
            for _ in range(200):
                if "line" in seen:
                    break
                await asyncio.sleep(0.01)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert seen["line"] == "hi"


def test_window_size_sent_during_the_wait_is_applied():
    async def client(reader, writer):
        writer.write(bytes([IAC, SB, NAWS, 0, 100, 0, 30, IAC, SE]))
        await writer.drain()
        await answer_terminal_type(reader, writer, "xterm")
        return await reader.readuntil(b"\r\n")

    seen = _connect(client)
    assert seen["width"] == 100
    assert seen["charset"] == UTF8


def test_unknown_names_are_cycled_until_one_is_known():
    async def client(reader, writer):
        writer.write(bytes([IAC, WILL, TTYPE]))
        await writer.drain()
        for name in (b"dumb", b"ansi-bbs"):
            assert await reader.readexactly(len(_TTYPE_SEND)) == _TTYPE_SEND
            writer.write(bytes([IAC, SB, TTYPE, TTYPE_IS]) + name + bytes([IAC, SE]))
            await writer.drain()
        return await reader.readuntil(b"\r\n")

    seen = _connect(client)
    assert seen["types"] == ("dumb", "ansi-bbs")
    assert seen["charset"] == CP437


def test_a_repeated_name_ends_the_cycle():
    async def client(reader, writer):
        writer.write(bytes([IAC, WILL, TTYPE]))
        await writer.drain()
        for name in (b"dumb", b"dumb"):
            assert await reader.readexactly(len(_TTYPE_SEND)) == _TTYPE_SEND
            writer.write(bytes([IAC, SB, TTYPE, TTYPE_IS]) + name + bytes([IAC, SE]))
            await writer.drain()
        return await reader.readuntil(b"\r\n")

    seen = _connect(client, wait=5.0)
    assert seen["types"] == ("dumb",)
    assert seen["charset"] == ASCII
    assert seen["after"] < 2.0


def test_ansi_is_cp437_but_unsettled():
    async def client(reader, writer):
        await answer_terminal_type(reader, writer, "ANSI")
        return await reader.readuntil(b"\r\n")

    seen = _connect(client)
    assert seen["charset"] == CP437 and not seen["certain"]


class _Stdout:
    def write(self, data):
        pass

    async def drain(self):
        pass


class _Process:
    term_size = (80, 24, 0, 0)
    env: dict = {}

    def __init__(self, terminal_type):
        self.stdout = _Stdout()
        self._terminal_type = terminal_type

    def get_extra_info(self, name, default=None):
        return default

    def get_terminal_type(self):
        return self._terminal_type


@pytest.mark.parametrize(
    ("terminal_type", "charset", "certain"),
    [("syncterm", CP437, True), ("xterm-256color", UTF8, True), ("weird", UTF8, False), (None, UTF8, False)],
)
def test_ssh_uses_the_pty_terminal_type(terminal_type, charset, certain):
    session = SSHSession(_Process(terminal_type))
    assert (session.output_charset, session.charset_certain) == (charset, certain)
