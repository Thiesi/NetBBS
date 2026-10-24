"""
FTN gateway state that is not configuration (design doc §6.8): MSGID
serials, the dupe history, the outbound queue, held inbound items and bad
echoes.

Every one of these grows with what other systems send or with what
callers write, so each has a bound and says what happens at it:

- the dupe history keeps `SEEN_RETENTION_DAYS` of MSGIDs, pruned by age;
- the outbound queue holds at most `MAX_PENDING_PER_NETWORK` unsent
  messages per network and refuses more (`FtnQueueFullError`), and sent
  rows are pruned after `SENT_RETENTION_DAYS`;
- held items (packets, files and TICs) are refused once `MAX_HELD_PACKETS`
  or `MAX_HELD_BYTES` is reached, and the session that brought them is
  told no; an unpaired file-echo half expires after `UNPAIRED_DAYS`;
- bad echoes (echomail for a tag no board carries, issue #1466) keep at
  most `MAX_BAD_ECHO_PER_TAG` per tag and `MAX_BAD_ECHO_MESSAGES` or
  `MAX_BAD_ECHO_BYTES` overall, dropping the oldest and counting each drop.
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass

from netbbs.config import get_config, set_config_without_commit
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso, utc_now_iso

_logger = logging.getLogger(__name__)

MSGID_SERIAL_KEY = "ftn_msgid_serial"
SEEN_RETENTION_DAYS = 180
MAX_PENDING_PER_NETWORK = 10_000
MAX_PENDING_PER_LINK = 2_000  # a point or downlink (issues #1478, #1479)
SENT_RETENTION_DAYS = 14
MAX_HELD_PACKETS = 200  # items: packets, files and TICs
# Enough for a file at BinkP's 64 MiB limit with room to spare (issue #1462).
MAX_HELD_BYTES = 256 * 1024 * 1024
UNPAIRED_DAYS = 7
MAX_BAD_ECHO_PER_TAG = 1_000
MAX_BAD_ECHO_MESSAGES = 10_000
MAX_BAD_ECHO_BYTES = 64 * 1024 * 1024


class FtnQueueFullError(Exception):
    """The network's outbound queue is at `MAX_PENDING_PER_NETWORK`."""


# --- MSGID serials ----------------------------------------------------------


def next_msgid_serial_without_commit(db: Database) -> int:
    """A MSGID serial this node has not used (FTS-0009: unique for three
    years). At least the current Unix time, and above the last one issued.

    The clock is what keeps a restore safe: a backup carries the serial it
    was taken with, and the next serial after restoring it is "now" again,
    past anything issued before the restore -- unless more than one serial
    a second was issued for long enough to run ahead of the clock. Kept as
    a whole number; the MSGID shows its low 32 bits.
    """
    stored = get_config(db, MSGID_SERIAL_KEY)
    last = int(stored) if stored and stored.isdigit() else 0
    serial = max(last + 1, int(time.time()))
    set_config_without_commit(db, MSGID_SERIAL_KEY, str(serial))
    return serial


# --- dupe history -----------------------------------------------------------


def record_seen_msgid_without_commit(db: Database, network_id: int, area_tag: str, msgid: str) -> bool:
    """Remember `msgid` for the area (`""` for netmail); False if it was
    already there -- the message is a duplicate."""
    cursor = db.connection.execute(
        "INSERT OR IGNORE INTO ftn_seen_msgids (network_id, area_tag, msgid, seen_at) VALUES (?, ?, ?, ?)",
        (network_id, area_tag, msgid, utc_now_iso()),
    )
    return cursor.rowcount == 1


def prune_seen_msgids(db: Database, *, now: datetime.datetime | None = None) -> int:
    """Forget MSGIDs older than `SEEN_RETENTION_DAYS`; returns how many."""
    cutoff = utc_iso((now or _utcnow()) - datetime.timedelta(days=SEEN_RETENTION_DAYS))
    cursor = db.connection.execute("DELETE FROM ftn_seen_msgids WHERE seen_at < ?", (cutoff,))
    db.connection.commit()
    return cursor.rowcount


# --- outbound queue ---------------------------------------------------------


@dataclass(frozen=True)
class OutboundMessage:
    id: int
    network_id: int
    kind: str  # "echomail" or "netmail"
    reference_id: str  # the post's or the letter's id
    destination: str  # the address the message is for
    route: str  # "uplink" or "direct"
    packed: bytes  # an encoded packed message (`netbbs.ftn.packet`)


def enqueue_outbound_without_commit(
    db: Database, network_id: int, *, kind: str, reference_id: str, destination: str,
    packed: bytes, route: str = "uplink", flavour: str = "normal", link_id: int | None = None,
) -> bool:
    """Queue an encoded message; False if this one is already queued (each
    post or letter goes out once per network). Raises `FtnQueueFullError`
    at `MAX_PENDING_PER_NETWORK` unsent messages. `flavour` is `normal`,
    `crash` or `hold` (issue #1471, `netbbs.ftn.outbound`).

    A message whose destination belongs to a point or downlink of this node
    (`netbbs.ftn.links.link_for_destination`) goes to that link's own
    outbound, whatever `route` says, unless `link_id` names the link
    already (issues #1478, #1479): it waits for a session that link's
    password proves. At most `MAX_PENDING_PER_LINK` wait for one link."""
    if db.connection.execute(
        "SELECT 1 FROM ftn_outbound WHERE network_id = ? AND kind = ? AND reference_id = ?",
        (network_id, kind, reference_id),
    ).fetchone() is not None:
        return False  # already queued (or sent): not a new row, so not against the cap
    pending = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_outbound WHERE network_id = ? AND status = 'pending'", (network_id,)
    ).fetchone()[0]
    if pending >= MAX_PENDING_PER_NETWORK:
        raise FtnQueueFullError(
            f"{pending} messages are already waiting for this network; nothing more is queued until they go"
        )
    # --- points and downlinks (issues #1478, #1479) ---
    if link_id is None:
        from netbbs.ftn.links import resolve_link_id  # links imports the queue's callers

        link_id = resolve_link_id(db, network_id, destination)
    if link_id is not None:
        route = "direct"
        waiting = db.connection.execute(
            "SELECT COUNT(*) FROM ftn_outbound WHERE link_id = ? AND status = 'pending'", (link_id,)
        ).fetchone()[0]
        if waiting >= MAX_PENDING_PER_LINK:
            raise FtnQueueFullError(
                f"{waiting} messages are already waiting for {destination}; nothing more is queued until it calls"
            )
        cursor = db.connection.execute(
            "INSERT OR IGNORE INTO ftn_outbound "
            "(network_id, kind, reference_id, destination, route, packed, created_at, flavour, link_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (network_id, kind, reference_id, destination, route, packed, utc_now_iso(), flavour, link_id),
        )
        return cursor.rowcount == 1
    # --- end points and downlinks ---
    cursor = db.connection.execute(
        "INSERT OR IGNORE INTO ftn_outbound "
        "(network_id, kind, reference_id, destination, route, packed, created_at, flavour) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (network_id, kind, reference_id, destination, route, packed, utc_now_iso(), flavour),
    )
    return cursor.rowcount == 1


def pending_outbound(
    db: Database, network_id: int, *, route: str = "uplink", destination: str | None = None, limit: int = 1000,
) -> list[OutboundMessage]:
    """Unsent messages for a session, oldest first."""
    query = (
        "SELECT id, network_id, kind, reference_id, destination, route, packed FROM ftn_outbound "
        "WHERE network_id = ? AND status = 'pending' AND route = ?"
    )
    params: list[object] = [network_id, route]
    if destination is not None:
        query += " AND destination = ?"
        params.append(destination)
    query += " ORDER BY id LIMIT ?"
    params.append(limit)
    return [
        OutboundMessage(row["id"], row["network_id"], row["kind"], row["reference_id"], row["destination"],
                        row["route"], bytes(row["packed"]))
        for row in db.connection.execute(query, params)
    ]


def count_pending_outbound(db: Database, network_id: int, *, route: str | None = None) -> int:
    """Unsent messages for the network, on one route or on both."""
    if route is None:
        return db.connection.execute(
            "SELECT COUNT(*) FROM ftn_outbound WHERE network_id = ? AND status = 'pending'", (network_id,)
        ).fetchone()[0]
    return db.connection.execute(
        "SELECT COUNT(*) FROM ftn_outbound WHERE network_id = ? AND status = 'pending' AND route = ?",
        (network_id, route),
    ).fetchone()[0]


def mark_outbound_sent(db: Database, ids: list[int]) -> None:
    """The remote confirmed the packet holding these (M_GOT)."""
    now = utc_now_iso()
    db.connection.executemany(
        "UPDATE ftn_outbound SET status = 'sent', sent_at = ?, packed = X'' WHERE id = ?",
        [(now, message_id) for message_id in ids],
    )
    db.connection.commit()


def reroute_outbound_to_uplink(db: Database, ids: list[int]) -> None:
    """A direct netmail whose calls failed goes via the uplink instead --
    a normal one: crash never does, and hold makes no calls (issue #1471)."""
    db.connection.executemany(
        "UPDATE ftn_outbound SET route = 'uplink' WHERE id = ? AND flavour = 'normal'", [(i,) for i in ids]
    )
    db.connection.commit()


def prune_sent_outbound(db: Database, *, now: datetime.datetime | None = None) -> int:
    """Forget sent rows after `SENT_RETENTION_DAYS`; returns how many. A
    sent row keeps no message, only that it went (so it isn't queued twice)."""
    cutoff = utc_iso((now or _utcnow()) - datetime.timedelta(days=SENT_RETENTION_DAYS))
    cursor = db.connection.execute(
        "DELETE FROM ftn_outbound WHERE status = 'sent' AND sent_at < ?", (cutoff,)
    )
    db.connection.commit()
    return cursor.rowcount


# --- held inbound items -----------------------------------------------------

HELD_PACKET, HELD_FILE, HELD_TIC = "packet", "file", "tic"


@dataclass(frozen=True)
class HeldPacket:
    """One held item: a packet, a file or a TIC (issue #1462). A held file
    may carry its TIC with it (`has_tic`): the pair failed a check
    together. `waiting` marks an unpaired half that pairs with its partner
    when it comes and expires after `UNPAIRED_DAYS`; `bad_echo` the file
    echo tag no file area carries."""

    id: int
    network_id: int | None
    remote_address: str
    file_name: str
    size: int
    reason: str
    received_at: str
    kind: str = HELD_PACKET
    has_tic: bool = False
    waiting: bool = False
    bad_echo: str | None = None


HeldItem = HeldPacket


def held_kind_of(file_name: str, content: bytes) -> str:
    """What a held item is when the caller doesn't say: a `.tic` by name, a
    packet by its header's packet type, any other thing a file."""
    if file_name.lower().endswith(".tic"):
        return HELD_TIC
    if len(content) >= 20 and content[18:20] == b"\x02\x00":
        return HELD_PACKET
    return HELD_FILE


def hold_inbound(
    db: Database, *, network_id: int | None, remote_address: str, file_name: str, content: bytes, reason: str,
    kind: str | None = None, tic: bytes | None = None, waiting: bool = False, wants: tuple[str, ...] = (),
    bad_echo: str | None = None, received_at: str | None = None, requested: bool = False,
) -> bool:
    """Keep an item for the SysOp to look at; False, keeping nothing, when
    the held store is at `MAX_HELD_PACKETS` items or would pass
    `MAX_HELD_BYTES` (a held file's TIC counts too). `received_at` keeps an
    earlier arrival time when an item is held again, so an unpaired half's
    seven days run from when it came. A `requested` file answers a file
    request made here (issue #1475): it waits for a TIC like any file, so a
    file echo's file still pairs, but never expires."""
    count, size = db.connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(content) + COALESCE(LENGTH(tic), 0)), 0) FROM ftn_held_inbound"
    ).fetchone()
    if count >= MAX_HELD_PACKETS or size + len(content) + len(tic or b"") > MAX_HELD_BYTES:
        return False
    db.connection.execute(
        "INSERT INTO ftn_held_inbound (network_id, remote_address, file_name, content, reason, received_at, "
        "kind, tic, waiting, wants, bad_echo, requested) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (network_id, remote_address, file_name, content, reason, received_at or utc_now_iso(),
         kind or held_kind_of(file_name, content), tic, int(waiting), "\n".join(wants) or None, bad_echo,
         int(requested)),
    )
    db.connection.commit()
    return True


_HELD_COLUMNS = (
    "id, network_id, remote_address, file_name, LENGTH(content) AS size, reason, received_at, kind, "
    "tic IS NOT NULL AS has_tic, waiting, bad_echo"
)


def _held_from_row(row) -> HeldPacket:
    return HeldPacket(row["id"], row["network_id"], row["remote_address"], row["file_name"], row["size"],
                      row["reason"], row["received_at"], row["kind"], bool(row["has_tic"]), bool(row["waiting"]),
                      row["bad_echo"])


def list_held(db: Database) -> list[HeldPacket]:
    rows = db.connection.execute(f"SELECT {_HELD_COLUMNS} FROM ftn_held_inbound ORDER BY id").fetchall()
    return [_held_from_row(row) for row in rows]


def get_held(db: Database, held_id: int) -> HeldPacket | None:
    row = db.connection.execute(f"SELECT {_HELD_COLUMNS} FROM ftn_held_inbound WHERE id = ?", (held_id,)).fetchone()
    return _held_from_row(row) if row is not None else None


def held_content(db: Database, held_id: int) -> bytes | None:
    row = db.connection.execute("SELECT content FROM ftn_held_inbound WHERE id = ?", (held_id,)).fetchone()
    return bytes(row["content"]) if row is not None else None


def held_tic(db: Database, held_id: int) -> bytes | None:
    """The TIC held together with a file, or None."""
    row = db.connection.execute("SELECT tic FROM ftn_held_inbound WHERE id = ?", (held_id,)).fetchone()
    return bytes(row["tic"]) if row is not None and row["tic"] is not None else None


def delete_held(db: Database, held_id: int, *, commit: bool = True) -> None:
    db.connection.execute("DELETE FROM ftn_held_inbound WHERE id = ?", (held_id,))
    if commit:
        db.connection.commit()


@dataclass(frozen=True)
class WaitingHalf:
    id: int
    file_name: str
    wants: tuple[str, ...]
    received_at: str


def waiting_halves(db: Database, network_id: int, kind: str) -> list[WaitingHalf]:
    """The network's unpaired halves of one kind (`HELD_FILE` or `HELD_TIC`),
    oldest first, without their content."""
    rows = db.connection.execute(
        "SELECT id, file_name, wants, received_at FROM ftn_held_inbound "
        "WHERE network_id = ? AND waiting = 1 AND kind = ? ORDER BY id",
        (network_id, kind),
    ).fetchall()
    return [WaitingHalf(row["id"], row["file_name"], tuple(row["wants"].split("\n")) if row["wants"] else (),
                        row["received_at"]) for row in rows]


def held_bad_file_echo_ids(db: Database, network_id: int, tag: str) -> list[int]:
    """The held items waiting because no file area carries `tag`."""
    rows = db.connection.execute(
        "SELECT id FROM ftn_held_inbound WHERE network_id = ? AND bad_echo = ? COLLATE NOCASE ORDER BY id",
        (network_id, tag),
    ).fetchall()
    return [row["id"] for row in rows]


@dataclass(frozen=True)
class BadFileEcho:
    """One file echo tag no file area carries, on one network: its held
    items (pairs, or TICs still waiting for their file)."""

    network_id: int
    tag: str
    count: int
    first_seen: str
    last_seen: str


def list_bad_file_echoes(db: Database, network_id: int) -> list[BadFileEcho]:
    rows = db.connection.execute(
        "SELECT bad_echo AS tag, COUNT(*) AS count, MIN(received_at) AS first_seen, MAX(received_at) AS last_seen "
        "FROM ftn_held_inbound WHERE network_id = ? AND bad_echo IS NOT NULL GROUP BY bad_echo COLLATE NOCASE "
        "ORDER BY bad_echo",
        (network_id,),
    ).fetchall()
    return [BadFileEcho(network_id, row["tag"], row["count"], row["first_seen"], row["last_seen"]) for row in rows]


def expire_unpaired(db: Database, *, now: datetime.datetime | None = None) -> list[HeldPacket]:
    """Delete the unpaired halves older than `UNPAIRED_DAYS` and return
    them; each one gets a node-log line."""
    cutoff = utc_iso((now or _utcnow()) - datetime.timedelta(days=UNPAIRED_DAYS))
    rows = db.connection.execute(
        f"SELECT {_HELD_COLUMNS} FROM ftn_held_inbound WHERE waiting = 1 AND requested = 0 "
        "AND received_at < ? ORDER BY id", (cutoff,)
    ).fetchall()
    expired = [_held_from_row(row) for row in rows]
    for item in expired:
        db.connection.execute("DELETE FROM ftn_held_inbound WHERE id = ?", (item.id,))
        _logger.warning("FTN: held %s %s from %s expired unpaired after %d days (%s)", item.kind, item.file_name,
                        item.remote_address, UNPAIRED_DAYS, item.reason)
    db.connection.commit()
    return expired


# --- bad echoes (issue #1466) ------------------------------------------------


@dataclass(frozen=True)
class BadEcho:
    """One echomail tag no board carries, on one network."""

    network_id: int
    tag: str
    count: int
    first_seen: str
    last_seen: str


def hold_bad_echo(db: Database, network_id: int, tag: str, remote_address: str, packet: bytes) -> int:
    """Keep one message (a one-message packet) for a tag no board carries,
    then keep the bounds: at most `MAX_BAD_ECHO_PER_TAG` per tag and
    `MAX_BAD_ECHO_MESSAGES` or `MAX_BAD_ECHO_BYTES` overall, oldest dropped
    first. Returns how many were dropped; each is counted on its network."""
    tag = tag.upper()
    db.connection.execute(
        "INSERT INTO ftn_bad_echoes (network_id, tag, remote_address, packet, received_at) VALUES (?, ?, ?, ?, ?)",
        (network_id, tag, remote_address, packet, utc_now_iso()),
    )
    dropped = 0
    over_tag = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_bad_echoes WHERE network_id = ? AND tag = ?", (network_id, tag)
    ).fetchone()[0] - MAX_BAD_ECHO_PER_TAG
    if over_tag > 0:
        rows = db.connection.execute(
            "SELECT id, network_id FROM ftn_bad_echoes WHERE network_id = ? AND tag = ? ORDER BY id LIMIT ?",
            (network_id, tag, over_tag),
        ).fetchall()
        dropped += _delete_bad_echo_rows(db, [(row["id"], row["network_id"]) for row in rows])
    while True:
        count, size = db.connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(packet)), 0) FROM ftn_bad_echoes"
        ).fetchone()
        if count <= MAX_BAD_ECHO_MESSAGES and size <= MAX_BAD_ECHO_BYTES:
            break
        oldest = db.connection.execute("SELECT id, network_id FROM ftn_bad_echoes ORDER BY id LIMIT 1").fetchone()
        dropped += _delete_bad_echo_rows(db, [(oldest["id"], oldest["network_id"])])
    db.connection.commit()
    if dropped:
        _logger.warning("FTN: the bad-echo store is full; dropped the %d oldest message(s)", dropped)
    return dropped


def _delete_bad_echo_rows(db: Database, rows: list[tuple[int, int]]) -> int:
    for row_id, network_id in rows:
        db.connection.execute("DELETE FROM ftn_bad_echoes WHERE id = ?", (row_id,))
        db.connection.execute(
            "UPDATE ftn_networks SET bad_echo_dropped = bad_echo_dropped + 1 WHERE id = ?", (network_id,)
        )
    return len(rows)


def list_bad_echoes(db: Database, network_id: int) -> list[BadEcho]:
    rows = db.connection.execute(
        "SELECT tag, COUNT(*) AS count, MIN(received_at) AS first_seen, MAX(received_at) AS last_seen "
        "FROM ftn_bad_echoes WHERE network_id = ? GROUP BY tag ORDER BY tag",
        (network_id,),
    ).fetchall()
    return [BadEcho(network_id, row["tag"], row["count"], row["first_seen"], row["last_seen"]) for row in rows]


def bad_echo_ids(db: Database, network_id: int, tag: str) -> list[int]:
    """The held message ids for a tag, oldest first."""
    rows = db.connection.execute(
        "SELECT id FROM ftn_bad_echoes WHERE network_id = ? AND tag = ? ORDER BY id", (network_id, tag.upper())
    ).fetchall()
    return [row["id"] for row in rows]


def take_bad_echo(db: Database, message_id: int) -> tuple[str, bytes] | None:
    """Remove one held message and return `(remote address, packet)`."""
    row = db.connection.execute(
        "SELECT remote_address, packet FROM ftn_bad_echoes WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return None
    db.connection.execute("DELETE FROM ftn_bad_echoes WHERE id = ?", (message_id,))
    db.connection.commit()
    return row["remote_address"], bytes(row["packet"])


def delete_bad_echo(db: Database, network_id: int, tag: str, *, commit: bool = True) -> int:
    """Drop a tag's held messages; returns how many."""
    cursor = db.connection.execute(
        "DELETE FROM ftn_bad_echoes WHERE network_id = ? AND tag = ?", (network_id, tag.upper())
    )
    if commit:
        db.connection.commit()
    return cursor.rowcount


def bad_echo_dropped(db: Database, network_id: int) -> int:
    row = db.connection.execute("SELECT bad_echo_dropped FROM ftn_networks WHERE id = ?", (network_id,)).fetchone()
    return row["bad_echo_dropped"] if row is not None else 0


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
