"""Regression coverage for issue #6: lone-CR lookahead must not eat input."""

import asyncio

from netbbs.net.char_input import read_line
from netbbs.net.session import SessionClosedError


class FakeByteSource:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def read_byte(self) -> int | None:
        if self._pos >= len(self._data):
            raise SessionClosedError("no more data")
        byte = self._data[self._pos]
        self._pos += 1
        return byte

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        if self._pos >= len(self._data):
            return None
        byte = self._data[self._pos]
        self._pos += 1
        return byte


async def _discard_output(text: str) -> None:
    pass


def test_lone_cr_preserves_first_byte_of_next_pipelined_line():
    async def scenario() -> None:
        source = FakeByteSource(b"first\rsecond\r")

        first = await read_line(source, _discard_output)
        second = await read_line(source, _discard_output)

        assert first == "first"
        assert second == "second"

    asyncio.run(scenario())


def test_crlf_still_consumes_lf_without_leaking_it_to_next_line():
    async def scenario() -> None:
        source = FakeByteSource(b"first\r\nsecond\r\n")

        first = await read_line(source, _discard_output)
        second = await read_line(source, _discard_output)

        assert first == "first"
        assert second == "second"

    asyncio.run(scenario())
