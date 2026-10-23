"""
TIC forwarding: a hub sending a file echo's file on to the other systems
carrying the echo (design doc §6.8, "Points and downlinks" and "File
echoes"; issue #1479, §16 "Issue #1154", Decision 17; FTS-5006).

A file that passed the TIC checks and is stored in its file area is
forwarded by `forward_without_commit`:

- **Who gets it.** Every active point and downlink carrying the file echo
  (`netbbs.ftn.links.links_carrying`), and the uplink when the file came
  from a link. Never the system it came from, and never one its TIC's
  Seenby names already.
- **Loops.** A TIC whose Path names this node passed here before: the
  file goes nowhere.
- **The TIC.** A new TIC per system, keeping the original's Area, Origin,
  File and Lfile, Size, Desc and Ldesc, Replaces and Crc: From this node,
  To the system, its Path lines with this node's added, Seenby what it came
  with plus this node and every system it goes to, and Pw the TIC password
  agreed with that system -- the link's, or the network's for the uplink.
  A link with no TIC password set gets nothing, with a node-log line: the
  other side would hold a TIC without one.
- **The file** goes as its file area entry, kept once sent, under its 8.3
  name (`tic.dos_name`, `Lfile` naming the real one), and travels with its
  TIC: the outbound queue drops one when the other can't be sent
  (`ticfwd` rows are paired, as a hatch's are). The TIC is written to the
  spool folder and deleted once sent.
- **When.** At once when the file is visible; in a moderated area once a
  moderator approves it (`ftn_tic_waiting` keeps the TIC until then, at
  most `MAX_WAITING` per network, for `WAITING_DAYS`). A file in a
  nodelist echo no file area carries is never stored, so it is not
  forwarded.
- **Bounds.** A system whose queue can't take both the file and its TIC is
  skipped with a node-log line; the others still get theirs.
"""

from __future__ import annotations

import datetime
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path

from netbbs import __version__
from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.networks import FtnNetwork, get_network
from netbbs.ftn.outbound import (
    KILL,
    MAX_PENDING_FILES_PER_LINK,
    MAX_PENDING_FILES_PER_NETWORK,
    queue_file_without_commit,
    spool_dir,
)
from netbbs.ftn.tic import Tic, dos_name, parse_tic, write_tic
from netbbs.storage.database import Database
from netbbs.timeutil import parse_utc_iso, utc_iso, utc_now_iso

_logger = logging.getLogger(__name__)

REFERENCE_KIND = "ticfwd"
MAX_WAITING = 1_000
WAITING_DAYS = 30


@dataclass
class Forwarded:
    sent_to: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    loop: bool = False


def _unix(iso: str) -> int | None:
    try:
        return int(parse_utc_iso(iso).timestamp())
    except ValueError:
        return None


def _write_spool_tic(db: Database, data: bytes) -> tuple[Path, str]:
    folder = spool_dir(db) / "ticfwd"
    folder.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    for _attempt in range(16):
        name = f"{secrets.token_hex(4).upper()}.TIC"
        path = folder / name
        try:
            descriptor = os.open(path, flags, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return path, name
    raise FtnFormatError("could not find a free name for a TIC in the spool folder")


def _room(db: Database, network_id: int, link_id: int | None) -> bool:
    """Whether the queue takes a file and its TIC for this system."""
    pending = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_outbound_files WHERE network_id = ?", (network_id,)
    ).fetchone()[0]
    if pending + 2 > MAX_PENDING_FILES_PER_NETWORK:
        return False
    if link_id is not None:
        waiting = db.connection.execute(
            "SELECT COUNT(*) FROM ftn_outbound_files WHERE link_id = ?", (link_id,)
        ).fetchone()[0]
        if waiting + 2 > MAX_PENDING_FILES_PER_LINK:
            return False
    return True


def queue_tic_copy_without_commit(
    db: Database, network: FtnNetwork, entry, tic: Tic, *, to: FtnAddress, password: str, link_id: int | None,
    earlier_path: tuple[str, ...], path: list[tuple[FtnAddress, int]], seen_by: list[FtnAddress], reference: str,
    spooled: list[Path],
) -> bool:
    """Queue `entry` (a file area entry) and a TIC for it to one system;
    False when its queue has no room for both. Every TIC written to the
    spool folder is added to `spooled`, for the caller to remove when its
    transaction doesn't commit. Raises `FtnFormatError` for a TIC that
    can't be written."""
    if not _room(db, network.id, link_id):
        return False
    send_name = dos_name(entry.filename)
    copy = replace(tic, file=send_name, long_name=entry.filename if entry.filename != send_name else None,
                   from_address=network.our_address, password=password, path=(), seen_by=())
    data = write_tic(copy, to=to, path=path, seen_by=seen_by, date=_unix(entry.created_at),
                     created=f"NetBBS {__version__}", earlier_path=earlier_path)
    tic_path, tic_name = _write_spool_tic(db, data)
    spooled.append(tic_path)
    common = dict(destination=to.four_d, route="direct" if link_id is not None else "uplink",
                  reference_kind=REFERENCE_KIND, reference_id=reference, link_id=link_id)
    queue_file_without_commit(db, network.id, send_name=send_name, size_bytes=entry.size_bytes,
                              file_id=entry.file_id, **common)
    queue_file_without_commit(db, network.id, send_name=tic_name, size_bytes=len(data), path=tic_path,
                              after_sent=KILL, **common)
    return True


def forward_without_commit(db: Database, network: FtnNetwork, entry, tic: Tic, *, sender=None,
                           spooled: list[Path]) -> Forwarded:
    """Forward a stored, visible TIC file to every system it goes to (see
    the module docstring). `sender` is the link it came from, None for the
    uplink. Raises nothing for a full queue."""
    from netbbs.ftn.links import FILE_ECHO, links_carrying

    outcome = Forwarded()
    ours = network.our_address
    if ours.point:
        return outcome
    if any(address.same_node(ours) for address in tic.path_addresses):
        outcome.loop = True
        _logger.warning("FTN %s: TIC for %s in %s names this node in its Path; not sent on (a loop)", network.name,
                        entry.filename, tic.area)
        return outcome
    seen = list(tic.seen_by)
    try:
        candidates = links_carrying(db, network.id, FILE_ECHO, tic.area)
    except sqlite3.OperationalError:
        candidates = []
    targets = [link for link in candidates
               if (sender is None or link.id != sender.id)
               and not any(address.same_node(link.address) for address in seen)]
    to_uplink = sender is not None and not any(address.same_node(network.uplink_address) for address in seen)
    systems = [(link.address, link.tic_password, link.id, link.label) for link in targets]
    if to_uplink:
        systems.append((network.uplink_address, network.tic_password, None, f"the uplink {network.uplink_address}"))
    sendable = []
    for system in systems:
        if system[1]:
            sendable.append(system)
            continue
        outcome.skipped.append(system[0].four_d)
        _logger.warning("FTN %s: %s in %s not sent to %s: no TIC password is set for it", network.name,
                        entry.filename, tic.area, system[3])
    # Seenby names this node and every system the file goes to.
    new_seen = list(seen)
    for address in [ours, *(system[0] for system in sendable)]:
        if not any(known.same_node(address) for known in new_seen):
            new_seen.append(address)
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    path = [(ours, now)]
    for address, password, link_id, label in sendable:
        reference = f"{entry.file_id[:24]}>{link_id if link_id is not None else 'up'}"
        try:
            queued = queue_tic_copy_without_commit(
                db, network, entry, tic, to=address, password=password, link_id=link_id, earlier_path=tic.path,
                path=path, seen_by=new_seen, reference=reference, spooled=spooled,
            )
        except (FtnFormatError, ValueError) as exc:
            outcome.skipped.append(address.four_d)
            _logger.warning("FTN %s: %s in %s not sent to %s: %s", network.name, entry.filename, tic.area, label, exc)
            continue
        if queued:
            outcome.sent_to.append(address.four_d)
        else:
            outcome.skipped.append(address.four_d)
            _logger.warning("FTN %s: %s in %s not sent to %s: its outbound files queue is full", network.name,
                            entry.filename, tic.area, label)
    if outcome.sent_to:
        _logger.info("FTN %s: %s in %s sent on to %s", network.name, entry.filename, tic.area,
                     ", ".join(outcome.sent_to))
    return outcome


def forward_or_wait(db: Database, network: FtnNetwork, entry, tic_bytes: bytes, tic: Tic, *, sender=None) -> None:
    """After a TIC file was stored and committed: forward it now when it is
    visible, else keep its TIC until a moderator approves it. Commits;
    never raises, as the file is stored either way."""
    try:
        if entry.status == "approved":
            _forward_committed(db, network, entry, tic, sender)
            return
        db.connection.execute(
            "INSERT OR REPLACE INTO ftn_tic_waiting (file_id, network_id, from_uplink, link_id, tic, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry.file_id, network.id, int(sender is None), sender.id if sender is not None else None, tic_bytes,
             utc_now_iso()),
        )
        _bound_waiting(db, network.id)
        db.connection.commit()
    except sqlite3.OperationalError:  # a schema older than the hub
        db.connection.rollback()
    except Exception:  # noqa: BLE001 -- the stored file stands; only its forwarding failed
        db.connection.rollback()
        _logger.exception("FTN %s: forwarding %s failed", network.name, entry.filename)


def _forward_committed(db: Database, network: FtnNetwork, entry, tic: Tic, sender) -> Forwarded:
    spooled: list[Path] = []
    try:
        outcome = forward_without_commit(db, network, entry, tic, sender=sender, spooled=spooled)
        db.connection.commit()
    except BaseException:
        db.connection.rollback()
        for path in spooled:
            path.unlink(missing_ok=True)
        raise
    return outcome


def _bound_waiting(db: Database, network_id: int) -> None:
    over = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_tic_waiting WHERE network_id = ?", (network_id,)
    ).fetchone()[0] - MAX_WAITING
    if over > 0:
        db.connection.execute(
            "DELETE FROM ftn_tic_waiting WHERE file_id IN (SELECT file_id FROM ftn_tic_waiting WHERE network_id = ? "
            "ORDER BY created_at, rowid LIMIT ?)", (network_id, over))
        _logger.warning("FTN: %d TIC file(s) waiting for approval were not forwarded: more than %d waited", over,
                        MAX_WAITING)


def forward_approved(db: Database, file_id: str) -> Forwarded | None:
    """A TIC file was approved: forward it, if its TIC waits. Never raises:
    the approval stands whatever happens here."""
    from netbbs.files.entries import FileEntryError, get_file
    from netbbs.ftn.links import get_link

    try:
        row = db.connection.execute(
            "SELECT network_id, from_uplink, link_id, tic FROM ftn_tic_waiting WHERE file_id = ?", (file_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        db.connection.execute("DELETE FROM ftn_tic_waiting WHERE file_id = ?", (file_id,))
        db.connection.commit()
        network = get_network(db, row["network_id"])
        if network is None or not network.enabled:
            return None
        try:
            entry = get_file(db, file_id)
        except FileEntryError:
            return None
        if entry.status != "approved":
            return None
        tic = parse_tic(bytes(row["tic"]))
        sender = None
        if not row["from_uplink"]:
            sender = get_link(db, row["link_id"]) if row["link_id"] is not None else _GONE_LINK
        return _forward_committed(db, network, entry, tic, sender)
    except Exception:  # noqa: BLE001 -- an approval must never fail on forwarding
        try:
            db.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        _logger.exception("FTN: forwarding approved file %s failed", file_id[:16])
        return None


@dataclass(frozen=True)
class _GoneLink:
    """The sender of a waiting TIC whose link was deleted since: it came
    from a link, so the uplink still gets it, and no link is left out."""

    id: int = -1


_GONE_LINK = _GoneLink()


def prune_tic_waiting(db: Database, *, now: datetime.datetime | None = None) -> int:
    """Forget TICs whose file was never approved within `WAITING_DAYS`."""
    cutoff = utc_iso((now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=WAITING_DAYS))
    try:
        cursor = db.connection.execute("DELETE FROM ftn_tic_waiting WHERE created_at < ?", (cutoff,))
    except sqlite3.OperationalError:
        return 0
    db.connection.commit()
    return cursor.rowcount
