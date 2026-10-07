"""
FTN packets: the 58-byte header and packed messages (FTS-0001, FSC-0039,
FSC-0048).

A packet is a header, any number of packed messages, and two zero bytes.
All numbers are little-endian 16-bit.

Header layouts read here:

- **Type 2+ (FSC-0048)**, which `build_packet` writes: a capability word at
  44 with a byte-swapped copy at 40, bit 0 set; zones at 46/48, points at
  50/52. A point sender writes -1 as its net at 20 and its real net in
  AuxNet at 38, so point-unaware software sees no false node.
- **FSC-0039**: the same capability word and zone/point fields, with the
  real net at 20 even for a point.
- **Type 2 (FTS-0001)**: no valid capability word; zones at 34/36 when the
  writer filled them, and no points.

Type 2.2 (FSC-0045, a 2 at offset 16 where 2/2+ keep a baud rate) is not
read. It is rare, and its domain fields have no place in the rest of the
format.

Packed messages carry only net/node of each end. Zones and points of a
message come from its kludges and Origin line (`netbbs.ftn.message`), so
this layer returns the four fixed fields as written and the variable ones
as raw bytes: their character set is named inside the text.

Bounds: `parse_packet` takes the whole packet in memory and refuses one
over `MAX_PACKET_BYTES`, more than `MAX_MESSAGES` messages, a name or
subject without its NUL inside its field limit, or text over
`MAX_TEXT_BYTES`. A truncated packet (no terminating `00 00`) yields the
messages before the break and records that it was truncated, which is
what tossers do with a packet cut off mid-session.
"""

from __future__ import annotations

import datetime
import struct
from dataclasses import dataclass, field

from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress

HEADER_BYTES = 58
PACKET_VERSION = 2
MAX_PACKET_BYTES = 16 * 1024 * 1024
MAX_MESSAGES = 10_000
MAX_TEXT_BYTES = 1024 * 1024
# Field sizes including the terminating NUL (FTS-0001).
TO_FROM_FIELD = 36
SUBJECT_FIELD = 72
DATE_FIELD = 20
PASSWORD_FIELD = 8

CAPABILITY_2PLUS = 0x0001

# Message attribute bits (FTS-0001) that mean something inside a packet.
ATTR_PRIVATE = 0x0001
ATTR_CRASH = 0x0002
ATTR_FILE_ATTACHED = 0x0010
ATTR_IN_TRANSIT = 0x0020
ATTR_KILL_SENT = 0x0080
ATTR_LOCAL = 0x0100
ATTR_HOLD = 0x0200
ATTR_FILE_REQUEST = 0x0800

# The product code NetBBS writes. 0xFE is FTSC's "no code assigned" value;
# a registered code can replace it without changing anything else.
PRODUCT_CODE = 0xFE

_HEADER = struct.Struct("<12H2B8s4H2B5H4s")
_MESSAGE_HEAD = struct.Struct("<7H")


@dataclass(frozen=True)
class PacketHeader:
    orig: FtnAddress
    dest: FtnAddress
    created: datetime.datetime | None  # local time of the writer, naive
    password: str = ""
    product_code: int = PRODUCT_CODE
    product_version: tuple[int, int] = (0, 0)
    kind: str = "2+"  # "2", or "2+" for any capability-word packet (2+ and FSC-0039)


@dataclass(frozen=True)
class PackedMessage:
    orig_net: int
    orig_node: int
    dest_net: int
    dest_node: int
    attributes: int
    cost: int
    date: bytes  # "DD Mon YY  HH:MM:SS", see `netbbs.ftn.message.parse_fts_date`
    to_name: bytes
    from_name: bytes
    subject: bytes
    text: bytes


@dataclass
class Packet:
    header: PacketHeader
    messages: list[PackedMessage] = field(default_factory=list)
    truncated: bool = False


def parse_packet(data: bytes) -> Packet:
    """Read a whole packet; raises `FtnFormatError` on a malformed one."""
    if len(data) > MAX_PACKET_BYTES:
        raise FtnFormatError(f"packet of {len(data)} bytes exceeds {MAX_PACKET_BYTES}")
    if len(data) < HEADER_BYTES:
        raise FtnFormatError("packet is shorter than its 58-byte header")
    header = _parse_header(data[:HEADER_BYTES])
    packet = Packet(header)
    offset = HEADER_BYTES
    while True:
        if offset + 2 > len(data):
            packet.truncated = True
            return packet
        (marker,) = struct.unpack_from("<H", data, offset)
        if marker == 0:
            return packet
        if marker != PACKET_VERSION:
            raise FtnFormatError(f"packed message at byte {offset} starts with {marker:#06x}, not 0x0002")
        if len(packet.messages) >= MAX_MESSAGES:
            raise FtnFormatError(f"packet holds more than {MAX_MESSAGES} messages")
        try:
            message, offset = _parse_message(data, offset)
        except _Truncated:
            packet.truncated = True
            return packet
        packet.messages.append(message)


def build_packet(header: PacketHeader, messages: list[PackedMessage]) -> bytes:
    """A Type 2+ packet holding `messages`."""
    return build_packet_from_packed(header, [pack_message(message) for message in messages])


def build_packet_from_packed(header: PacketHeader, packed: list[bytes]) -> bytes:
    """A Type 2+ packet holding messages already encoded by `pack_message`,
    as the outbound queue stores them."""
    return _build_header(header) + b"".join(packed) + b"\x00\x00"


class _Truncated(Exception):
    pass


def _parse_header(raw: bytes) -> PacketHeader:
    (orig_node, dest_node, year, month, day, hour, minute, second, baud, version,
     orig_net, dest_net, product_low, revision_major, password, q_orig_zone, q_dest_zone,
     aux_net, capability_copy, product_high, revision_minor, capability, orig_zone, dest_zone,
     orig_point, dest_point, _product_data) = _HEADER.unpack(raw)
    if version != PACKET_VERSION:
        raise FtnFormatError(f"packet version {version} is not 2")
    if baud == 2:
        raise FtnFormatError("Type 2.2 (FSC-0045) packets are not supported")
    swapped = ((capability_copy & 0xFF) << 8) | (capability_copy >> 8)
    if capability & CAPABILITY_2PLUS and swapped == capability:
        # FSC-0048's point sender; FSC-0039 writes the real net here.
        if orig_net == 0xFFFF and orig_point:
            orig_net = aux_net
        kind = "2+"
        orig = FtnAddress(orig_zone or q_orig_zone, orig_net, orig_node, orig_point)
        dest = FtnAddress(dest_zone or q_dest_zone, dest_net, dest_node, dest_point)
        product = (product_high << 8) | product_low
    else:
        kind = "2"
        if orig_net == 0xFFFF:
            raise FtnFormatError("a Type 2 packet cannot come from net -1")
        orig = FtnAddress(q_orig_zone, orig_net, orig_node)
        dest = FtnAddress(q_dest_zone, dest_net, dest_node)
        product = product_low
    return PacketHeader(
        orig=orig,
        dest=dest,
        created=_header_time(year, month, day, hour, minute, second),
        password=password.split(b"\x00", 1)[0].decode("ascii", errors="replace"),
        product_code=product,
        product_version=(revision_major, revision_minor),
        kind=kind,
    )


def _header_time(year: int, month: int, day: int, hour: int, minute: int, second: int) -> datetime.datetime | None:
    # FTS-0001's month counts from 0. Many writers fill these sloppily, so a
    # nonsensical date is no reason to refuse the packet.
    try:
        return datetime.datetime(year, month + 1, day, hour, minute, second)
    except ValueError:
        return None


def _build_header(header: PacketHeader) -> bytes:
    orig, dest = header.orig, header.dest
    created = header.created or datetime.datetime.now()
    # A password read from a remote packet may hold bytes ASCII lacks; they
    # go back as "?" rather than refuse the packet a held message is put in.
    password = header.password.encode("ascii", errors="replace")
    if len(password) > PASSWORD_FIELD:
        raise FtnFormatError(f"packet password is longer than {PASSWORD_FIELD} bytes")
    capability = CAPABILITY_2PLUS
    swapped = ((capability & 0xFF) << 8) | (capability >> 8)
    product = header.product_code
    return _HEADER.pack(
        orig.node, dest.node,
        created.year, created.month - 1, created.day, created.hour, created.minute, created.second,
        0, PACKET_VERSION,
        0xFFFF if orig.point else orig.net, dest.net,
        product & 0xFF, header.product_version[0],
        password.ljust(PASSWORD_FIELD, b"\x00"),
        orig.zone, dest.zone,
        orig.net if orig.point else 0,
        swapped,
        (product >> 8) & 0xFF, header.product_version[1],
        capability,
        orig.zone, dest.zone,
        orig.point, dest.point,
        b"\x00" * 4,
    )


def _parse_message(data: bytes, offset: int) -> tuple[PackedMessage, int]:
    if offset + _MESSAGE_HEAD.size + DATE_FIELD > len(data):
        raise _Truncated
    (_version, orig_node, dest_node, orig_net, dest_net, attributes, cost) = _MESSAGE_HEAD.unpack_from(data, offset)
    offset += _MESSAGE_HEAD.size
    # A writer that fills all 20 bytes leaves no NUL; the 19 are the date.
    date = data[offset:offset + DATE_FIELD].split(b"\x00", 1)[0][:DATE_FIELD - 1]
    offset += DATE_FIELD
    to_name, offset = _read_string(data, offset, TO_FROM_FIELD, "To name")
    from_name, offset = _read_string(data, offset, TO_FROM_FIELD, "From name")
    subject, offset = _read_string(data, offset, SUBJECT_FIELD, "subject")
    text, offset = _read_string(data, offset, MAX_TEXT_BYTES + 1, "message text")
    return PackedMessage(
        orig_net=orig_net, orig_node=orig_node, dest_net=dest_net, dest_node=dest_node,
        attributes=attributes, cost=cost, date=date,
        to_name=to_name, from_name=from_name, subject=subject, text=text,
    ), offset


def _read_string(data: bytes, offset: int, limit: int, what: str) -> tuple[bytes, int]:
    end = data.find(b"\x00", offset, offset + limit)
    if end < 0:
        if offset + limit > len(data):
            raise _Truncated
        raise FtnFormatError(f"{what} has no terminating NUL within {limit} bytes")
    return data[offset:end], end + 1


def pack_message(message: PackedMessage) -> bytes:
    """One packed message's bytes, as they sit inside a packet."""
    for value, limit, what in (
        (message.to_name, TO_FROM_FIELD, "To name"),
        (message.from_name, TO_FROM_FIELD, "From name"),
        (message.subject, SUBJECT_FIELD, "subject"),
        (message.date, DATE_FIELD, "date"),
    ):
        if len(value) >= limit or b"\x00" in value:
            raise FtnFormatError(f"{what} must be under {limit} bytes with no NUL")
    if len(message.text) > MAX_TEXT_BYTES or b"\x00" in message.text:
        raise FtnFormatError(f"message text must be at most {MAX_TEXT_BYTES} bytes with no NUL")
    return b"".join((
        _MESSAGE_HEAD.pack(PACKET_VERSION, message.orig_node, message.dest_node, message.orig_net,
                           message.dest_net, message.attributes, message.cost),
        message.date.ljust(DATE_FIELD, b"\x00"),
        message.to_name, b"\x00",
        message.from_name, b"\x00",
        message.subject, b"\x00",
        message.text, b"\x00",
    ))
