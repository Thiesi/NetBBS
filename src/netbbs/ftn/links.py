"""
Points and downlinks: the systems a network's SysOp adds under this node
(design doc §6.8, "Points and downlinks"; issues #1478 and #1479, §16
"Issue #1154", Decisions 15 and 17).

A **link** is one record of `ftn_links`:

- a **point**: this node's own address with a point number, `86:549/40.3`.
  Points call in and are never called: their mail is held for them.
- a **downlink**: a node this node feeds, `86:549/41`. It either calls in
  too (`hold`), or this node calls it when mail waits (`poll`, with the
  host it answers on). A downlink's own points are reached through it.

Every link has a session password, and only a session that proves it is
that link's: it is handed the link's mail and files, and what it sends is
tossed as the link's. Nothing is ever held for a link and given to a
caller that only presents its address -- `give_held_unproven` is for
nodelist nodes, never for links. A link the SysOp switched off
(`disabled`) still has to prove its password, so nobody else can take its
address, but it is given nothing and what it sends is held. `paused` is
the link's own `%PAUSE` (`netbbs.ftn.robot_server`): its echomail and file
echoes stop, its netmail does not.

**Routing.** `link_for_destination` is the one answer to "does this
address belong to a link?": a link's own address, or a point under a
downlink. `netbbs.ftn.queue.enqueue_outbound_without_commit` and
`netbbs.ftn.outbound.queue_file_without_commit` ask it for every message
and file they queue, so netmail, bounces and anything later stages queue
for a linked address go to the link's own outbound, whoever queued it.

**Areas and fan-out** (issue #1479). `ftn_link_areas` holds the echoes
and file echoes each link carries, read-write or read-only (`link_areas`,
`set_link_area_without_commit`, `links_carrying`), and
`enqueue_for_link_without_commit` queues one encoded message for one link
-- with `fanout_reference` for a copy of one post sent to several links,
since a reference id is unique per network and kind. `netbbs.ftn.fanout`
sends echomail on, `netbbs.ftn.ticfwd` file echoes, and
`netbbs.ftn.robot_server` lets a link change its own areas.

**Bounds.** At most `MAX_LINKS_PER_NETWORK` links per network; at most
`queue.MAX_PENDING_PER_LINK` messages and `outbound.MAX_PENDING_FILES_PER_LINK`
files wait for one link, and more is refused; what waits for a link longer
than `outbound.HOLD_DAYS` is dropped and its sender told. A link's
echomail waiting for a moderator (`ftn_point_waiting`) keeps at most
`MAX_POINT_WAITING` per network, oldest dropped, for `POINT_WAITING_DAYS`.
"""

from __future__ import annotations

import datetime
import logging
import re
import sqlite3
from dataclasses import dataclass, replace

from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress, parse_address
from netbbs.ftn.binkp import DEFAULT_PORT
from netbbs.ftn.networks import (
    BUNDLE_MODES,
    BUNDLE_OFF,
    MAX_PACKET_PASSWORD,
    MAX_SESSION_PASSWORD,
    FtnNetwork,
    FtnNetworkError,
    get_network,
    normalise_area_tag,
)
from netbbs.ftn.transport import parse_host_port
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso, utc_now_iso

_logger = logging.getLogger(__name__)

POINT, DOWNLINK = "point", "downlink"
KINDS = (POINT, DOWNLINK)
HOLD_LINK, POLL_LINK = "hold", "poll"
LINK_FLAVOURS = (HOLD_LINK, POLL_LINK)
ACTIVE, PAUSED, DISABLED = "active", "paused", "disabled"
STATUSES = (ACTIVE, PAUSED, DISABLED)
ECHO, FILE_ECHO = "echo", "file"
AREA_MODES = ("rw", "read")

MAX_LINKS_PER_NETWORK = 500
MAX_LINK_NAME = 40
MAX_OUTCOME = 200
MAX_POINT_WAITING = 1_000
POINT_WAITING_DAYS = 30

_PRINTABLE = re.compile(r"[\x21-\x7e]*")


class FtnLinkError(FtnNetworkError):
    """A link a SysOp can't save, with the reason."""


@dataclass(frozen=True)
class FtnLink:
    network_id: int
    kind: str
    address: FtnAddress
    session_password: str
    name: str = ""
    packet_password: str = ""
    areafix_password: str = ""
    filefix_password: str = ""  # empty: FileFix requests carry the AreaFix password
    tic_password: str = ""
    flavour: str = HOLD_LINK
    host: str = ""
    port: int = DEFAULT_PORT
    bundle: str = BUNDLE_OFF
    status: str = ACTIVE
    last_call_at: str | None = None
    last_call_outcome: str = ""
    id: int | None = None

    @property
    def is_point(self) -> bool:
        return self.kind == POINT

    @property
    def enabled(self) -> bool:
        """Whether the link is served at all: anything but `disabled`."""
        return self.status != DISABLED

    @property
    def polled(self) -> bool:
        """Whether this node calls the link when mail waits."""
        return self.kind == DOWNLINK and self.flavour == POLL_LINK and bool(self.host)

    @property
    def effective_filefix_password(self) -> str:
        return self.filefix_password or self.areafix_password

    @property
    def label(self) -> str:
        """`86:549/40.3 (Name)`, or the address alone."""
        return f"{self.address.four_d} ({self.name})" if self.name else self.address.four_d


def is_own_point(network: FtnNetwork, address: FtnAddress) -> bool:
    """Whether `address` is a point of this node on the network."""
    ours = network.our_address
    return bool(address.point) and (address.zone, address.net, address.node) == (ours.zone, ours.net, ours.node)


def next_point_number(db: Database, network: FtnNetwork) -> int:
    """The lowest point number no link of the network has yet."""
    taken = {link.address.point for link in list_links(db, network.id) if is_own_point(network, link.address)}
    number = 1
    while number in taken:
        number += 1
    return number


def validate_link(network: FtnNetwork, link: FtnLink) -> FtnLink:
    """The link as it will be stored; raises `FtnLinkError` with a
    SysOp-readable reason."""
    if network.our_address.point:
        raise FtnLinkError(f"This BBS is itself a point on {network.name}, so it can't have points or downlinks.")
    if link.kind not in KINDS:
        raise FtnLinkError("A link is a point or a downlink.")
    address = link.address.with_domain(network.domain)
    ours = network.our_address
    if link.kind == POINT:
        if not is_own_point(network, address):
            raise FtnLinkError(f"A point's address is this BBS's with a point number: {ours.four_d}.1, "
                               f"{ours.four_d}.2, ...")
    else:
        if address.point:
            raise FtnLinkError("A downlink is a node, without a point number; its own points are reached "
                               "through it.")
        if address.same_node(ours):
            raise FtnLinkError("A downlink's address must differ from this BBS's.")
    if FtnAddress(address.zone, address.net, address.node).same_node(network.uplink_address):
        raise FtnLinkError("The uplink can't be a link: it is the network record's own.")
    name = " ".join(link.name.split())
    if len(name) > MAX_LINK_NAME or not name.isprintable():
        raise FtnLinkError(f"The name is at most {MAX_LINK_NAME} printable characters.")
    if not link.session_password:
        raise FtnLinkError("A link needs a session password: only a session that proves it gets the link's mail.")
    for value, limit, label in (
        (link.session_password, MAX_SESSION_PASSWORD, "Session password"),
        (link.packet_password, MAX_PACKET_PASSWORD, "Packet password"),
        (link.areafix_password, MAX_SESSION_PASSWORD, "AreaFix password"),
        (link.filefix_password, MAX_SESSION_PASSWORD, "FileFix password"),
        (link.tic_password, MAX_SESSION_PASSWORD, "TIC password"),
    ):
        if len(value) > limit or not _PRINTABLE.fullmatch(value):
            raise FtnLinkError(f"{label} must be at most {limit} printable characters without spaces.")
    if link.flavour not in LINK_FLAVOURS:
        raise FtnLinkError("Calls are hold (it calls in) or poll (this BBS calls it).")
    host, port = link.host.strip(), link.port
    if host:
        try:
            host, written = parse_host_port(host)
        except ValueError as exc:
            raise FtnLinkError(f"Host: {exc}. Write an IPv6 address with its port as [2001:db8::1]:24554.") from None
        port = written or port
    if not 1 <= port <= 65535:
        raise FtnLinkError("The port must be between 1 and 65535.")
    if link.flavour == POLL_LINK:
        if link.kind == POINT:
            raise FtnLinkError("A point calls in; it is never polled. Its calls are hold.")
        if not host:
            raise FtnLinkError("A polled downlink needs the host it answers BinkP on.")
    if link.bundle not in BUNDLE_MODES:
        raise FtnLinkError("Bundles are off or ZIP.")
    if link.status not in STATUSES:
        raise FtnLinkError("A link is active, paused or disabled.")
    return replace(link, network_id=network.id, address=address, name=name, host=host, port=port)


def save_link(db: Database, link: FtnLink, *, commit: bool = True) -> FtnLink:
    """Create (no `id`) or update a link; returns it as stored."""
    network = get_network(db, link.network_id)
    if network is None:
        raise FtnLinkError("That network no longer exists.")
    validated = validate_link(network, link)
    values = (
        validated.network_id, validated.kind, validated.address.four_d, validated.name, validated.session_password,
        validated.packet_password, validated.areafix_password, validated.filefix_password, validated.tic_password,
        validated.flavour, validated.host, validated.port, validated.bundle, validated.status,
    )
    try:
        if validated.id is None:
            count = db.connection.execute(
                "SELECT COUNT(*) FROM ftn_links WHERE network_id = ?", (validated.network_id,)
            ).fetchone()[0]
            if count >= MAX_LINKS_PER_NETWORK:
                raise FtnLinkError(f"{network.name} has {count} links, the most one network may have.")
            cursor = db.connection.execute(
                "INSERT INTO ftn_links (network_id, kind, address, name, session_password, packet_password, "
                "areafix_password, filefix_password, tic_password, flavour, host, port, bundle, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, utc_now_iso()),
            )
            validated = replace(validated, id=cursor.lastrowid)
        else:
            cursor = db.connection.execute(
                "UPDATE ftn_links SET network_id = ?, kind = ?, address = ?, name = ?, session_password = ?, "
                "packet_password = ?, areafix_password = ?, filefix_password = ?, tic_password = ?, flavour = ?, "
                "host = ?, port = ?, bundle = ?, status = ? WHERE id = ?",
                (*values, validated.id),
            )
            if cursor.rowcount == 0:
                raise FtnLinkError("That link no longer exists.")
            # What waits for it goes to its new address.
            db.connection.execute("UPDATE ftn_outbound SET destination = ? WHERE link_id = ? AND status = 'pending' "
                                  "AND destination != ?",
                                  (validated.address.four_d, validated.id, validated.address.four_d))
    except sqlite3.IntegrityError:
        db.connection.rollback()
        raise FtnLinkError(f"{validated.address.four_d} is already a link on {network.name}.") from None
    except FtnLinkError:
        db.connection.rollback()
        raise
    if commit:
        db.connection.commit()
    return validated


def delete_link(db: Database, link_id: int, *, commit: bool = True) -> int:
    """Remove a link. What waits for it is dropped, each netmail's sender
    told; returns how many messages and files were dropped."""
    from netbbs.ftn.outbound import drop_link_rows

    link = get_link(db, link_id)
    if link is None:
        return 0
    dropped = drop_link_rows(db, link_id, f"{link.address.four_d} is no longer served by this BBS")
    db.connection.execute("DELETE FROM ftn_links WHERE id = ?", (link_id,))
    if commit:
        db.connection.commit()
    return dropped


def _link_from_row(row: sqlite3.Row) -> FtnLink:
    try:
        address = parse_address(row["address"])
    except FtnFormatError as exc:  # only a hand-edited row can get here
        raise FtnLinkError(f"link {row['id']} has an unreadable address: {exc}") from exc
    return FtnLink(
        id=row["id"], network_id=row["network_id"], kind=row["kind"], address=address, name=row["name"],
        session_password=row["session_password"], packet_password=row["packet_password"],
        areafix_password=row["areafix_password"], filefix_password=row["filefix_password"],
        tic_password=row["tic_password"], flavour=row["flavour"], host=row["host"], port=row["port"],
        bundle=row["bundle"], status=row["status"], last_call_at=row["last_call_at"],
        last_call_outcome=row["last_call_outcome"],
    )


def list_links(db: Database, network_id: int) -> list[FtnLink]:
    """The network's links: points first, by number, then downlinks."""
    rows = db.connection.execute("SELECT * FROM ftn_links WHERE network_id = ?", (network_id,)).fetchall()
    links = [_link_from_row(row) for row in rows]
    return sorted(links, key=lambda link: (link.kind != POINT, link.address.zone, link.address.net,
                                          link.address.node, link.address.point))


def get_link(db: Database, link_id: int) -> FtnLink | None:
    row = db.connection.execute("SELECT * FROM ftn_links WHERE id = ?", (link_id,)).fetchone()
    return _link_from_row(row) if row is not None else None


def link_for_destination(db: Database, network_id: int, address: FtnAddress | str) -> FtnLink | None:
    """The link mail to `address` goes to, or None: the link with that
    address, else the downlink whose point it is. A disabled link takes
    nothing, so mail for it is routed as if it were not there."""
    if isinstance(address, str):
        try:
            address = parse_address(address)
        except FtnFormatError:
            return None
    boss = FtnAddress(address.zone, address.net, address.node)
    found = None
    for link in list_links(db, network_id):
        if not link.enabled:
            continue
        if link.address.same_node(address):
            return link
        if address.point and link.kind == DOWNLINK and link.address.same_node(boss):
            found = link
    return found


def resolve_link_id(db: Database, network_id: int, destination: FtnAddress | str) -> int | None:
    """The id of the link `destination` belongs to (`link_for_destination`),
    for the queue; None when it belongs to none, or the table isn't there
    yet (a schema older than this one)."""
    try:
        link = link_for_destination(db, network_id, destination)
    except sqlite3.OperationalError:
        return None
    return link.id if link is not None else None


def link_for_session(links: list[FtnLink], addresses: list[FtnAddress]) -> FtnLink | None:
    """The link a caller presents itself as: the first of `links` whose
    address it names. Disabled links count: a caller claiming one must
    still prove its password."""
    for link in links:
        if any(address.same_node(link.address) for address in addresses):
            return link
    return None


def record_link_call(db: Database, link_id: int, outcome: str) -> None:
    """Remember a link's last session and what it moved, for the console."""
    db.connection.execute("UPDATE ftn_links SET last_call_at = ?, last_call_outcome = ? WHERE id = ?",
                          (utc_now_iso(), outcome[:MAX_OUTCOME], link_id))
    db.connection.commit()


def next_link_bundle_sequence(db: Database, link_id: int) -> int:
    """A link's next bundle sequence character (0-35), as
    `netbbs.ftn.networks.next_bundle_sequence` keeps the uplink's."""
    row = db.connection.execute("SELECT bundle_sequence FROM ftn_links WHERE id = ?", (link_id,)).fetchone()
    sequence = (row["bundle_sequence"] if row is not None else 0) % 36
    db.connection.execute("UPDATE ftn_links SET bundle_sequence = ? WHERE id = ?", ((sequence + 1) % 36, link_id))
    db.connection.commit()
    return sequence


def count_link_waiting(db: Database, link_id: int) -> tuple[int, int]:
    """`(messages, files)` waiting for the link."""
    messages = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_outbound WHERE link_id = ? AND status = 'pending'", (link_id,)
    ).fetchone()[0]
    files = db.connection.execute("SELECT COUNT(*) FROM ftn_outbound_files WHERE link_id = ?", (link_id,)).fetchone()[0]
    return messages, files


# --- areas and fan-out (#1479) ---------------------------------------------------


def fanout_reference(reference_id: str, link: FtnLink) -> str:
    """The reference id of one link's copy of a message several links get:
    a reference is unique per network and kind in the queue."""
    return f"{reference_id}>{link.id}"


def enqueue_for_link_without_commit(db: Database, link: FtnLink, *, kind: str, reference_id: str, packed: bytes,
                                    flavour: str = "normal") -> bool:
    """Queue one encoded message for one link; False when that reference
    is queued already. Raises `netbbs.ftn.queue.FtnQueueFullError` when the
    link or the network is at its bound."""
    from netbbs.ftn.queue import enqueue_outbound_without_commit

    return enqueue_outbound_without_commit(
        db, link.network_id, kind=kind, reference_id=reference_id, destination=link.address.four_d, packed=packed,
        route="direct", flavour=flavour, link_id=link.id,
    )


@dataclass(frozen=True)
class LinkArea:
    link_id: int
    kind: str  # ECHO or FILE_ECHO
    tag: str
    mode: str  # "rw", or "read": the link gets the area but what it sends to it is refused


def link_areas(db: Database, link_id: int, kind: str = ECHO) -> list[LinkArea]:
    rows = db.connection.execute(
        "SELECT link_id, kind, tag, mode FROM ftn_link_areas WHERE link_id = ? AND kind = ? ORDER BY tag",
        (link_id, kind),
    ).fetchall()
    return [LinkArea(row["link_id"], row["kind"], row["tag"], row["mode"]) for row in rows]


def set_link_area_without_commit(db: Database, link_id: int, kind: str, tag: str, mode: str = "rw") -> LinkArea:
    """Make the link carry an echo (`ECHO`) or file echo (`FILE_ECHO`)."""
    if kind not in (ECHO, FILE_ECHO) or mode not in AREA_MODES:
        raise FtnLinkError(f"area kind {kind!r} or mode {mode!r} is unknown")
    tag = normalise_area_tag(tag)
    db.connection.execute(
        "INSERT INTO ftn_link_areas (link_id, kind, tag, mode, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (link_id, kind, tag) DO UPDATE SET mode = excluded.mode",
        (link_id, kind, tag, mode, utc_now_iso()),
    )
    return LinkArea(link_id, kind, tag, mode)


def remove_link_area_without_commit(db: Database, link_id: int, kind: str, tag: str) -> bool:
    cursor = db.connection.execute(
        "DELETE FROM ftn_link_areas WHERE link_id = ? AND kind = ? AND tag = ? COLLATE NOCASE", (link_id, kind, tag)
    )
    return cursor.rowcount > 0


def links_carrying(db: Database, network_id: int, kind: str, tag: str) -> list[FtnLink]:
    """The active links of the network that carry the area: who a fan-out
    sends to. Paused and disabled links get nothing."""
    rows = db.connection.execute(
        "SELECT l.* FROM ftn_links l JOIN ftn_link_areas a ON a.link_id = l.id "
        "WHERE l.network_id = ? AND l.status = 'active' AND a.kind = ? AND a.tag = ? COLLATE NOCASE ORDER BY l.id",
        (network_id, kind, tag),
    ).fetchall()
    return [_link_from_row(row) for row in rows]


# --- a link's echomail waiting for a moderator ----------------------------------


def hold_link_echomail_without_commit(db: Database, network_id: int, link_id: int | None, post_id: str,
                                      packed: bytes) -> None:
    """Keep a point's or downlink's echomail, as it was received, until its
    post is approved (`release_link_echomail_without_commit`): only then is
    it sent on (issues #1478, #1479). At most `MAX_POINT_WAITING` per
    network: the oldest is dropped, with a node-log line, and its post
    stays local."""
    db.connection.execute(
        "INSERT OR REPLACE INTO ftn_point_waiting (post_id, network_id, link_id, packed, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (post_id, network_id, link_id, packed, utc_now_iso()),
    )
    over = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_point_waiting WHERE network_id = ?", (network_id,)
    ).fetchone()[0] - MAX_POINT_WAITING
    if over > 0:
        rows = db.connection.execute(
            "SELECT post_id FROM ftn_point_waiting WHERE network_id = ? ORDER BY created_at, rowid LIMIT ?",
            (network_id, over),
        ).fetchall()
        for row in rows:
            db.connection.execute("DELETE FROM ftn_point_waiting WHERE post_id = ?", (row["post_id"],))
            _logger.warning("FTN: a link's post %s waited for approval among more than %d; it stays local",
                            row["post_id"], MAX_POINT_WAITING)


def release_link_echomail_without_commit(db: Database, post_id: str) -> bool:
    """A link's post was approved: send its echomail on, to the uplink and
    every other link carrying the echo (`netbbs.ftn.fanout`). False when
    none waits for it (not a link's post, or dropped) or nothing was
    queued. Raises nothing for a full queue: the copies that fit go."""
    from netbbs.ftn.fanout import fan_out_without_commit
    from netbbs.ftn.packet import unpack_message

    try:
        row = db.connection.execute(
            "SELECT network_id, link_id, packed FROM ftn_point_waiting WHERE post_id = ?", (post_id,)
        ).fetchone()
    except sqlite3.OperationalError:  # a schema older than points
        return False
    if row is None:
        return False
    network = get_network(db, row["network_id"])
    db.connection.execute("DELETE FROM ftn_point_waiting WHERE post_id = ?", (post_id,))
    if network is None or not network.enabled:
        return False
    try:
        packed = unpack_message(bytes(row["packed"]))
    except FtnFormatError as exc:
        _logger.warning("FTN %s: a link's post %s can't be sent on: %s", network.name, post_id, exc)
        return False
    tag = _area_of(packed.text)
    if tag is None:
        return False
    sender = get_link(db, row["link_id"]) if row["link_id"] is not None else None
    return fan_out_without_commit(db, network, packed, tag, post_id, sender=sender).copies > 0


def _area_of(text: bytes) -> str | None:
    """The echo tag of a packed echomail's text, from its AREA line."""
    first = text.split(b"\r", 1)[0].removeprefix(b"\x01")
    if not first.startswith(b"AREA:"):
        return None
    tag = first[5:].decode("ascii", errors="replace").strip()
    return tag or None


def prune_point_waiting(db: Database, *, now: datetime.datetime | None = None) -> int:
    """Forget a point's echomail whose post was never approved within
    `POINT_WAITING_DAYS` (rejected or forgotten); returns how many."""
    cutoff = utc_iso((now or datetime.datetime.now(datetime.timezone.utc))
                     - datetime.timedelta(days=POINT_WAITING_DAYS))
    cursor = db.connection.execute("DELETE FROM ftn_point_waiting WHERE created_at < ?", (cutoff,))
    db.connection.commit()
    return cursor.rowcount
