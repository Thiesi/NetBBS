"""
Tests for `netbbs.net.local_cli.LocalCLISession` (design doc -- SysOp
foundation round) -- exercised entirely through injected fake byte-read
functions, no real terminal involved. `read_line`/`read_key` just
delegate to `netbbs.net.char_input`, already covered on its own merits
elsewhere (tests/test_char_input*.py); this file only checks that
delegation actually happens and that `write`'s CRLF normalization
matches `TelnetSession.write`'s.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net import local_cli
from netbbs.net.local_cli import LocalCLISession
from netbbs.net.session import SessionClosedError


class _FakeBuffer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    def flush(self) -> None:
        pass


class _FakeStdout:
    def __init__(self) -> None:
        self.text = ""
        self.buffer = _FakeBuffer()

    def write(self, s: str) -> None:
        self.text += s

    def flush(self) -> None:
        pass


def _patch_stdout(monkeypatch) -> _FakeStdout:
    """
    Deliberately called from *inside* each test body, not from a
    separate `@pytest.fixture` -- pytest's own capture plugin resets
    `sys.stdout` to a fresh capture object of its own at the start of
    each test's "call" phase, which clobbers a patch applied during a
    fixture's "setup"-phase execution (confirmed by hand: the fixture
    version of this helper passed under `pytest -s` but silently wrote
    to the real stdout, not the fake, under plain `pytest -q`). Calling
    this as the first line of the test body runs it *during* the call
    phase, after that reset already happened, so the patch actually
    sticks for the rest of the test.
    """
    fake = _FakeStdout()
    monkeypatch.setattr(local_cli.sys, "stdout", fake)
    return fake


def _fake_byte_source(text: str):
    """A shared byte queue: `read_byte_fn` blocks (conceptually) until
    a byte is available, `read_byte_with_timeout_fn` gives up
    immediately once the queue is empty -- matches how char_input uses
    the timeout variant purely to peek for a byte that might not be
    coming."""
    queue: list[int] = list(text.encode("utf-8"))

    def read_byte_fn() -> bytes:
        if not queue:
            return b""
        return bytes([queue.pop(0)])

    def read_byte_with_timeout_fn(timeout: float) -> bytes | None:
        if not queue:
            return None
        return bytes([queue.pop(0)])

    return read_byte_fn, read_byte_with_timeout_fn


# -- write() normalization ---------------------------------------------


def test_write_normalizes_bare_lf_to_crlf(monkeypatch):
    fake_stdout = _patch_stdout(monkeypatch)
    session = LocalCLISession()
    asyncio.run(session.write("line1\nline2\r\nline3"))
    assert fake_stdout.text == "line1\r\nline2\r\nline3"


def test_write_line_appends_crlf(monkeypatch):
    fake_stdout = _patch_stdout(monkeypatch)
    session = LocalCLISession()
    asyncio.run(session.write_line("hello"))
    assert fake_stdout.text == "hello\r\n"


def test_write_raw_goes_through_the_binary_buffer(monkeypatch):
    fake_stdout = _patch_stdout(monkeypatch)
    session = LocalCLISession()
    asyncio.run(session.write_raw(b"\x01\x02\xff"))
    assert fake_stdout.buffer.data == b"\x01\x02\xff"


# -- read_byte / read_byte_with_timeout ----------------------------------


def test_read_byte_returns_the_next_byte():
    read_byte_fn, read_byte_with_timeout_fn = _fake_byte_source("A")
    session = LocalCLISession(read_byte_fn=read_byte_fn, read_byte_with_timeout_fn=read_byte_with_timeout_fn)
    result = asyncio.run(session.read_byte())
    assert result == ord("A")


def test_read_byte_raises_session_closed_on_eof():
    read_byte_fn, read_byte_with_timeout_fn = _fake_byte_source("")
    session = LocalCLISession(read_byte_fn=read_byte_fn, read_byte_with_timeout_fn=read_byte_with_timeout_fn)
    with pytest.raises(SessionClosedError):
        asyncio.run(session.read_byte())


def test_read_byte_with_timeout_returns_none_when_nothing_arrives():
    read_byte_fn, read_byte_with_timeout_fn = _fake_byte_source("")
    session = LocalCLISession(read_byte_fn=read_byte_fn, read_byte_with_timeout_fn=read_byte_with_timeout_fn)
    result = asyncio.run(session.read_byte_with_timeout(0.01))
    assert result is None


# -- read_line / read_key delegate to char_input -------------------------


def test_read_line_delegates_to_char_input(monkeypatch):
    _patch_stdout(monkeypatch)
    read_byte_fn, read_byte_with_timeout_fn = _fake_byte_source("ab\r")
    session = LocalCLISession(read_byte_fn=read_byte_fn, read_byte_with_timeout_fn=read_byte_with_timeout_fn)
    result = asyncio.run(session.read_line())
    assert result == "ab"


def test_read_key_delegates_to_char_input(monkeypatch):
    _patch_stdout(monkeypatch)
    read_byte_fn, read_byte_with_timeout_fn = _fake_byte_source("q")
    session = LocalCLISession(read_byte_fn=read_byte_fn, read_byte_with_timeout_fn=read_byte_with_timeout_fn)
    result = asyncio.run(session.read_key())
    assert result == "q"


def test_read_line_masks_when_echo_is_false(monkeypatch):
    fake_stdout = _patch_stdout(monkeypatch)
    read_byte_fn, read_byte_with_timeout_fn = _fake_byte_source("hunter2\r")
    session = LocalCLISession(read_byte_fn=read_byte_fn, read_byte_with_timeout_fn=read_byte_with_timeout_fn)
    result = asyncio.run(session.read_line(echo=False))
    assert result == "hunter2"
    assert "hunter2" not in fake_stdout.text
