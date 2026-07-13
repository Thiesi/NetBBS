"""
Tests for netbbs.net.char_input — the transport-agnostic character-mode
line/key reading extracted from netbbs.net.telnet once netbbs.net.ssh
needed identical logic against a completely different byte source.

Exercised here against a minimal fake ByteSource rather than a real
socket, unlike tests/test_telnet.py (which still covers this exact same
logic end-to-end over a real loopback connection via TelnetSession,
proving the extraction didn't change real-world behavior). These tests
exist to pin down the shared logic in isolation, independent of either
transport.
"""

from __future__ import annotations

import asyncio

import pytest

import netbbs.net.char_input as char_input_module
from netbbs.net.char_input import read_key, read_line
from netbbs.net.session import SessionClosedError


class FakeByteSource:
    """Feeds a fixed sequence of bytes one at a time; raises
    SessionClosedError once exhausted, matching a real transport's
    behavior when the connection closes mid-read."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def read_byte(self) -> int | None:
        if self._pos >= len(self._data):
            raise SessionClosedError("no more data")
        b = self._data[self._pos]
        self._pos += 1
        return b

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        if self._pos >= len(self._data):
            return None
        b = self._data[self._pos]
        self._pos += 1
        return b


class Writer:
    """Collects everything written via the write callback, for
    assertions on echo output."""

    def __init__(self):
        self.written: list[str] = []

    async def __call__(self, text: str) -> None:
        self.written.append(text)

    @property
    def joined(self) -> str:
        return "".join(self.written)


def test_read_line_returns_typed_text():
    async def scenario():
        source = FakeByteSource(b"hello\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "hello"

    asyncio.run(scenario())


def test_read_line_echoes_each_character():
    async def scenario():
        source = FakeByteSource(b"hi\r\n")
        writer = Writer()
        await read_line(source, writer)
        assert writer.joined == "hi\r\n"

    asyncio.run(scenario())


def test_read_line_echo_false_masks_with_asterisk():
    async def scenario():
        source = FakeByteSource(b"secret\r\n")
        writer = Writer()
        line = await read_line(source, writer, echo=False)
        assert line == "secret"
        assert writer.joined == "******\r\n"

    asyncio.run(scenario())


def test_read_line_bare_cr_terminates():
    async def scenario():
        source = FakeByteSource(b"abc\r")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "abc"

    asyncio.run(scenario())


def test_read_line_backspace_removes_last_character():
    async def scenario():
        source = FakeByteSource(b"abc\x08\r\n")  # "abc" + Backspace
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "ab"

    asyncio.run(scenario())


def test_read_line_backspace_on_empty_line_does_nothing():
    async def scenario():
        source = FakeByteSource(b"\x08a\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "a"

    asyncio.run(scenario())


def test_read_line_delete_byte_also_works_as_backspace():
    async def scenario():
        source = FakeByteSource(b"abc\x7f\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "ab"

    asyncio.run(scenario())


def test_read_line_decodes_two_byte_utf8():
    async def scenario():
        source = FakeByteSource("Müller".encode("utf-8") + b"\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "Müller"

    asyncio.run(scenario())


def test_read_line_discards_csi_escape_sequence():
    async def scenario():
        # ESC [ A (an up-arrow CSI sequence) shouldn't corrupt the line.
        source = FakeByteSource(b"ab\x1b[Ac\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "abc"

    asyncio.run(scenario())


def test_read_line_discards_ss3_escape_sequence():
    async def scenario():
        source = FakeByteSource(b"ab\x1bOPc\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "abc"

    asyncio.run(scenario())


def test_read_line_none_from_source_is_skipped():
    """A ByteSource returning None mid-stream (a transport-level action
    with no data, e.g. Telnet negotiation or an SSH resize notification)
    shouldn't appear in the line or need special handling by the reader
    -- both transports already resolve this internally per-byte."""

    _CR = 0x0D
    _LF = 0x0A

    class SourceWithNones:
        def __init__(self):
            self._bytes = iter([ord("a"), None, ord("b"), _CR, _LF])

        async def read_byte(self):
            return next(self._bytes)

        async def read_byte_with_timeout(self, timeout):
            return None

    async def scenario():
        writer = Writer()
        line = await read_line(SourceWithNones(), writer)
        assert line == "ab"

    asyncio.run(scenario())


def test_read_key_returns_immediately_no_enter_needed():
    async def scenario():
        source = FakeByteSource(b"q")
        writer = Writer()
        key = await read_key(source, writer)
        assert key == "q"

    asyncio.run(scenario())


def test_read_key_skips_control_bytes_and_returns_next_real_key():
    async def scenario():
        source = FakeByteSource(b"\r\n\x08\x7fz")
        writer = Writer()
        key = await read_key(source, writer)
        assert key == "z"

    asyncio.run(scenario())


def test_read_key_echo_false_masks_with_asterisk():
    async def scenario():
        source = FakeByteSource(b"x")
        writer = Writer()
        await read_key(source, writer, echo=False)
        assert writer.joined == "*"

    asyncio.run(scenario())


def test_connection_closed_mid_line_raises_session_closed_error():
    async def scenario():
        source = FakeByteSource(b"ab")  # no terminator -- source raises on next read
        writer = Writer()
        with pytest.raises(SessionClosedError):
            await read_line(source, writer)

    asyncio.run(scenario())


# -- bounded escape-sequence parsing (issue #5, CSI half) -------------------


def test_oversized_csi_sequence_raises_session_closed_error():
    async def scenario():
        # ESC [ followed by more non-terminating parameter bytes than the
        # cap allows -- none of these bytes fall in 0x40-0x7E, so the CSI
        # loop never finds a final byte and keeps consuming until the
        # length cap trips.
        oversized = b"9" * (char_input_module._MAX_ESCAPE_SEQUENCE_LENGTH + 1)
        source = FakeByteSource(b"a\x1b[" + oversized + b"Ab\r\n")
        writer = Writer()
        with pytest.raises(SessionClosedError, match="too long"):
            await read_line(source, writer)

    asyncio.run(scenario())


def test_csi_sequence_within_length_cap_still_works():
    async def scenario():
        # One byte under the cap, still non-terminating until the final
        # byte -- confirms the cap is a strict "more than N", not "N or
        # fewer also rejected".
        within_cap = b"9" * (char_input_module._MAX_ESCAPE_SEQUENCE_LENGTH - 1)
        source = FakeByteSource(b"a\x1b[" + within_cap + b"Ab\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "ab"

    asyncio.run(scenario())


def test_csi_sequence_exceeding_total_deadline_raises_session_closed_error(monkeypatch):
    """A client that keeps a CSI sequence "alive" by continuously sending
    parameter bytes -- each one arriving well within the per-byte
    _FOLLOWUP_BYTE_TIMEOUT, so no individual read ever times out -- must
    still be bounded by one total deadline for the whole sequence, not
    just by how many bytes it sends. Isolated from the length cap by
    raising it out of the way, so only the timeout can trip here."""

    monkeypatch.setattr(char_input_module, "_ESCAPE_SEQUENCE_TIMEOUT", 0.05)
    monkeypatch.setattr(char_input_module, "_MAX_ESCAPE_SEQUENCE_LENGTH", 1_000_000)

    class SlowTrickleSource:
        """First byte enters CSI mode ('['); every byte after that is a
        real (non-terminating) parameter byte returned after a short
        delay, forever -- never times out an individual read, never
        sends a final byte in 0x40-0x7E."""

        def __init__(self):
            self._first = True

        async def read_byte(self) -> int | None:
            raise AssertionError("read_line should only use read_byte_with_timeout here")

        async def read_byte_with_timeout(self, timeout: float) -> int | None:
            await asyncio.sleep(0.02)
            if self._first:
                self._first = False
                return 0x5B  # '[' -- enter CSI parameter mode
            return ord("9")

    async def scenario():
        source = SlowTrickleSource()
        # _discard_escape_sequence is what read_line/read_key dispatch to
        # after consuming the leading ESC byte itself -- called directly
        # here since SlowTrickleSource has no fixed buffer to pre-seed
        # with one.
        with pytest.raises(SessionClosedError, match="timed out"):
            await char_input_module._discard_escape_sequence(source)

    asyncio.run(scenario())


def test_valid_csi_and_ss3_sequences_still_work_within_new_bounds():
    async def scenario():
        source = FakeByteSource(b"a\x1b[Ab\x1bOPc\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "abc"

    asyncio.run(scenario())
