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

`import_nodelist` replaces a network's whole stored list in one
transaction; `direct_route` says where a netmail to an address can be
delivered without the uplink. A point is reached through its boss node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import DEFAULT_PORT
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

MAX_NODELIST_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 200_000
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\]")


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


def import_nodelist(db: Database, network_id: int, text: str) -> int:
    """Replace the network's stored nodelist with `text`; returns how many
    nodes it lists. Nothing changes if it can't be read."""
    if len(text.encode("utf-8", errors="replace")) > MAX_NODELIST_BYTES:
        raise NodelistError(f"a nodelist over {MAX_NODELIST_BYTES // (1024 * 1024)} MiB is not imported")
    entries = parse_nodelist(text)
    if not entries:
        raise NodelistError("no nodes found: is this a nodelist?")
    try:
        db.connection.execute("DELETE FROM ftn_nodelist WHERE network_id = ?", (network_id,))
        db.connection.executemany(
            "INSERT OR REPLACE INTO ftn_nodelist (network_id, zone, net, node, name, binkp_host, binkp_port) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(network_id, e.zone, e.net, e.node, e.name, e.binkp_host, e.binkp_port) for e in entries],
        )
        db.connection.execute(
            "UPDATE ftn_networks SET nodelist_imported_at = ?, nodelist_entries = ? WHERE id = ?",
            (utc_now_iso(), len(entries), network_id),
        )
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    return len(entries)


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
