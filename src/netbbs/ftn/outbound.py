"""
What a BinkP session hands to one node: the outbound files queue, and the
interface the mailer and the listener take a session's outgoing files
from (design doc §6.8, "Outbound files"; issues #1470 and #1471).

**The files queue** (`ftn_outbound_files`) is general. A netmail's file
attaches use it now; stage C's file requests and hatched TIC files are
meant to use it as they are. A row is one file waiting for a session with
one node:

- **What.** A file area entry (`file_id`, read from its stored content
  when the session starts) or a file on disk (`path`). It goes out under
  `send_name`, a name BinkP's safe-name rule leaves alone.
- **Where.** `route` and `destination` work as a queued message's do:
  `uplink` rows go in any session with the network's uplink; `direct` rows
  in a session with the node `destination` names (a point's boss node).
- **Flavour.** `normal`, `crash` or `hold`, as for messages (below).
- **After it is sent.** `after_sent` says what happens to a file on disk
  once the remote confirmed it: `keep`, `kill` (delete) or `truncate`
  (cut to nothing, BSO's `#`). Only a file in this node's own spool
  folder (`spool_dir`) can be killed or truncated, and a file area entry
  is always kept. The row itself is deleted once the file is sent.
- **Bounds.** At most `MAX_PENDING_FILES_PER_NETWORK` rows per network;
  more is refused with `FtnQueueFullError`. A session carries at most
  `MAX_SESSION_FILE_BYTES` of them and leaves the rest for the next one. A
  file that is gone, or over BinkP's per-file limit, when its session
  starts is dropped with a node-log line and reported to the netmail's
  sender (`report_undelivered`).

**Flavours** (issue #1471), for messages and files alike:

- `normal`: direct when the nodelist lists where the node answers, else
  via the uplink, and via the uplink after `mailer.DIRECT_ATTEMPTS` failed
  direct calls.
- `crash`: direct to the destination, never via the uplink; the mailer
  calls at once, and after `CRASH_ATTEMPTS` failed calls the netmail is
  dropped and its sender told why.
- `hold`: never makes this node call. It goes in the next session with
  its node that a password proved -- the uplink's, or a point's or
  downlink's (below). A node that is neither gets its held items in an
  unproven session (calling in, or called for other mail) only when its
  network sets `give_held_unproven`, off by default: anyone can present an
  address. It is dropped, and its sender told, after `HOLD_DAYS`.

**Points and downlinks** (issues #1478, #1479; `netbbs.ftn.links`). A row
for a linked address names its link (`link_id`), set by the queue itself
when the row is queued, and has route `direct`. Such a row goes only in a
session that link's password proved (`outbound_for_link`, every flavour),
never in `waiting_for_node`'s sessions and never via the uplink;
`links_with_callable` tells the mailer which polled downlinks to call. At
most `MAX_PENDING_FILES_PER_LINK` files (and `queue.MAX_PENDING_PER_LINK`
messages) wait for one link; what waits longer than `HOLD_DAYS` is dropped
and reported, as held mail is (`expire_held`), and so is everything
waiting for a link that is deleted (`drop_link_rows`).

**The session interface.** `outbound_for_node` gives one node's waiting
messages (as one packet) and files as a `SessionOutbound`. A network that
bundles its mail (`bundle_outgoing`, issue #1477) gets the packet inside a
ZIP bundle named for the day of the week (FTS-5005, `0000fff8.mo0`) with a
sequence character moved on per bundle; `packet_name` is then the bundle's
name, so the messages are marked sent only when the remote confirms the
bundle. The caller
passes `outbound.files` to `binkp.run_session`, then calls
`await outbound.settle(lane, result.sent)` with what the remote confirmed
-- which marks messages sent, deletes sent file rows and applies
`after_sent` -- and `outbound.close()` on every exit path. A file is read
in chunks as BinkP sends it (`_LazyFile`), so neither a 64 MiB attach
nor every node's waiting files are held in memory before a session.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from netbbs.files.storage import storage_root
from netbbs.ftn.address import FtnAddress, parse_address
from netbbs.ftn.binkp import MAX_FILE_BYTES, OutgoingFile, safe_file_name
from netbbs.ftn.bundle import build_bundle, bundle_name, packet_name
from netbbs.ftn.networks import BUNDLE_ZIP, FtnNetwork, next_bundle_sequence
from netbbs.ftn.packet import PacketHeader, build_packet_from_packed
from netbbs.ftn.queue import FtnQueueFullError, OutboundMessage, mark_outbound_sent
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso, utc_now_iso

_logger = logging.getLogger(__name__)

NORMAL, CRASH, HOLD = "normal", "crash", "hold"
FLAVOURS = (NORMAL, CRASH, HOLD)
KEEP, KILL, TRUNCATE = "keep", "kill", "truncate"

MAX_PENDING_FILES_PER_NETWORK = 200
MAX_PENDING_FILES_PER_LINK = 50  # a point or downlink (issues #1478, #1479)
# BinkP's own bound on what one session receives, kept for what one sends.
MAX_SESSION_FILE_BYTES = 256 * 1024 * 1024
MAX_MESSAGES_PER_PACKET = 500
CRASH_ATTEMPTS = 5
HOLD_DAYS = 30
# Rows of these reference kinds travel together: a hatch's file and TIC.
_PAIRED_KINDS = frozenset({"hatch"})


@dataclass(frozen=True)
class OutboundFile:
    id: int
    network_id: int
    destination: str
    route: str
    flavour: str
    file_id: str | None
    path: str | None
    send_name: str
    size_bytes: int
    after_sent: str
    reference_kind: str
    reference_id: str
    attempts: int = 0


def spool_dir(db: Database) -> Path:
    """This node's own folder for files made only to be sent (a generated
    file list, say): the only place `kill` and `truncate` touch."""
    return storage_root(db) / ".ftn-outbound"


def _in_spool(db: Database, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(spool_dir(db).resolve())
    except OSError:
        return False


def queue_file_without_commit(
    db: Database, network_id: int, *, destination: FtnAddress | str, route: str, send_name: str,
    size_bytes: int, reference_kind: str, reference_id: str, flavour: str = NORMAL,
    file_id: str | None = None, path: Path | str | None = None, after_sent: str = KEEP, link_id: int | None = None,
) -> int:
    """Queue one file for a session; returns its row id. Exactly one of
    `file_id` and `path`. Raises `ValueError` for a row that breaks the
    rules above, and `FtnQueueFullError` at `MAX_PENDING_FILES_PER_NETWORK`."""
    if (file_id is None) == (path is None):
        raise ValueError("a queued file is a file area entry or a path, not both or neither")
    if route not in ("uplink", "direct") or flavour not in FLAVOURS or after_sent not in (KEEP, KILL, TRUNCATE):
        raise ValueError(f"route {route!r}, flavour {flavour!r} or after_sent {after_sent!r} is unknown")
    if after_sent != KEEP and (path is None or not _in_spool(db, Path(path))):
        raise ValueError("only a file in the FTN spool folder can be killed or truncated once sent")
    if safe_file_name(send_name) != send_name:
        raise ValueError(f"{send_name!r} is not a name BinkP sends as it is")
    if not 0 <= size_bytes <= MAX_FILE_BYTES:
        raise ValueError(f"{send_name} is {size_bytes} bytes; BinkP sends at most {MAX_FILE_BYTES}")
    pending = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_outbound_files WHERE network_id = ?", (network_id,)
    ).fetchone()[0]
    if pending >= MAX_PENDING_FILES_PER_NETWORK:
        raise FtnQueueFullError(
            f"{pending} files are already waiting for this network; nothing more is queued until they go"
        )
    # --- points and downlinks (issues #1478, #1479): a linked address's file waits for that link ---
    if link_id is None:
        from netbbs.ftn.links import resolve_link_id

        link_id = resolve_link_id(db, network_id, destination)
    if link_id is not None:
        route = "direct"
        waiting = db.connection.execute(
            "SELECT COUNT(*) FROM ftn_outbound_files WHERE link_id = ?", (link_id,)
        ).fetchone()[0]
        if waiting >= MAX_PENDING_FILES_PER_LINK:
            raise FtnQueueFullError(f"{waiting} files are already waiting for {destination}; nothing more is "
                                    "queued until it calls")
        cursor = db.connection.execute(
            "INSERT INTO ftn_outbound_files (network_id, destination, route, flavour, file_id, path, send_name, "
            "size_bytes, after_sent, reference_kind, reference_id, created_at, link_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (network_id, str(destination), route, flavour, file_id, str(path) if path is not None else None,
             send_name, size_bytes, after_sent, reference_kind, reference_id, utc_now_iso(), link_id),
        )
        return cursor.lastrowid
    # --- end points and downlinks ---
    cursor = db.connection.execute(
        "INSERT INTO ftn_outbound_files (network_id, destination, route, flavour, file_id, path, send_name, "
        "size_bytes, after_sent, reference_kind, reference_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (network_id, str(destination), route, flavour, file_id, str(path) if path is not None else None, send_name,
         size_bytes, after_sent, reference_kind, reference_id, utc_now_iso()),
    )
    return cursor.lastrowid


def _file_from_row(row) -> OutboundFile:
    return OutboundFile(row["id"], row["network_id"], row["destination"], row["route"], row["flavour"],
                        row["file_id"], row["path"], row["send_name"], row["size_bytes"], row["after_sent"],
                        row["reference_kind"], row["reference_id"], row["attempts"])


def pending_files(db: Database, network_id: int) -> list[OutboundFile]:
    rows = db.connection.execute(
        "SELECT * FROM ftn_outbound_files WHERE network_id = ? ORDER BY id", (network_id,)
    ).fetchall()
    return [_file_from_row(row) for row in rows]


def files_for_reference(db: Database, reference_kind: str, reference_id: str) -> list[OutboundFile]:
    rows = db.connection.execute(
        "SELECT * FROM ftn_outbound_files WHERE reference_kind = ? AND reference_id = ? ORDER BY id",
        (reference_kind, reference_id),
    ).fetchall()
    return [_file_from_row(row) for row in rows]


def _boss(text: str) -> tuple[int, int, int] | None:
    try:
        address = parse_address(text)
    except Exception:  # noqa: BLE001 -- a row nothing can parse is for nobody
        return None
    return address.zone, address.net, address.node


def _for_node(destination: str, route: str, node: FtnAddress, *, uplink: bool) -> bool:
    if uplink:
        return route == "uplink"
    return route == "direct" and _boss(destination) == (node.zone, node.net, node.node)


def waiting_for_node(
    db: Database, network: FtnNetwork, node: FtnAddress, *, uplink: bool, hold_only: bool = False,
) -> tuple[list[OutboundMessage], list[OutboundFile]]:
    """The messages (at most `MAX_MESSAGES_PER_PACKET`) and files waiting for
    a session with `node`, oldest first: every uplink row when `uplink`,
    else the direct rows whose destination is `node` or one of its points.
    `hold_only` keeps held rows only -- what a node calling in unproven is
    handed.

    Held rows go only in a session a password proved -- the uplink's,
    today -- unless the network sets `give_held_unproven`: a direct
    session, called or answered, proves nothing about who is on the other
    end, and held mail has no other way to go, so whoever claims the
    address first would take it."""
    if not uplink and not network.give_held_unproven:
        if hold_only:
            return [], []
        flavour_clause = " AND flavour != 'hold'"
    else:
        flavour_clause = " AND flavour = 'hold'" if hold_only else ""
    route = "uplink" if uplink else "direct"
    # Ids first, without the encoded messages: up to 10,000 may wait, and
    # only one packet's worth is read.
    # A link's rows go only in a session its password proved (`waiting_for_link`).
    candidates = db.connection.execute(
        "SELECT id, destination, route FROM ftn_outbound "
        f"WHERE network_id = ? AND status = 'pending' AND route = ? AND link_id IS NULL{flavour_clause} ORDER BY id",
        (network.id, route),
    ).fetchall()
    chosen = [row["id"] for row in candidates
              if _for_node(row["destination"], row["route"], node, uplink=uplink)][:MAX_MESSAGES_PER_PACKET]
    messages = []
    for message_id in chosen:
        row = db.connection.execute(
            "SELECT id, network_id, kind, reference_id, destination, route, packed FROM ftn_outbound WHERE id = ?",
            (message_id,),
        ).fetchone()
        messages.append(OutboundMessage(row["id"], row["network_id"], row["kind"], row["reference_id"],
                                        row["destination"], row["route"], bytes(row["packed"])))
    file_rows = db.connection.execute(
        f"SELECT * FROM ftn_outbound_files WHERE network_id = ? AND route = ? AND link_id IS NULL{flavour_clause} "
        "ORDER BY id",
        (network.id, route),
    ).fetchall()
    files = [_file_from_row(row) for row in file_rows
             if _for_node(row["destination"], row["route"], node, uplink=uplink)]
    return messages, files


def direct_nodes_waiting(db: Database, network: FtnNetwork, *, hold_only: bool = False,
                         include_hold: bool = True) -> list[FtnAddress]:
    """Every node direct mail or files wait for (points by their boss), in
    the order their oldest row was queued. `hold_only` counts held rows
    only; `include_hold=False` leaves them out (what makes this node call)."""
    clause = " AND flavour = 'hold'" if hold_only else ("" if include_hold else " AND flavour != 'hold'")
    nodes: dict[tuple[int, int, int], None] = {}
    for table, status in (("ftn_outbound", " AND status = 'pending'"), ("ftn_outbound_files", "")):
        for row in db.connection.execute(
            f"SELECT destination FROM {table} WHERE network_id = ? AND route = 'direct' AND link_id IS NULL"
            f"{status}{clause} ORDER BY id",
            (network.id,),
        ):
            boss = _boss(row["destination"])
            if boss is not None:
                nodes.setdefault(boss, None)
    return [FtnAddress(*boss) for boss in nodes]


def count_callable(db: Database, network_id: int, *, route: str) -> tuple[int, int]:
    """`(waiting, crash)`: messages and files on `route` that make this node
    call (all but held ones), and how many of them are crash."""
    waiting = crash = 0
    for table, status in (("ftn_outbound", " AND status = 'pending'"), ("ftn_outbound_files", "")):
        row = db.connection.execute(
            f"SELECT COUNT(*) AS waiting, COALESCE(SUM(flavour = 'crash'), 0) AS crash FROM {table} "
            f"WHERE network_id = ? AND route = ? AND flavour != 'hold' AND link_id IS NULL{status}",
            (network_id, route),
        ).fetchone()
        waiting += row["waiting"]
        crash += row["crash"]
    return waiting, crash


def count_waiting(db: Database, network_id: int) -> tuple[int, int, int]:
    """`(messages, files, held)` waiting for the network, for the status
    screen: held counts the messages and files of both kept for a session
    the other side starts."""
    messages = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_outbound WHERE network_id = ? AND status = 'pending'", (network_id,)
    ).fetchone()[0]
    files = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_outbound_files WHERE network_id = ?", (network_id,)
    ).fetchone()[0]
    held = db.connection.execute(
        "SELECT (SELECT COUNT(*) FROM ftn_outbound WHERE network_id = ? AND status = 'pending' AND flavour = 'hold')"
        " + (SELECT COUNT(*) FROM ftn_outbound_files WHERE network_id = ? AND flavour = 'hold')",
        (network_id, network_id),
    ).fetchone()[0]
    return messages, files, held


def reroute_files_to_uplink(db: Database, ids: list[int]) -> None:
    """A normal direct file whose calls failed goes via the uplink, as its
    netmail does."""
    db.connection.executemany(
        "UPDATE ftn_outbound_files SET route = 'uplink' WHERE id = ? AND flavour = 'normal'", [(i,) for i in ids]
    )
    db.connection.commit()


# --- reading what goes out ---------------------------------------------------


def source_path(db: Database, item: OutboundFile) -> Path | None:
    """Where a queued file's bytes are now, or None when it is gone: a file
    area entry deleted or never approved, or a path that is no file."""
    if item.file_id is not None:
        row = db.connection.execute(
            "SELECT storage_path, status FROM files WHERE file_id = ?", (item.file_id,)
        ).fetchone()
        if row is None or row["status"] != "approved":
            return None
        path = Path(row["storage_path"])
    else:
        path = Path(item.path)
    return path if path.is_file() and not path.is_symlink() else None


class _LazyFile:
    """A file's bytes as BinkP's sender reads them -- its length, and slices
    -- opened at the first read and read a chunk at a time, so nothing is
    held in memory before or during the session. The length is fixed when
    the file is queued for the session; a file that shrank since reads
    short and the remote refuses it, which leaves its row for next time."""

    def __init__(self, path: Path, size: int):
        self._path = path
        self._size = size
        self._handle = None

    def __len__(self) -> int:
        return self._size

    def __getitem__(self, key: slice) -> bytes:
        if not isinstance(key, slice):
            raise TypeError("a queued file is read in slices")
        start, stop, _step = key.indices(self._size)
        if stop <= start:
            return b""
        if self._handle is None:
            self._handle = open(self._path, "rb")  # noqa: SIM115 -- closed by `close`
        self._handle.seek(start)
        return self._handle.read(stop - start)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


@dataclass
class SessionOutbound:
    """What one session hands to one node; see the module docstring."""

    network_id: int
    files: list[OutgoingFile] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)
    packet_name: str | None = None
    file_rows: dict[str, OutboundFile] = field(default_factory=dict)
    _lazy: list[_LazyFile] = field(default_factory=list, repr=False)

    @property
    def empty(self) -> bool:
        return not self.files

    async def settle(self, lane, sent: list[str]) -> tuple[int, int]:
        """Record what the remote confirmed (`SessionResult.sent`): the
        packet's messages are sent, each confirmed file's row goes and its
        `after_sent` is carried out. Returns `(messages, files)` sent."""
        confirmed = set(sent)
        messages = len(self.message_ids) if self.packet_name is not None and self.packet_name in confirmed else 0
        if messages:
            await lane.run(mark_outbound_sent, self.message_ids)
        done = [row for name, row in self.file_rows.items() if name in confirmed]
        if done:
            await lane.run(_delete_file_rows, [row.id for row in done])
            for row in done:
                if row.path is not None and row.after_sent != KEEP:
                    await asyncio.to_thread(_after_sent, Path(row.path), row.after_sent)
        return messages, len(done)

    def close(self) -> None:
        for lazy in self._lazy:
            lazy.close()


def _delete_file_rows(db: Database, ids: list[int]) -> None:
    db.connection.executemany("DELETE FROM ftn_outbound_files WHERE id = ?", [(i,) for i in ids])
    db.connection.commit()


def _after_sent(path: Path, action: str) -> None:
    try:
        if action == KILL:
            path.unlink(missing_ok=True)
        elif action == TRUNCATE:
            with open(path, "r+b") as handle:
                handle.truncate(0)
    except OSError as exc:
        _logger.warning("FTN: could not %s %s after sending it: %s", action, path, exc)


@dataclass(frozen=True)
class _Prepared:
    messages: list[OutboundMessage]
    files: list[tuple[OutboundFile, Path, int, int]]  # row, source, size, mtime
    dropped: list[tuple[OutboundFile, str]]


def waiting_for_link(db: Database, link_id: int) -> tuple[list[OutboundMessage], list[OutboundFile]]:
    """The messages (at most `MAX_MESSAGES_PER_PACKET`) and files waiting for
    a point or downlink, oldest first, of every flavour: a session with a
    link is one its password proved (issues #1478, #1479)."""
    rows = db.connection.execute(
        "SELECT id, network_id, kind, reference_id, destination, route, packed FROM ftn_outbound "
        "WHERE link_id = ? AND status = 'pending' ORDER BY id LIMIT ?",
        (link_id, MAX_MESSAGES_PER_PACKET),
    ).fetchall()
    messages = [OutboundMessage(row["id"], row["network_id"], row["kind"], row["reference_id"], row["destination"],
                                row["route"], bytes(row["packed"])) for row in rows]
    files = [_file_from_row(row) for row in db.connection.execute(
        "SELECT * FROM ftn_outbound_files WHERE link_id = ? ORDER BY id", (link_id,))]
    return messages, files


def links_with_callable(db: Database, network_id: int) -> list[int]:
    """The ids of the network's links that messages or files wait for which
    may make this node call -- all but held ones -- oldest first."""
    ids: dict[int, None] = {}
    for table, status in (("ftn_outbound", " AND status = 'pending'"), ("ftn_outbound_files", "")):
        for row in db.connection.execute(
            f"SELECT link_id FROM {table} WHERE network_id = ? AND link_id IS NOT NULL AND flavour != 'hold'"
            f"{status} ORDER BY id",
            (network_id,),
        ):
            ids.setdefault(row["link_id"], None)
    return list(ids)


def _prepare(db: Database, network: FtnNetwork, node: FtnAddress, uplink: bool, hold_only: bool,
             link_id: int | None = None) -> _Prepared:
    if link_id is not None:
        messages, rows = waiting_for_link(db, link_id)
    else:
        messages, rows = waiting_for_node(db, network, node, uplink=uplink, hold_only=hold_only)
    files: list[tuple[OutboundFile, Path, int, int]] = []
    dropped: list[tuple[OutboundFile, str]] = []
    budget = MAX_SESSION_FILE_BYTES
    names: set[str] = set()
    for row in rows:
        path = source_path(db, row)
        if path is None:
            dropped.append((row, f"{row.send_name} is no longer on this BBS"))
            continue
        stat = path.stat()
        if stat.st_size > MAX_FILE_BYTES:
            dropped.append((row, f"{row.send_name} is now {stat.st_size} bytes, over BinkP's limit"))
            continue
        if row.send_name.casefold() in names or stat.st_size > budget:
            continue  # the next session takes it: one name once per session, and the byte bound
        names.add(row.send_name.casefold())
        budget -= stat.st_size
        files.append((row, path, stat.st_size, int(stat.st_mtime)))
    # A hatched file and its TIC go together (issue #1476): when one can't
    # be sent, the other is dropped with it rather than arrive alone.
    broken = {row.reference_id for row, _reason in dropped if row.reference_kind in _PAIRED_KINDS}
    if broken:
        for row in [row for row in rows if row.reference_kind in _PAIRED_KINDS and row.reference_id in broken]:
            if all(row.id != gone.id for gone, _reason in dropped):
                dropped.append((row, f"{row.send_name}'s partner can't be sent"))
        files = [entry for entry in files if not (entry[0].reference_kind in _PAIRED_KINDS
                                                  and entry[0].reference_id in broken)]
    return _Prepared(messages, files, dropped)


def _drop_unsendable(db: Database, dropped: list[tuple[OutboundFile, str]]) -> None:
    for row, reason in dropped:
        db.connection.execute("DELETE FROM ftn_outbound_files WHERE id = ?", (row.id,))
        _logger.warning("FTN: queued file %s for %s dropped: %s", row.send_name, row.destination, reason)
    db.connection.commit()
    for row, _reason in dropped:
        # A spooled file made only to be sent (a hatch's TIC) goes with its row.
        if row.path is not None and row.after_sent == KILL and _in_spool(db, Path(row.path)):
            _after_sent(Path(row.path), KILL)
    for row, reason in dropped:
        report_undelivered(db, row.reference_kind, row.reference_id, f"its file {row.send_name} could not be sent: "
                                                                     f"{reason}")


async def outbound_for_node(lane, network: FtnNetwork, node: FtnAddress, *, uplink: bool,
                            hold_only: bool = False) -> SessionOutbound:
    """The session interface: everything waiting for a session with `node`
    -- the uplink when `uplink` -- as one packet and its files. See the
    module docstring for how the caller settles and closes it."""
    prepared = await lane.run(_prepare, network, node, uplink, hold_only)
    bundle = network.bundle_outgoing == BUNDLE_ZIP and network.id is not None
    return await _session_outbound(lane, network, node, prepared, password=network.packet_password if uplink else "",
                                   next_sequence=(lambda db: next_bundle_sequence(db, network.id)) if bundle else None)


async def outbound_for_link(lane, network: FtnNetwork, link) -> SessionOutbound:
    """What a session a point's or downlink's password proved hands it
    (issues #1478, #1479): everything queued for that link, of every
    flavour, in a packet to its address under its packet password, bundled
    as the link prefers. Settled and closed as `outbound_for_node`'s is."""
    from netbbs.ftn.links import next_link_bundle_sequence

    prepared = await lane.run(_prepare, network, link.address, False, False, link.id)
    bundle = link.bundle == BUNDLE_ZIP
    return await _session_outbound(lane, network, link.address, prepared, password=link.packet_password,
                                   next_sequence=(lambda db: next_link_bundle_sequence(db, link.id)) if bundle
                                   else None)


async def _session_outbound(lane, network: FtnNetwork, node: FtnAddress, prepared: _Prepared, *, password: str,
                            next_sequence) -> SessionOutbound:
    if prepared.dropped:
        await lane.run(_drop_unsendable, prepared.dropped)
    outbound = SessionOutbound(network.id)
    if prepared.messages:
        header = PacketHeader(orig=network.our_address, dest=node, created=datetime.datetime.now(),
                              password=password)
        name = packet_name(secrets.randbits(32))
        packet = build_packet_from_packed(header, [m.packed for m in prepared.messages])
        # --- mail bundles (issue #1477): the packet in a ZIP, when the network (or link) asks ---
        if next_sequence is not None:
            sequence = await lane.run(next_sequence)
            packet = build_bundle([(name, packet)])
            name = bundle_name(network.our_address, node, datetime.date.today(), sequence)
        # --- end mail bundles ---
        outbound.packet_name = name
        outbound.files.append(OutgoingFile(name, packet))
        outbound.message_ids = [m.id for m in prepared.messages]
    for row, path, size, mtime in prepared.files:
        lazy = _LazyFile(path, size)
        outbound._lazy.append(lazy)
        outbound.files.append(OutgoingFile(row.send_name, lazy, mtime))  # type: ignore[arg-type]
        outbound.file_rows[row.send_name] = row
    return outbound


# --- crash and hold bookkeeping (issue #1471) ---------------------------------


def note_failed_crash(db: Database, network_id: int, message_ids: list[int], file_ids: list[int]) -> int:
    """Count one failed call against the crash rows it would have carried;
    those reaching `CRASH_ATTEMPTS` are dropped and their senders told.
    Returns how many were dropped."""
    for table, ids in (("ftn_outbound", message_ids), ("ftn_outbound_files", file_ids)):
        db.connection.executemany(
            f"UPDATE {table} SET attempts = attempts + 1 WHERE id = ? AND flavour = 'crash'", [(i,) for i in ids]
        )
    db.connection.commit()
    return _drop_rows(db, network_id, "flavour = 'crash' AND attempts >= ?", (CRASH_ATTEMPTS,),
                      f"the crash call failed {CRASH_ATTEMPTS} times")


def fail_crash_without_route(db: Database, network_id: int, message_ids: list[int], file_ids: list[int]) -> int:
    """Crash rows whose destination the nodelist no longer lists: they never
    go via the uplink, so they are dropped and reported."""
    dropped = 0
    for table, ids in (("ftn_outbound", message_ids), ("ftn_outbound_files", file_ids)):
        for row_id in ids:
            db.connection.execute(f"UPDATE {table} SET attempts = ? WHERE id = ? AND flavour = 'crash'",
                                  (CRASH_ATTEMPTS, row_id))
    db.connection.commit()
    dropped += _drop_rows(db, network_id, "flavour = 'crash' AND attempts >= ?", (CRASH_ATTEMPTS,),
                          "the nodelist no longer lists where its destination answers BinkP")
    return dropped


def expire_held(db: Database, *, now: datetime.datetime | None = None) -> int:
    """Drop held rows older than `HOLD_DAYS` (their node never called) and
    tell their senders; returns how many."""
    cutoff = utc_iso((now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=HOLD_DAYS))
    dropped = _drop_rows(db, None, "flavour = 'hold' AND created_at < ?", (cutoff,),
                         f"its destination did not call this BBS within {HOLD_DAYS} days")
    # A point's or downlink's mail waits for its session whatever its
    # flavour, and goes the same way when none came (issues #1478, #1479).
    return dropped + _drop_rows(db, None, "link_id IS NOT NULL AND created_at < ?", (cutoff,),
                                f"its destination did not collect it within {HOLD_DAYS} days")


def drop_link_rows(db: Database, link_id: int, reason: str) -> int:
    """Drop everything waiting for a link (it was removed), telling each
    netmail's sender; returns how many messages and files."""
    return _drop_rows(db, None, "link_id = ?", (link_id,), reason)


def _drop_rows(db: Database, network_id: int | None, condition: str, params: tuple, reason: str) -> int:
    scope = "" if network_id is None else " AND network_id = ?"
    scope_params = () if network_id is None else (network_id,)
    messages = db.connection.execute(
        f"SELECT id, kind, reference_id, destination FROM ftn_outbound WHERE status = 'pending' AND {condition}{scope}",
        (*params, *scope_params),
    ).fetchall()
    files = db.connection.execute(
        f"SELECT * FROM ftn_outbound_files WHERE {condition}{scope}", (*params, *scope_params)
    ).fetchall()
    for row in messages:
        db.connection.execute("DELETE FROM ftn_outbound WHERE id = ?", (row["id"],))
        # Its files cannot follow a netmail that never went.
        db.connection.execute("DELETE FROM ftn_outbound_files WHERE reference_kind = ? AND reference_id = ?",
                              (row["kind"], row["reference_id"]))
    for row in files:
        db.connection.execute("DELETE FROM ftn_outbound_files WHERE id = ?", (row["id"],))
    db.connection.commit()
    reported: set[tuple[str, str]] = set()
    for row in messages:
        _logger.warning("FTN: %s %s to %s dropped: %s", row["kind"], row["reference_id"], row["destination"], reason)
        reported.add((row["kind"], row["reference_id"]))
        report_undelivered(db, row["kind"], row["reference_id"], reason)
    for row in files:
        _logger.warning("FTN: queued file %s to %s dropped: %s", row["send_name"], row["destination"], reason)
        if (row["reference_kind"], row["reference_id"]) not in reported:
            reported.add((row["reference_kind"], row["reference_id"]))
            report_undelivered(db, row["reference_kind"], row["reference_id"],
                               f"its file {row['send_name']} was not sent: {reason}")
    return len(messages) + len(files)


def report_undelivered(db: Database, reference_kind: str, reference_id: str, reason: str) -> None:
    """Tell a netmail's sender it was not delivered, as mail from the BBS
    (issue #1471: crash mail is reported to its sender on failure). Only a
    caller's netmail has a sender to tell: a robot request or a bounce is
    logged and nothing more."""
    if reference_kind != "netmail" or not reference_id.isdigit():
        return
    from netbbs.auth.users import get_user_by_id
    from netbbs.mail import MailboxFullError, MailError, send_system_mail

    row = db.connection.execute(
        "SELECT sender_user_id, recipient_label, subject FROM mail_messages WHERE id = ?", (int(reference_id),)
    ).fetchone()
    if row is None or row["sender_user_id"] is None:
        return
    sender = get_user_by_id(db, row["sender_user_id"])
    if sender is None:
        return
    try:
        send_system_mail(
            db, sender, f"Netmail not delivered: {row['subject']}"[:100],
            f"Your netmail to {row['recipient_label']}, \"{row['subject']}\", was not delivered: {reason}.\n\n"
            "Your copy is still in Sent.",
        )
    except (MailboxFullError, MailError) as exc:  # a full mailbox: the node log has it
        _logger.warning("FTN: could not tell %s that netmail %s failed: %s", sender.username, reference_id, exc)
