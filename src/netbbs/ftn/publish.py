"""
Publishing: a pointlist of this node's points and a nodelist segment for
the net it coordinates (design doc §6.8 "Publishing"; issue #1480, §16
"Issue #1154", Decision 18). Everything here is synchronous and `db`-first.

- **The pointlist** is FTS-5002's "Boss" format: a `Boss,zone:net/node`
  line for this node, then one line per listed point with an empty first
  field, `,point,name,location,sysop,phone,speed,flags`. The points are
  the network's `ftn_links` points; each one's line (its system name,
  location, sysop, phone, speed and flags, and whether it is listed at
  all) is kept beside the link in `ftn_publish_points`, so the link record
  itself is not changed. A disabled point is left out.
- **The nodelist segment** is FTS-5000's format for a net (§7): a `Host`
  line for the net this node is in, whose fields the SysOp sets, then the
  node lines the SysOp maintains in `ftn_segment_nodes` (`,node,...`, or
  `Pvt`, `Hold` or `Down`), by node number.

Both start with FTS-5000's special first line, `;A ... Day number ddd :
ccccc`, whose CRC-16 is computed exactly as `netbbs.ftn.nodelist` checks
it (`list_crc`): from the second line to the end, CR LF line ends, the
end-of-file byte left out. Lines end CR LF and the file ends with Ctrl-Z.
Fields are 7-bit printable ASCII with underscores for spaces; a line
longer than FTS-5000's 157 characters is refused.

**An edition** is written to `<NAME>.ddd`, `ddd` being the day of the year
in the node's time zone, in the publishing folder (the SysOp's, else
`<files>/ftn-publish/<domain>`), and with **ZIP** on also to `<NAME>.Zdd`
(FTS-5000 §4) holding it. Earlier editions stay. With a file echo set,
the edition (the ZIP when there is one) is hatched there through
`netbbs.ftn.hatch.hatch_generated_file`, with `Replaces` naming the
previous edition's file. A list that can't be built is not written; a
hatch that is refused leaves the written file and says why.

**The schedule** is off, daily or weekly on one weekday. The mailer asks
`publish_scheduled` on every check of an enabled network; an edition is
published at most once on a scheduled day, on the first check that day.
"""

from __future__ import annotations

import datetime
import io
import logging
import os
import re
import secrets
import sqlite3
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from netbbs import __version__
from netbbs.auth.users import User
from netbbs.files.storage import storage_root
from netbbs.ftn.hatch import HatchError, hatch_generated_file
from netbbs.ftn.links import DISABLED, POINT, FtnLink, is_own_point, list_links
from netbbs.ftn.networks import FtnNetwork, FtnNetworkError, get_network, normalise_area_tag
from netbbs.ftn.binkp import DEFAULT_PORT
from netbbs.ftn.nodelist import list_crc, list_lines, parse_header
from netbbs.ftn.queue import FtnQueueFullError
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.timeutil import get_node_timezone, utc_now_iso

_logger = logging.getLogger(__name__)

POINTLIST, SEGMENT = "pointlist", "segment"
LIST_KINDS = (POINTLIST, SEGMENT)
LIST_LABELS = {POINTLIST: "pointlist", SEGMENT: "nodelist segment"}
SCHEDULE_OFF, SCHEDULE_DAILY, SCHEDULE_WEEKLY = "off", "daily", "weekly"
SCHEDULES = (SCHEDULE_OFF, SCHEDULE_DAILY, SCHEDULE_WEEKLY)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
NODE_KEYWORDS = ("", "Pvt", "Hold", "Down")
UNPUBLISHED = "-Unpublished-"
DEFAULT_SPEED = "300"
DEFAULT_POINTLIST_NAME = "POINTS"

MAX_LINE = 157  # FTS-5000 §5: "at most 157 characters (plus terminator)"
MAX_FIELD = 60
MAX_SYSOP = 36  # FTS-5000 §5.3, field 5
MAX_PHONE = 29
MAX_FLAGS = 120
MAX_FOLDER = 400
MAX_SEGMENT_NODES = 5_000
MAX_NODE_NUMBER = 32767

_LIST_NAME = re.compile(r"[A-Z0-9_-]{1,8}")
_PHONE = re.compile(r"\d+(-\d+)+")
_FLAG = re.compile(r"[\x21-\x2b\x2d-\x7e]+")  # printable ASCII but space and comma


class PublishError(FtnNetworkError):
    """Settings or a list line a SysOp can't save, or a list that can't be
    built, with the reason."""


# --- one line's fields ---------------------------------------------------------------


@dataclass(frozen=True)
class ListFields:
    """Fields 3-8 of a nodelist or pointlist line, as the SysOp wrote them."""

    name: str = ""
    location: str = ""
    sysop: str = ""
    phone: str = UNPUBLISHED
    speed: str = DEFAULT_SPEED
    flags: str = ""


def clean_text(value: str, label: str, *, limit: int = MAX_FIELD, required: bool = True) -> str:
    """A string field as a list carries it: runs of spaces become one
    underscore. Raises `PublishError` for a comma, anything outside 7-bit
    printable ASCII, an empty required field or one over `limit`."""
    text = "_".join(value.split())
    if not text:
        if required:
            raise PublishError(f"{label} is required.")
        return ""
    if "," in text:
        raise PublishError(f"{label} can't contain a comma: commas separate a list's fields.")
    if not all(0x21 <= ord(c) <= 0x7E for c in text):
        raise PublishError(f"{label} must be plain ASCII (no accents or symbols beyond ASCII); FTS-5000 lists "
                           "are 7-bit.")
    if len(text) > limit:
        raise PublishError(f"{label} is at most {limit} characters.")
    return text


def clean_phone(value: str) -> str:
    text = value.strip() or UNPUBLISHED
    if text.lower() == UNPUBLISHED.lower():
        return UNPUBLISHED
    if not _PHONE.fullmatch(text) or len(text) > MAX_PHONE:
        raise PublishError(f"Phone is {UNPUBLISHED} or digits in dashed groups, country code first "
                           "(1-800-555-0100).")
    return text


def clean_speed(value: str) -> str:
    text = value.strip() or DEFAULT_SPEED
    if not text.isdigit() or len(text) > 6:
        raise PublishError(f"Speed is a number; {DEFAULT_SPEED} for a system without a phone line.")
    return text


def clean_flags(value: str) -> str:
    """The flags, comma-separated as the list writes them: `IBN,INA:bbs.example`."""
    flags = [part.strip() for part in value.split(",") if part.strip()]
    for flag in flags:
        if not _FLAG.fullmatch(flag):
            raise PublishError(f"Flag {flag[:20]!r} must be plain ASCII without spaces.")
    text = ",".join(flags)
    if len(text) > MAX_FLAGS:
        raise PublishError(f"The flags are at most {MAX_FLAGS} characters in all.")
    return text


def clean_fields(fields: ListFields, *, defaults: ListFields | None = None) -> ListFields:
    """`fields` as a list writes them. An empty name, location or sysop
    takes `defaults`' when given, else is refused."""
    fallback = defaults or ListFields()
    return ListFields(
        name=clean_text(fields.name or fallback.name, "System name"),
        location=clean_text(fields.location or fallback.location, "Location"),
        sysop=clean_text(fields.sysop or fallback.sysop, "Sysop", limit=MAX_SYSOP),
        phone=clean_phone(fields.phone),
        speed=clean_speed(fields.speed),
        flags=clean_flags(fields.flags),
    )


def data_line(keyword: str, number: int | str, fields: ListFields) -> str:
    """One data line (FTS-5000 §5.3). Raises `PublishError` when it is
    longer than `MAX_LINE`."""
    parts = [keyword, str(number), fields.name, fields.location, fields.sysop, fields.phone, fields.speed]
    if fields.flags:
        parts.append(fields.flags)
    line = ",".join(parts)
    if len(line) > MAX_LINE:
        raise PublishError(f"The line for {number} is {len(line)} characters; lists keep lines to {MAX_LINE} "
                           "(shorten the name, location or flags).")
    return line


# --- settings ------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishSettings:
    network_id: int
    pointlist_on: bool = False
    pointlist_name: str = DEFAULT_POINTLIST_NAME
    pointlist_echo: str = ""
    segment_on: bool = False
    segment_name: str = ""  # empty: NET<net>
    segment_echo: str = ""
    host: ListFields = field(default_factory=ListFields)
    folder: str = ""  # empty: <files>/ftn-publish/<domain>
    archive: bool = False
    schedule: str = SCHEDULE_OFF
    weekday: int = 4  # Friday, when FidoNet's own list comes out
    # What was published last, per list: written by `publish_now` only.
    scheduled_on: str | None = None
    pointlist_at: str | None = None
    pointlist_day: int | None = None
    pointlist_crc: int | None = None
    pointlist_file: str = ""
    segment_at: str | None = None
    segment_day: int | None = None
    segment_crc: int | None = None
    segment_file: str = ""
    failure: str = ""
    failed_at: str | None = None

    def is_on(self, kind: str) -> bool:
        return self.pointlist_on if kind == POINTLIST else self.segment_on

    def echo(self, kind: str) -> str:
        return self.pointlist_echo if kind == POINTLIST else self.segment_echo

    def last(self, kind: str) -> tuple[str | None, int | None, int | None, str]:
        """`(when, day, crc, file)` of the list's last edition."""
        if kind == POINTLIST:
            return self.pointlist_at, self.pointlist_day, self.pointlist_crc, self.pointlist_file
        return self.segment_at, self.segment_day, self.segment_crc, self.segment_file


def default_segment_name(network: FtnNetwork) -> str:
    return f"NET{network.our_address.net}"[:8]


def list_name(network: FtnNetwork, settings: PublishSettings, kind: str) -> str:
    if kind == POINTLIST:
        return settings.pointlist_name or DEFAULT_POINTLIST_NAME
    return settings.segment_name or default_segment_name(network)


def get_settings(db: Database, network_id: int) -> PublishSettings:
    """The network's publishing settings; the defaults (nothing on) when it
    has none."""
    row = db.connection.execute("SELECT * FROM ftn_publish_settings WHERE network_id = ?", (network_id,)).fetchone()
    if row is None:
        return PublishSettings(network_id=network_id)
    return PublishSettings(
        network_id=network_id, pointlist_on=bool(row["pointlist_on"]), pointlist_name=row["pointlist_name"],
        pointlist_echo=row["pointlist_echo"], segment_on=bool(row["segment_on"]), segment_name=row["segment_name"],
        segment_echo=row["segment_echo"],
        host=ListFields(row["host_name"], row["host_location"], row["host_sysop"], row["host_phone"],
                        row["host_speed"], row["host_flags"]),
        folder=row["folder"], archive=bool(row["archive"]), schedule=row["schedule"], weekday=row["weekday"],
        scheduled_on=row["scheduled_on"],
        pointlist_at=row["pointlist_at"], pointlist_day=row["pointlist_day"], pointlist_crc=row["pointlist_crc"],
        pointlist_file=row["pointlist_file"],
        segment_at=row["segment_at"], segment_day=row["segment_day"], segment_crc=row["segment_crc"],
        segment_file=row["segment_file"], failure=row["failure"], failed_at=row["failed_at"],
    )


def _clean_list_name(value: str, label: str) -> str:
    text = value.strip().upper()
    if text and not _LIST_NAME.fullmatch(text):
        raise PublishError(f"{label} is 1-8 letters, digits, - or _ (the file is NAME.ddd).")
    return text


def validate_settings(network: FtnNetwork, settings: PublishSettings) -> PublishSettings:
    """The settings as they will be stored; raises `PublishError` with a
    SysOp-readable reason."""
    if network.our_address.point and (settings.pointlist_on or settings.segment_on):
        raise PublishError(f"This BBS is a point on {network.name}: it has no points to list and coordinates "
                           "no net.")
    pointlist_name = _clean_list_name(settings.pointlist_name, "Pointlist name") or DEFAULT_POINTLIST_NAME
    segment_name = _clean_list_name(settings.segment_name, "Segment name")
    echoes = []
    for value, label in ((settings.pointlist_echo, "Pointlist echo"), (settings.segment_echo, "Segment echo")):
        try:
            echoes.append(normalise_area_tag(value) if value.strip() else "")
        except FtnNetworkError as exc:
            raise PublishError(f"{label}: {exc}") from None
    folder = settings.folder.strip()
    if folder:
        if len(folder) > MAX_FOLDER or not folder.isprintable():
            raise PublishError("The folder is a path on this machine, at most 400 characters.")
        if not Path(os.path.expanduser(folder)).is_absolute():
            raise PublishError("The folder must be a full path (/bbs/publish), not one relative to wherever the "
                               "BBS was started.")
    if settings.schedule not in SCHEDULES:
        raise PublishError("The schedule is off, daily or weekly.")
    if not 0 <= settings.weekday <= 6:
        raise PublishError("The weekday is Monday to Sunday.")
    host = settings.host
    if settings.segment_on:
        host = clean_fields(settings.host)
        data_line("Host", network.our_address.net, host)
    else:  # kept as typed, but still checked when it is filled in
        host = replace(host, phone=clean_phone(host.phone), speed=clean_speed(host.speed),
                       flags=clean_flags(host.flags))
    return replace(settings, pointlist_name=pointlist_name, segment_name=segment_name, pointlist_echo=echoes[0],
                   segment_echo=echoes[1], folder=folder, host=host)


def save_settings(db: Database, network: FtnNetwork, settings: PublishSettings) -> PublishSettings:
    """Store the network's publishing settings (what was last published is
    left as it is); returns them as stored. Commits."""
    s = validate_settings(network, replace(settings, network_id=network.id))
    db.connection.execute(
        "INSERT INTO ftn_publish_settings (network_id, pointlist_on, pointlist_name, pointlist_echo, segment_on, "
        "segment_name, segment_echo, host_name, host_location, host_sysop, host_phone, host_speed, host_flags, "
        "folder, archive, schedule, weekday) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (network_id) DO UPDATE SET pointlist_on = excluded.pointlist_on, "
        "pointlist_name = excluded.pointlist_name, pointlist_echo = excluded.pointlist_echo, "
        "segment_on = excluded.segment_on, segment_name = excluded.segment_name, "
        "segment_echo = excluded.segment_echo, host_name = excluded.host_name, "
        "host_location = excluded.host_location, host_sysop = excluded.host_sysop, "
        "host_phone = excluded.host_phone, host_speed = excluded.host_speed, host_flags = excluded.host_flags, "
        "folder = excluded.folder, archive = excluded.archive, schedule = excluded.schedule, "
        "weekday = excluded.weekday",
        (network.id, int(s.pointlist_on), s.pointlist_name, s.pointlist_echo, int(s.segment_on), s.segment_name,
         s.segment_echo, s.host.name, s.host.location, s.host.sysop, s.host.phone, s.host.speed, s.host.flags,
         s.folder, int(s.archive), s.schedule, s.weekday),
    )
    db.connection.commit()
    return get_settings(db, network.id)


# --- the pointlist's points -----------------------------------------------------------


@dataclass(frozen=True)
class PointEntry:
    """One point and its pointlist line, as stored (empty fields fall back
    to the link's name, see `point_fields`)."""

    link: FtnLink
    listed: bool = True
    fields: ListFields = field(default_factory=ListFields)

    @property
    def number(self) -> int:
        return self.link.address.point


def point_defaults(link: FtnLink) -> ListFields:
    """What an empty field of a point's line becomes: the link's name for
    the system and sysop, `Point_n` without one, and an unknown location."""
    name = link.name or f"Point_{link.address.point}"
    return ListFields(name=name, location="Unknown", sysop=link.name or "Unknown")


def list_point_entries(db: Database, network: FtnNetwork) -> list[PointEntry]:
    """Every point of this node on the network, by number, with its line."""
    entries = []
    for link in list_links(db, network.id):
        if link.kind != POINT or not is_own_point(network, link.address):
            continue
        row = db.connection.execute("SELECT * FROM ftn_publish_points WHERE link_id = ?", (link.id,)).fetchone()
        if row is None:
            entries.append(PointEntry(link))
        else:
            entries.append(PointEntry(link, bool(row["listed"]), ListFields(
                row["name"], row["location"], row["sysop"], row["phone"], row["speed"], row["flags"])))
    return entries


def point_line(entry: PointEntry) -> str:
    """The point's line in the Boss format: `,point,name,...`. Raises
    `PublishError` for a field a list can't carry."""
    return data_line("", entry.number, clean_fields(entry.fields, defaults=point_defaults(entry.link)))


def save_point_entry(db: Database, entry: PointEntry) -> PointEntry:
    """Store a point's pointlist line once it makes a valid line. Commits."""
    point_line(entry)  # refuses what no list can carry
    f = entry.fields
    values = ("_".join(f.name.split()), "_".join(f.location.split()), "_".join(f.sysop.split()),
              clean_phone(f.phone), clean_speed(f.speed), clean_flags(f.flags))
    db.connection.execute(
        "INSERT INTO ftn_publish_points (link_id, listed, name, location, sysop, phone, speed, flags) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (link_id) DO UPDATE SET listed = excluded.listed, "
        "name = excluded.name, location = excluded.location, sysop = excluded.sysop, phone = excluded.phone, "
        "speed = excluded.speed, flags = excluded.flags",
        (entry.link.id, int(entry.listed), *values),
    )
    db.connection.commit()
    return replace(entry, fields=ListFields(*values))


# --- the segment's nodes --------------------------------------------------------------


@dataclass(frozen=True)
class SegmentNode:
    network_id: int
    node: int
    fields: ListFields
    keyword: str = ""
    id: int | None = None


def list_segment_nodes(db: Database, network_id: int) -> list[SegmentNode]:
    rows = db.connection.execute(
        "SELECT * FROM ftn_segment_nodes WHERE network_id = ? ORDER BY node", (network_id,)
    ).fetchall()
    return [SegmentNode(row["network_id"], row["node"], ListFields(
        row["name"], row["location"], row["sysop"], row["phone"], row["speed"], row["flags"]),
        row["keyword"], row["id"]) for row in rows]


def segment_line(node: SegmentNode) -> str:
    if node.keyword not in NODE_KEYWORDS:
        raise PublishError("A node is listed plainly, or as Pvt, Hold or Down.")
    return data_line(node.keyword, node.node, clean_fields(node.fields))


def save_segment_node(db: Database, network: FtnNetwork, node: SegmentNode) -> SegmentNode:
    """Create (no `id`) or update one node line of the segment. Commits."""
    if not 1 <= node.node <= MAX_NODE_NUMBER:
        raise PublishError(f"A node number is 1 to {MAX_NODE_NUMBER}; 0 is the Host line's.")
    fields = clean_fields(node.fields)
    segment_line(replace(node, fields=fields))
    values = (node.keyword, node.node, fields.name, fields.location, fields.sysop, fields.phone, fields.speed,
              fields.flags)
    try:
        if node.id is None:
            count = db.connection.execute(
                "SELECT COUNT(*) FROM ftn_segment_nodes WHERE network_id = ?", (network.id,)).fetchone()[0]
            if count >= MAX_SEGMENT_NODES:
                raise PublishError(f"A segment lists at most {MAX_SEGMENT_NODES} nodes here.")
            cursor = db.connection.execute(
                "INSERT INTO ftn_segment_nodes (keyword, node, name, location, sysop, phone, speed, flags, "
                "network_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*values, network.id))
            node_id = cursor.lastrowid
        else:
            cursor = db.connection.execute(
                "UPDATE ftn_segment_nodes SET keyword = ?, node = ?, name = ?, location = ?, sysop = ?, phone = ?, "
                "speed = ?, flags = ? WHERE id = ? AND network_id = ?", (*values, node.id, network.id))
            if cursor.rowcount == 0:
                raise PublishError("That node line no longer exists.")
            node_id = node.id
    except sqlite3.IntegrityError:
        db.connection.rollback()
        raise PublishError(f"Node {node.node} is already listed.") from None
    db.connection.commit()
    return replace(node, network_id=network.id, fields=fields, id=node_id)


def delete_segment_node(db: Database, node_id: int) -> None:
    db.connection.execute("DELETE FROM ftn_segment_nodes WHERE id = ?", (node_id,))
    db.connection.commit()


def suggested_node(db: Database, network: FtnNetwork) -> SegmentNode:
    """A new node line's starting point: the first downlink in this node's
    net not listed yet, its name and BinkP host filled in; else the lowest
    free node number."""
    ours = network.our_address
    taken = {n.node for n in list_segment_nodes(db, network.id)}
    for link in list_links(db, network.id):
        address = link.address
        if link.kind == POINT or (address.zone, address.net) != (ours.zone, ours.net) or address.node in taken:
            continue
        if not 1 <= address.node <= MAX_NODE_NUMBER:
            continue
        flags = ""
        if link.host and "," not in link.host:
            host = f"[{link.host}]" if ":" in link.host else link.host
            flags = ("IBN" if link.port == DEFAULT_PORT else f"IBN:{link.port}") + f",INA:{host}"
        return SegmentNode(network.id, address.node, ListFields(name="_".join(link.name.split()), flags=flags))
    number = 1
    while number in taken:
        number += 1
    return SegmentNode(network.id, number, ListFields(flags="IBN"))


# --- building a list --------------------------------------------------------------------


def _ascii(text: str) -> str:
    return "".join(c if 0x20 <= ord(c) <= 0x7E else "?" for c in text)


def header_line(title: str, day: datetime.date, crc: int) -> str:
    """FTS-5000's special first line: `;A <title> -- Friday, October 23,
    2026 -- Day number 296 : 12345`."""
    date = f"{WEEKDAYS[day.weekday()]}, {day:%B} {day.day}, {day.year}"
    return f";A {_ascii(title)} -- {date} -- Day number {day.timetuple().tm_yday:03d} : {crc:05d}"


def _assemble(title: str, day: datetime.date, body: list[str]) -> bytes:
    lines = [line.encode("ascii") for line in body]
    crc = list_crc([b"", *lines])
    head = header_line(title, day, crc).encode("ascii")
    return b"".join(line + b"\r\n" for line in [head, *lines]) + b"\x1a"


def build_pointlist(db: Database, network: FtnNetwork, day: datetime.date) -> bytes:
    """The pointlist (FTS-5002, Boss format) for `day`. Raises
    `PublishError` with no point to list or a line no list can carry."""
    entries = [e for e in list_point_entries(db, network) if e.listed and e.link.status != DISABLED]
    if not entries:
        raise PublishError("No point is listed: add points on the Links screen, or list them under P[o]ints.")
    ours = network.our_address
    body = [";A", f";A Points of {ours.four_d}, made by NetBBS {__version__}", ";A",
            f"Boss,{ours.zone}:{ours.net}/{ours.node}"]
    for entry in entries:
        try:
            body.append(point_line(entry))
        except PublishError as exc:
            raise PublishError(f"Point {entry.number}: {exc}") from None
    return _assemble(f"{network.name} pointlist for {ours.four_d}", day, body)


def build_segment(db: Database, network: FtnNetwork, settings: PublishSettings, day: datetime.date) -> bytes:
    """The nodelist segment for this node's net (FTS-5000 §7) for `day`.
    Raises `PublishError` without a Host line, or for a line no list can
    carry."""
    net = network.our_address.net
    try:
        host = data_line("Host", net, clean_fields(settings.host))
    except PublishError as exc:
        raise PublishError(f"The Host line: {exc}") from None
    body = [";A", f";A Net {net} of {network.name}, made by NetBBS {__version__}", ";A", host]
    for node in list_segment_nodes(db, network.id):
        try:
            body.append(segment_line(node))
        except PublishError as exc:
            raise PublishError(f"Node {node.node}: {exc}") from None
    return _assemble(f"{network.name} nodelist segment for net {net}", day, body)


def edition_names(base: str, day: datetime.date) -> tuple[str, str]:
    """`(<NAME>.ddd, <NAME>.Zdd)`: the list's name and its ZIP's (FTS-5000 §4)."""
    number = day.timetuple().tm_yday
    return f"{base}.{number:03d}", f"{base}.Z{number % 100:02d}"


def publish_folder(db: Database, network: FtnNetwork, settings: PublishSettings) -> Path:
    if settings.folder:
        return Path(os.path.expanduser(settings.folder))
    return storage_root(db) / "ftn-publish" / network.domain


def _zip(name: str, data: bytes, moment: datetime.datetime) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo(name, date_time=(moment.year, moment.month, moment.day, moment.hour, moment.minute,
                                                moment.second - moment.second % 2))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, data)
    return buffer.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# --- publishing ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Published:
    """One list's outcome: what was written, and whether it was hatched."""

    kind: str
    file_name: str = ""  # the edition's file: the ZIP when one is written
    day: int | None = None
    crc: int | None = None
    written: tuple[str, ...] = ()
    hatched_into: str = ""
    problem: str = ""  # why it was not written, or not hatched

    @property
    def summary(self) -> str:
        label = LIST_LABELS[self.kind].capitalize()
        if not self.written:
            return f"{label} not published: {self.problem}"
        text = f"{label} {self.file_name} (day {self.day:03d}, CRC-16 {self.crc:05d})"
        if self.hatched_into:
            text += f", hatched into {self.hatched_into}"
        if self.problem:
            text += f"; not hatched: {self.problem}"
        return text


def local_today(db: Database) -> datetime.datetime:
    return datetime.datetime.now(get_node_timezone(db))


def _publish_one(db: Database, network: FtnNetwork, settings: PublishSettings, kind: str,
                 moment: datetime.datetime) -> Published:
    day = moment.date()
    try:
        data = (build_pointlist(db, network, day) if kind == POINTLIST
                else build_segment(db, network, settings, day))
    except PublishError as exc:
        return Published(kind, problem=str(exc))
    crc = parse_header(list_lines(data)[0]).crc
    number = day.timetuple().tm_yday
    base = list_name(network, settings, kind)
    plain, zipped = edition_names(base, day)
    folder = publish_folder(db, network, settings)
    archive = _zip(plain, data, moment) if settings.archive else None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _write_atomic(folder / plain, data)
        written = [plain]
        if archive is not None:
            _write_atomic(folder / zipped, archive)
            written.append(zipped)
    except OSError as exc:
        return Published(kind, problem=f"could not write to {folder}: {exc.strerror or exc}")
    send_name = zipped if archive is not None else plain
    send_data = archive if archive is not None else data
    hatched, problem = "", ""
    echo = settings.echo(kind)
    previous = settings.last(kind)[3]
    if echo:
        what = f"{network.name} {LIST_LABELS[kind]}, day {number:03d}"
        try:
            hatch_generated_file(db, network, echo, send_data, send_name=send_name, desc=what,
                                 reference_id=f"{network.id}:{kind}:{utc_now_iso()}:{secrets.token_hex(2)}",
                                 replaces=(previous,) if previous else ())
            hatched = echo
        except (HatchError, FtnQueueFullError) as exc:
            problem = str(exc)
    prefix = "pointlist" if kind == POINTLIST else "segment"
    db.connection.execute(
        f"UPDATE ftn_publish_settings SET {prefix}_at = ?, {prefix}_day = ?, {prefix}_crc = ?, {prefix}_file = ? "
        "WHERE network_id = ?", (utc_now_iso(), number, crc, send_name, network.id))
    db.connection.commit()
    return Published(kind, send_name, number, crc, tuple(written), hatched, problem)


def publish_now(db: Database, network_id: int, *, actor: User | None,
                moment: datetime.datetime | None = None) -> list[Published]:
    """Publish every list that is on for the network, today's edition: write
    it, hatch it when a file echo is set, record it and audit-log it. One
    list's failure doesn't stop the other; each outcome says what happened.
    Raises `PublishError` when no list is on."""
    network = get_network(db, network_id)
    if network is None:
        raise PublishError("That network no longer exists.")
    settings = get_settings(db, network_id)
    kinds = [kind for kind in LIST_KINDS if settings.is_on(kind)]
    if not kinds:
        raise PublishError("Neither the pointlist nor the nodelist segment is on: switch one on under [S]ettings.")
    moment = moment or local_today(db)
    results = [_publish_one(db, network, settings, kind, moment) for kind in kinds]
    problems = "; ".join(f"{LIST_LABELS[r.kind]}: {r.problem}" for r in results if r.problem)
    db.connection.execute("UPDATE ftn_publish_settings SET failure = ?, failed_at = ? WHERE network_id = ?",
                          (problems[:400], utc_now_iso() if problems else None, network_id))
    db.connection.commit()
    for result in results:
        detail = f"{network.name}: {result.summary}"
        if result.written:
            record_action(db, actor=actor, action="publish_ftn_list", detail=detail[:500])
        level = logging.WARNING if result.problem else logging.INFO
        _logger.log(level, "FTN %s", detail)
    return results


def is_due(settings: PublishSettings, today: datetime.date) -> bool:
    """Whether a scheduled edition is due today and not published yet."""
    if settings.schedule == SCHEDULE_OFF or not (settings.pointlist_on or settings.segment_on):
        return False
    if settings.schedule == SCHEDULE_WEEKLY and today.weekday() != settings.weekday:
        return False
    return settings.scheduled_on != today.isoformat()


def publish_scheduled(db: Database, network_id: int, *, moment: datetime.datetime | None = None) -> list[Published]:
    """Publish the network's lists when the schedule says so (see the module
    docstring); the outcomes, empty when nothing was due. Never raises: a
    failure is a node-log line and the status screen's last failure. The
    day is marked first, so a failing edition is tried once that day."""
    try:
        settings = get_settings(db, network_id)
        moment = moment or local_today(db)
        if not is_due(settings, moment.date()):
            return []
        db.connection.execute("UPDATE ftn_publish_settings SET scheduled_on = ? WHERE network_id = ?",
                              (moment.date().isoformat(), network_id))
        db.connection.commit()
        return publish_now(db, network_id, actor=None, moment=moment)
    except Exception:  # noqa: BLE001 -- the mailer's pass must go on
        try:
            db.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        _logger.exception("FTN: scheduled publishing for network %s failed", network_id)
        return []
