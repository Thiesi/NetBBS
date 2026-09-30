"""
Real ZMODEM file-transfer protocol (design doc).

Interoperates with actual Zmodem-capable terminal clients (SyncTERM,
lrzsz's rz/sz, etc.) — the whole reason this exists rather than a
NetBBS-specific transfer scheme: a generic Telnet/SSH client can't drive
a custom raw-byte protocol on its own (see design doc discussion), but
Zmodem is a real, decades-old wire protocol that many
terminal emulators already auto-detect and drive without any NetBBS-
specific support needed.

**What a real client needs, and what this module therefore does (issue
#963).** The first version only ever talked to itself, and no real
client could complete a transfer with it:

- **Auto-start.** A download opens with `rz\\r` and a ZRQINIT in the
  *hex* header form (`**` ZDLE `B00…`), exactly as `sz` does; an upload
  opens with a hex ZRINIT (`**` ZDLE `B01…`). Those two byte patterns
  are what terminals watch for to start their own receiver or sender.
- **Hex headers.** A real receiver answers in hex headers (ZRINIT,
  ZRPOS, ZACK, ZFIN), and this module's receiver sends them too. Every
  header read accepts hex, CRC-16 binary (`ZBIN`) and CRC-32 binary
  (`ZBIN32`) alike, and a data subpacket's CRC follows the header that
  opened its frame.
- **Frames.** ZCRCW *ends* a frame: the sender waits for the ZACK and
  opens the next frame with a fresh ZDATA header, the receiver waits
  for one. ZCRCQ asks for a ZACK without ending the frame; ZCRCG just
  continues. The sender streams ZCRCG subpackets when the receiver's
  ZRINIT says it can (CANFDX and CANOVIO) and sends one ZCRCW frame per
  subpacket when it can't.
- **Position and recovery.** The sender honours ZRPOS at any point,
  which is both resume (a receiver asking to start past 0) and error
  recovery. The receiver answers a data CRC error, or a frame at the
  wrong offset, with a ZRPOS for the byte it wants next, up to
  `_MAX_ERRORS` times per transfer.
- **Ending.** ZEOF, ZRINIT, then ZFIN both ways and `OO` from the
  sender, and whatever the terminal still sends is read off the line so
  it can't turn into keystrokes on the next screen.
- **Cancelling.** Five CAN (Ctrl-X) bytes in a row from the caller
  cancel a transfer, and a transfer NetBBS gives up on sends the
  terminal the usual CAN×10, BS×10 so its side stops too.

**Still deliberately scoped down:**

- **One file per transfer.** A sender offering more gets ZSKIP for every
  file after the first, matching the file-area model this plugs into
  (`netbbs.net.file_flow`).
- **Sending uses CRC-16 only.** Every conformant receiver accepts it.
  Receiving accepts CRC-32 from a sender that uses it anyway, but the
  ZRINIT sent here doesn't ask for it (no CANFC32): the transports are
  TCP, whose own checks already make a corrupted byte vanishingly rare.
- **No run-length encoding, encryption or compression** (ZBINR32,
  CANRLE, CANCRY, CANLZW), and no remote commands (ZCOMMAND is never
  executed).
- **Bounded waits, no endless retry loop.** Every wait for the peer's
  next header is bounded (`_START_TIMEOUT` for the first one, which may
  include the caller picking a file in their terminal's dialog,
  `_HANDSHAKE_TIMEOUT` after that), so a terminal without Zmodem support
  ends in a clear error rather than a hung session.

Third-party interoperability is verified in two layers. The unit tests
run this module against itself and against byte sequences written the
way `lrzsz` writes them (`tests/test_zmodem.py`), and an interop test
drives real `sz`/`rz` binaries when they're installed
(`tests/test_zmodem_lrzsz.py`, skipped visibly otherwise). A real
terminal (SyncTERM) against a running node remains the final check.
"""

from __future__ import annotations

import asyncio
import binascii
import contextlib
import hashlib
import re
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from netbbs.net.session import Session, SessionClosedError

# -- protocol constants (Chuck Forsberg's ZMODEM spec) -----------------

ZPAD = 0x2A  # '*' — pad character, begins every header
ZDLE = 0x18  # Ctrl-X — escape byte; also the cancel signal when doubled
CAN = ZDLE  # the same byte, read as "cancel" when five arrive in a row
BS = 0x08

ZBIN = 0x41  # 'A' — binary header, CRC-16 follows
ZHEX = 0x42  # 'B' — hex header, CRC-16, all printable
ZBIN32 = 0x43  # 'C' — binary header, CRC-32 follows

XON = 0x11
XOFF = 0x13
# Flow-control bytes a sender always ZDLE-escapes, so an unescaped one on
# the line is the terminal's or the modem's, never data: dropped on read,
# as lrzsz does.
_FLOW_CONTROL = frozenset({XON, XOFF, XON | 0x80, XOFF | 0x80})

# Frame types
ZRQINIT = 0
ZRINIT = 1
ZSINIT = 2
ZACK = 3
ZFILE = 4
ZSKIP = 5
ZNAK = 6
ZABORT = 7
ZFIN = 8
ZRPOS = 9
ZDATA = 10
ZEOF = 11
ZFERR = 12
ZCRC = 13
ZCHALLENGE = 14
ZCOMPL = 15
ZCAN = 16
ZFREECNT = 17
ZCOMMAND = 18

# Data subpacket terminators
ZCRCE = 0x68  # end of frame, no more subpackets, no ACK expected
ZCRCG = 0x69  # more data, no ACK expected
ZCRCQ = 0x6A  # more data, ACK expected, sender may continue without waiting
ZCRCW = 0x6B  # end of frame, ACK required before the next frame
_TERMINATORS = frozenset({ZCRCE, ZCRCG, ZCRCQ, ZCRCW})

# The two escapes that aren't "byte XOR 0x40": DEL and 0xFF, which a
# sender asked to escape control characters sends as ZDLE 'l' / ZDLE 'm'.
ZRUB0 = 0x6C
ZRUB1 = 0x6D

# ZRINIT capability flags (ZF0)
CANFDX = 0x01  # full duplex
CANOVIO = 0x02  # can receive data while writing to disk
CANFC32 = 0x20  # can use CRC-32

# Bytes that must be ZDLE-escaped wherever they appear in header or
# subpacket payload bytes — ZDLE itself, plus DLE/XON/XOFF and their
# 8th-bit-set counterparts (flow-control bytes a terminal or modem might
# otherwise act on). Narrower than the full historical allowlist (which
# also covers legacy X.25/"Telenet" and 8th-bit-stripping links this
# project's TCP transports don't have) — over-escaping is always safe,
# under-escaping isn't, and this covers everything that actually matters
# here.
_ESCAPE_BYTES = frozenset({ZDLE, 0x10, 0x90, 0x11, 0x91, 0x13, 0x93})

# Size of each ZDATA subpacket: the spec's 1024 bytes, which every
# receiver accepts (lrzsz and others take larger ones only when asked).
_SUBPACKET_SIZE = 1024

# When streaming, how much data one frame carries before a ZCRCW asks
# for an acknowledgement. Bounds how much a sender that loses its
# receiver has in flight, and how much is resent after a ZRPOS.
_FRAME_BYTES = 32 * 1024

# See module docstring's "bounded waits". The first header from the
# terminal may wait on a person: SyncTERM, for instance, opens a file
# dialog when an upload starts.
_START_TIMEOUT = 60.0
_HANDSHAKE_TIMEOUT = 15.0
# The closing ZFIN exchange is a courtesy once the file is safely across:
# a peer that doesn't answer it costs a few seconds, not the transfer.
_FIN_TIMEOUT = 5.0

# Header retries (ZFILE, ZEOF, ZFIN resent when the answer doesn't
# come), and the data errors a receiver asks to have resent before it
# gives up on the transfer.
_MAX_RETRIES = 3
_MAX_ERRORS = 10

# After a transfer ends, what the terminal still sends is read off the
# line until it has been quiet this long, but never for more than
# `_DRAIN_LIMIT`.
_DRAIN_QUIET = 0.5
_DRAIN_LIMIT = 5.0

# What lrzsz sends to make the other side give up: ten CANs, then ten
# backspaces to erase whatever a terminal that doesn't speak Zmodem
# printed for them.
_ABORT_SEQUENCE = bytes([CAN] * 10 + [BS] * 10)

# GitHub issue #34: none of the bulk-data reception path below had any
# bound at all -- a peer could stream indefinitely, send one enormous
# unterminated subpacket, or simply stall forever mid-transfer while
# still holding the session task and growing process memory. Three
# independent bounds, each catching a different failure shape:
#
# - _MAX_SUBPACKET_BYTES caps one *decoded* subpacket, regardless of
#   the overall transfer size limit below -- generous enough for any
#   well-behaved sender (the "8k" Zmodem variant sends 8 KiB), still
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
_MAX_SUBPACKET_BYTES = 32 * 1024
_BULK_IDLE_TIMEOUT = 30.0


class ZmodemError(Exception):
    """
    Raised for any Zmodem protocol failure that ends a transfer — a
    malformed or unexpected frame, a cancel signal from the peer, data
    errors past `_MAX_ERRORS`, or no response in time.
    """


class ZmodemCancelled(ZmodemError):
    """The peer cancelled the transfer (five CANs, ZCAN or ZABORT). No
    abort sequence is sent back: the peer has already stopped."""


class _NoResponse(ZmodemError):
    """No header arrived in time."""


class _CrcError(ZmodemError):
    """A data subpacket failed its CRC. A receiver recovers from this by
    asking for the data again (ZRPOS); anywhere else it ends the
    transfer."""


@dataclass(frozen=True)
class ReceivedFile:
    """
    `data: bytes` was replaced with `sha256`/`size_bytes` (GitHub issue
    #34, reopened a second time) once `receive_file` started streaming
    directly to a caller-supplied path instead of accumulating the
    whole transfer in memory — there is no complete in-memory buffer
    left to hand back here at all. The content itself lives at whatever
    `dest_path` `receive_file` was called with; `netbbs.net.file_flow.
    _handle_upload` is what still has that path and moves it into
    permanent storage via `netbbs.files.entries.upload_file_from_temp`.
    """

    filename: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class _Header:
    frame_type: int
    position: int
    crc32: bool  # the frame's data subpackets carry CRC-32, not CRC-16

    @property
    def flags(self) -> int:
        """ZF0, the capability byte of a ZRINIT (and the option byte of
        other headers): the last of the four header bytes."""
        return (self.position >> 24) & 0xFF

    @property
    def buffer_size(self) -> int:
        """A ZRINIT's receive buffer size (ZP0/ZP1); 0 means the
        receiver can take a continuous stream."""
        return self.position & 0xFFFF


def safe_filename(raw: str) -> str:
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


# -- CRCs ----------------------------------------------------------------


def _crc16(data: bytes) -> int:
    """Poly 0x1021, init 0, no reflection (CRC-16/XMODEM) — the baseline
    every conformant ZMODEM implementation must support. `crc_hqx` is
    exactly this CRC, computed in C: a pure-Python bit loop cost seconds
    of event-loop time per megabyte."""
    return binascii.crc_hqx(data, 0)


def _crc32(data: bytes) -> int:
    """ZMODEM's CRC-32 is the standard one (zlib's), sent least
    significant byte first."""
    return zlib.crc32(data) & 0xFFFFFFFF


# -- ZDLE encoding -------------------------------------------------------


_NEEDS_ESCAPE = re.compile(b"[" + re.escape(bytes(sorted(_ESCAPE_BYTES))) + b"]")


def _zdle_encode(data: bytes) -> bytes:
    return _NEEDS_ESCAPE.sub(lambda m: bytes([ZDLE, m.group()[0] ^ 0x40]), data)


def _unescape(b2: int) -> int:
    """The data byte a ZDLE-escape `ZDLE b2` stands for."""
    if b2 == ZRUB0:
        return 0x7F
    if b2 == ZRUB1:
        return 0xFF
    return b2 ^ 0x40


async def _read_raw_byte(session: Session) -> int:
    """Read the next real data byte, transparently skipping any
    transport-level action (Telnet negotiation, an SSH resize
    notification) with no data significance — same "loop past `None`"
    contract `netbbs.net.char_input` already uses against the same
    `Session.read_byte`. A byte handed back by `_push_back` comes first."""
    pending = getattr(session, "_zmodem_pushback", None)
    if pending:
        return pending.pop()
    while True:
        b = await session.read_byte()
        if b is not None:
            return b


def _push_back(session: Session, b: int) -> None:
    """Return one byte read too far, so the next `_read_raw_byte` gets it
    first. Only `_skip_hex_line_end` needs this, for at most one byte."""
    pending = getattr(session, "_zmodem_pushback", None)
    if pending is None:
        pending = []
        session._zmodem_pushback = pending
    pending.append(b)


async def _read_bulk_raw_byte(session: Session) -> int:
    """
    `_read_raw_byte`, bounded by `_BULK_IDLE_TIMEOUT` (GitHub issue #34,
    reopened) — the one primitive every byte read during the
    bulk-transfer phase (`_read_subpacket`) must go through, not just
    the first byte of each loop iteration, so a peer can't stall
    indefinitely after a lone `ZDLE` or between a terminator and its
    CRC bytes.
    """
    try:
        return await asyncio.wait_for(_read_raw_byte(session), timeout=_BULK_IDLE_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise ZmodemError("transfer stalled — no data received in time") from exc


async def _read_zdle_byte(session: Session, *, read_raw=_read_raw_byte) -> int:
    """
    Read one logical byte, resolving ZDLE-escaping if present and
    dropping unescaped flow-control bytes (see `_FLOW_CONTROL`).

    A `ZDLE` immediately followed by another literal `ZDLE` byte is
    never a valid escape sequence under this encoding (escaping the
    `ZDLE` byte value itself produces `ZDLE 0x58`, never `ZDLE ZDLE` —
    see `_zdle_encode`), so seeing that pair unambiguously means the
    peer sent a cancel signal, not corrupted framing.

    `read_raw` (GitHub issue #34, reopened) is the raw-byte primitive
    to use for both bytes read here — defaults to the untimed
    `_read_raw_byte`, correct for header reads (already bounded as a
    whole by `_next_header`'s timeout). `_read_subpacket` instead passes
    `_read_bulk_raw_byte`, so its own CRC-byte reads get the same
    per-byte idle bound as every other byte in the bulk-transfer phase.
    """
    skipped = 0
    while True:
        b = await read_raw(session)
        if b in _FLOW_CONTROL:
            # Counted, so a peer sending nothing but XON/XOFF can't keep
            # this read alive forever on the idle timeout alone.
            skipped += 1
            if skipped > _MAX_SUBPACKET_BYTES:
                raise ZmodemError("flow-control flood with no data")
            continue
        if b != ZDLE:
            return b
        b2 = await read_raw(session)
        if b2 == ZDLE:
            raise ZmodemCancelled("transfer cancelled by peer")
        return _unescape(b2)


# -- headers -------------------------------------------------------------


def _position_bytes(position: int) -> bytes:
    # Little-endian per spec: P0 is the least-significant byte, P3 the
    # most-significant (and, in a ZRINIT, the capability flags ZF0).
    return bytes(
        [
            position & 0xFF,
            (position >> 8) & 0xFF,
            (position >> 16) & 0xFF,
            (position >> 24) & 0xFF,
        ]
    )


def _binary_header(frame_type: int, position: int = 0) -> bytes:
    payload = bytes([frame_type]) + _position_bytes(position)
    crc = _crc16(payload)
    crc_bytes = bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    return bytes([ZPAD, ZDLE, ZBIN]) + _zdle_encode(payload + crc_bytes)


def _hex_header(frame_type: int, position: int = 0) -> bytes:
    """A header in the all-printable hex form: `**` ZDLE `B`, fourteen
    lowercase hex digits (type, four position bytes, CRC-16), CR, LF
    with the high bit set, and an XON — except after ZACK and ZFIN,
    matching lrzsz's `zshhdr`."""
    payload = bytes([frame_type]) + _position_bytes(position)
    crc = _crc16(payload)
    digits = (payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])).hex().encode("ascii")
    frame = bytes([ZPAD, ZPAD, ZDLE, ZHEX]) + digits + b"\r\x8a"
    if frame_type not in (ZACK, ZFIN):
        frame += bytes([XON])
    return frame


async def _send_header(session: Session, frame_type: int, position: int = 0) -> None:
    """A binary (CRC-16) header: what a sender uses for the headers that
    data subpackets follow (ZFILE, ZDATA) and for ZEOF."""
    await session.write_raw(_binary_header(frame_type, position))


async def _send_hex_header(session: Session, frame_type: int, position: int = 0) -> None:
    """A hex header: everything a receiver sends, and a sender's ZRQINIT
    and ZFIN."""
    await session.write_raw(_hex_header(frame_type, position))


_HEX_DIGITS = {ord(c): int(c, 16) for c in "0123456789abcdefABCDEF"}


async def _read_hex_header_body(session: Session) -> tuple[bytes, bool]:
    """The seven bytes a hex header encodes, and whether they were all
    valid hex. Reads with the high bit stripped and flow control
    skipped, as lrzsz's `noxrd7` does."""
    digits = []
    while len(digits) < 14:
        b = (await _read_raw_byte(session)) & 0x7F
        if b in (XON, XOFF):
            continue
        digits.append(b)
    if any(d not in _HEX_DIGITS for d in digits):
        return b"", False
    body = bytes(_HEX_DIGITS[digits[i]] << 4 | _HEX_DIGITS[digits[i + 1]] for i in range(0, 14, 2))
    return body, True


# Headers whose frame goes on with a data subpacket (ZSINIT's attention
# string, ZFILE's file info, ZDATA's file data, ZCOMMAND's command).
_HEADERS_WITH_DATA = frozenset({ZSINIT, ZFILE, ZDATA, ZCOMMAND})


async def _skip_hex_line_end(session: Session) -> None:
    """Consume the CR and LF that end a hex header, read with the high bit
    stripped, as lrzsz's `zrhhdr` does ("throw away possible cr/lf").

    Only needed before a data subpacket: after any other header the
    scanner skips them as noise, but a subpacket reader would take them
    for data and fail the CRC (issue #963: `sz -e` opens with a hex
    ZSINIT). A byte that isn't the expected CR or LF is handed back, so a
    sender that leaves them out loses nothing. The data must follow
    anyway, so these reads carry the bulk idle bound, not a short one."""
    b = await _read_bulk_raw_byte(session)
    if b & 0x7F != 0x0D:
        _push_back(session, b)
        return
    b = await _read_bulk_raw_byte(session)
    if b & 0x7F != 0x0A:
        _push_back(session, b)


async def _read_header(session: Session) -> tuple[int, int]:
    """
    Scan for and decode the next header, returning `(frame_type,
    position)`. See `_scan_header` for the full result.
    """
    header = await _scan_header(session)
    return header.frame_type, header.position


async def _scan_header(session: Session) -> _Header:
    """
    Scan for and decode the next header of any of the three forms.

    Scans past any bytes that aren't part of a header — real terminal
    clients interleave harmless noise (the CR LF XON that ends a hex
    header, a trailing newline from a preceding text prompt, the rest of
    a data frame being skipped after a ZRPOS) before a header actually
    starts. A header whose CRC doesn't match is skipped the same way:
    during resynchronisation, data can contain what looks like the start
    of one. Five CAN bytes in a row are the peer cancelling.
    """
    cans = 0
    while True:
        b = await _read_raw_byte(session)
        if b == CAN:
            cans += 1
            if cans >= 5:
                raise ZmodemCancelled("transfer cancelled by peer")
            continue
        cans = 0
        if b != ZPAD:
            continue
        b = await _read_raw_byte(session)
        while b == ZPAD:
            b = await _read_raw_byte(session)
        if b != ZDLE:
            continue
        kind = await _read_raw_byte(session)
        if kind == ZHEX:
            body, valid = await _read_hex_header_body(session)
            if not valid or _crc16(body[:5]) != (body[5] << 8) | body[6]:
                continue
            payload, crc32 = body[:5], False
            if payload[0] in _HEADERS_WITH_DATA:
                await _skip_hex_line_end(session)
        elif kind in (ZBIN, ZBIN32):
            payload = bytearray()
            for _ in range(5):
                payload.append(await _read_zdle_byte(session))
            if kind == ZBIN:
                crc_hi = await _read_zdle_byte(session)
                crc_lo = await _read_zdle_byte(session)
                if _crc16(bytes(payload)) != (crc_hi << 8) | crc_lo:
                    continue
                crc32 = False
            else:
                received = bytearray()
                for _ in range(4):
                    received.append(await _read_zdle_byte(session))
                if _crc32(bytes(payload)) != int.from_bytes(received, "little"):
                    continue
                crc32 = True
        else:
            # ZDLE CAN is two CANs towards a cancel (ZDLE is the same
            # byte); anything else is noise that merely looked like the
            # start of a header.
            if kind == CAN:
                cans = 2
            continue
        position = payload[1] | (payload[2] << 8) | (payload[3] << 16) | (payload[4] << 24)
        return _Header(payload[0], position, crc32)


async def _next_header(session: Session, timeout: float) -> _Header:
    """`_scan_header`, bounded by `timeout` — see module docstring's
    "bounded waits"."""
    try:
        return await asyncio.wait_for(_scan_header(session), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise _NoResponse("no response from client — does your terminal support Zmodem?") from exc


async def _wait_for_header(session: Session) -> tuple[int, int]:
    """`_read_header`, bounded by `_HANDSHAKE_TIMEOUT`."""
    header = await _next_header(session, _HANDSHAKE_TIMEOUT)
    return header.frame_type, header.position


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


async def _read_subpacket(session: Session, *, crc32: bool = False) -> tuple[bytes, int]:
    """Returns `(data, terminator)`. Raises `_CrcError` on a CRC
    mismatch, and `ZmodemError` on a cancel signal from the peer, an
    unterminated subpacket growing past `_MAX_SUBPACKET_BYTES`, or no
    byte arriving within `_BULK_IDLE_TIMEOUT` of the previous one
    anywhere in this function (GitHub issue #34, reopened). `crc32`
    follows the header that opened the frame."""
    data = bytearray()
    terminator = None
    skipped = 0
    while terminator is None:
        b = await _read_bulk_raw_byte(session)
        if b in _FLOW_CONTROL:
            # Skipped bytes count against the same cap as data: otherwise
            # a peer streaming only XON/XOFF would never trip it.
            skipped += 1
            if len(data) + skipped > _MAX_SUBPACKET_BYTES:
                raise ZmodemError(
                    f"data subpacket exceeded {_MAX_SUBPACKET_BYTES} bytes with no terminator"
                )
            continue
        if b != ZDLE:
            if len(data) + skipped >= _MAX_SUBPACKET_BYTES:
                raise ZmodemError(
                    f"data subpacket exceeded {_MAX_SUBPACKET_BYTES} bytes with no terminator"
                )
            data.append(b)
            continue
        b2 = await _read_bulk_raw_byte(session)
        if b2 == ZDLE:
            raise ZmodemCancelled("transfer cancelled by peer")
        if b2 in _TERMINATORS:
            terminator = b2
        else:
            if len(data) + skipped >= _MAX_SUBPACKET_BYTES:
                raise ZmodemError(
                    f"data subpacket exceeded {_MAX_SUBPACKET_BYTES} bytes with no terminator"
                )
            data.append(_unescape(b2))

    covered = bytes(data) + bytes([terminator])
    if crc32:
        received = bytearray()
        for _ in range(4):
            received.append(await _read_zdle_byte(session, read_raw=_read_bulk_raw_byte))
        if _crc32(covered) != int.from_bytes(received, "little"):
            raise _CrcError("data subpacket CRC mismatch")
    else:
        crc_hi = await _read_zdle_byte(session, read_raw=_read_bulk_raw_byte)
        crc_lo = await _read_zdle_byte(session, read_raw=_read_bulk_raw_byte)
        if _crc16(covered) != (crc_hi << 8) | crc_lo:
            raise _CrcError("data subpacket CRC mismatch")
    return bytes(data), terminator


# -- shared ends of a transfer ----------------------------------------------


async def _drain(session: Session) -> None:
    """Read off whatever the terminal still sends once a transfer is
    over — a repeated ZRINIT, the end of an aborted stream, the CANs a
    terminal answers an abort with — so none of it reaches the next
    screen as keystrokes."""
    deadline = time.monotonic() + _DRAIN_LIMIT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(_read_raw_byte(session), timeout=min(_DRAIN_QUIET, remaining))
        except (asyncio.TimeoutError, SessionClosedError):
            return


async def _give_up(session: Session, exc: BaseException) -> None:
    """Tell the terminal the transfer is over after NetBBS ended it, then
    clear the line. A peer that cancelled has already stopped."""
    if isinstance(exc, asyncio.CancelledError):
        return
    with contextlib.suppress(Exception):
        if not isinstance(exc, ZmodemCancelled):
            await session.write_raw(_ABORT_SEQUENCE)
        await _drain(session)


def _refuse_on_cancel(header: _Header) -> None:
    if header.frame_type in (ZCAN, ZABORT):
        raise ZmodemCancelled("transfer cancelled by peer")


# -- sender (download: NetBBS sends a file to the connecting client) -----


def _binary_transfer(session: Session):
    """Keep Zmodem's frames out of the session's screen copy (issue #764).
    A duck-typed session without the hook simply has no copy to protect."""
    mark = getattr(session, "binary_transfer", None)
    return _clearing_pushback(session, mark() if mark is not None else contextlib.nullcontext())


@contextlib.contextmanager
def _clearing_pushback(session: Session, inner):
    """No byte handed back by `_push_back` outlives its transfer."""
    try:
        with inner:
            yield
    finally:
        if getattr(session, "_zmodem_pushback", None):
            session._zmodem_pushback.clear()


async def _await_no_break_in(session: Session) -> None:
    wait = getattr(session, "wait_for_break_in_end", None)
    if wait is not None:
        await wait()


async def send_file(session: Session, filename: str, data: bytes) -> None:
    # Nothing is awaited between the wait and the mark, so a break-in can't
    # start in between: from the mark on, the Monitor refuses one.
    await _await_no_break_in(session)
    with _binary_transfer(session):
        await _send_file(session, filename, data)


def _file_info(filename: str, size: int) -> bytes:
    """ZFILE's subpacket: the name, a NUL, then size, modification time
    (octal seconds), mode (octal, 0 for "none given"), serial number,
    files remaining and bytes remaining, and a closing NUL."""
    name = safe_filename(filename).encode("ascii", errors="replace")
    return name + b"\x00" + f"{size} {int(time.time()):o} 0 0 1 {size}".encode("ascii") + b"\x00"


async def _send_file(session: Session, filename: str, data: bytes) -> None:
    """
    Send `data` to the client as `filename` via Zmodem, the way `sz`
    does: `rz\\r` and a hex ZRQINIT to start the terminal's receiver,
    then ZFILE, data frames from wherever the receiver's ZRPOS asks,
    ZEOF and ZFIN.
    """
    try:
        await _send_file_frames(session, filename, data)
    except BaseException as exc:
        await _give_up(session, exc)
        raise
    await _drain(session)


async def _send_file_frames(session: Session, filename: str, data: bytes) -> None:
    await session.write_raw(b"rz\r" + _hex_header(ZRQINIT))
    ready = await _await_receiver(session)
    streaming = bool(ready.flags & CANFDX) and bool(ready.flags & CANOVIO)
    frame_limit = ready.buffer_size or (_FRAME_BYTES if streaming else _SUBPACKET_SIZE)

    offset = await _offer_file(session, filename, data)
    errors = 0
    while True:
        restart = await _send_data(session, data, offset, streaming=streaming, frame_limit=frame_limit)
        if restart is None:
            restart = await _send_eof(session, len(data))
            if restart is None:
                break
        offset = min(restart, len(data))
        errors += 1
        if errors > _MAX_ERRORS:
            raise ZmodemError("too many errors — the transfer kept failing")

    await _send_fin(session)


async def _await_receiver(session: Session) -> _Header:
    """The receiver's ZRINIT, answering a ZCHALLENGE on the way."""
    deadline = time.monotonic() + _START_TIMEOUT
    while True:
        header = await _next_header(session, max(0.0, deadline - time.monotonic()))
        _refuse_on_cancel(header)
        if header.frame_type == ZRINIT:
            return header
        if header.frame_type == ZCHALLENGE:
            await _send_hex_header(session, ZACK, header.position)
        # Anything else (the terminal's own ZRQINIT, a stray ZNAK) is
        # not an answer yet.


async def _offer_file(session: Session, filename: str, data: bytes) -> int:
    """Send ZFILE and return the offset the receiver wants the data
    from: 0, or more when it resumes a partial download."""
    info = _file_info(filename, len(data))
    for _attempt in range(_MAX_RETRIES):
        await _send_header(session, ZFILE)
        await _send_subpacket(session, info, ZCRCW)
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT
        try:
            while True:
                header = await _next_header(session, max(0.0, deadline - time.monotonic()))
                _refuse_on_cancel(header)
                if header.frame_type == ZRPOS:
                    return min(header.position, len(data))
                if header.frame_type == ZSKIP:
                    raise ZmodemError("receiver skipped the file")
                if header.frame_type == ZFIN:
                    raise ZmodemError("receiver ended the session")
                if header.frame_type == ZCRC:
                    # The receiver checks a partial file before resuming.
                    length = header.position or len(data)
                    await _send_header(session, ZCRC, _crc32(data[:length]))
                    continue
                if header.frame_type == ZNAK:
                    break  # resend the offer
                # A repeated ZRINIT, sent before our ZFILE arrived, is
                # not an answer to it.
        except _NoResponse:
            continue
    raise ZmodemError("no response from client — does your terminal support Zmodem?")


async def _send_data(
    session: Session, data: bytes, offset: int, *, streaming: bool, frame_limit: int
) -> int | None:
    """Send the data from `offset` on. Returns None once the last frame
    (ZCRCE) is out, or the offset to restart from when the receiver asks
    for a resend (ZRPOS)."""
    while True:
        await _send_header(session, ZDATA, offset)
        in_frame = 0
        while True:
            chunk = data[offset : offset + _SUBPACKET_SIZE]
            offset += len(chunk)
            in_frame += len(chunk)
            if offset >= len(data):
                terminator = ZCRCE
            elif not streaming or in_frame >= frame_limit:
                terminator = ZCRCW
            else:
                terminator = ZCRCG
            await _send_subpacket(session, chunk, terminator)
            if terminator != ZCRCG:
                break
        if terminator == ZCRCE:
            return None
        restart = await _await_ack(session, offset)
        if restart is not None:
            return min(restart, len(data))


async def _await_ack(session: Session, offset: int) -> int | None:
    """Wait for the ZACK of the frame that ended at `offset`. Returns
    None when it came, or the position a ZRPOS asks to resend from."""
    deadline = time.monotonic() + _HANDSHAKE_TIMEOUT
    while True:
        header = await _next_header(session, max(0.0, deadline - time.monotonic()))
        _refuse_on_cancel(header)
        if header.frame_type == ZACK and header.position == offset:
            return None
        if header.frame_type == ZRPOS:
            return header.position
        if header.frame_type == ZSKIP:
            raise ZmodemError("receiver skipped the file")
        if header.frame_type == ZFIN:
            raise ZmodemError("receiver ended the session")
        # A ZACK for an earlier position, a stray ZRINIT: keep waiting.


async def _send_eof(session: Session, length: int) -> int | None:
    """Send ZEOF and wait for the receiver's ZRINIT (it has the whole
    file). Returns None then, or the position a ZRPOS asks to resend
    from when the receiver is missing data."""
    for _attempt in range(_MAX_RETRIES):
        await _send_header(session, ZEOF, length)
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT
        try:
            while True:
                header = await _next_header(session, max(0.0, deadline - time.monotonic()))
                _refuse_on_cancel(header)
                if header.frame_type == ZRINIT:
                    return None
                if header.frame_type == ZRPOS:
                    return header.position
                if header.frame_type == ZSKIP:
                    raise ZmodemError("receiver skipped the file")
                if header.frame_type == ZNAK:
                    break  # resend ZEOF
                # A late ZACK: keep waiting.
        except _NoResponse:
            continue
    raise ZmodemError("no response from client after the last data")


async def _send_fin(session: Session) -> None:
    """Close the session: ZFIN, the receiver's ZFIN, then `OO`. The file
    is already across, so a receiver that never answers ends this
    quietly rather than failing the transfer."""
    for _attempt in range(_MAX_RETRIES):
        await _send_hex_header(session, ZFIN)
        deadline = time.monotonic() + _FIN_TIMEOUT
        try:
            while True:
                header = await _next_header(session, max(0.0, deadline - time.monotonic()))
                if header.frame_type == ZFIN:
                    await session.write_raw(b"OO")
                    return
                if header.frame_type in (ZCAN, ZABORT):
                    return
        except ZmodemCancelled:
            return
        except _NoResponse:
            continue


# -- receiver (upload: NetBBS receives a file from the connecting client) -


# What this receiver tells a sender it can do: full duplex, and taking
# data while writing it, so the sender may stream. No CANFC32: see the
# module docstring.
_RECEIVER_FLAGS = CANFDX | CANOVIO


async def receive_file(session: Session, *, max_bytes: int, dest_path: Path) -> ReceivedFile:
    await _await_no_break_in(session)
    with _binary_transfer(session):
        return await _receive_file(session, max_bytes=max_bytes, dest_path=dest_path)


async def _receive_file(session: Session, *, max_bytes: int, dest_path: Path) -> ReceivedFile:
    """
    Receive one file from the client via Zmodem, streaming it directly
    to `dest_path` as it arrives and returning its filename, content
    hash, and size — never holding the complete transfer in memory at
    once (GitHub issue #34, reopened a second time: a node's peak memory
    use otherwise scaled with however many uploads were in flight).

    `dest_path` is caller-supplied (`netbbs.net.file_flow._handle_upload`
    passes one from `netbbs.files.storage.new_incoming_temp_path`) --
    this module only knows it's writing to a path, and assumes
    `dest_path.parent` already exists. Deleted on any failure below -- a
    stalled/cancelled/oversized/malformed transfer must not leave a
    partial file behind in the caller's staging area. Left in place,
    complete and ready for the caller to move into permanent storage,
    only on a clean return.

    Opens with a hex ZRINIT, which is also what makes a terminal like
    SyncTERM start its upload by itself (`sz` answers a ZRINIT the same
    way when it's already running).

    `max_bytes` (GitHub issue #34, typically `netbbs.config.
    get_max_upload_bytes`) bounds the transfer twice: the size the
    sender itself advertises in `ZFILE` is checked and rejected before
    any bulk data is ever read or `dest_path` even opened, and the
    actual running received-byte count is checked after every subpacket
    regardless -- an advertised size is peer-supplied metadata, not
    authoritative.
    """
    # BaseException, not Exception -- a session cancellation (e.g. the
    # GitHub issue #29 background revocation watcher firing mid-upload,
    # or a genuine disconnect) raises asyncio.CancelledError, which is
    # not an Exception subclass. dest_path must still be cleaned up on
    # that path too -- and it's re-raised unchanged either way.
    try:
        received = await _receive_file_frames(session, max_bytes=max_bytes, dest_path=dest_path)
    except BaseException as exc:
        dest_path.unlink(missing_ok=True)
        await _give_up(session, exc)
        raise
    await _drain(session)
    return received


async def _receive_file_frames(session: Session, *, max_bytes: int, dest_path: Path) -> ReceivedFile:
    zrinit = _RECEIVER_FLAGS << 24
    await _send_hex_header(session, ZRINIT, zrinit)

    offer = await _await_offer(session, zrinit)
    filename = await _read_offer(session, offer, max_bytes=max_bytes)
    await _send_hex_header(session, ZRPOS, 0)

    hasher = hashlib.sha256()
    received_bytes = 0
    errors = 0

    async def ask_again() -> None:
        """Ask the sender to send again from `received_bytes`."""
        nonlocal errors
        errors += 1
        if errors > _MAX_ERRORS:
            raise ZmodemError("too many errors — the transfer kept failing")
        await _send_hex_header(session, ZRPOS, received_bytes)

    with open(dest_path, "wb") as dest_file:
        while True:
            header = await _next_header(session, _HANDSHAKE_TIMEOUT)
            _refuse_on_cancel(header)
            if header.frame_type == ZEOF:
                if header.position == received_bytes:
                    break
                await ask_again()  # the sender thinks it's done; we're missing data
                continue
            if header.frame_type == ZFILE:
                # The same offer again (the sender saw a second ZRINIT):
                # answer it again with where we are.
                await _read_subpacket(session, crc32=header.crc32)
                await _send_hex_header(session, ZRPOS, received_bytes)
                continue
            if header.frame_type in (ZRQINIT, ZNAK, ZSINIT):
                continue
            if header.frame_type != ZDATA:
                raise ZmodemError(f"expected ZDATA, got frame type {header.frame_type}")
            if header.position != received_bytes:
                # Data from somewhere else in the file: ask for ours; the
                # next header read steps over this frame's subpackets.
                await ask_again()
                continue

            while True:
                try:
                    chunk, terminator = await _read_subpacket(session, crc32=header.crc32)
                except _CrcError:
                    await ask_again()
                    break
                if received_bytes + len(chunk) > max_bytes:
                    raise ZmodemError(f"upload exceeded the {max_bytes}-byte limit")
                dest_file.write(chunk)
                hasher.update(chunk)
                received_bytes += len(chunk)
                if terminator in (ZCRCW, ZCRCQ):
                    await _send_hex_header(session, ZACK, received_bytes)
                if terminator in (ZCRCW, ZCRCE):
                    break  # the frame is over; a header comes next
                # ZCRCG/ZCRCQ: more subpackets follow in this frame.

    await _close_receiving(session, zrinit)
    return ReceivedFile(filename=filename, sha256=hasher.hexdigest(), size_bytes=received_bytes)


async def _await_offer(session: Session, zrinit: int) -> _Header:
    """Wait for the sender's ZFILE, answering its ZRQINIT (a sender that
    started after our first ZRINIT asks for another) and its ZSINIT."""
    deadline = time.monotonic() + _START_TIMEOUT
    while True:
        header = await _next_header(session, max(0.0, deadline - time.monotonic()))
        _refuse_on_cancel(header)
        if header.frame_type == ZFILE:
            return header
        if header.frame_type == ZRQINIT:
            await _send_hex_header(session, ZRINIT, zrinit)
            continue
        if header.frame_type == ZSINIT:
            await _read_subpacket(session, crc32=header.crc32)
            await _send_hex_header(session, ZACK, 0)
            continue
        if header.frame_type == ZFIN:
            await _send_hex_header(session, ZFIN)
            raise ZmodemError("the terminal ended the transfer without sending a file")
        if header.frame_type in (ZNAK, ZFREECNT):
            continue
        raise ZmodemError(f"expected ZFILE, got frame type {header.frame_type}")


async def _read_offer(session: Session, offer: _Header, *, max_bytes: int) -> str:
    """Read ZFILE's subpacket and return the file's name, rejecting a
    file whose advertised size is over `max_bytes`."""
    info, _terminator = await _read_subpacket(session, crc32=offer.crc32)
    if b"\x00" in info:
        raw_filename, metadata = info.split(b"\x00", 1)
    else:
        # Defensive fallback for a sender that omits the NUL terminator
        # entirely, in which case the whole "filename size mtime ..."
        # field runs together space-separated.
        raw_filename, _, metadata = info.partition(b" ")
    name = raw_filename.decode("utf-8", errors="replace")
    filename = safe_filename(name) if name else "unnamed"

    # The metadata field is "{size} {mtime} {mode} {serial}
    # {files_remaining} {bytes_remaining}" (space-separated, ASCII), per
    # spec -- only the leading size field matters here. Absent/malformed
    # metadata isn't itself an error (some senders omit it); it just
    # means the early-rejection check below can't run, and the
    # running-total check during actual reception remains the
    # authoritative bound regardless.
    size_field = metadata.split(b"\x00", 1)[0].split(b" ", 1)[0]
    if size_field.isascii() and size_field.isdigit() and int(size_field) > max_bytes:
        raise ZmodemError(
            f"advertised file size {int(size_field)} exceeds the {max_bytes}-byte upload limit"
        )
    return filename


async def _close_receiving(session: Session, zrinit: int) -> None:
    """After the file: a ZRINIT says it arrived, a further ZFILE (the
    sender was given several files) is skipped, and ZFIN is answered;
    the sender's `OO` is read off by the drain that follows. The file is
    complete by now, so a sender that goes quiet ends this without
    failing it."""
    await _send_hex_header(session, ZRINIT, zrinit)
    while True:
        try:
            header = await _next_header(session, _HANDSHAKE_TIMEOUT)
        except ZmodemError:
            return
        if header.frame_type == ZFIN:
            await _send_hex_header(session, ZFIN)
            return
        if header.frame_type == ZFILE:
            with contextlib.suppress(ZmodemError):
                await _read_subpacket(session, crc32=header.crc32)
            await _send_hex_header(session, ZSKIP)
            continue
        if header.frame_type == ZEOF:
            await _send_hex_header(session, ZRINIT, zrinit)
            continue
        if header.frame_type in (ZCAN, ZABORT):
            return
