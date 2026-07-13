"""Regression tests for bounded Telnet subnegotiation parsing."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net import telnet
from netbbs.net.session import SessionClosedError
from netbbs.net.telnet import IAC, NAWS, SB, SE, TelnetSession


class _UnusedWriter:
    """Minimal writer placeholder; these tests exercise input only."""

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _session_with_input(data: bytes) -> TelnetSession:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    return TelnetSession(reader, _UnusedWriter())


def test_oversized_subnegotiation_is_rejected_before_unbounded_growth():
    async def scenario() -> None:
        payload = b"x" * (telnet._MAX_SUBNEGOTIATION_BODY + 1)
        session = _session_with_input(bytes([IAC, SB, NAWS]) + payload)

        with pytest.raises(SessionClosedError, match="too large"):
            await session.read_byte()

    asyncio.run(scenario())


def test_incomplete_subnegotiation_has_one_total_deadline(monkeypatch):
    async def scenario() -> None:
        monkeypatch.setattr(telnet, "_SUBNEGOTIATION_TIMEOUT", 0.01)
        session = _session_with_input(bytes([IAC, SB, NAWS]))

        with pytest.raises(SessionClosedError, match="timed out"):
            await session.read_byte()

    asyncio.run(scenario())


def test_bounded_parser_still_accepts_valid_naws():
    async def scenario() -> None:
        width = 132
        height = 43
        body = bytes([width >> 8, width & 0xFF, height >> 8, height & 0xFF])
        session = _session_with_input(bytes([IAC, SB, NAWS]) + body + bytes([IAC, SE]))

        assert await session.read_byte() is None
        assert session.terminal_width == width
        assert session.terminal_height == height

    asyncio.run(scenario())
