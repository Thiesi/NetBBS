"""
Hatching: sending one of this node's own files into a file echo (design doc
§6.8 "Hatching", §16 "Issue #1154", Decision 14; issue #1476).

A file in a file area that carries a file echo can be hatched: this node
writes a TIC for it (`tic.write_tic`) and queues the file and the TIC to the
network's uplink through the outbound files queue (`netbbs.ftn.outbound`).
Everything here is synchronous and `db`-first.

- **What goes out.** The file area entry itself (always kept once sent),
  under its own name when that is an 8.3 name and under `tic.dos_name`
  otherwise, with `Lfile` carrying the real one; then the TIC, written to
  this node's spool folder and deleted once sent. Both go to the uplink,
  normal flavour, the file first.
- **The TIC.** Area is the area's echo tag; Origin and From are this node;
  To is the uplink; Desc is the description's first line and Ldesc every
  line; Crc, Size and Date are the stored file's; one Path line (this node,
  now); Seenby this node and the uplink; Pw the network's TIC password.
- **Once.** Each hatch is recorded in `ftn_hatches` and audit-logged. A file
  already hatched into a network is hatched again only when the SysOp asks
  for it again on purpose, and that TIC carries `Replaces` with the names
  the earlier copy went under.
- **Refused**, with the reason: a file not approved; a file that came over
  FTN (by TIC, as a netmail's attach, or saved from the held store), so
  nothing echoes back to the network it came from; an area with no file
  echo; a network with no TIC password; a file over BinkP's 64 MiB; a name
  a receiver would take for a control file (`.TIC`, `.PKT`, `.REQ`).
- **Automatic hatching.** A file area can hatch its approved uploads by
  itself (`set_auto_hatch`, off by default). `auto_hatch` runs when a
  caller's upload is stored approved and when one is approved: a local
  upload not hatched before is hatched once. It never fails the upload or
  the approval; a refusal is a node-log line.
"""

from __future__ import annotations

import datetime
import logging
import os
import secrets
import zlib
from dataclasses import dataclass
from pathlib import Path

from netbbs import __version__
from netbbs.auth.users import User
from netbbs.files.entries import FileEntry, FileEntryError, get_file
from netbbs.ftn import FtnFormatError
from netbbs.ftn.binkp import MAX_FILE_BYTES
from netbbs.ftn.networks import FtnNetwork, get_network
from netbbs.ftn.outbound import KILL, MAX_PENDING_FILES_PER_NETWORK, queue_file_without_commit, spool_dir
from netbbs.ftn.queue import FtnQueueFullError
from netbbs.ftn.tic import Tic, dos_name, name_problem, write_tic
from netbbs.moderation.log import record_action
from netbbs.storage.database import Database
from netbbs.timeutil import parse_utc_iso, utc_now_iso

_logger = logging.getLogger(__name__)

REFERENCE_KIND = "hatch"
# Names a receiving mailer or file processor reads as its own control files.
_CONTROL_EXTENSIONS = (".tic", ".pkt", ".req")
_CHUNK = 1024 * 1024


class HatchError(Exception):
    """A file can't be hatched; the message says why, for the SysOp."""


@dataclass(frozen=True)
class PreviousHatch:
    hatched_at: str
    automatic: bool
    send_name: str
    file_name: str


@dataclass(frozen=True)
class HatchTarget:
    """Where a file would be hatched, and whether it was before."""

    network: FtnNetwork
    tag: str
    previous: PreviousHatch | None


@dataclass(frozen=True)
class Hatched:
    network_name: str
    tag: str
    file_name: str
    send_name: str
    tic_name: str
    replaces: tuple[str, ...]


# --- the area setting ----------------------------------------------------------


def auto_hatch_enabled(db: Database, area_id: int) -> bool:
    row = db.connection.execute("SELECT * FROM file_areas WHERE id = ?", (area_id,)).fetchone()
    return row is not None and "ftn_auto_hatch" in row.keys() and bool(row["ftn_auto_hatch"])


def set_auto_hatch(db: Database, area_id: int, enabled: bool, *, actor: User) -> None:
    db.connection.execute("UPDATE file_areas SET ftn_auto_hatch = ? WHERE id = ?", (int(enabled), area_id))
    db.connection.commit()
    record_action(db, actor=actor, action="set_file_area_auto_hatch", object_type="file_area", object_id=area_id,
                  detail=f"automatic hatching {'on' if enabled else 'off'}")


# --- checks ----------------------------------------------------------------------


def _area_echo(db: Database, area_id: int) -> tuple[FtnNetwork, str] | None:
    row = db.connection.execute(
        "SELECT ftn_network_id, ftn_file_echo FROM file_areas WHERE id = ?", (area_id,)
    ).fetchone()
    if row is None or row["ftn_network_id"] is None or not row["ftn_file_echo"]:
        return None
    network = get_network(db, row["ftn_network_id"])
    return (network, row["ftn_file_echo"]) if network is not None else None


def came_over_ftn(db: Database, entry: FileEntry) -> str | None:
    """How `entry` came over FTN, or None for one of this node's own files:
    hatching it would send it back to the network it came from."""
    row = db.connection.execute("SELECT * FROM files WHERE id = ?", (entry.id,)).fetchone()
    if row is None:
        return None
    keys = row.keys()
    if "ftn_file_echo" in keys and row["ftn_file_echo"]:
        return f"it arrived by TIC in {row['ftn_file_echo']}"
    if ("ftn_origin" in keys and row["ftn_origin"]) or ("ftn_network_id" in keys and row["ftn_network_id"]):
        return "it came over FTN"
    if entry.uploader_user_id is None:
        return "it was not uploaded on this BBS"
    return None


def previous_hatch(db: Database, file_id: str, network_id: int) -> PreviousHatch | None:
    row = db.connection.execute(
        "SELECT hatched_at, automatic, send_name, file_name FROM ftn_hatches WHERE file_id = ? AND network_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (file_id, network_id),
    ).fetchone()
    if row is None:
        return None
    return PreviousHatch(row["hatched_at"], bool(row["automatic"]), row["send_name"], row["file_name"])


def hatch_target(db: Database, entry: FileEntry) -> HatchTarget:
    """Where `entry` would be hatched; raises `HatchError` with the reason
    when it can't be."""
    echo = _area_echo(db, entry.area_id)
    if echo is None:
        raise HatchError("This file area carries no file echo, so there is nowhere to hatch into.")
    network, tag = echo
    name = entry.filename
    if entry.status != "approved":
        raise HatchError(f"{name} is not approved yet; it can be hatched once it is.")
    origin = came_over_ftn(db, entry)
    if origin is not None:
        raise HatchError(f"{name} is not hatched: {origin}, and hatching it would send it back to the network.")
    if not network.tic_password:
        raise HatchError(f"No TIC password is set for {network.name}; set the one agreed with the hub in the "
                         "network editor first.")
    if entry.size_bytes > MAX_FILE_BYTES:
        raise HatchError(f"{name} is {entry.size_bytes} bytes; BinkP sends at most {MAX_FILE_BYTES} "
                         f"({MAX_FILE_BYTES // (1024 * 1024)} MiB).")
    problem = name_problem(name)
    if problem is not None:
        raise HatchError(f"{name!r} can't be sent as it is: {problem}.")
    if name.lower().endswith(_CONTROL_EXTENSIONS) or dos_name(name).lower().endswith(_CONTROL_EXTENSIONS):
        raise HatchError(f"{name} would be taken for a control file by the systems receiving it; rename it first.")
    return HatchTarget(network, tag, previous_hatch(db, entry.file_id, network.id))


# --- hatching ----------------------------------------------------------------------


def _stored_crc(path: Path) -> tuple[int, int]:
    """`(crc32, size)` of the stored file, read a chunk at a time."""
    value, size = 0, 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            value = zlib.crc32(chunk, value)
            size += len(chunk)
    return value & 0xFFFFFFFF, size


def _description_lines(entry: FileEntry) -> list[str]:
    return [line for line in (entry.description or "").splitlines() if line.strip()]


def _unix(iso: str) -> int | None:
    try:
        return int(parse_utc_iso(iso).timestamp())
    except ValueError:
        return None


def _write_spool_tic(db: Database, data: bytes) -> tuple[Path, str]:
    """Write the TIC into the spool folder under a fresh 8.3 name."""
    folder = spool_dir(db) / "hatch"
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
    raise HatchError("Could not find a free name for the TIC in the spool folder.")


def hatch_file(
    db: Database, file_id: str, *, actor: User | None, again: bool = False, automatic: bool = False,
) -> Hatched:
    """Hatch file area entry `file_id` into its area's file echo: write its
    TIC, queue both to the uplink, record and audit-log it. A file hatched
    before into the same network is refused unless `again`, which sends it
    with `Replaces` for the earlier copy's names. Raises `HatchError` (or
    `FtnQueueFullError`, whose message says why) and changes nothing."""
    try:
        entry = get_file(db, file_id)
    except FileEntryError:
        raise HatchError("That file is gone; nothing was hatched.") from None
    target = hatch_target(db, entry)
    network, tag = target.network, target.tag
    if target.previous is not None and not again:
        raise HatchError(f"{entry.filename} was already hatched into {tag} ({target.previous.hatched_at[:10]}).")
    pending = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_outbound_files WHERE network_id = ?", (network.id,)
    ).fetchone()[0]
    if pending + 2 > MAX_PENDING_FILES_PER_NETWORK:
        raise FtnQueueFullError(f"{pending} files are already waiting for {network.name}; hatch again once they "
                                "have gone")
    path = Path(entry.storage_path)
    if not path.is_file() or path.is_symlink():
        raise HatchError(f"{entry.filename}'s stored copy is missing; nothing was hatched.")
    crc, size = _stored_crc(path)
    if size > MAX_FILE_BYTES:
        raise HatchError(f"{entry.filename} is {size} bytes; BinkP sends at most {MAX_FILE_BYTES}.")
    send_name = dos_name(entry.filename)
    replaces: tuple[str, ...] = ()
    if target.previous is not None:
        replaces = tuple(dict.fromkeys(n for n in (target.previous.send_name, target.previous.file_name)))
    lines = _description_lines(entry)
    now = datetime.datetime.now(datetime.timezone.utc)
    us = network.our_address
    tic = Tic(
        area=tag, file=send_name, crc=crc, long_name=entry.filename if entry.filename != send_name else None,
        size=size, desc=lines[0] if lines else entry.filename, ldesc=tuple(lines), replaces=replaces,
        origin=us, from_address=us, password=network.tic_password,
    )
    try:
        data = write_tic(tic, to=network.uplink_address, path=[(us, int(now.timestamp()))],
                         seen_by=[us, network.uplink_address], date=_unix(entry.created_at),
                         created=f"NetBBS {__version__}")
    except FtnFormatError as exc:
        raise HatchError(f"{entry.filename} can't be hatched: {exc}.") from None
    tic_path, tic_name = _write_spool_tic(db, data)
    try:
        cursor = db.connection.execute(
            "INSERT INTO ftn_hatches (network_id, file_id, area_id, tag, file_name, send_name, tic_name, crc32, "
            "size_bytes, replaces, automatic, hatched_by, hatched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (network.id, entry.file_id, entry.area_id, tag, entry.filename, send_name, tic_name, f"{crc:08X}", size,
             "\n".join(replaces) or None, int(automatic), actor.id if actor is not None else None, utc_now_iso()),
        )
        reference = str(cursor.lastrowid)
        common = dict(destination=network.uplink_address.four_d, route="uplink", reference_kind=REFERENCE_KIND,
                      reference_id=reference)
        queue_file_without_commit(db, network.id, send_name=send_name, size_bytes=size, file_id=entry.file_id,
                                  **common)
        queue_file_without_commit(db, network.id, send_name=tic_name, size_bytes=len(data), path=tic_path,
                                  after_sent=KILL, **common)
        db.connection.commit()
    except BaseException:
        db.connection.rollback()
        tic_path.unlink(missing_ok=True)
        raise
    how = "automatically" if automatic else ("again" if replaces else "")
    detail = (f"hatched {entry.filename} into {tag} on {network.name}{' ' + how if how else ''} "
              f"(as {send_name}, CRC-32 {crc:08X}, {size} bytes)")
    record_action(db, actor=actor, action="hatch_ftn_file", object_type="file_area", object_id=entry.area_id,
                  detail=detail)
    _logger.info("FTN %s: %s", network.name, detail)
    return Hatched(network.name, tag, entry.filename, send_name, tic_name, replaces)


def auto_hatch(db: Database, file_id: str) -> Hatched | None:
    """Hatch a caller's upload that just became visible, when its area
    hatches automatically and it was never hatched. Never raises: a refusal
    is a node-log line, and the upload or approval stands."""
    try:
        try:
            entry = get_file(db, file_id)
        except FileEntryError:
            return None
        if entry.status != "approved" or not auto_hatch_enabled(db, entry.area_id):
            return None
        echo = _area_echo(db, entry.area_id)
        if echo is None or came_over_ftn(db, entry) is not None:
            return None
        if previous_hatch(db, entry.file_id, echo[0].id) is not None:
            return None
        return hatch_file(db, file_id, actor=None, automatic=True)
    except (HatchError, FtnQueueFullError) as exc:
        _logger.warning("FTN: automatic hatch of %s not done: %s", file_id[:16], exc)
    except Exception:  # noqa: BLE001 -- an upload or approval must never fail on its hatch
        try:
            db.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        _logger.exception("FTN: automatic hatch of %s failed", file_id[:16])
    return None
