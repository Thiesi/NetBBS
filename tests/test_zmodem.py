"""
Tests for netbbs.net.zmodem — real ZMODEM protocol framing, CRC-16, and
the sender/receiver state machines.

The round-trip tests run this module's own `send_file` against its own
`receive_file`, connected by an in-memory duplex byte pipe rather than a
real Telnet/SSH socket — genuinely exercises every framing/escaping/CRC
code path (this is the real wire protocol, not a mock of it), but can't
substitute for testing against an actual external Zmodem client
(SyncTERM, lrzsz). See the module docstring for why that's flagged as a
separate, real-terminal verification step.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import tempfile
from pathlib import Path

import pytest

import netbbs.net.zmodem as zmodem_module
from netbbs.net.session import Session, SessionClosedError
from netbbs.net.zmodem import (
    ZCRCE,
    ZCRCW,
    ZDATA,
    ZDLE,
    ZEOF,
    ZFILE,
    ZPAD,
    ZRINIT,
    ZRPOS,
    ZmodemError,
    _crc16,
    safe_filename,
    _send_header,
    _send_subpacket,
    _wait_for_header,
    _zdle_encode,
    receive_file,
    send_file,
)


@pytest.fixture(autouse=True)
def _short_drain(monkeypatch):
    """Every transfer ends by reading the line until it has been quiet a
    moment; a fraction of that keeps these tests quick."""
    monkeypatch.setattr(zmodem_module, "_DRAIN_QUIET", 0.02)


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

    async def read_line(self, echo: bool = True, **kwargs) -> str:
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
    """Round-trips through a real temp file, same as the real streaming
    receive path (GitHub issue #34, reopened a second time:
    receive_file no longer returns the content directly, only its hash/
    size) -- reads it back afterward purely for this test helper's own
    assertions, not something production code does."""

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            dest_path = Path(tmp) / "incoming"
            sender_session, receiver_session = _session_pair()
            sender_task = asyncio.create_task(send_file(sender_session, filename, data))
            receiver_task = asyncio.create_task(
                receive_file(receiver_session, max_bytes=10_000_000, dest_path=dest_path)
            )
            await sender_task
            result = await receiver_task
            received = dest_path.read_bytes()
            assert result.size_bytes == len(received)
            assert result.sha256 == hashlib.sha256(received).hexdigest()
            return result.filename, received

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


def _corrupt_first_data_subpacket(receiver_session: FakeSession, *, every_time: bool = False) -> None:
    """Flip one byte inside the data of the first (or every) ZDATA frame
    the receiver reads, simulating a bit flipped in transit."""
    original_feed = receiver_session._read_pipe.feed
    state = {"armed": False, "done": False}
    zdata_header = zmodem_module._binary_header(ZDATA, 0)[:4]

    def corrupting_feed(data: bytes) -> None:
        if data.startswith(zdata_header):
            state["armed"] = True
            original_feed(data)
            return
        if state["armed"] and not state["done"] and len(data) > 8:
            data = bytearray(data)
            data[2] ^= 0x01  # an ordinary data byte, never ZDLE-escaped here
            state["armed"] = False
            state["done"] = not every_time
            original_feed(bytes(data))
            return
        original_feed(data)

    receiver_session._read_pipe.feed = corrupting_feed


def test_corrupted_data_is_asked_for_again_and_arrives_intact(tmp_path):
    """A bit-flip in transit is caught as a CRC mismatch, not silently
    accepted -- and the receiver asks for the data again (ZRPOS), as a
    real receiver does, so the file still arrives whole."""

    async def scenario():
        sender_session, receiver_session = _session_pair()
        _corrupt_first_data_subpacket(receiver_session)
        payload = b"hello world" * 10
        sender_task = asyncio.create_task(send_file(sender_session, "x.txt", payload))
        result = await receive_file(receiver_session, max_bytes=10_000_000, dest_path=tmp_path / "incoming")
        await sender_task
        assert (tmp_path / "incoming").read_bytes() == payload
        assert result.size_bytes == len(payload)

    asyncio.run(scenario())


def test_data_that_keeps_arriving_corrupted_ends_the_transfer(tmp_path, monkeypatch):
    monkeypatch.setattr(zmodem_module, "_MAX_ERRORS", 3)

    async def scenario():
        sender_session, receiver_session = _session_pair()
        _corrupt_first_data_subpacket(receiver_session, every_time=True)
        sender_task = asyncio.create_task(send_file(sender_session, "x.txt", b"hello world" * 10))
        with pytest.raises(ZmodemError, match="too many errors"):
            await receive_file(receiver_session, max_bytes=10_000_000, dest_path=tmp_path / "incoming")
        sender_task.cancel()
        await asyncio.gather(sender_task, return_exceptions=True)

    asyncio.run(scenario())
    # GitHub issue #34: a failed transfer must not leave a partial file
    # behind in the caller's staging area.
    assert not (tmp_path / "incoming").exists()


def test_no_response_from_peer_times_out(monkeypatch):
    monkeypatch.setattr(zmodem_module, "_START_TIMEOUT", 0.1)
    monkeypatch.setattr(zmodem_module, "_HANDSHAKE_TIMEOUT", 0.1)

    async def scenario():
        sender_session, receiver_session = _session_pair()
        # No receiver ever reads/responds -- send_file's first header
        # wait should time out rather than hang forever, and then tell
        # the terminal to stop (CAN x10, BS x10).
        with pytest.raises(ZmodemError, match="no response"):
            await send_file(sender_session, "x.txt", b"data")
        written = bytes(receiver_session._read_pipe._buffer)
        assert written.startswith(b"rz\r**\x18B00000000000000\r\x8a\x11")
        assert written.endswith(bytes([0x18] * 10 + [0x08] * 10))

    asyncio.run(scenario())


def test_receiver_rejects_unexpected_frame_type(tmp_path):
    async def scenario():
        sender_session, receiver_session = _session_pair()
        # Send something that isn't a valid ZFILE after ZRINIT.
        receiver_task = asyncio.create_task(
            receive_file(receiver_session, max_bytes=10_000_000, dest_path=tmp_path / "incoming")
        )
        await _wait_for_header(sender_session)  # consume the receiver's ZRINIT
        await _send_header(sender_session, ZEOF)  # nonsense at this point
        with pytest.raises(ZmodemError, match="ZFILE"):
            await receiver_task

    asyncio.run(scenario())


# -- GitHub issue #34: bounds on the bulk-data reception path ---------------


def test_advertised_size_over_the_limit_is_rejected_before_bulk_data(tmp_path):
    async def scenario():
        sender_session, receiver_session = _session_pair()
        receiver_task = asyncio.create_task(
            receive_file(receiver_session, max_bytes=10, dest_path=tmp_path / "incoming")
        )
        # send_file's own ZFILE metadata always advertises the true
        # size (20 bytes here), so this exercises the early-rejection
        # path against an honest sender declaring more than allowed.
        sender_task = asyncio.create_task(send_file(sender_session, "big.bin", b"x" * 20))

        with pytest.raises(ZmodemError, match="advertised"):
            await receiver_task
        sender_task.cancel()
        try:
            await sender_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(scenario())


def test_sender_exceeding_its_own_declared_size_is_rejected(tmp_path):
    """A malicious sender could advertise a small size (passing the
    early check) and then simply keep sending -- the running received-
    byte count, checked on every subpacket regardless of what was
    declared, is the actual authoritative bound (GitHub issue #34)."""

    async def scenario():
        sender_session, receiver_session = _session_pair()
        receiver_task = asyncio.create_task(
            receive_file(receiver_session, max_bytes=10, dest_path=tmp_path / "incoming")
        )

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRINIT
        await _send_header(sender_session, ZFILE)
        await _send_subpacket(sender_session, b"lie.txt\x005 0 0 0 0 0\x00", ZCRCW)  # declares only 5 bytes

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRPOS

        await _send_header(sender_session, ZDATA, 0)
        await _send_subpacket(sender_session, b"x" * 20, ZCRCE)  # actually sends far more

        with pytest.raises(ZmodemError, match="exceeded"):
            await receiver_task

    asyncio.run(scenario())


def test_unterminated_subpacket_past_the_cap_is_rejected(monkeypatch, tmp_path):
    import netbbs.net.zmodem as zmodem_module

    monkeypatch.setattr(zmodem_module, "_MAX_SUBPACKET_BYTES", 8)

    async def scenario():
        sender_session, receiver_session = _session_pair()
        receiver_task = asyncio.create_task(
            receive_file(receiver_session, max_bytes=10_000_000, dest_path=tmp_path / "incoming")
        )

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRINIT
        await _send_header(sender_session, ZFILE)
        await _send_subpacket(sender_session, b"x.bin\x00", ZCRCW)

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRPOS

        await _send_header(sender_session, ZDATA, 0)
        # Raw data with no ZDLE terminator at all, past the (patched)
        # 8-byte cap -- a genuinely malformed/hostile subpacket that
        # never ends.
        await sender_session.write_raw(b"y" * 100)

        with pytest.raises(ZmodemError, match="no terminator"):
            await receiver_task

    asyncio.run(scenario())


def test_stalled_transfer_hits_the_idle_timeout(monkeypatch, tmp_path):
    import netbbs.net.zmodem as zmodem_module

    monkeypatch.setattr(zmodem_module, "_BULK_IDLE_TIMEOUT", 0.1)

    async def scenario():
        sender_session, receiver_session = _session_pair()
        receiver_task = asyncio.create_task(
            receive_file(receiver_session, max_bytes=10_000_000, dest_path=tmp_path / "incoming")
        )

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRINIT
        await _send_header(sender_session, ZFILE)
        await _send_subpacket(sender_session, b"x.bin\x00", ZCRCW)

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRPOS

        await _send_header(sender_session, ZDATA, 0)
        await sender_session.write_raw(b"y")  # one byte, then nothing -- ever

        with pytest.raises(ZmodemError, match="stalled"):
            await receiver_task

    asyncio.run(scenario())


def test_stall_immediately_after_a_lone_zdle_hits_the_idle_timeout(monkeypatch, tmp_path):
    """Regression test for GitHub issue #34 (reopened): before routing
    every bulk-phase byte through _read_bulk_raw_byte, the byte
    immediately following ZDLE was read via the untimed
    _read_raw_byte -- a peer sending a bare ZDLE and then withholding
    everything else could stall the receiver forever despite the
    subpacket-level idle timeout supposedly covering this phase."""
    import netbbs.net.zmodem as zmodem_module

    monkeypatch.setattr(zmodem_module, "_BULK_IDLE_TIMEOUT", 0.1)

    async def scenario():
        sender_session, receiver_session = _session_pair()
        receiver_task = asyncio.create_task(
            receive_file(receiver_session, max_bytes=10_000_000, dest_path=tmp_path / "incoming")
        )

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRINIT
        await _send_header(sender_session, ZFILE)
        await _send_subpacket(sender_session, b"x.bin\x00", ZCRCW)

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRPOS

        await _send_header(sender_session, ZDATA, 0)
        await sender_session.write_raw(b"partial" + bytes([ZDLE]))  # lone ZDLE, then nothing -- ever

        with pytest.raises(ZmodemError, match="stalled"):
            await receiver_task

    asyncio.run(scenario())


def test_stall_after_the_terminator_before_crc_hi_hits_the_idle_timeout(monkeypatch, tmp_path):
    """A valid terminator arrived, but the sender then withholds both
    CRC bytes entirely -- must still time out, not wait forever for a
    CRC that will never come."""
    import netbbs.net.zmodem as zmodem_module

    monkeypatch.setattr(zmodem_module, "_BULK_IDLE_TIMEOUT", 0.1)

    async def scenario():
        sender_session, receiver_session = _session_pair()
        receiver_task = asyncio.create_task(
            receive_file(receiver_session, max_bytes=10_000_000, dest_path=tmp_path / "incoming")
        )

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRINIT
        await _send_header(sender_session, ZFILE)
        await _send_subpacket(sender_session, b"x.bin\x00", ZCRCW)

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRPOS

        await _send_header(sender_session, ZDATA, 0)
        await sender_session.write_raw(b"partial" + bytes([ZDLE, ZCRCE]))  # terminator sent, CRC withheld

        with pytest.raises(ZmodemError, match="stalled"):
            await receiver_task

    asyncio.run(scenario())


def test_stall_between_crc_hi_and_crc_lo_hits_the_idle_timeout(monkeypatch, tmp_path):
    """The terminator and the CRC high byte both arrived, but the
    sender withholds the final CRC low byte -- the narrowest possible
    stall position, and the one most likely to be missed by a fix that
    only re-times the *first* byte after the terminator."""
    import netbbs.net.zmodem as zmodem_module

    monkeypatch.setattr(zmodem_module, "_BULK_IDLE_TIMEOUT", 0.1)

    async def scenario():
        sender_session, receiver_session = _session_pair()
        receiver_task = asyncio.create_task(
            receive_file(receiver_session, max_bytes=10_000_000, dest_path=tmp_path / "incoming")
        )

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRINIT
        await _send_header(sender_session, ZFILE)
        await _send_subpacket(sender_session, b"x.bin\x00", ZCRCW)

        frame_type, _ = await _wait_for_header(sender_session)
        assert frame_type == ZRPOS

        await _send_header(sender_session, ZDATA, 0)
        # 0x42 ('B') isn't in the escape set, so this is an unambiguous,
        # unescaped crc_hi byte -- crc_lo is what's withheld.
        await sender_session.write_raw(b"partial" + bytes([ZDLE, ZCRCE, 0x42]))

        with pytest.raises(ZmodemError, match="stalled"):
            await receiver_task

    asyncio.run(scenario())


def test_round_trip_still_works_within_the_limit():
    """Confirms the bounds above don't interfere with a normal transfer
    comfortably inside them."""
    name, data = _round_trip("readme.txt", b"hello world")
    assert name == "readme.txt"
    assert data == b"hello world"


# -- safe_filename (GitHub issue #34) --------------------------------------


def test_safe_filename_strips_unix_path_components():
    assert safe_filename("../../etc/passwd") == "passwd"


def test_safe_filename_strips_windows_path_components():
    assert safe_filename("C:\\Users\\alice\\file.txt") == "file.txt"


def test_safe_filename_drops_control_characters():
    assert safe_filename("evil\x00\x01name.txt") == "evilname.txt"


def test_safe_filename_caps_length():
    assert len(safe_filename("x" * 500)) == 255


def test_safe_filename_falls_back_when_empty():
    assert safe_filename("") == "unnamed"
    assert safe_filename("/") == "unnamed"
    assert safe_filename("\x00\x00\x00") == "unnamed"


def test_safe_filename_preserves_an_ordinary_name():
    assert safe_filename("report-final.pdf") == "report-final.pdf"


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


# -- issue #963: what a real client (lrzsz, SyncTERM) sends and expects ----
#
# These drive NetBBS's sender and receiver with byte sequences written the
# way lrzsz's `sz`/`rz` write them -- hex headers from the receiver,
# ZCRCW ending a frame, CRC-32 frames, flow-control noise, five-CAN
# cancels -- rather than only with NetBBS's own other half.
# `tests/test_zmodem_lrzsz.py` runs the real binaries where installed.


def test_crc16_matches_the_xmodem_check_value():
    assert _crc16(b"123456789") == 0x31C3


def test_crc32_matches_the_standard_check_value():
    assert zmodem_module._crc32(b"123456789") == 0xCBF43926


def test_a_hex_zrqinit_is_the_pattern_terminals_watch_for():
    assert zmodem_module._hex_header(zmodem_module.ZRQINIT) == b"**\x18B00000000000000\r\x8a\x11"


def test_hex_zack_and_zfin_carry_no_trailing_xon():
    assert not zmodem_module._hex_header(zmodem_module.ZACK, 5).endswith(b"\x11")
    assert not zmodem_module._hex_header(zmodem_module.ZFIN).endswith(b"\x11")
    assert zmodem_module._hex_header(ZRPOS, 5).endswith(b"\r\x8a\x11")


def _reader(data: bytes) -> FakeSession:
    pipe = _BytePipe()
    pipe.feed(data)
    return FakeSession(read_pipe=pipe, write_pipe=_BytePipe())


def test_a_hex_header_reads_back_including_uppercase_digits():
    async def scenario():
        frame = zmodem_module._hex_header(ZRPOS, 0x01020304)
        header = await zmodem_module._scan_header(_reader(b"noise\r\n" + frame))
        assert (header.frame_type, header.position, header.crc32) == (ZRPOS, 0x01020304, False)
        upper = frame[:4] + frame[4:18].upper() + frame[18:]
        header = await zmodem_module._scan_header(_reader(upper))
        assert header.position == 0x01020304

    asyncio.run(scenario())


def test_a_header_with_a_bad_crc_is_skipped_not_fatal():
    async def scenario():
        bad = bytearray(zmodem_module._hex_header(ZRPOS, 7))
        bad[10] = ord("f") if bad[10] != ord("f") else ord("e")
        good = zmodem_module._hex_header(zmodem_module.ZACK, 9)
        header = await zmodem_module._scan_header(_reader(bytes(bad) + good))
        assert (header.frame_type, header.position) == (zmodem_module.ZACK, 9)

    asyncio.run(scenario())


def _bin32_header(frame_type: int, position: int = 0) -> bytes:
    payload = bytes([frame_type]) + zmodem_module._position_bytes(position)
    crc = zmodem_module._crc32(payload).to_bytes(4, "little")
    return bytes([ZPAD, ZDLE, zmodem_module.ZBIN32]) + _zdle_encode(payload + crc)


def _subpacket32(data: bytes, terminator: int) -> bytes:
    crc = zmodem_module._crc32(data + bytes([terminator])).to_bytes(4, "little")
    return _zdle_encode(data) + bytes([ZDLE, terminator]) + _zdle_encode(crc)


def _subpacket16(data: bytes, terminator: int) -> bytes:
    crc = _crc16(data + bytes([terminator]))
    return _zdle_encode(data) + bytes([ZDLE, terminator]) + _zdle_encode(bytes([crc >> 8, crc & 0xFF]))


class _ScriptedPeer:
    """The client end of a transfer, written by hand the way lrzsz
    behaves, with helpers to read what NetBBS sends."""

    def __init__(self, session: FakeSession):
        self.session = session

    async def header(self):
        return await zmodem_module._next_header(self.session, 2.0)

    async def send(self, data: bytes) -> None:
        await self.session.write_raw(data)


def test_upload_opens_with_a_hex_zrinit_so_terminals_start_their_sender(tmp_path):
    async def scenario():
        client, server = _session_pair()
        task = asyncio.create_task(receive_file(server, max_bytes=100, dest_path=tmp_path / "in"))
        await asyncio.sleep(0.05)
        opening = bytes(client._read_pipe._buffer)
        assert opening.startswith(b"**\x18B01")  # what SyncTERM matches on
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_upload_from_an_lrzsz_style_sender_with_crc32_frames(tmp_path):
    """sz answers our ZRINIT with rz\\r + ZRQINIT (we answer that with
    another ZRINIT), sends ZFILE and data with CRC-32, streams ZCRCG,
    ends a frame with ZCRCW and waits for the ZACK, then opens the next
    frame with a fresh ZDATA header -- and sprinkles XON in between."""

    async def scenario():
        client, server = _session_pair()
        peer = _ScriptedPeer(client)
        task = asyncio.create_task(receive_file(server, max_bytes=10_000, dest_path=tmp_path / "in"))
        first = await peer.header()
        assert first.frame_type == ZRINIT
        assert first.flags & zmodem_module.CANFDX and first.flags & zmodem_module.CANOVIO

        await peer.send(b"rz\r" + zmodem_module._hex_header(zmodem_module.ZRQINIT))
        assert (await peer.header()).frame_type == ZRINIT  # answered again

        await peer.send(_bin32_header(ZFILE) + _subpacket32(b"my notes.txt\x0010 0 0 0 1 10\x00", ZCRCW))
        start = await peer.header()
        assert (start.frame_type, start.position) == (ZRPOS, 0)

        await peer.send(_bin32_header(ZDATA, 0) + _subpacket32(b"abc", zmodem_module.ZCRCG)
                        + bytes([0x11]) + _subpacket32(b"\x18\x7f", ZCRCW))
        ack = await peer.header()
        assert (ack.frame_type, ack.position) == (zmodem_module.ZACK, 5)

        await peer.send(_bin32_header(ZDATA, 5) + _subpacket32(b"\xffdefg", ZCRCE))
        await peer.send(_bin32_header(ZEOF, 10))
        assert (await peer.header()).frame_type == ZRINIT
        await peer.send(zmodem_module._hex_header(zmodem_module.ZFIN))
        assert (await peer.header()).frame_type == zmodem_module.ZFIN
        await peer.send(b"OO")

        result = await task
        assert result.filename == "my notes.txt"  # a space in the name survives
        assert (tmp_path / "in").read_bytes() == b"abc\x18\x7f\xffdefg"
        # The `OO` was read off the line, not left for the next screen.
        assert not server._read_pipe._buffer

    asyncio.run(scenario())


def test_rub_escapes_decode_to_del_and_ff(tmp_path):
    """A sender escaping control characters sends 0x7F and 0xFF as ZDLE
    'l' and ZDLE 'm'."""

    async def scenario():
        client, server = _session_pair()
        peer = _ScriptedPeer(client)
        task = asyncio.create_task(receive_file(server, max_bytes=100, dest_path=tmp_path / "in"))
        await peer.header()
        await peer.send(_binary(ZFILE) + _subpacket16(b"r.bin\x00", ZCRCW))
        await peer.header()
        crc = _crc16(b"\x7f\xff" + bytes([ZCRCE]))
        body = bytes([ZDLE, zmodem_module.ZRUB0, ZDLE, zmodem_module.ZRUB1, ZDLE, ZCRCE]) + _zdle_encode(
            bytes([crc >> 8, crc & 0xFF])
        )
        await peer.send(_binary(ZDATA, 0) + body + _binary(ZEOF, 2))
        await peer.header()
        await peer.send(zmodem_module._hex_header(zmodem_module.ZFIN))
        await peer.header()
        await task
        assert (tmp_path / "in").read_bytes() == b"\x7f\xff"

    asyncio.run(scenario())


def _binary(frame_type: int, position: int = 0) -> bytes:
    return zmodem_module._binary_header(frame_type, position)


def test_a_second_file_in_a_batch_is_skipped(tmp_path):
    async def scenario():
        client, server = _session_pair()
        peer = _ScriptedPeer(client)
        task = asyncio.create_task(receive_file(server, max_bytes=100, dest_path=tmp_path / "in"))
        await peer.header()
        await peer.send(_binary(ZFILE) + _subpacket16(b"one.txt\x003\x00", ZCRCW))
        await peer.header()
        await peer.send(_binary(ZDATA, 0) + _subpacket16(b"one", ZCRCE) + _binary(ZEOF, 3))
        assert (await peer.header()).frame_type == ZRINIT
        await peer.send(_binary(ZFILE) + _subpacket16(b"two.txt\x003\x00", ZCRCW))
        assert (await peer.header()).frame_type == zmodem_module.ZSKIP
        await peer.send(zmodem_module._hex_header(zmodem_module.ZFIN))
        assert (await peer.header()).frame_type == zmodem_module.ZFIN
        result = await task
        assert result.filename == "one.txt"
        assert (tmp_path / "in").read_bytes() == b"one"

    asyncio.run(scenario())


def test_five_cans_from_the_terminal_cancel_an_upload(tmp_path):
    async def scenario():
        client, server = _session_pair()
        task = asyncio.create_task(receive_file(server, max_bytes=100, dest_path=tmp_path / "in"))
        await asyncio.sleep(0.05)
        client._read_pipe._buffer.clear()
        await client.write_raw(bytes([0x18] * 5))
        with pytest.raises(zmodem_module.ZmodemCancelled):
            await task
        # The terminal already stopped: no abort sequence is sent back.
        assert bytes([0x18] * 10) not in bytes(client._read_pipe._buffer)

    asyncio.run(scenario())
    assert not (tmp_path / "in").exists()


class _LrzszStyleReceiver:
    """The receiving end of a download, as `rz` behaves: ZRINIT (hex) with
    the given capabilities, ZRPOS from `resume_at`, ZACK after every
    ZCRCW/ZCRCQ, a new header after ZCRCW and ZCRCE, ZRINIT after ZEOF,
    ZFIN answered and `OO` read."""

    def __init__(self, session: FakeSession, *, flags: int, resume_at: int = 0):
        self.session = session
        self.flags = flags
        self.resume_at = resume_at
        self.data = bytearray()
        self.headers: list[int] = []
        self.terminators: list[int] = []
        self.opening = b""

    async def run(self) -> None:
        s = self.session
        await asyncio.sleep(0.02)
        self.opening = bytes(s._read_pipe._buffer)[:8]
        await s.write_raw(zmodem_module._hex_header(ZRINIT, self.flags << 24))
        while True:
            header = await zmodem_module._next_header(s, 2.0)
            self.headers.append(header.frame_type)
            if header.frame_type == zmodem_module.ZRQINIT:
                continue
            if header.frame_type == ZFILE:
                await zmodem_module._read_subpacket(s, crc32=header.crc32)
                self.data = bytearray(b"\x00" * self.resume_at)
                await s.write_raw(zmodem_module._hex_header(ZRPOS, self.resume_at))
                continue
            if header.frame_type == ZDATA:
                assert header.position == len(self.data)
                while True:
                    chunk, terminator = await zmodem_module._read_subpacket(s, crc32=header.crc32)
                    self.data += chunk
                    self.terminators.append(terminator)
                    if terminator in (ZCRCW, zmodem_module.ZCRCQ):
                        await s.write_raw(zmodem_module._hex_header(zmodem_module.ZACK, len(self.data)))
                    if terminator in (ZCRCW, ZCRCE):
                        break
                continue
            if header.frame_type == ZEOF:
                await s.write_raw(zmodem_module._hex_header(ZRINIT, self.flags << 24))
                continue
            if header.frame_type == zmodem_module.ZFIN:
                await s.write_raw(zmodem_module._hex_header(zmodem_module.ZFIN))
                # rz reads past the CR LF that ends the sender's hex
                # ZFIN to the two 'O's.
                seen = bytearray()
                while seen.count(b"O") < 2:
                    seen.append(await asyncio.wait_for(s.read_byte(), 2))
                assert seen.endswith(b"OO")
                return


def _download_to(receiver_factory, payload: bytes):
    async def scenario():
        server, client = _session_pair()
        receiver = receiver_factory(client)
        receiving = asyncio.create_task(receiver.run())
        await send_file(server, "file.bin", payload)
        await asyncio.wait_for(receiving, 5)
        return receiver

    return asyncio.run(scenario())


def test_download_opens_with_rz_and_a_hex_zrqinit():
    receiver = _download_to(lambda s: _LrzszStyleReceiver(s, flags=0x23), b"hi")
    assert receiver.opening == b"rz\r**\x18B0"
    assert bytes(receiver.data) == b"hi"


def test_download_to_a_streaming_receiver_uses_zcrcg_and_zcrcw_frames():
    payload = bytes(range(256)) * 400  # 100 KiB: several 32 KiB frames
    receiver = _download_to(lambda s: _LrzszStyleReceiver(s, flags=0x23), payload)
    assert bytes(receiver.data) == payload
    assert zmodem_module.ZCRCG in receiver.terminators
    assert receiver.terminators[-1] == ZCRCE
    # Every ZCRCW frame was followed by a fresh ZDATA header.
    assert receiver.headers.count(ZDATA) == receiver.terminators.count(ZCRCW) + 1


def test_download_to_a_receiver_that_cannot_overlap_io_waits_for_each_subpacket():
    payload = b"x" * 3000
    receiver = _download_to(lambda s: _LrzszStyleReceiver(s, flags=0), payload)
    assert bytes(receiver.data) == payload
    assert zmodem_module.ZCRCG not in receiver.terminators
    assert receiver.terminators == [ZCRCW, ZCRCW, ZCRCE]


def test_download_resumes_where_the_receiver_asks():
    payload = bytes(range(200))
    receiver = _download_to(lambda s: _LrzszStyleReceiver(s, flags=0x23, resume_at=150), payload)
    assert bytes(receiver.data[150:]) == payload[150:]
    assert len(receiver.data) == 200


def test_several_frames_of_every_awkward_byte_round_trip():
    # Past several 32 KiB frames; the megabyte case runs against real
    # lrzsz in tests/test_zmodem_lrzsz.py.
    payload = (bytes([0xFF, 0x18, 0x11, 0x13, 0x91, 0x93, 0x0D, 0x0A, 0x2A, 0x7F]) + bytes(range(256))) * 600
    _, data = _round_trip("big.bin", payload)
    assert data == payload


def test_a_flood_of_flow_control_bytes_still_hits_the_subpacket_cap(monkeypatch, tmp_path):
    """Unescaped XON/XOFF are dropped on read, but still count against
    _MAX_SUBPACKET_BYTES: a peer streaming nothing else can't keep the
    receiver busy forever on the idle timeout alone (PR #969 review)."""
    monkeypatch.setattr(zmodem_module, "_MAX_SUBPACKET_BYTES", 8)

    async def scenario():
        sender_session, receiver_session = _session_pair()
        receiver_task = asyncio.create_task(
            receive_file(receiver_session, max_bytes=10_000_000, dest_path=tmp_path / "incoming")
        )
        await _wait_for_header(sender_session)
        await _send_header(sender_session, ZFILE)
        await _send_subpacket(sender_session, b"x.bin\x00", ZCRCW)
        await _wait_for_header(sender_session)
        await _send_header(sender_session, ZDATA, 0)
        await sender_session.write_raw(bytes([0x11, 0x13]) * 50)
        with pytest.raises(ZmodemError, match="no terminator"):
            await receiver_task

    asyncio.run(scenario())


def test_a_flood_of_flow_control_bytes_in_a_crc_is_refused(monkeypatch):
    monkeypatch.setattr(zmodem_module, "_MAX_SUBPACKET_BYTES", 8)

    async def scenario():
        session = _reader(bytes([0x11]) * 50 + b"A")
        with pytest.raises(ZmodemError, match="flood"):
            await zmodem_module._read_zdle_byte(session)

    asyncio.run(scenario())
