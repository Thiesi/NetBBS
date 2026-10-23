"""
FTN nodelists, as far as direct netmail needs them (FTS-5000, FTS-5004;
design doc §6.8, Decision 5).

A nodelist is one line per system, comma-separated:
`keyword,number,name,location,sysop,phone,speed,flags...`. The keyword
sets where the number belongs:

- `Zone` starts a zone (its number is the zone, and the net too);
- `Region` and `Host` start a net (their number is the net);
- `Hub`, `Pvt` and an empty keyword are nodes in the current net;
- `Hold` and `Down` are nodes that take no calls now, kept out.

Lines starting `;` are comments; a trailing Ctrl-Z ends the file.

The BinkP flags say where a node answers: `IBN` (FTS-5004), optionally
`IBN:host`, `IBN:port` or `IBN:host:port`, with `INA:host` giving the host
for a bare `IBN` or a port-only one. A node with `IBN` but no host anywhere
is not reachable directly.

`import_list` replaces a network's whole stored list in one transaction,
after checking the first line's CRC-16, and keeps the list's raw text, one
per network, so that a NODEDIFF can be applied to it (`apply_diff`, issue
#1464). `direct_route` says where a netmail to an address can be delivered
without the uplink. A point is reached through its boss node.

**CRC-16 (FTS-5000 §5.2).** The first line ends `Day number nnn : ccccc`.
The check value is the CRC-16 (polynomial 0x1021, starting at 0: XMODEM's,
which `binascii.crc_hqx` computes) of everything from the second line to
the end, each line ending CR LF, the end-of-file byte left out. It is
computed as if the file had exactly that form, whatever line ends it
arrived with.

**NODEDIFF (FTS-5000 §6).** The first line is the first line of the list it
applies to. Then come `Ann` (add the next nn lines), `Cnn` (copy nn lines of
the old list) and `Dnn` (skip nn lines of the old list). The result's own
first line carries its CRC.
"""

from __future__ import annotations

import binascii
import re
from dataclasses import dataclass

from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import DEFAULT_PORT
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

MAX_NODELIST_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 200_000
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\]")
_LINE_END = re.compile(rb"\r\n|\n|\r")
_DIFF_COMMAND = re.compile(rb"([ACDacd])(\d{1,6})")
_TRAILING_DAY = re.compile(rb"(\d{1,3})\s*$")

# How the stored list came: `nodelist_how` on the network.
HOW_FULL, HOW_DIFF, HOW_MANUAL = "full", "diff", "manual"


class NodelistError(ValueError):
    """A nodelist that can't be imported, with the reason."""


@dataclass(frozen=True)
class NodelistEntry:
    zone: int
    net: int
    node: int
    name: str
    binkp_host: str | None = None
    binkp_port: int | None = None


def parse_nodelist(text: str) -> list[NodelistEntry]:
    """Every reachable-or-not node in `text`, `Hold`/`Down` ones left out.
    Raises `NodelistError` if no zone line comes before the first node."""
    entries: list[NodelistEntry] = []
    zone = net = None
    for raw in text.split("\x1a", 1)[0].splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        fields = line.split(",")
        if len(fields) < 3 or not fields[1].strip().isdigit():
            continue
        keyword, number = fields[0].strip().lower(), int(fields[1].strip())
        if number > 0xFFFF:
            continue
        if keyword == "zone":
            zone = net = number
            node = 0
        elif keyword in ("region", "host"):
            if zone is None:
                raise NodelistError("a net is listed before any Zone line")
            net, node = number, 0
        elif keyword in ("", "hub", "pvt"):
            if zone is None:
                raise NodelistError("a node is listed before any Zone line")
            node = number
        else:  # hold, down, and anything unknown: no calls
            continue
        if len(entries) >= MAX_ENTRIES:
            raise NodelistError(f"more than {MAX_ENTRIES} entries")
        host, port = _binkp_target(fields[7:])
        entries.append(NodelistEntry(zone, net, node, fields[2].strip().replace("_", " ")[:60], host, port))
    return entries


def _binkp_target(flags: list[str]) -> tuple[str | None, int | None]:
    ina = None
    ibn: str | None = None
    for flag in flags:
        name, _, value = flag.strip().partition(":")
        name = name.upper()
        if name == "INA" and value and ina is None:
            ina = value
        elif name == "IBN" and ibn is None:
            ibn = value
    if ibn is None:
        return None, None
    host, port = None, DEFAULT_PORT
    if ibn:
        first, _, second = ibn.partition(":")
        if first.isdigit() and not second:
            port = int(first)
        else:
            host = first
            if second.isdigit():
                port = int(second)
    host = host or ina
    if host is None or not _HOST.fullmatch(host) or not 0 < port < 65536:
        return None, None
    return host.strip("[]"), port


# --- CRC-16, the first line and NODEDIFF (issue #1464) -----------------------


def list_lines(data: bytes) -> list[bytes]:
    """The lines of a nodelist or NODEDIFF without their line ends; an
    end-of-file byte ends the text."""
    lines = _LINE_END.split(data.split(b"\x1a", 1)[0])
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def raw_text(lines: list[bytes]) -> bytes:
    """`lines` in FTS-5000's own form, as kept: CR LF after each line, no
    end-of-file byte."""
    return b"".join(line + b"\r\n" for line in lines)


@dataclass(frozen=True)
class ListHeader:
    """What a list's first line says: its day number and check value, each
    None when the line doesn't carry one."""
    day: int | None
    crc: int | None


def parse_header(line: bytes) -> ListHeader:
    """FTS-5000's suggested parse: the check value follows the last colon,
    and the day number is the digits just before it."""
    before, colon, after = line.rpartition(b":")
    if not colon:
        return ListHeader(None, None)
    crc_text = after.strip()
    crc = int(crc_text) if crc_text.isdigit() and len(crc_text) <= 5 and int(crc_text) <= 0xFFFF else None
    day = _TRAILING_DAY.search(before)
    return ListHeader(int(day.group(1)) if day else None, crc)


def list_crc(lines: list[bytes]) -> int:
    """The CRC-16 of a list's lines after the first, as FTS-5000 computes it."""
    crc = 0
    for line in lines[1:]:
        crc = binascii.crc_hqx(line + b"\r\n", crc)
    return crc


def check_crc(lines: list[bytes], *, required: bool) -> ListHeader:
    """The list's first line, once its CRC-16 matches. Raises `NodelistError`
    on a mismatch, and when the first line carries no check value and
    `required` is set (a list from the nodelist echo must carry one)."""
    if not lines:
        raise NodelistError("the file is empty")
    header = parse_header(lines[0])
    if header.crc is None:
        if required:
            raise NodelistError("its first line carries no CRC-16 (Day number nnn : ccccc)")
        return header
    computed = list_crc(lines)
    if computed != header.crc:
        raise NodelistError(f"CRC-16 mismatch: the first line says {header.crc:05d}, the list is {computed:05d}")
    return header


def _diff_command(line: bytes) -> tuple[str, int] | None:
    match = _DIFF_COMMAND.fullmatch(line.strip())
    if match is None:
        return None
    return match.group(1).decode().upper(), int(match.group(2))


def apply_nodediff(base: list[bytes], diff: list[bytes]) -> list[bytes]:
    """The list `diff` makes of `base`. Raises `NodelistError` if the diff
    names another list (its first line differs from `base`'s), has a line
    that is not a command where one is due, or runs past either file. The
    result's CRC is the caller's to check."""
    if not base:
        raise NodelistError("there is no kept list to apply it to")
    if not diff or diff[0] != base[0]:
        raise NodelistError("it is for another list (its first line is not the kept list's)")
    out: list[bytes] = []
    size = source = 0
    index = 1
    while index < len(diff):
        command = _diff_command(diff[index])
        if command is None:
            raise NodelistError(f"line {index + 1} is not an A, C or D command")
        letter, count = command
        index += 1
        if letter == "A":
            if index + count > len(diff):
                raise NodelistError(f"an A{count} at line {index} runs past the end of the diff")
            added = diff[index:index + count]
            index += count
        else:
            if source + count > len(base):
                raise NodelistError(f"a {letter}{count} runs past the end of the kept list")
            added = base[source:source + count] if letter == "C" else []
            source += count
        out.extend(added)
        size += sum(len(line) + 2 for line in added)
        if size > MAX_NODELIST_BYTES:
            raise NodelistError(f"the list it makes is over {MAX_NODELIST_BYTES // (1024 * 1024)} MiB")
    return out


def diff_result_header(diff: list[bytes]) -> bytes | None:
    """The first line a diff writes -- the new list's first line -- or None
    if a copy comes before any add."""
    index = 1
    while index < len(diff):
        command = _diff_command(diff[index])
        if command is None or command[0] == "C":
            return None
        if command[0] == "A":
            return diff[index + 1] if command[1] and index + 1 < len(diff) else None
        index += 1
    return None


# --- importing, and the kept raw list ----------------------------------------


@dataclass(frozen=True)
class ImportOutcome:
    nodes: int
    day: int | None
    crc: int | None  # the verified check value; None for a by-hand list without one


def import_list(db: Database, network_id: int, data: bytes, *, how: str, require_crc: bool) -> ImportOutcome:
    """Check `data`'s CRC-16, then replace the network's stored nodelist with
    it and keep its raw text for later diffs, in one transaction. Nothing
    changes if it can't be read: `NodelistError` says why."""
    if len(data) > MAX_NODELIST_BYTES:
        raise NodelistError(f"a nodelist over {MAX_NODELIST_BYTES // (1024 * 1024)} MiB is not imported")
    lines = list_lines(data)
    header = check_crc(lines, required=require_crc)
    entries = parse_nodelist("\n".join(line.decode("cp437") for line in lines))
    if not entries:
        raise NodelistError("no nodes found: is this a nodelist?")
    now = utc_now_iso()
    try:
        db.connection.execute("DELETE FROM ftn_nodelist WHERE network_id = ?", (network_id,))
        db.connection.executemany(
            "INSERT OR REPLACE INTO ftn_nodelist (network_id, zone, net, node, name, binkp_host, binkp_port) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(network_id, e.zone, e.net, e.node, e.name, e.binkp_host, e.binkp_port) for e in entries],
        )
        db.connection.execute(
            "INSERT INTO ftn_nodelist_raw (network_id, content, kept_at) VALUES (?, ?, ?) "
            "ON CONFLICT (network_id) DO UPDATE SET content = excluded.content, kept_at = excluded.kept_at",
            (network_id, raw_text(lines), now),
        )
        db.connection.execute(
            "UPDATE ftn_networks SET nodelist_imported_at = ?, nodelist_entries = ?, nodelist_day = ?, "
            "nodelist_how = ?, nodelist_crc = ?, nodelist_waiting_full = 0 WHERE id = ?",
            (now, len(entries), header.day, how, header.crc, network_id),
        )
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    return ImportOutcome(len(entries), header.day, header.crc)


def import_nodelist(db: Database, network_id: int, text: str) -> int:
    """Import a list given as text, as by hand; returns how many nodes it
    lists."""
    return import_list(db, network_id, text.encode("cp437", errors="replace"),
                       how=HOW_MANUAL, require_crc=False).nodes


def kept_list(db: Database, network_id: int) -> list[bytes] | None:
    """The lines of the network's kept raw list, or None if none is kept."""
    row = db.connection.execute(
        "SELECT content FROM ftn_nodelist_raw WHERE network_id = ?", (network_id,)
    ).fetchone()
    return list_lines(bytes(row["content"])) if row is not None else None


def apply_diff(db: Database, network_id: int, data: bytes) -> ImportOutcome | None:
    """Apply a NODEDIFF to the kept list, check the result's CRC-16 and import
    it. None when the kept list already is the one the diff makes (the full
    list came first). Raises `NodelistError`, changing nothing, when the
    diff can't be applied."""
    if len(data) > MAX_NODELIST_BYTES:
        raise NodelistError(f"a diff over {MAX_NODELIST_BYTES // (1024 * 1024)} MiB is not applied")
    diff = list_lines(data)
    base = kept_list(db, network_id)
    if base and diff and diff[0] != base[0] and diff_result_header(diff) == base[0]:
        return None
    result = apply_nodediff(base or [], diff)
    try:
        check_crc(result, required=True)
    except NodelistError as exc:
        raise NodelistError(f"the list it makes fails its check: {exc}") from None
    return import_list(db, network_id, raw_text(result), how=HOW_DIFF, require_crc=True)


def record_failure(db: Database, network_id: int, reason: str, *, waiting_for_full: bool) -> None:
    """Keep the latest failure for the status screen. `waiting_for_full`
    says no diff can be applied until a full list comes (the fall-back)."""
    db.connection.execute(
        "UPDATE ftn_networks SET nodelist_failure = ?, nodelist_failed_at = ?, "
        "nodelist_waiting_full = MAX(nodelist_waiting_full, ?) WHERE id = ?",
        (reason, utc_now_iso(), int(waiting_for_full), network_id),
    )
    db.connection.commit()


@dataclass(frozen=True)
class NodelistStatus:
    imported_at: str | None
    entries: int
    day: int | None
    how: str | None
    crc: int | None
    failure: str | None
    failed_at: str | None
    waiting_for_full: bool


def nodelist_status(db: Database, network_id: int) -> NodelistStatus | None:
    row = db.connection.execute(
        "SELECT nodelist_imported_at, nodelist_entries, nodelist_day, nodelist_how, nodelist_crc, "
        "nodelist_failure, nodelist_failed_at, nodelist_waiting_full FROM ftn_networks WHERE id = ?",
        (network_id,),
    ).fetchone()
    if row is None:
        return None
    return NodelistStatus(row["nodelist_imported_at"], row["nodelist_entries"], row["nodelist_day"],
                          row["nodelist_how"], row["nodelist_crc"], row["nodelist_failure"],
                          row["nodelist_failed_at"], bool(row["nodelist_waiting_full"]))


def direct_route(db: Database, network_id: int, address: FtnAddress) -> tuple[FtnAddress, str, int] | None:
    """`(node to call, host, port)` for a netmail to `address`, or None when
    it must go via the uplink. A point is called through its boss node."""
    boss = FtnAddress(address.zone, address.net, address.node)
    row = db.connection.execute(
        "SELECT binkp_host, binkp_port FROM ftn_nodelist WHERE network_id = ? AND zone = ? AND net = ? AND node = ?",
        (network_id, boss.zone, boss.net, boss.node),
    ).fetchone()
    if row is None or not row["binkp_host"]:
        return None
    return boss, row["binkp_host"], row["binkp_port"] or DEFAULT_PORT
