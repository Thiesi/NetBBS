"""
Real ZMODEM file-transfer protocol (design doc round 21/22/24).

Interoperates with actual Zmodem-capable terminal clients (SyncTERM,
lrzsz's rz/sz, etc.) — the whole reason this exists rather than a
NetBBS-specific transfer scheme: a generic Telnet/SSH client can't drive
a custom raw-byte protocol on its own (see design doc round 21's
discussion), but Zmodem is a real, decades-old wire protocol that many
terminal emulators already auto-detect and drive without any NetBBS-
specific support needed.

**Deliberately scoped down from the full 1988 spec, confirmed with
Thiesi before writing this:**

- **CRC-16 only, not CRC-32.** CRC-16 is the mandatory baseline every
  conformant ZMODEM implementation accepts; CRC-32 is a negotiated
  enhancement not worth the extra code for this project's needs.
- **No resume/crash-recovery.** Every transfer starts at offset 0 — a
  receiver-requested nonzero `ZRPOS` offset is treated as an error, not
  honored.
- **No batch mode.** One file per transfer, matching the file-area
  upload/download model this plugs into (`netbbs.net.file_flow`).
- **No retry/timeout resync state machine.** Classic ZMODEM's retry
  logic exists to cope with a noisy serial line dropping or corrupting
  bytes. Telnet/SSH both ride on TCP, which already guarantees reliable,
  in-order, error-checked delivery — the failure mode that machinery
  compensates for essentially doesn't happen here. A CRC mismatch or
  malformed frame raises `ZmodemError` and aborts the transfer
  immediately, rather than attempting automatic recovery.
- **One deliberate exception to "no timeouts":** every point where this
  module is waiting on the peer's *next expected response* (not
  mid-transfer bulk data, which has no fixed duration) is bounded by
  `_HANDSHAKE_TIMEOUT`. Without this, invoking `/upload` or `/download`
  against a terminal that doesn't actually support Zmodem — the most
  likely real-world failure, not data corruption — would hang the whole
  session forever waiting for a `ZRINIT`/`ZFILE` that will never come.
  This is a bounded wait-then-abort, not a retry loop: still consistent
  with "abort on error, don't attempt recovery."

Third-party-client interoperability (the actual point of doing this at
all) is verified here via this module's own sender talking to its own
receiver — genuinely exercises every framing/CRC/escaping code path, but
can't substitute for testing against a real external client. That needs
a real terminal (SyncTERM, or any lrzsz-equipped shell) connecting to a
running node — flagged explicitly in the round 24 sign-off note as
something to verify directly once such a client is available, the same
"verify directly, don't just claim it" standard the rest of this
project's networking work has held to.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from netbbs.net.session import Session

# -- protocol constants (Chuck Forsberg's ZMODEM spec) -----------------

ZPAD = 0x2A  # '*' — pad character, begins every header
ZDLE = 0x18  # Ctrl-X — escape byte; also the cancel signal when doubled

ZBIN = 0x41  # 'A' — binary header, CRC-16 follows

# Frame types
ZRQINIT = 0
ZRINIT = 1
ZFILE = 4
ZSKIP = 5
ZFIN = 8
ZRPOS = 9
ZDATA = 10
ZEOF = 11
ZACK = 3

# Data subpacket terminators
ZCRCE = 0x68  # end of frame, no more subpackets, no ACK expected
ZCRCG = 0x69  # more data, no ACK expected
ZCRCQ = 0x6A  # more data, ACK expected, sender may continue without waiting
ZCRCW = 0x6B  # more data, ACK required before the next subpacket

# Bytes that must be ZDLE-escaped wherever they appear in header or
# subpacket payload bytes — ZDLE itself, plus DLE/XON/XOFF and their
# 8th-bit-set counterparts (flow-control bytes a terminal or modem might
# otherwise act on). Narrower than the full historical allowlist (which
# also covers legacy X.25/"Telenet" and 8th-bit-stripping links this
# project's TCP transports don't have) — over-escaping is always safe,
# under-escaping isn't, and this covers everything that actually matters
# here.
_ESCAPE_BYTES = frozenset({ZDLE, 0x10, 0x90, 0x11, 0x91, 0x13, 0x93})

# Size of each ZDATA subpacket. Every subpacket in this implementation
# is ZCRCW-terminated (see module docstring: correctness over
# throughput, no streaming/windowing) — an 8 KiB chunk keeps the
# round-trip-per-chunk cost reasonable without needing any of that.
_SUBPACKET_SIZE = 8192

# See module docstring's "one deliberate exception to no timeouts."
_HANDSHAKE_TIMEOUT = 15.0

# GitHub issue #34: none of the bulk-data reception path below had any
# bound at all -- a peer could stream indefinitely, send one enormous
# unterminated subpacket, or simply stall forever mid-transfer while
# still holding the session task and growing process memory. Three
# independent bounds, each catching a different failure shape:
#
# - _MAX_SUBPACKET_BYTES caps one *decoded* subpacket, regardless of
#   the overall transfer size limit below -- a small multiple of this
#   implementation's own chunk size (_SUBPACKET_SIZE), generous enough
#   for any well-behaved sender using a different chunk size, still
#   finite for one that never sends a terminator.
# - _BULK_IDLE_TIMEOUT bounds *idle* time waiting for the next byte of
#   a subpacket -- not total transfer duration (a large but genuinely
#   in-progress transfer must not be killed for taking a while), just
#   a stalled one.
# - receive_file()'s own max_bytes parameter (netbbs.config.
#   get_max_upload_bytes) bounds the complete transfer, checked against
#   both the advertised ZFILE size (rejected before any data reception
#   starts) and the actual running received-byte count (in case the
#   advertised size was wrong or absent).
_MAX_SUBPACKET_BYTES = _SUBPACKET_SIZE * 4
_BULK_IDLE_TIMEOUT = 30.0


class ZmodemError(Exception):
    """
    Raised for any Zmodem protocol failure — a CRC mismatch, a
    malformed or unexpected frame, a cancel signal from the peer, or (at
    a header-wait point only) no response within `_HANDSHAKE_TIMEOUT`.

    No retry is attempted for any of these — see module docstring.
    """


@dataclass(frozen=True)
class ReceivedFile:
    filename: str
    data: bytes


def _safe_filename(raw: str) -> str:
    """Extracts a safe basename from a remote-supplied filename (GitHub
    issue #34): strips any path component (a peer could otherwise claim
    a name like `../../etc/passwd` or an absolute path), drops NUL and
    other control characters, and caps the result's length. Falls back
    to `"unnamed"` for anything that sanitizes down to nothing, same as
    the pre-existing empty-name fallback this replaces."""
    # basename: strip anything before the last '/' or '\\' -- covers
    # both Unix and Windows-style separators regardless of which OS the
    # sending client or this node happens to be running on.
    basename = re.split(r"[/\\]", raw)[-1]
    cleaned = "".join(ch for ch in basename if ch.isprintable() and ch not in "\x00")
    cleaned = cleaned.strip()
    if not cleaned:
        return "unnamed"
    return cleaned[:255]


# -- CRC-16 (CCITT) ----------------------------------------------------


def _crc16(data: bytes) -> int:
    """Poly 0x1021, init 0, no reflection — the baseline every
    conformant ZMODEM implementation must support (see module
    docstring re: CRC-16-only scoping)."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# -- ZDLE encoding -------------------------------------------------------


def _zdle_encode(data: bytes) -> bytes:
    out = bytearray()
    for byte in data:
        if byte in _ESCAPE_BYTES:
            out.append(ZDLE)
            out.append(byte ^ 0x40)
        else:
            out.append(byte)
    return bytes(out)


async def _read_raw_byte(session: Session) -> int:
    """Read the next real data byte, transparently skipping any
    transport-level action (Telnet negotiation, an SSH resize
    notification) with no data significance — same "loop past `None`"
    contract `netbbs.net.char_input` already uses against the same
    `Session.read_byte`."""
    while True:
        b = await session.read_byte()
        if b is not None:
            return b


async def _read_zdle_byte(session: Session) -> int:
    """
    Read one logical byte, resolving ZDLE-escaping if present.

    A `ZDLE` immediately followed by another literal `ZDLE` byte is
    never a valid escape sequence under this encoding (escaping the
    `ZDLE` byte value itself produces `ZDLE 0x58`, never `ZDLE ZDLE` —
    see `_zdle_encode`), so seeing that pair unambiguously means the
    peer sent a cancel signal, not corrupted framing.
    """
    b = await _read_raw_byte(session)
    if b != ZDLE:
        return b
    b2 = await _read_raw_byte(session)
    if b2 == ZDLE:
        raise ZmodemError("transfer cancelled by peer")
    return b2 ^ 0x40


# -- headers -------------------------------------------------------------


def _position_bytes(position: int) -> bytes:
    # Little-endian per spec: P0 is the least-significant byte, P3 the
    # most-significant.
    return bytes(
        [
            position & 0xFF,
            (position >> 8) & 0xFF,
            (position >> 16) & 0xFF,
            (position >> 24) & 0xFF,
        ]
    )


async def _send_header(session: Session, frame_type: int, position: int = 0) -> None:
    payload = bytes([frame_type]) + _position_bytes(position)
    crc = _crc16(payload)
    crc_bytes = bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    frame = bytes([ZPAD, ZDLE, ZBIN]) + _zdle_encode(payload + crc_bytes)
    await session.write_raw(frame)


async def _read_header(session: Session) -> tuple[int, int]:
    """
    Scan for and decode the next binary header, returning
    `(frame_type, position)`.

    Scans past any bytes that aren't part of a valid `ZPAD ZDLE ZBIN`
    prefix — real terminal clients can interleave harmless noise (e.g. a
    trailing newline from a preceding text prompt) before a header
    actually starts.
    """
    while True:
        b = await _read_raw_byte(session)
        if b != ZPAD:
            continue
        b = await _read_raw_byte(session)
        while b == ZPAD:
            b = await _read_raw_byte(session)
        if b != ZDLE:
            continue
        b = await _read_raw_byte(session)
        if b == ZBIN:
            break
        # Not a binary header (ZHEX, or garbage) — not supported in
        # this implementation; keep scanning rather than raising, since
        # a byte that merely looks like the start of a header we don't
        # understand isn't necessarily an actual protocol violation.

    payload = bytearray()
    for _ in range(5):
        payload.append(await _read_zdle_byte(session))
    crc_hi = await _read_zdle_byte(session)
    crc_lo = await _read_zdle_byte(session)
    if _crc16(bytes(payload)) != (crc_hi << 8) | crc_lo:
        raise ZmodemError("header CRC mismatch")

    frame_type = payload[0]
    position = payload[1] | (payload[2] << 8) | (payload[3] << 16) | (payload[4] << 24)
    return frame_type, position


async def _wait_for_header(session: Session) -> tuple[int, int]:
    """`_read_header`, bounded by `_HANDSHAKE_TIMEOUT` — see module
    docstring's "one deliberate exception to no timeouts.\""""
    try:
        return await asyncio.wait_for(_read_header(session), timeout=_HANDSHAKE_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise ZmodemError(
            "no response from client — does your terminal support Zmodem?"
        ) from exc


# -- data subpackets -------------------------------------------------------


async def _send_subpacket(session: Session, data: bytes, terminator: int) -> None:
    crc = _crc16(data + bytes([terminator]))
    crc_bytes = bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    frame = (
        _zdle_encode(data)
        + bytes([ZDLE, terminator])
        + _zdle_encode(crc_bytes)
    )
    await session.write_raw(frame)


async def _read_subpacket(session: Session) -> tuple[bytes, int]:
    """Returns `(data, terminator)`. Raises `ZmodemError` on CRC
    mismatch, a cancel signal from the peer, an unterminated subpacket
    growing past `_MAX_SUBPACKET_BYTES`, or no byte arriving within
    `_BULK_IDLE_TIMEOUT` of the previous one (GitHub issue #34)."""
    data = bytearray()
    terminator = None
    while terminator is None:
        try:
            b = await asyncio.wait_for(_read_raw_byte(session), timeout=_BULK_IDLE_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise ZmodemError("transfer stalled — no data received in time") from exc
        if b != ZDLE:
            if len(data) >= _MAX_SUBPACKET_BYTES:
                raise ZmodemError(
                    f"data subpacket exceeded {_MAX_SUBPACKET_BYTES} bytes with no terminator"
                )
            data.append(b)
            continue
        b2 = await _read_raw_byte(session)
        if b2 == ZDLE:
            raise ZmodemError("transfer cancelled by peer")
        if b2 in (ZCRCE, ZCRCG, ZCRCQ, ZCRCW):
            terminator = b2
        else:
            if len(data) >= _MAX_SUBPACKET_BYTES:
                raise ZmodemError(
                    f"data subpacket exceeded {_MAX_SUBPACKET_BYTES} bytes with no terminator"
                )
            data.append(b2 ^ 0x40)

    crc_hi = await _read_zdle_byte(session)
    crc_lo = await _read_zdle_byte(session)
    if _crc16(bytes(data) + bytes([terminator])) != (crc_hi << 8) | crc_lo:
        raise ZmodemError("data subpacket CRC mismatch")
    return bytes(data), terminator


# -- sender (download: NetBBS sends a file to the connecting client) -----


async def send_file(session: Session, filename: str, data: bytes) -> None:
    """
    Send `data` to the client as `filename` via Zmodem.

    Assumes the client's terminal is either already running an `rz`-
    equivalent, or will auto-detect the initial `ZRQINIT` and start one
    — see module docstring re: this being the entire reason to use a
    real, terminal-recognized protocol instead of a custom one.
    """
    await _send_header(session, ZRQINIT)
    frame_type, _ = await _wait_for_header(session)
    if frame_type != ZRINIT:
        raise ZmodemError(f"expected ZRINIT, got frame type {frame_type}")

    file_info = f"{filename}\x00{len(data)} 0 0 0 0 0\x00".encode("ascii", errors="replace")
    await _send_header(session, ZFILE)
    await _send_subpacket(session, file_info, ZCRCW)

    frame_type, position = await _wait_for_header(session)
    if frame_type == ZSKIP:
        raise ZmodemError("receiver skipped the file")
    if frame_type != ZRPOS:
        raise ZmodemError(f"expected ZRPOS, got frame type {frame_type}")
    if position != 0:
        raise ZmodemError("resume is not supported")  # module docstring scoping

    await _send_header(session, ZDATA, 0)
    offset = 0
    while True:
        chunk = data[offset : offset + _SUBPACKET_SIZE]
        offset += len(chunk)
        at_end = offset >= len(data)
        await _send_subpacket(session, chunk, ZCRCE if at_end else ZCRCW)
        if at_end:
            break
        frame_type, _ = await _wait_for_header(session)
        if frame_type != ZACK:
            raise ZmodemError(f"expected ZACK, got frame type {frame_type}")

    await _send_header(session, ZEOF, len(data))
    frame_type, _ = await _wait_for_header(session)
    if frame_type != ZRINIT:
        raise ZmodemError(f"expected ZRINIT after ZEOF, got frame type {frame_type}")

    await _send_header(session, ZFIN)
    # A well-behaved peer replies with its own ZFIN and two 'O' bytes —
    # not waited for here. The transfer's actual content is already
    # fully and verifiably delivered by this point (the receiver only
    # sends ZRINIT after ZEOF once it has confirmed the complete file);
    # anything after this is session teardown the peer's own client
    # handles on its own, not something the caller needs to block on.


# -- receiver (upload: NetBBS receives a file from the connecting client) -


async def receive_file(session: Session, *, max_bytes: int) -> ReceivedFile:
    """
    Receive one file from the client via Zmodem, returning its filename
    and complete contents.

    Assumes the client's terminal is either already running a `sz`-
    equivalent, or the SysOp/user has just told it to start one — unlike
    download, upload has no auto-detectable trigger analogous to
    `ZRQINIT` appearing unprompted in the stream, since *we* have to
    speak first (send `ZRINIT`) to invite the client to send.

    `max_bytes` (GitHub issue #34, typically `netbbs.config.
    get_max_upload_bytes`) bounds the transfer twice: the size the
    sender itself advertises in `ZFILE` is checked and rejected before
    any bulk data is ever read, and the actual running received-byte
    count is checked after every subpacket regardless -- an advertised
    size is peer-supplied metadata, not authoritative, so the second
    check is the one that actually matters if the two ever disagree.
    """
    await _send_header(session, ZRINIT, 0)

    frame_type, _ = await _wait_for_header(session)
    if frame_type == ZRQINIT:
        # The sender's own initial "please announce yourself" — already
        # satisfied by the ZRINIT just sent above (sent proactively,
        # not in response to this), so there's nothing further to send
        # here. Sending another ZRINIT would desync the header stream:
        # the sender only expects to read *one* ZRINIT before moving on
        # to ZFILE, so a second one would be misread as the reply to
        # whatever the sender asks next.
        frame_type, _ = await _wait_for_header(session)
    if frame_type != ZFILE:
        raise ZmodemError(f"expected ZFILE, got frame type {frame_type}")

    info, _terminator = await _read_subpacket(session)
    parts = info.split(b"\x00", 1)
    # Defensive fallback for a sender that omits the NUL terminator
    # entirely, in which case the whole "filename size mtime ..." field
    # runs together space-separated -- take only the first token either
    # way, same as this code did before issue #34's filename hardening.
    raw_filename = parts[0].decode("ascii", errors="replace").split(" ")[0]
    filename = _safe_filename(raw_filename) if raw_filename else "unnamed"

    if len(parts) > 1:
        # ZFILE's metadata field is "{size} {mtime} {mode} {serial}
        # {files_remaining} {bytes_remaining}" (space-separated,
        # ASCII), per spec -- only the leading size field matters here.
        # Absent/malformed metadata isn't itself an error (some senders
        # omit it); it just means the early-rejection check below can't
        # run, and the running-total check during actual reception
        # remains the authoritative bound regardless.
        size_field = parts[1].split(b" ", 1)[0]
        if size_field.isdigit() and int(size_field) > max_bytes:
            raise ZmodemError(
                f"advertised file size {int(size_field)} exceeds the {max_bytes}-byte upload limit"
            )

    await _send_header(session, ZRPOS, 0)

    received = bytearray()
    while True:
        frame_type, position = await _wait_for_header(session)
        if frame_type == ZEOF:
            break
        if frame_type != ZDATA:
            raise ZmodemError(f"expected ZDATA, got frame type {frame_type}")
        if position != len(received):
            raise ZmodemError("out-of-order data (resume is not supported)")

        while True:
            chunk, terminator = await _read_subpacket(session)
            if len(received) + len(chunk) > max_bytes:
                raise ZmodemError(f"upload exceeded the {max_bytes}-byte limit")
            received.extend(chunk)
            if terminator in (ZCRCW, ZCRCQ):
                await _send_header(session, ZACK, len(received))
            if terminator == ZCRCE:
                break
            # ZCRCG/ZCRCQ/ZCRCW (already ACKed if needed above): more
            # subpackets follow under this same ZDATA run.

    await _send_header(session, ZRINIT, 0)
    frame_type, _ = await _wait_for_header(session)
    if frame_type != ZFIN:
        raise ZmodemError(f"expected ZFIN, got frame type {frame_type}")
    await _send_header(session, ZFIN)

    return ReceivedFile(filename=filename, data=bytes(received))
