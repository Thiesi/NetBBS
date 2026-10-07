"""
FTN message text: kludges, the AREA line, tear and Origin lines, SEEN-BY
and PATH (FTS-0004, FSC-0074, FTS-0009, FTS-4008, FTS-5003).

The text of a packed message is CR-terminated lines. In order:

- echomail only: `AREA:TAG`, the echo the message belongs to;
- kludges, lines starting with ^A (0x01): `MSGID`, `REPLY`, `PID`,
  `TZUTC`, `CHRS`, and for netmail `INTL`, `FMPT` and `TOPT` (which have
  no colon);
- the body;
- echomail: a tear line (`--- product`) and one Origin line
  (` * Origin: text (address)`);
- echomail: `SEEN-BY:` lines and `^APATH:` lines, both 2D net/node lists.
  SEEN-BY is sorted, PATH keeps its order; both write the net once and
  then bare node numbers while it stays the same, and no line exceeds 80
  characters. Netmail instead collects `^AVia` lines here.

`decode_message` turns a `PackedMessage` into an `FtnMessage` and
`encode_message` the reverse. Decoding is tolerant where real software is
sloppy -- a missing tear line, LF bytes among the CRs, `*Origin` without
its leading space -- and keeps every kludge it does not interpret, so a
message forwarded on is the message received.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress, find_address, parse_address
from netbbs.ftn.chrs import (
    DEFAULT_CHARSET,
    MAX_NAME_BYTES,
    MAX_SUBJECT_BYTES,
    Charset,
    choose_outbound_charset,
    codec_for_kludge,
    decode,
    kludge_for_codec,
    truncate_encoded,
)
from netbbs.ftn.packet import PackedMessage

KLUDGE = "\x01"
MAX_CONTROL_LINE = 80
MAX_ORIGIN_LINE = 79
MAX_AREA_TAG = 60

_ORIGIN = re.compile(r"^ ?\* ?Origin: ?(?P<text>.*)$")
_AREA_TAG = re.compile(r"[\x21-\x60\x7b-\x7e]{1,60}")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FTS_DATE = re.compile(r"\s*(\d{1,2}) ([A-Za-z]{3}) (\d{2})\s+(\d{1,2}):(\d{2}):(\d{2})")
# SEAdog's variant: "Wed  7 Oct 26 14:05".
_SEADOG_DATE = re.compile(r"\s*[A-Za-z]{3}\s+(\d{1,2}) ([A-Za-z]{3}) (\d{2}) (\d{1,2}):(\d{2})")
_TZUTC = re.compile(r"([+-]?)(\d{2})(\d{2})")
_KLUDGE_LINE = re.compile(r"(?P<name>[^\s:]*):?\s*(?P<value>.*)", re.DOTALL)


@dataclass
class FtnMessage:
    to_name: str
    from_name: str
    subject: str
    body: str  # lines joined by "\n"
    area: str | None = None  # the echo tag; None for netmail
    date: datetime.datetime | None = None  # as written: the writer's local time, naive
    kludges: list[tuple[str, str]] = field(default_factory=list)  # before the body, in order
    trailing_kludges: list[tuple[str, str]] = field(default_factory=list)  # after it, but PATH
    tear_line: str | None = None  # after "---", without it
    origin: str | None = None  # after " * Origin: ", without it
    seen_by: list[tuple[int, int]] = field(default_factory=list)
    path: list[tuple[int, int]] = field(default_factory=list)
    attributes: int = 0
    orig_net: int = 0
    orig_node: int = 0
    dest_net: int = 0
    dest_node: int = 0
    charset: str | None = None  # the Python codec it was read in or is written in

    @property
    def is_echomail(self) -> bool:
        return self.area is not None

    def kludge(self, name: str) -> str | None:
        """The first kludge called `name` (case-insensitive), wherever it is."""
        wanted = name.upper()
        for key, value in (*self.kludges, *self.trailing_kludges):
            if key.upper() == wanted:
                return value
        return None

    @property
    def msgid(self) -> str | None:
        return _normalise_msgid(self.kludge("MSGID"))

    @property
    def reply(self) -> str | None:
        return _normalise_msgid(self.kludge("REPLY"))

    @property
    def utc_offset(self) -> datetime.timedelta | None:
        return parse_tzutc(self.kludge("TZUTC") or "")

    @property
    def origin_address(self) -> FtnAddress | None:
        """Where the message was written: the Origin line's address, else
        the MSGID's."""
        if self.origin:
            found = find_address(self.origin)
            if found is not None:
                return found
        msgid = self.msgid
        if msgid:
            try:
                return parse_address(msgid.split()[0])
            except FtnFormatError:
                return None
        return None

    def utc_date(self) -> datetime.datetime | None:
        """The date in UTC, when a TZUTC kludge says how to get there."""
        offset = self.utc_offset
        if self.date is None or offset is None:
            return None
        return (self.date - offset).replace(tzinfo=datetime.timezone.utc)


def decode_message(packed: PackedMessage, *, default_charset: str = DEFAULT_CHARSET) -> FtnMessage:
    """Read a packed message's text into its parts."""
    codec = codec_for_kludge(
        _raw_kludge(packed.text, b"CHRS") or _raw_kludge(packed.text, b"CHARSET"),
        codepage=_raw_kludge(packed.text, b"CODEPAGE"),
        default=default_charset,
    )
    text = decode(packed.text, codec).replace("\n", "")
    lines = text.split("\r")
    if lines and lines[-1] == "":
        lines.pop()

    message = FtnMessage(
        to_name=decode(packed.to_name, codec),
        from_name=decode(packed.from_name, codec),
        subject=decode(packed.subject, codec),
        body="",
        date=parse_fts_date(packed.date.decode("ascii", errors="replace")),
        attributes=packed.attributes,
        orig_net=packed.orig_net,
        orig_node=packed.orig_node,
        dest_net=packed.dest_net,
        dest_node=packed.dest_node,
        charset=codec,
    )

    if lines:
        first = lines[0].removeprefix(KLUDGE)
        if first.startswith("AREA:"):
            message.area = first[5:].strip()
            lines = lines[1:]

    content: list[str] = []
    for line in lines:
        if line.startswith(KLUDGE):
            name, value = _split_kludge(line[1:])
            if name.upper() == "PATH":
                message.path.extend(parse_net_nodes(value))
            elif content:
                message.trailing_kludges.append((name, value))
            else:
                message.kludges.append((name, value))
        else:
            content.append(line)

    seen_by_lines = []
    while content and content[-1].startswith("SEEN-BY:"):
        seen_by_lines.insert(0, content.pop())
    message.seen_by = [entry for line in seen_by_lines for entry in parse_net_nodes(line[8:])]
    while content and not content[-1].strip():
        content.pop()
    if content:
        match = _ORIGIN.match(content[-1])
        if match:
            message.origin = match["text"].rstrip()
            content.pop()
    if content and (content[-1] == "---" or content[-1].startswith("--- ")):
        message.tear_line = content[-1][4:].rstrip()
        content.pop()
    message.body = "\n".join(content).rstrip("\n")
    return message


def encode_message(message: FtnMessage) -> PackedMessage:
    """Write `message` as a packed message.

    The character set is `message.charset` when set, otherwise CP437 if
    everything fits and UTF-8 if not; any CHRS kludge is replaced by one
    naming the set actually used. To and From are cut to 35 bytes and the
    subject to 71, on character boundaries.
    """
    if message.charset:
        charset = Charset(message.charset, kludge_for_codec(message.charset))
    else:
        charset = choose_outbound_charset(message.to_name, message.from_name, message.subject,
                                          message.body, message.origin or "", message.tear_line or "")
    kludges = [(name, value) for name, value in message.kludges if name.upper() not in ("CHRS", "CHARSET")]
    kludges.append(("CHRS", charset.kludge))

    lines: list[str] = []
    if message.area is not None:
        if not _AREA_TAG.fullmatch(message.area):
            raise FtnFormatError(f"echo tag {message.area!r} is not 1-{MAX_AREA_TAG} printable characters")
        lines.append(f"AREA:{message.area}")
    lines.extend(_format_kludge(name, value) for name, value in kludges)
    body = _plain(message.body)
    lines.extend(body.split("\n") if body else [])
    if message.tear_line is not None:
        lines.append(f"--- {_plain(message.tear_line)}".rstrip())
    if message.origin is not None:
        lines.append(f" * Origin: {_plain(message.origin)}"[:MAX_ORIGIN_LINE])
    if message.seen_by:
        lines.extend(format_net_node_lines("SEEN-BY: ", sorted(set(message.seen_by))))
    lines.extend(_format_kludge(name, value) for name, value in message.trailing_kludges)
    if message.path:
        lines.extend(KLUDGE + line for line in format_net_node_lines("PATH: ", message.path, width=MAX_CONTROL_LINE - 1))

    codec = charset.codec
    return PackedMessage(
        orig_net=message.orig_net,
        orig_node=message.orig_node,
        dest_net=message.dest_net,
        dest_node=message.dest_node,
        attributes=message.attributes,
        cost=0,
        date=format_fts_date(message.date or datetime.datetime.now()).encode("ascii"),
        to_name=truncate_encoded(message.to_name, codec, MAX_NAME_BYTES),
        from_name=truncate_encoded(message.from_name, codec, MAX_NAME_BYTES),
        subject=truncate_encoded(message.subject, codec, MAX_SUBJECT_BYTES),
        text="".join(line + "\r" for line in lines).encode(codec, errors="replace"),
    )


def build_origin(text: str, address: FtnAddress) -> str:
    """The Origin line's text: `text (address)`, with `text` shortened so the
    whole ` * Origin: ` line stays within 79 characters."""
    suffix = f" ({address})"
    room = MAX_ORIGIN_LINE - len(" * Origin: ") - len(suffix)
    return text[:max(room, 0)].rstrip() + suffix


def format_msgid(address: FtnAddress, serial: int) -> str:
    """A MSGID value: the origin address and an 8-hex-digit serial
    (FTS-0009). The serial must not repeat for three years."""
    return f"{address} {serial & 0xFFFFFFFF:08x}"


def parse_net_nodes(text: str) -> list[tuple[int, int]]:
    """A SEEN-BY or PATH list: `net/node` sets the net, a bare `node` reuses
    it. Zones and points are dropped (both lists are 2D); anything else is
    skipped."""
    entries = []
    net = None
    for token in text.split():
        if ":" in token:
            token = token.split(":", 1)[1]
        token = token.split(".", 1)[0].split("@", 1)[0]
        if "/" in token:
            net_text, _, node_text = token.partition("/")
            if not (net_text.isdigit() and node_text.isdigit()):
                continue
            net = int(net_text)
            node = int(node_text)
        elif token.isdigit() and net is not None:
            node = int(token)
        else:
            continue
        if net <= 0xFFFF and node <= 0xFFFF:
            entries.append((net, node))
    return entries


def format_net_node_lines(prefix: str, entries: list[tuple[int, int]], *, width: int = MAX_CONTROL_LINE) -> list[str]:
    """`entries` as lines of `prefix` plus net/node tokens, each line at most
    `width` characters, the net written again at the start of every line."""
    lines: list[str] = []
    line = ""
    net = None
    for entry_net, entry_node in entries:
        bare = f"{entry_node}" if entry_net == net and line else f"{entry_net}/{entry_node}"
        if line and len(prefix) + len(line) + 1 + len(bare) > width:
            lines.append(prefix + line)
            line = ""
            bare = f"{entry_net}/{entry_node}"
        line = f"{line} {bare}" if line else bare
        net = entry_net
    if line:
        lines.append(prefix + line)
    return lines


def parse_fts_date(text: str) -> datetime.datetime | None:
    """FTS-0001's `DD Mon YY  HH:MM:SS`, or SEAdog's `Day DD Mon YY HH:MM`.
    Two-digit years 80-99 are 19xx and the rest 20xx. None if unreadable."""
    match = _FTS_DATE.match(text)
    second = None
    if match:
        day, month_name, year, hour, minute, second = match.groups()
    else:
        match = _SEADOG_DATE.match(text)
        if not match:
            return None
        day, month_name, year, hour, minute = match.groups()
    month = month_name.capitalize()
    if month not in _MONTHS:
        return None
    full_year = 1900 + int(year) if int(year) >= 80 else 2000 + int(year)
    try:
        return datetime.datetime(full_year, _MONTHS.index(month) + 1, int(day),
                                 int(hour), int(minute), int(second or 0))
    except ValueError:
        return None


def format_fts_date(moment: datetime.datetime) -> str:
    """`DD Mon YY  HH:MM:SS`, 19 characters, in the time `moment` carries."""
    return f"{moment.day:02d} {_MONTHS[moment.month - 1]} {moment.year % 100:02d}  {moment:%H:%M:%S}"


def parse_tzutc(text: str) -> datetime.timedelta | None:
    """A TZUTC value (FTS-4008): `-hhmm` or `hhmm`, a leading `+` tolerated."""
    match = _TZUTC.fullmatch(text.strip())
    if not match:
        return None
    sign, hours, minutes = match.groups()
    if int(hours) > 14 or int(minutes) > 59:
        return None
    offset = datetime.timedelta(hours=int(hours), minutes=int(minutes))
    return -offset if sign == "-" else offset


def format_tzutc(offset: datetime.timedelta) -> str:
    """A TZUTC value: `-hhmm`, or `hhmm` with no plus sign, as FTS-4008 says."""
    minutes = int(offset.total_seconds() // 60)
    sign = "-" if minutes < 0 else ""
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}{minutes % 60:02d}"


def _plain(text: str) -> str:
    # What a caller typed must not become structure: a ^A would start a
    # kludge line, a CR a line of its own, and a NUL would end the text.
    return text.replace("\r", "").replace(KLUDGE, "").replace("\x00", "")


def _split_kludge(line: str) -> tuple[str, str]:
    # "MSGID: value", "CHRS:value" and "FMPT 1" all occur.
    match = _KLUDGE_LINE.match(line)
    return match["name"], match["value"].strip()


def _format_kludge(name: str, value: str) -> str:
    # FMPT, TOPT, INTL and Via take no colon (FTS-4001); every other kludge does.
    separator = " " if name.upper() in ("FMPT", "TOPT", "INTL", "VIA") else ": "
    return f"{KLUDGE}{name}{separator}{value}"


def _raw_kludge(text: bytes, name: bytes) -> str | None:
    # Found before decoding, since it says how to decode. Kludge names and
    # values are ASCII in every character set FTN uses.
    for line in text.split(b"\r"):
        line = line.lstrip(b"\n")
        if line.startswith(b"\x01" + name + b":"):
            return line[len(name) + 2:].decode("ascii", errors="replace").strip()
        if line.startswith(b"\x01" + name + b" "):
            return line[len(name) + 2:].decode("ascii", errors="replace").strip()
    return None


def _normalise_msgid(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(value.split())
