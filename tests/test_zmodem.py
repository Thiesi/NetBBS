"""
Tests for netbbs.net.zmodem — real ZMODEM protocol framing, CRC-16, and
the sender/receiver state machines (design doc round 21/22/24).

The round-trip tests run this module's own `send_file` against its own
`receive_file`, connected by an in-memory duplex byte pipe rather than a
real Telnet/SSH socket — genuinely exercises every framing/escaping/CRC
code path (this is the real wire protocol, not a mock of it), but can't
substitute for testing against an actual external Zmodem client
(SyncTERM, lrzsz). See the module docstring and design doc round 24 for
why that's flagged as a separate, real-terminal verification step.
"""

from __future__ import annotations

import asyncio
import collections

import pytest

from netbbs.net.session import Session, SessionClosedError
from netbbs.net.zmodem import (
    ZDLE,
    ZEOF,
    ZPAD,
    ZmodemError,
    _crc16,
    _send_header,
    _wait_for_header,
    _zdle_encode,
    receive_file,
    send_file,
)


# -- fake in-memory duplex Session, for exercising real protocol logic ----


class _BytePipe:
    def __init__(self):
        self._buffer: collections.deque[int] = collections.deque()
        self._event = asyncio.Event()
        self._closed = False

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)
        self._event.set()

    def close(self) -> None:
        self._closed = True
        self._event.set()

    async def read_byte(self) -> int:
        while not self._buffer:
            if self._closed:
                raise SessionClosedError("pipe closed")
            self._event.clear()
            await self._event.wait()
        return self._buffer.popleft()


class FakeSession(Session):
    """Minimal Session implementation over an in-memory byte pipe —
    only read_byte/write_raw are exercised by netbbs.net.zmodem;
    read_line/read_key aren't implemented since nothing here uses
    them."""

    def __init__(self, read_pipe: _BytePipe, write_pipe: _BytePipe):
        self._read_pipe = read_pipe
        self._write_pipe = write_pipe

    async def write(self, text: str) -> None:
        self._write_pipe.feed(text.encode())

    async def write_raw(self, data: bytes) -> None:
        self._write_pipe.feed(data)

    async def read_line(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        self._write_pipe.close()

    async def read_byte(self) -> int | None:
        return await self._read_pipe.read_byte()


def _session_pair() -> tuple[FakeSession, FakeSession]:
    a_to_b = _BytePipe()
    b_to_a = _BytePipe()
    sender_side = FakeSession(read_pipe=b_to_a, write_pipe=a_to_b)
    receiver_side = FakeSession(read_pipe=a_to_b, write_pipe=b_to_a)
    return sender_side, receiver_side


# -- CRC-16 and ZDLE escaping (pure functions) -----------------------------


def test_crc16_of_empty_is_zero():
    assert _crc16(b"") == 0


def test_crc16_is_deterministic():
    assert _crc16(b"hello") == _crc16(b"hello")


def test_crc16_differs_for_different_input():
    assert _crc16(b"hello") != _crc16(b"jello")


def test_zdle_encode_escapes_zdle_byte():
    encoded = _zdle_encode(bytes([ZDLE]))
    assert encoded == bytes([ZDLE, ZDLE ^ 0x40])


def test_zdle_encode_leaves_ordinary_bytes_unescaped():
    assert _zdle_encode(b"hello") == b"hello"


def test_zdle_encode_does_not_escape_zpad():
    # ZPAD (0x2a) only matters as a header *prefix*; it's an ordinary
    # data byte otherwise and must not be escaped.
    assert _zdle_encode(bytes([ZPAD])) == bytes([ZPAD])


# -- round trip: this module's sender against its own receiver ------------


def _round_trip(filename: str, data: bytes) -> tuple[str, bytes]:
    async def scenario():
        sender_session, receiver_session = _session_pair()
        sender_task = asyncio.create_task(send_file(sender_session, filename, data))
        receiver_task = asyncio.create_task(receive_file(receiver_session))
        await sender_task
        result = await receiver_task
        return result.filename, result.data

    return asyncio.run(scenario())


def test_round_trip_small_file():
    name, data = _round_trip("readme.txt", b"hello world")
    assert name == "readme.txt"
    assert data == b"hello world"


def test_round_trip_empty_file():
    name, data = _round_trip("empty.txt", b"")
    assert name == "empty.txt"
    assert data == b""


def test_round_trip_multi_chunk_file():
    # Larger than _SUBPACKET_SIZE (8192), forcing multiple ZDATA
    # subpackets and ZACK round trips, not just a single chunk.
    payload = bytes((i % 256) for i in range(20000))
    name, data = _round_trip("big.bin", payload)
    assert name == "big.bin"
    assert data == payload


def test_round_trip_preserves_reserved_protocol_bytes_in_content():
    # File content containing every byte value ZDLE-escaping has to
    # handle correctly (ZDLE itself, ZPAD, XON/XOFF, DLE) -- proves
    # escaping/unescaping round-trips exactly, not just "ordinary" text.
    payload = bytes([0x18, 0x2A, 0x10, 0x90, 0x11, 0x91, 0x13, 0x93, 0x00, 0xFF]) * 50
    name, data = _round_trip("binary.dat", payload)
    assert data == payload


def test_round_trip_preserves_all_256_byte_values():
    payload = bytes(range(256)) * 10
    _, data = _round_trip("allbytes.dat", payload)
    assert data == payload


# -- error handling ---------------------------------------------------------


def test_corrupted_data_raises_zmodem_error():
    """A bit-flip in transit should be caught as a CRC mismatch, not
    silently accepted -- proves the "abort on error, no retry" scoping
    (design doc round 24) actually detects corruption rather than
    trusting the transport blindly."""

    async def scenario():
        sender_session, receiver_session = _session_pair()

        # Corrupt the very last byte written to the receiver's read
        # pipe (the tail end of the first data subpacket) after a short
        # delay, simulating a single flipped bit reaching the receiver.
        original_feed = receiver_session._read_pipe.feed

        state = {"corrupted": False}

        def corrupting_feed(data: bytes) -> None:
            if not state["corrupted"] and len(data) > 4:
                data = bytearray(data)
                data[-3] ^= 0xFF
                state["corrupted"] = True
                original_feed(bytes(data))
            else:
                original_feed(data)

        receiver_session._read_pipe.feed = corrupting_feed

        sender_task = asyncio.create_task(send_file(sender_session, "x.txt", b"hello world" * 10))
        with pytest.raises(ZmodemError):
            await receive_file(receiver_session)
        sender_task.cancel()
        try:
            await sender_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(scenario())


def test_no_response_from_peer_times_out(monkeypatch):
    import netbbs.net.zmodem as zmodem_module

    monkeypatch.setattr(zmodem_module, "_HANDSHAKE_TIMEOUT", 0.1)

    async def scenario():
        sender_session, _receiver_session = _session_pair()
        # No receiver ever reads/responds -- send_file's first
        # _wait_for_header should time out rather than hang forever.
        with pytest.raises(ZmodemError, match="no response"):
            await send_file(sender_session, "x.txt", b"data")

    asyncio.run(scenario())


def test_receiver_rejects_unexpected_frame_type():
    async def scenario():
        sender_session, receiver_session = _session_pair()
        # Send something that isn't a valid ZFILE after ZRINIT.
        receiver_task = asyncio.create_task(receive_file(receiver_session))
        await _wait_for_header(sender_session)  # consume the receiver's ZRINIT
        await _send_header(sender_session, ZEOF)  # nonsense at this point
        with pytest.raises(ZmodemError, match="ZFILE"):
            await receiver_task

    asyncio.run(scenario())


def test_read_header_raises_on_cancel_signal():
    async def scenario():
        pipe_out, pipe_in = _BytePipe(), _BytePipe()
        session = FakeSession(read_pipe=pipe_in, write_pipe=pipe_out)
        # A ZDLE immediately followed by another literal ZDLE is never
        # valid escaped data (see zmodem.py's _read_zdle_byte docstring)
        # -- unambiguously a cancel signal.
        pipe_in.feed(bytes([ZPAD, ZDLE, 0x41, ZDLE, ZDLE]))
        from netbbs.net.zmodem import _read_header

        with pytest.raises(ZmodemError, match="cancelled"):
            await _read_header(session)

    asyncio.run(scenario())
