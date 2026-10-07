"""
FTN gateway state that is not configuration (design doc §6.8): MSGID
serials, the dupe history, the outbound queue and held inbound packets.

Every one of these grows with what other systems send or with what
callers write, so each has a bound and says what happens at it:

- the dupe history keeps `SEEN_RETENTION_DAYS` of MSGIDs, pruned by age;
- the outbound queue holds at most `MAX_PENDING_PER_NETWORK` unsent
  messages per network and refuses more (`FtnQueueFullError`), and sent
  rows are pruned after `SENT_RETENTION_DAYS`;
- held packets are refused once `MAX_HELD_PACKETS` or `MAX_HELD_BYTES` is
  reached, and the session that brought them is told no.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass

from netbbs.config import get_config, set_config_without_commit
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso, utc_now_iso

MSGID_SERIAL_KEY = "ftn_msgid_serial"
SEEN_RETENTION_DAYS = 180
MAX_PENDING_PER_NETWORK = 10_000
SENT_RETENTION_DAYS = 14
MAX_HELD_PACKETS = 200
MAX_HELD_BYTES = 64 * 1024 * 1024


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
    packed: bytes, route: str = "uplink",
) -> bool:
    """Queue an encoded message; False if this one is already queued (each
    post or letter goes out once per network). Raises `FtnQueueFullError`
    at `MAX_PENDING_PER_NETWORK` unsent messages."""
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
    cursor = db.connection.execute(
        "INSERT OR IGNORE INTO ftn_outbound "
        "(network_id, kind, reference_id, destination, route, packed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (network_id, kind, reference_id, destination, route, packed, utc_now_iso()),
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
    """A direct netmail whose calls failed goes via the uplink instead."""
    db.connection.executemany("UPDATE ftn_outbound SET route = 'uplink' WHERE id = ?", [(i,) for i in ids])
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


# --- held inbound packets ---------------------------------------------------


@dataclass(frozen=True)
class HeldPacket:
    id: int
    network_id: int | None
    remote_address: str
    file_name: str
    size: int
    reason: str
    received_at: str


def hold_inbound(
    db: Database, *, network_id: int | None, remote_address: str, file_name: str, content: bytes, reason: str,
) -> bool:
    """Keep a packet for the SysOp to look at; False, keeping nothing, when
    the held store is at `MAX_HELD_PACKETS` or would pass `MAX_HELD_BYTES`."""
    count, size = db.connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0) FROM ftn_held_inbound"
    ).fetchone()
    if count >= MAX_HELD_PACKETS or size + len(content) > MAX_HELD_BYTES:
        return False
    db.connection.execute(
        "INSERT INTO ftn_held_inbound (network_id, remote_address, file_name, content, reason, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (network_id, remote_address, file_name, content, reason, utc_now_iso()),
    )
    db.connection.commit()
    return True


def list_held(db: Database) -> list[HeldPacket]:
    rows = db.connection.execute(
        "SELECT id, network_id, remote_address, file_name, LENGTH(content) AS size, reason, received_at "
        "FROM ftn_held_inbound ORDER BY id"
    ).fetchall()
    return [HeldPacket(row["id"], row["network_id"], row["remote_address"], row["file_name"], row["size"],
                       row["reason"], row["received_at"]) for row in rows]


def held_content(db: Database, held_id: int) -> bytes | None:
    row = db.connection.execute("SELECT content FROM ftn_held_inbound WHERE id = ?", (held_id,)).fetchone()
    return bytes(row["content"]) if row is not None else None


def delete_held(db: Database, held_id: int) -> None:
    db.connection.execute("DELETE FROM ftn_held_inbound WHERE id = ?", (held_id,))
    db.connection.commit()


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
