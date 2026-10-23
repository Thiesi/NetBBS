"""
QWK packets (issue #1389): writing one for a reader, and reading one back.

A packet is a ZIP named `<BBSID>.QWK` holding:

- `CONTROL.DAT`: the BBS, the packet's time, the user, and the conference
  list (QWKE lets a conference name run past QWK's 13 characters);
- `MESSAGES.DAT`: the Qmail copyright block, then each message's header and
  text blocks (`netbbs.qwk.format`);
- one `NNN.NDX` per conference, and `PERSONAL.NDX` for the messages
  addressed to the user: 5-byte records, the header's block number as a
  Microsoft binary single and the conference's low byte;
- `DOOR.ID`, naming the door and saying names are mixed case;
- `TOREADER.EXT`, QWKE's per-conference flags, whose presence is what tells
  a reader such as MultiMail to use QWKE;
- `HELLO`, a short welcome, when one is given.

`build_packet` is pure: it takes the conferences and messages and returns
the ZIP's bytes. Who gets which messages is decided elsewhere
(`netbbs.qwk.offline` for a caller; QWKnet will decide for a node).
`read_packet` parses a packet, for tests and for a QWKnet node reading its
hub's packets; it is as strict about sizes as the reply reader.
"""

from __future__ import annotations

import datetime
import io
import re
import zipfile
from dataclasses import dataclass, field

from netbbs.qwk.format import (
    BLOCK,
    COPYRIGHT_BLOCK,
    QwkFormatError,
    body_lines,
    build_headers_dat,
    decode_text,
    encode_text,
    from_cp437,
    header_flag,
    pack_header,
    parse_headers_dat,
    qwke_lines,
    split_qwke,
    to_cp437,
    to_msbin,
    unpack_header,
)

#: QWK's BBS ID: 1-8 characters, letters and digits, used as the packet's
#: file name and checked against a reply packet's first block.
_BBS_ID = re.compile(r"^[A-Z0-9]{1,8}$")

#: QWKE allows conference names this long; plain QWK readers cut them.
MAX_CONFERENCE_NAME = 255


def valid_bbs_id(value: str) -> bool:
    return bool(_BBS_ID.match(value))


@dataclass(frozen=True)
class Conference:
    """One conference in a packet. `flags` are QWKE's AREA flags for it
    (`TOREADER.EXT`): `a` all messages or `p` personal, then the BBS's
    own -- `P` private only, `O` public only, `R` read-only, `E` echomail,
    `L` local."""

    number: int
    name: str
    flags: str = "a"


@dataclass(frozen=True)
class PacketMessage:
    """One message to put in a packet. `number` is its message number,
    which a reply names as its reference. `personal` lists it in
    `PERSONAL.NDX`."""

    conference: int
    number: int
    when: datetime.datetime
    to: str
    sender: str
    subject: str
    body: str
    reference: int | None = None
    private: bool = False
    personal: bool = False
    #: QWKnet (#1482): the message's HEADERS.DAT fields, and the header's
    #: network tagline flag. A caller's packet has neither.
    headers: dict[str, str] | None = None
    tagline: bool = False


@dataclass(frozen=True)
class PacketInfo:
    """The BBS and user lines of `CONTROL.DAT`, and `DOOR.ID`'s door."""

    bbs_id: str
    bbs_name: str
    user_name: str
    created_at: datetime.datetime
    sysop_name: str = "SysOp"
    city: str = ""
    phone: str = ""
    door: str = "NetBBS"
    version: str = ""
    hello: str | None = None


def _messages_dat(
    messages: list[PacketMessage], headers: list[tuple[int, dict[str, str]]] | None = None,
) -> tuple[bytes, list[tuple[int, int, bool]]]:
    """MESSAGES.DAT, and each message's (conference, header block, personal).
    Each message's HEADERS.DAT fields, keyed by its header's byte offset,
    are added to `headers` when given."""
    out = [COPYRIGHT_BLOCK.ljust(BLOCK, b" ")]
    block_number = 2  # the copyright block is block 1
    index: list[tuple[int, int, bool]] = []
    for logical, message in enumerate(messages, start=1):
        if not 0 < message.number <= 9_999_999:
            raise QwkFormatError(f"message number {message.number} does not fit QWK's 7 digits")
        lines = qwke_lines(to=message.to, sender=message.sender, subject=message.subject)
        text = encode_text([*lines, *body_lines(message.body)])
        blocks = 1 + len(text) // BLOCK
        out.append(pack_header(
            status="+" if message.private else " ",
            number=str(message.number),
            when=message.when,
            to=message.to,
            sender=message.sender,
            subject=message.subject,
            reference=message.reference,
            blocks=blocks,
            conference=message.conference,
            logical=logical,
            tagline=message.tagline,
        ))
        out.append(text)
        if headers is not None and message.headers:
            headers.append(((block_number - 1) * BLOCK, message.headers))
        index.append((message.conference, block_number, message.personal))
        block_number += blocks
    return b"".join(out), index


def _ndx(records: list[tuple[int, int]]) -> bytes:
    return b"".join(to_msbin(block) + bytes([conference & 0xFF]) for conference, block in records)


def ndx_name(conference: int) -> str:
    """A conference's index file: three digits, four past 999."""
    return f"{conference:03d}.NDX"


def _crlf(lines: list[str]) -> bytes:
    return b"".join(to_cp437(line) + b"\r\n" for line in lines)


def _control_dat(info: PacketInfo, conferences: list[Conference], total: int) -> bytes:
    lines = [
        info.bbs_name,
        info.city,
        info.phone,
        f"{info.sysop_name}, Sysop",
        f"00000,{info.bbs_id}",
        info.created_at.strftime("%m-%d-%Y,%H:%M:%S"),
        info.user_name.upper(),
        "",
        "0",
        str(total),
        str(len(conferences) - 1),
    ]
    for conference in conferences:
        lines.append(str(conference.number))
        lines.append(conference.name.replace("\r", " ").replace("\n", " ")[:MAX_CONFERENCE_NAME])
    lines += ["HELLO" if info.hello else "", "", ""]
    return _crlf(lines)


def _door_id(info: PacketInfo) -> bytes:
    version = info.version or "1"
    return _crlf([
        f"DOOR = {info.door}",
        f"VERSION = {version}",
        f"SYSTEM = {info.door} {version}",
        "MIXEDCASE = YES",
    ])


def build_packet(info: PacketInfo, conferences: list[Conference], messages: list[PacketMessage]) -> bytes:
    """The packet's ZIP, as bytes. Every message's conference must be in
    `conferences`; a conference with no messages gets an empty index."""
    if not valid_bbs_id(info.bbs_id):
        raise QwkFormatError(f"{info.bbs_id!r} is not a QWK BBS ID (1-8 letters and digits)")
    if not conferences:
        raise QwkFormatError("a packet lists at least one conference")
    numbers = {conference.number for conference in conferences}
    if len(numbers) != len(conferences):
        raise QwkFormatError("two conferences share a number")
    stray = {message.conference for message in messages} - numbers
    if stray:
        raise QwkFormatError(f"messages in conferences the packet does not list: {sorted(stray)}")
    headers: list[tuple[int, dict[str, str]]] = []
    messages_dat, index = _messages_dat(messages, headers)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("CONTROL.DAT", _control_dat(info, conferences, len(messages)))
        archive.writestr("MESSAGES.DAT", messages_dat)
        archive.writestr("DOOR.ID", _door_id(info))
        archive.writestr(
            "TOREADER.EXT", _crlf([f"AREA {conference.number} {conference.flags}" for conference in conferences])
        )
        for conference in conferences:
            records = [(number, block) for number, block, _personal in index if number == conference.number]
            archive.writestr(ndx_name(conference.number), _ndx(records))
        personal = [(number, block) for number, block, is_personal in index if is_personal]
        if personal:
            archive.writestr("PERSONAL.NDX", _ndx(personal))
        if info.hello:
            archive.writestr("HELLO", _crlf(info.hello.splitlines()))
        if headers:
            archive.writestr("HEADERS.DAT", build_headers_dat(headers))
    return buffer.getvalue()


# -- reading --------------------------------------------------------------

#: Bounds on a packet read back. A QWKnet hub's packet is remote input.
MAX_PACKET_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_PACKET_MEMBERS = 4096
MAX_MESSAGES_DAT_BYTES = 128 * 1024 * 1024
MAX_SMALL_MEMBER_BYTES = 1024 * 1024
MAX_HEADERS_DAT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ReadMessage:
    conference: int
    number: int | None
    when: datetime.datetime | None
    to: str
    sender: str
    subject: str
    body: str
    reference: int | None
    private: bool
    #: Its HEADERS.DAT fields (keys lower case), the header's status
    #: character ('V' is a Synchronet vote) and network tagline flag.
    headers: dict[str, str] = field(default_factory=dict)
    status: str = " "
    tagline: bool = False


@dataclass(frozen=True)
class ReadPacket:
    bbs_name: str
    bbs_id: str
    user_name: str
    conferences: list[Conference]
    messages: list[ReadMessage]
    files: list[str] = field(default_factory=list)


def read_member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes | None:
    """The member called `name` (any case), read to at most `limit` bytes:
    the ZIP's own sizes are not trusted, so a member that inflates past the
    limit is refused whatever its header claims."""
    wanted = name.upper()
    for info in archive.infolist():
        if info.filename.replace("\\", "/").split("/")[-1].upper() == wanted and not info.is_dir():
            if info.file_size > limit:
                raise QwkFormatError(f"{name} is larger than {limit} bytes")
            with archive.open(info) as handle:
                data = handle.read(limit + 1)
            if len(data) > limit:
                raise QwkFormatError(f"{name} is larger than {limit} bytes")
            return data
    return None


def open_archive(data: bytes, *, max_bytes: int, max_members: int) -> zipfile.ZipFile:
    if len(data) > max_bytes:
        raise QwkFormatError(f"the archive is larger than {max_bytes} bytes")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise QwkFormatError("not a ZIP archive") from exc
    if len(archive.infolist()) > max_members:
        raise QwkFormatError(f"the archive holds more than {max_members} files")
    return archive


def parse_messages(
    data: bytes, *, limit: int | None = None, raw: bool = False,
) -> tuple[bytes, list, list[str]]:
    """Walk MESSAGES.DAT or a reply packet's .MSG: the first block's bytes,
    then `(header, lines)` per message, and problems that stopped the walk.
    A damaged header ends it: nothing after a wrong block count can be
    found again. With `raw`, each message is `(header, lines, offset,
    text)`: its header's byte offset, which HEADERS.DAT names it by, and
    its text blocks undecoded, for a message marked UTF-8 there."""
    if len(data) < BLOCK:
        raise QwkFormatError("too short to hold a first block")
    first = data[:BLOCK]
    messages = []
    problems: list[str] = []
    offset = BLOCK
    while offset + BLOCK <= len(data):
        if limit is not None and len(messages) >= limit:
            problems.append(f"only the first {limit} messages were read")
            break
        position = len(messages) + 1
        header = unpack_header(data[offset:offset + BLOCK])
        if header.blocks is None or header.blocks < 1:
            if data[offset:offset + BLOCK].strip(b" \x00") == b"":
                break  # padding at the end
            problems.append(f"message {position} has a damaged header; nothing after it was read")
            break
        end = offset + header.blocks * BLOCK
        if end > len(data):
            problems.append(f"message {position} runs past the end of the file; nothing after it was read")
            break
        text = data[offset + BLOCK:end]
        lines = decode_text(text)
        messages.append((header, lines, offset, text) if raw else (header, lines))
        offset = end
    return first, messages, problems


def read_packet(data: bytes) -> ReadPacket:
    """Parse a QWK packet. Raises `QwkFormatError` for one that cannot be
    read at all."""
    archive = open_archive(data, max_bytes=MAX_PACKET_ARCHIVE_BYTES, max_members=MAX_PACKET_MEMBERS)
    control = read_member(archive, "CONTROL.DAT", MAX_SMALL_MEMBER_BYTES)
    messages_dat = read_member(archive, "MESSAGES.DAT", MAX_MESSAGES_DAT_BYTES)
    if control is None or messages_dat is None:
        raise QwkFormatError("a QWK packet holds CONTROL.DAT and MESSAGES.DAT")
    lines = from_cp437(control).replace("\r\n", "\n").split("\n")
    if len(lines) < 11:
        raise QwkFormatError("CONTROL.DAT is too short")
    bbs_id = lines[4].split(",", 1)[1].strip() if "," in lines[4] else ""
    try:
        count = int(lines[10].strip()) + 1
    except ValueError as exc:
        raise QwkFormatError("CONTROL.DAT's conference count is not a number") from exc
    if not 0 <= count <= 65536 or len(lines) < 11 + 2 * count:
        raise QwkFormatError("CONTROL.DAT's conference list is cut short")
    flags: dict[int, str] = {}
    toreader = read_member(archive, "TOREADER.EXT", MAX_SMALL_MEMBER_BYTES)
    if toreader is not None:
        for line in from_cp437(toreader).splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0].upper() == "AREA" and parts[1].isdigit():
                flags[int(parts[1])] = parts[2]
    conferences = []
    for i in range(count):
        number_text = lines[11 + 2 * i].strip()
        if not number_text.isdigit():
            raise QwkFormatError("CONTROL.DAT names a conference without a number")
        number = int(number_text)
        conferences.append(Conference(number, lines[12 + 2 * i].strip(), flags.get(number, "a")))
    headers_dat = read_member(archive, "HEADERS.DAT", MAX_HEADERS_DAT_BYTES)
    all_headers = parse_headers_dat(headers_dat) if headers_dat is not None else {}
    _first, walked, problems = parse_messages(messages_dat, raw=True)
    if problems:
        raise QwkFormatError("; ".join(problems))
    messages = []
    for header, text, offset, raw_text in walked:
        if not header.active:
            continue
        fields = all_headers.get(offset, {})
        if header_flag(fields, "utf8"):
            text = decode_text(raw_text, utf8=True)
        qwke = split_qwke(text, {"to": header.to, "from": header.sender, "subject": header.subject})
        messages.append(ReadMessage(
            conference=header.conference,
            number=header.number,
            when=header.when,
            to=fields.get("to") or qwke.values.get("to", header.to),
            sender=fields.get("sender") or qwke.values.get("from", header.sender),
            subject=fields.get("subject") or qwke.values.get("subject", header.subject),
            body="\n".join(qwke.lines),
            reference=header.reference,
            private=header.private,
            headers=fields,
            status=header.status,
            tagline=header.tagline,
        ))
    return ReadPacket(
        bbs_name=lines[0].strip(),
        bbs_id=bbs_id,
        user_name=lines[6].strip(),
        conferences=conferences,
        messages=messages,
        files=[info.filename for info in archive.infolist()],
    )
