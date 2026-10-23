"""
The QWK record layer (issue #1389): 128-byte blocks, the message header,
message text, Microsoft binary numbers and QWKE's long-field lines.

Shared by the packet writer and reader (`netbbs.qwk.packet`) and the reply
packet writer and reader (`netbbs.qwk.rep`). Nothing here knows about
callers, boards or the database, so QWKnet (#1481, #1482) uses it as it is.

**The header** (Mark Herring's QWK, as documented by Patrick Y. Lee), one
128-byte block, offsets from 0:

    0       status flag (' ' public, '+' private, '-'/'*' the same read, ...)
    1-7     message number, ASCII -- in a reply packet, the conference
    8-15    date, MM-DD-YY
    16-20   time, HH:MM
    21-45   To
    46-70   From
    71-95   Subject
    96-107  password (spaces)
    108-115 reference message number, ASCII (the message this answers)
    116-121 number of 128-byte blocks, the header included, ASCII
    122     active flag: 0xE1 active, 0xE2 to be killed
    123-124 conference number, unsigned little-endian word
    125-126 logical message number in the packet (QWK 1.3), little-endian
    127     network tagline flag ('*' present, ' ' none)

**Text** follows in whole blocks, lines ended by 0xE3 (CP437's pi) instead
of CR/LF, padded with spaces. Text is CP437: a character CP437 lacks is
written as '?', and pi itself, which would end a line, as "pi".

**QWKE** (Peter Rocca's QWKE 1.02) keeps the 25-character fields and puts
the whole value on a `To:`, `From:` or `Subject:` line at the top of the
text, followed by a blank line, when the field is too short for it. Those
lines are written only for a field that does not fit.
"""

from __future__ import annotations

import datetime
import re
import struct
from dataclasses import dataclass

BLOCK = 128
FIELD = 25
LINE_END = 0xE3
ACTIVE = 0xE1
KILLED = 0xE2

#: The first block of MESSAGES.DAT, as every QWK door has written it since
#: Qmail. Readers skip it; some check that it is there.
COPYRIGHT_BLOCK = b"Produced by Qmail...Copyright (c) 1987 by Sparkware.  All Rights Reserved"

#: Status flags that mean a private message, read or not.
PRIVATE_FLAGS = frozenset("+*~`%^!#")

#: The QWKE field lines, in the order they are written.
_QWKE_FIELDS = ("To", "From", "Subject")
_QWKE_LINE = re.compile(r"^(to|from|subject):[ \t]?(.*)$", re.IGNORECASE)

# Anything below a space but a tab is dropped from text read in: a reply
# packet is untrusted, and an escape sequence in it would reach other
# callers' terminals through the post.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


class QwkFormatError(ValueError):
    """A block that is not what QWK says it must be."""


def to_cp437(text: str) -> bytes:
    """`text` as CP437 bytes, pi spelled out (its byte ends a line) and
    every character CP437 lacks as '?'."""
    return text.replace("π", "pi").encode("cp437", errors="replace")


def from_cp437(data: bytes) -> str:
    return data.decode("cp437", errors="replace")


def clean_line(text: str) -> str:
    """One line of untrusted text with its control characters removed."""
    return _CONTROL.sub("", text)


def field(text: str, width: int = FIELD) -> bytes:
    """`text` cut and space-padded to a fixed-width header field."""
    data = to_cp437(text.replace("\r", " ").replace("\n", " "))[:width]
    return data.ljust(width, b" ")


def fits(text: str, width: int = FIELD) -> bool:
    return len(to_cp437(text)) <= width


def read_field(data: bytes) -> str:
    """A header field as text: trailing spaces and NULs off, controls out."""
    return clean_line(from_cp437(data.rstrip(b" \x00"))).strip()


def read_number(data: bytes) -> int | None:
    """An ASCII number field, or `None` when it holds no number."""
    text = data.decode("ascii", errors="replace").strip(" \x00")
    if not text or not text.isdigit() or len(text) > 9:
        return None
    return int(text)


def to_msbin(value: int) -> bytes:
    """`value` as a 4-byte Microsoft binary single, as the .NDX files hold
    each message's block number. MBF puts the exponent last, biased by two
    more than IEEE's, and the sign where IEEE has the exponent's low bit."""
    if value == 0:
        return b"\x00\x00\x00\x00"
    b0, b1, b2, b3 = struct.pack("<f", float(value))
    exponent = ((b3 << 1) & 0xFF) | (b2 >> 7)
    return bytes([b0, b1, (b3 & 0x80) | (b2 & 0x7F), exponent + 2])


def from_msbin(data: bytes) -> int:
    """The integer a 4-byte Microsoft binary single holds."""
    if len(data) != 4:
        raise QwkFormatError("a Microsoft binary number is 4 bytes")
    if data[3] == 0:
        return 0
    exponent = data[3] - 2
    ieee3 = (data[2] & 0x80) | (exponent >> 1)
    ieee2 = ((exponent << 7) & 0x80) | (data[2] & 0x7F)
    return int(round(struct.unpack("<f", bytes([data[0], data[1], ieee2, ieee3]))[0]))


@dataclass(frozen=True)
class Header:
    """One message header, decoded. `number` is the message number in a
    packet and the conference number (as ASCII) in a reply packet; `blocks`
    counts the header too."""

    status: str
    number: int | None
    when: datetime.datetime | None
    to: str
    sender: str
    subject: str
    reference: int | None
    blocks: int | None
    active: bool
    conference: int
    logical: int
    tagline: bool

    @property
    def private(self) -> bool:
        return self.status in PRIVATE_FLAGS


def pack_header(
    *, status: str, number: str, when: datetime.datetime, to: str, sender: str, subject: str,
    reference: int | None, blocks: int, conference: int, logical: int = 0, tagline: bool = False,
) -> bytes:
    """One 128-byte header block."""
    if not 0 <= conference <= 0xFFFF:
        raise QwkFormatError(f"conference {conference} does not fit QWK's 16 bits")
    if blocks < 1 or blocks > 999_999:
        raise QwkFormatError(f"a message of {blocks} blocks does not fit QWK")
    header = b"".join([
        status.encode("ascii")[:1].ljust(1, b" "),
        number.encode("ascii")[:7].ljust(7, b" "),
        when.strftime("%m-%d-%y").encode("ascii"),
        when.strftime("%H:%M").encode("ascii"),
        field(to),
        field(sender),
        field(subject),
        b" " * 12,
        (str(reference) if reference else "").encode("ascii")[:8].ljust(8, b" "),
        str(blocks).encode("ascii").ljust(6, b" "),
        bytes([ACTIVE]),
        struct.pack("<H", conference),
        struct.pack("<H", logical & 0xFFFF),
        b"*" if tagline else b" ",
    ])
    assert len(header) == BLOCK
    return header


def unpack_header(block: bytes) -> Header:
    """Decode one header block. Lenient about what a reader may leave blank
    (the date, the logical number); the caller checks what it relies on."""
    if len(block) != BLOCK:
        raise QwkFormatError("a header is one 128-byte block")
    status = chr(block[0]) if 32 <= block[0] < 127 else " "
    when = None
    try:
        when = datetime.datetime.strptime(
            block[8:16].decode("ascii") + " " + block[16:21].decode("ascii"), "%m-%d-%y %H:%M"
        )
    except (UnicodeDecodeError, ValueError):
        when = None
    return Header(
        status=status,
        number=read_number(block[1:8]),
        when=when,
        to=read_field(block[21:46]),
        sender=read_field(block[46:71]),
        subject=read_field(block[71:96]),
        reference=read_number(block[108:116]),
        blocks=read_number(block[116:122]),
        active=block[122] != KILLED,
        conference=struct.unpack("<H", block[123:125])[0],
        logical=struct.unpack("<H", block[125:127])[0],
        tagline=block[127:128] == b"*",
    )


def qwke_lines(*, to: str, sender: str, subject: str) -> list[str]:
    """The QWKE lines a message needs: one for each field too long for its
    25 characters, then a blank line. Empty when everything fits."""
    lines = [
        f"{name}: {value}"
        for name, value in zip(_QWKE_FIELDS, (to, sender, subject))
        if not fits(value)
    ]
    return [*lines, ""] if lines else []


def encode_text(lines: list[str]) -> bytes:
    """Message text as QWK writes it: each line ended by 0xE3, padded with
    spaces to whole blocks (at least one)."""
    data = b"".join(to_cp437(line) + bytes([LINE_END]) for line in lines)
    padding = (-len(data)) % BLOCK
    if not data:
        padding = BLOCK
    return data + b" " * padding


def body_lines(body: str) -> list[str]:
    """A stored body as the lines a packet carries: CR/LF and bare CR made
    single line breaks, trailing blank lines dropped."""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def decode_text(data: bytes) -> list[str]:
    """Text blocks as lines: split at 0xE3 (a reader writing CR/LF instead
    is tolerated), the padding after the last line dropped, controls out."""
    data = data.rstrip(b" \x00")
    if not data:
        return []
    if LINE_END in data:
        raw_lines = data.split(bytes([LINE_END]))
        if raw_lines and raw_lines[-1].strip(b" \x00") == b"":
            raw_lines.pop()
    else:
        raw_lines = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    return [clean_line(from_cp437(line.replace(b"\r", b"").replace(b"\n", b""))).rstrip() for line in raw_lines]


@dataclass(frozen=True)
class QwkeFields:
    """The QWKE lines found at the top of a text, by field (lower case),
    and the text after them."""

    values: dict[str, str]
    lines: list[str]


def split_qwke(lines: list[str], header: dict[str, str]) -> QwkeFields:
    """Take QWKE `To:`/`From:`/`Subject:` lines off the top of `lines`. Only
    the first run of them counts, and the blank line after them goes too.

    A line counts only when it continues its header field: the 25
    characters in the header (`header`, by lower-case field name) are the
    start of the long value, as they are when a QWKE reader wrote both. So
    a message from a reader without QWKE that happens to begin "To: Bob"
    keeps that line as text."""
    values: dict[str, str] = {}
    index = 0
    while index < len(lines) and index < len(_QWKE_FIELDS):
        match = _QWKE_LINE.match(lines[index])
        if match is None or match.group(1).lower() in values:
            break
        name, value = match.group(1).lower(), match.group(2).strip()
        expected = header.get(name, "")
        if not value or not value.casefold().startswith(expected.casefold()):
            break
        values[name] = value
        index += 1
    if values and index < len(lines) and not lines[index].strip():
        index += 1
    return QwkeFields(values=values, lines=lines[index:])
