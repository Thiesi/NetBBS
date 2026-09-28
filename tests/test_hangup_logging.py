"""A caller hanging up is one INFO line, not an ERROR with a traceback (issue #834).

A TCP reset reaches a *read* as `ConnectionResetError` itself (asyncio
hands the transport's error to the stream reader), not only as
`IncompleteReadError`, so each transport maps it to `SessionClosedError`,
and each listener logs a disconnect at INFO.
"""

from __future__ import annotations

import asyncio
import logging

import asyncssh
import pytest

from netbbs.net import ssh as ssh_module
from netbbs.net import telnet as telnet_module
from netbbs.net.session import SessionClosedError
from netbbs.net.ssh import SSHSession
from netbbs.net.telnet import IAC, NAWS, SB, TelnetServer, TelnetSession


class _Writer:
    def __init__(self, drain_error: BaseException | None = None) -> None:
        self._drain_error = drain_error
        self._closing = False

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return ("203.0.113.9", 4242)
        return default

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        if self._drain_error is not None:
            raise self._drain_error

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True

    async def wait_closed(self) -> None:
        pass


def _reset_reader(prefix: bytes = b"") -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    if prefix:
        reader.feed_data(prefix)
    reader.set_exception(ConnectionResetError(104, "Connection reset by peer"))
    return reader


@pytest.mark.parametrize("prefix", [b"", bytes([IAC]), bytes([IAC, 251]), bytes([IAC, SB, NAWS])])
def test_telnet_reset_during_read_is_a_session_close(prefix):
    async def scenario() -> None:
        session = TelnetSession(_reset_reader(prefix), _Writer())
        with pytest.raises(SessionClosedError):
            await session.read_byte()

    asyncio.run(scenario())


def test_telnet_reset_during_peek_reads_as_nothing():
    async def scenario() -> None:
        session = TelnetSession(_reset_reader(), _Writer())
        assert await session.read_byte_with_timeout(0.5) is None

    asyncio.run(scenario())


def test_telnet_reset_during_initial_negotiation_is_a_session_close():
    async def scenario() -> None:
        session = TelnetSession(asyncio.StreamReader(), _Writer(ConnectionResetError()))
        with pytest.raises(SessionClosedError):
            await session.negotiate_initial_options()

    asyncio.run(scenario())


def test_telnet_hangup_logs_one_info_line(caplog):
    async def handler(session) -> None:
        await session.read_byte()

    async def scenario() -> None:
        server = TelnetServer("127.0.0.1", 0, handler)
        await server._handle_connection(_reset_reader(), _Writer())

    with caplog.at_level(logging.DEBUG, logger=telnet_module.__name__):
        asyncio.run(scenario())
    records = [r for r in caplog.records if r.name == telnet_module.__name__]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.INFO, "telnet caller 203.0.113.9 disconnected")
    ]
    assert all(r.exc_info is None for r in records)


@pytest.mark.parametrize("error", [ValueError("a real bug"), ConnectionResetError(104, "an outbound socket")])
def test_telnet_real_error_still_logs_a_traceback(caplog, error):
    """A socket error that did not come through the caller's own session --
    an outbound request the session made -- is not a hang-up (Claude review)."""

    async def handler(session) -> None:
        raise error

    async def scenario() -> None:
        server = TelnetServer("127.0.0.1", 0, handler)
        await server._handle_connection(asyncio.StreamReader(), _Writer())

    with caplog.at_level(logging.INFO, logger=telnet_module.__name__):
        asyncio.run(scenario())
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and errors[0].exc_info is not None


class _Stdin:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def read(self, n: int) -> bytes:
        raise self._error


class _Stdout:
    def __init__(self, error: BaseException | None = None) -> None:
        self._error = error

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        if self._error is not None:
            raise self._error


class _Process:
    term_size = (80, 24, 0, 0)
    env: dict = {}

    def __init__(self, error: BaseException) -> None:
        self.stdin = _Stdin(error)
        self.stdout = _Stdout(error)

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return ("198.51.100.7", 22)
        return default

    def exit(self, status: int) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@pytest.mark.parametrize(
    "error", [ConnectionResetError(104, "reset"), asyncssh.ConnectionLost("lost"), BrokenPipeError()]
)
def test_ssh_reset_during_read_and_write_is_a_session_close(error):
    async def scenario() -> None:
        session = SSHSession(_Process(error))
        with pytest.raises(SessionClosedError):
            await session.read_byte()
        with pytest.raises(SessionClosedError):
            await session.write("x")
        assert await session.read_byte_with_timeout(0.5) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [ConnectionResetError(104, "reset"), asyncssh.ConnectionLost("lost")])
def test_ssh_hangup_logs_one_info_line(caplog, error):
    async def handler(session) -> None:
        await session.read_byte()

    async def scenario() -> None:
        server = ssh_module.SSHServer("127.0.0.1", 0, None, handler)
        await server._handle_process(_Process(error))

    with caplog.at_level(logging.DEBUG, logger=ssh_module.__name__):
        asyncio.run(scenario())
    records = [r for r in caplog.records if r.name == ssh_module.__name__]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.INFO, "SSH caller 198.51.100.7 disconnected")
    ]
