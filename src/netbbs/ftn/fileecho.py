"""
File echoes: TIC pairs into file areas (design doc §6.8 "File echoes",
§16 "Issue #1154", Decisions 1-4; issue #1462).

A file arrives as a pair, the file and a `.TIC` naming it. Everything here
is synchronous and `db`-first; `netbbs.ftn.exchange` drives it after a
session, and the console after a SysOp maps a bad file echo.

- **Pairing.** `open_tic` reads a TIC and finds its file among what the
  session brought, else among the network's unpaired files in the held
  store. `waiting_tic_for` does the reverse for a file. A half without its
  partner waits in the held store (`settle`), and `queue.expire_unpaired`
  deletes it after seven days.
- **Checks.** `judge` runs §6.8's checks 3-11 in order on a parsed TIC
  (check 1, the secure session, and check 2, the parse, come before it)
  and says what happens: accept, hold with the reason, wait for the file,
  drop a duplicate, or hold as a bad echo. Size and CRC are taken from the
  bytes actually received, never from the TIC.
- **Storing.** `settle` carries the verdict out. An accepted file goes into
  its area as a TIC file through the area's own moderation: pending in a
  moderated area, visible otherwise. It has no local uploader; its label
  is the network and the TIC's `Origin`, as `fsxNet (21:1/100)`, and it
  records its network, tag, origin and CRC-32. `Replaces` is applied once
  the file is visible (`apply_replaces`, also called on approval), and
  matches only files that arrived by TIC in the same area.
- **Nodelist echo.** A file in the network's nodelist echo is handed to
  `netbbs.ftn.nodelist_feed.handle_received_file` after it passed, whether
  or not a file area carries the echo.
- **Held items.** `held_file_for_saving`, `save_held_to_area` and
  `export_held` are the held screen's [S]ave and [E]xport (issue #1460).
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from netbbs.auth.users import User
from netbbs.boards.content_id import compute_content_id
from netbbs.config import get_max_upload_bytes
from netbbs.files.areas import FileArea, _row_to_file_area
from netbbs.files.diz import fit_description
from netbbs.files.entries import FileEntry, get_file, upload_file
from netbbs.files.storage import new_incoming_temp_path, store_bytes
from netbbs.ftn import FtnFormatError
from netbbs.ftn.binkp import MAX_FILE_BYTES, safe_file_name
from netbbs.ftn import nodelist_feed
from netbbs.ftn.networks import FtnNetwork, file_area_id_for_echo
from netbbs.ftn.queue import (
    HELD_FILE,
    HELD_TIC,
    get_held,
    held_content,
    held_tic,
    hold_inbound,
    waiting_halves,
)
from netbbs.ftn.tic import (
    Tic,
    crc32,
    matches_replaces,
    name_problem,
    parse_tic,
    replaces_pattern_problem,
    stored_name,
)
from netbbs.moderation.log import record_action
from netbbs.search import reindex_file
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

_logger = logging.getLogger(__name__)

ACCEPT, HOLD, WAIT, DUPLICATE, BAD_ECHO = "accept", "hold", "wait", "duplicate", "bad_echo"


class HeldItemError(Exception):
    """A held item can't be saved or exported; the message says why."""


@dataclass
class Half:
    """One half of a pair: its name, its bytes, and the held item it waits
    in, if it waits in one."""

    name: str
    data: bytes
    held_id: int | None = None
    received_at: str | None = None


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str = ""
    tag: str = ""
    area_id: int | None = None  # the file area carrying the echo
    name: str = ""  # the name the file is stored under
    nodelist_echo: bool = False


def pair_key(name: str) -> str:
    """How a received name and a TIC's name are matched: BinkP's safe form
    of the name, without case."""
    return safe_file_name(name).casefold()


def tic_wants(tic: Tic) -> tuple[str, ...]:
    """The keys a received file may match to be this TIC's file."""
    keys = [pair_key(tic.file)]
    if tic.long_name and pair_key(tic.long_name) not in keys:
        keys.append(pair_key(tic.long_name))
    return tuple(keys)


# --- pairing ----------------------------------------------------------------


@dataclass(frozen=True)
class OpenedTic:
    tic: Tic | None
    error: str | None = None
    session_index: int | None = None  # the partner among the session's files
    held_file: Half | None = None  # or the partner waiting in the held store


def open_tic(db: Database, network: FtnNetwork, data: bytes, session_names: list[str]) -> OpenedTic:
    """Check 2, and the TIC's file: one the session brought (first), else a
    file of this network waiting in the held store."""
    try:
        tic = parse_tic(data)
    except FtnFormatError as exc:
        return OpenedTic(None, error=f"unreadable TIC: {exc}")
    wants = tic_wants(tic)
    for index, name in enumerate(session_names):
        if pair_key(name) in wants:
            return OpenedTic(tic, session_index=index)
    for half in waiting_halves(db, network.id, HELD_FILE):
        if pair_key(half.file_name) in wants:
            content = held_content(db, half.id)
            if content is not None:
                return OpenedTic(tic, held_file=Half(half.file_name, content, half.id, half.received_at))
    return OpenedTic(tic)


def waiting_tic_for(db: Database, network: FtnNetwork, file_name: str) -> tuple[Half, Tic] | None:
    """The waiting TIC of this network that names `file_name`, if any."""
    key = pair_key(file_name)
    for half in waiting_halves(db, network.id, HELD_TIC):
        if key in half.wants:
            content = held_content(db, half.id)
            if content is None:
                continue
            try:
                tic = parse_tic(content)
            except FtnFormatError:
                continue
            return Half(half.file_name, content, half.id, half.received_at), tic
    return None


def load_held_pair(db: Database, held_id: int) -> tuple[Half, Tic, Half | None] | None:
    """A held item as `(TIC half, TIC, file half or None)`, for checking it
    again: a file held with its TIC, or a TIC alone. None for anything
    else, or a TIC that doesn't parse."""
    item = get_held(db, held_id)
    content = held_content(db, held_id)
    if item is None or content is None:
        return None
    if item.kind == HELD_FILE:
        tic_bytes = held_tic(db, held_id)
        if tic_bytes is None:
            return None
        tic_half = Half(f"{item.file_name}.tic", tic_bytes)
        file_half: Half | None = Half(item.file_name, content, held_id, item.received_at)
    elif item.kind == HELD_TIC:
        tic_half, tic_bytes, file_half = Half(item.file_name, content, held_id, item.received_at), content, None
    else:
        return None
    try:
        return tic_half, parse_tic(tic_bytes), file_half
    except FtnFormatError:
        return None


# --- the checks -------------------------------------------------------------


def judge(db: Database, network: FtnNetwork, tic: Tic, data: bytes | None, *, link_checks: bool = True) -> Verdict:
    """§6.8's checks 3-11 on a pair whose TIC parsed. `link_checks=False`
    skips 3 and 4, for a bad file echo's pairs run again after the SysOp
    mapped it: they passed those already."""
    tag = tic.area.upper()
    if link_checks:
        if tic.from_address is None or not tic.from_address.same_node(network.uplink_address):
            sender = tic.from_address.four_d if tic.from_address is not None else "nobody (no From line)"
            return Verdict(HOLD, f"the TIC is from {sender}, not the uplink {network.uplink_address.four_d}", tag)
        if not network.tic_password:
            return Verdict(HOLD, f"no TIC password is set for {network.name}, so no TIC is accepted", tag)
        if tic.password.upper() != network.tic_password.upper():
            return Verdict(HOLD, "the TIC password does not match", tag)
    area_id = file_area_id_for_echo(db, network.id, tag)
    nodelist = _is_nodelist_echo(network, tag)
    if area_id is None and not nodelist:
        return Verdict(BAD_ECHO, f"no file area carries file echo {tag} (an unknown echo)", tag)
    try:
        name = stored_name(tic)
    except FtnFormatError as exc:
        return Verdict(HOLD, str(exc), tag)
    if data is None:
        return Verdict(WAIT, f"waiting for {tic.file}", tag, area_id, name, nodelist)
    if tic.size is not None and tic.size != len(data):
        return Verdict(HOLD, f"{name} is {len(data)} bytes, but the TIC says {tic.size}", tag)
    limit = min(get_max_upload_bytes(db), MAX_FILE_BYTES)
    if len(data) > limit:
        return Verdict(HOLD, f"{name} is {len(data)} bytes, over this node's limit of {limit}", tag)
    actual = crc32(data)
    if actual != tic.crc:
        return Verdict(HOLD, f"{name} has CRC-32 {actual:08X}, but the TIC says {tic.crc:08X}", tag)
    if area_id is not None:
        if db.connection.execute(
            "SELECT 1 FROM files WHERE area_id = ? AND ftn_file_echo IS NOT NULL AND filename = ? COLLATE NOCASE "
            "AND ftn_crc32 = ?",
            (area_id, name, f"{actual:08X}"),
        ).fetchone() is not None:
            return Verdict(DUPLICATE, f"{name} with this CRC-32 already came by TIC", tag, area_id, name)
        patterns = _usable_replaces(tic, network)
        for row in db.connection.execute(
            "SELECT filename, ftn_file_echo FROM files WHERE area_id = ? AND filename = ? COLLATE NOCASE "
            "AND status IN ('approved', 'pending')",
            (area_id, name),
        ):
            if row["ftn_file_echo"] is None or not any(matches_replaces(p, row["filename"]) for p in patterns):
                return Verdict(HOLD, f"a file named {row['filename']} is already in the area, and Replaces "
                                     "doesn't cover it", tag)
    return Verdict(ACCEPT, "", tag, area_id, name, nodelist)


def _is_nodelist_echo(network: FtnNetwork, tag: str) -> bool:
    return bool(network.nodelist_echo) and network.nodelist_echo.upper() == tag.upper()


def _usable_replaces(tic: Tic, network: FtnNetwork) -> list[str]:
    usable = []
    for value in tic.replaces:
        problem = replaces_pattern_problem(value)
        if problem is None:
            usable.append(value)
        else:
            _logger.warning("FTN %s: TIC for %s: Replaces %r ignored: %s", network.name, tic.file[:80], value[:80],
                            problem)
    return usable


# --- carrying a verdict out -------------------------------------------------


def settle(
    db: Database, network: FtnNetwork, remote: str, tic_half: Half, tic: Tic, file_half: Half | None,
    verdict: Verdict, description: str | None, result,
) -> None:
    """Carry out `verdict` for a pair (or a TIC alone), counting into
    `result` (a `TossResult`). The halves' held items are consumed: what
    stays held is held again as one item, keeping the earliest arrival."""
    consumed = [half.held_id for half in (tic_half, file_half) if half is not None and half.held_id is not None]
    arrived = min((h.received_at for h in (tic_half, file_half) if h is not None and h.received_at), default=None)
    if verdict.outcome == ACCEPT and file_half is not None:
        if verdict.area_id is not None:
            try:
                entry = store_tic_file(db, network, tic, verdict, file_half.data, description, commit=False)
            except FtnFormatError as exc:
                db.connection.rollback()
                return settle(db, network, remote, tic_half, tic, file_half,
                              Verdict(HOLD, f"could not be stored: {exc}", verdict.tag), None, result)
            _delete_held_without_commit(db, consumed)
            db.connection.commit()
            reindex_file(db, entry.area_id, entry.file_id)
            if entry.status == "approved":
                apply_replaces(db, entry.id)
            result.files += 1
            _logger.info("FTN %s: %s arrived by TIC in %s (%s)", network.name, verdict.name, verdict.tag,
                         "waiting for approval" if entry.status == "pending" else "visible")
            if verdict.nodelist_echo:
                _nodelist_feed(db, network, verdict.tag, verdict.name, Path(entry.storage_path))
            return
        if _feed_without_area(db, network, verdict, file_half.data):
            _delete_held_without_commit(db, consumed)
            db.connection.commit()
            result.files += 1
            return
        verdict = Verdict(HOLD, f"no file area carries the nodelist echo {verdict.tag}, and {verdict.name} is not "
                                "its nodelist", verdict.tag)
    if verdict.outcome == DUPLICATE:
        _delete_held_without_commit(db, consumed)
        db.connection.commit()
        result.file_duplicates += 1
        _logger.info("FTN %s: duplicate %s in %s dropped", network.name, verdict.name, verdict.tag)
        return
    _delete_held_without_commit(db, consumed)
    bad_echo = verdict.tag if verdict.outcome == BAD_ECHO else None
    if file_half is not None:
        kept = hold_inbound(db, network_id=network.id, remote_address=remote, file_name=file_half.name,
                            content=file_half.data, reason=verdict.reason, kind=HELD_FILE, tic=tic_half.data,
                            bad_echo=bad_echo, received_at=arrived)
    else:
        # A TIC without its file waits for it, whatever else it is held for,
        # and pairs with it when it comes.
        kept = hold_inbound(db, network_id=network.id, remote_address=remote, file_name=tic_half.name,
                            content=tic_half.data, reason=verdict.reason, kind=HELD_TIC, waiting=True,
                            wants=tic_wants(tic), bad_echo=bad_echo, received_at=arrived)
    if kept:
        result.held += 1
        _logger.warning("FTN %s: %s held: %s", network.name, (file_half or tic_half).name, verdict.reason)
    else:
        db.connection.rollback()  # what was held before stays held
        result.lost += 1
        _logger.error("FTN %s: %s was lost (held store full): %s", network.name, (file_half or tic_half).name,
                      verdict.reason)


def judge_and_settle(
    db: Database, network: FtnNetwork, remote: str, tic_half: Half, tic: Tic, file_half: Half | None,
    description: str | None, result, *, link_checks: bool = True,
) -> Verdict:
    """`judge`, then `settle`, in one go on the lane: what was checked is
    what is stored, whatever happened since an earlier look."""
    verdict = judge(db, network, tic, file_half.data if file_half is not None else None, link_checks=link_checks)
    settle(db, network, remote, tic_half, tic, file_half, verdict, description, result)
    return verdict


def hold_waiting_file(db: Database, network: FtnNetwork, remote: str, name: str, data: bytes, reason: str) -> bool:
    """Hold a file no TIC has named yet, waiting for one."""
    return hold_inbound(db, network_id=network.id, remote_address=remote, file_name=name, content=data,
                        reason=reason, kind=HELD_FILE, waiting=True)


def _delete_held_without_commit(db: Database, ids: list[int]) -> None:
    for held_id in ids:
        db.connection.execute("DELETE FROM ftn_held_inbound WHERE id = ?", (held_id,))


def _area_by_id(db: Database, area_id: int) -> FileArea:
    row = db.connection.execute("SELECT * FROM file_areas WHERE id = ?", (area_id,)).fetchone()
    if row is None:
        raise FtnFormatError("the file area is gone")
    return _row_to_file_area(row)


def uploader_label(network: FtnNetwork, tic: Tic) -> str:
    """What a TIC file shows for its uploader: the network and the TIC's
    Origin address. Never with `@`, which names a Link identity."""
    origin = tic.origin.four_d if tic.origin is not None else "unknown origin"
    name = " ".join(network.name.replace("@", " at ").split())
    return f"{name} ({origin})"


def store_tic_file(
    db: Database, network: FtnNetwork, tic: Tic, verdict: Verdict, data: bytes, description: str | None, *,
    commit: bool = True,
) -> FileEntry:
    """Put a checked file into its area as a TIC file, through the area's
    moderation. `Replaces` waits on the row until the file is visible."""
    return store_ftn_file(
        db, network, area_id=verdict.area_id, name=verdict.name, data=data, description=description,
        label=uploader_label(network, tic), origin=tic.origin.four_d if tic.origin is not None else None,
        crc=tic.crc, tag=verdict.tag, replaces=_usable_replaces(tic, network), commit=commit,
    )


def store_ftn_file(
    db: Database, network: FtnNetwork, *, area_id: int, name: str, data: bytes, description: str | None,
    label: str, origin: str | None, crc: int, tag: str | None = None, replaces: list[str] = (),
    commit: bool = True,
) -> FileEntry:
    """Put a file that came over FTN into file area `area_id`, through the
    area's moderation: pending in a moderated area, visible otherwise. It
    has no local uploader; `label` is what the listing shows instead. A
    TIC file passes its echo `tag` (what `Replaces` may match) and its
    `replaces` values; a netmail's attached file (issue #1470) passes
    neither. `name` must already have passed `tic.name_problem`."""
    area = _area_by_id(db, area_id)
    description = fit_description(description) if description else None
    sha256, path = store_bytes(db, data)
    status = "pending" if area.moderated else "approved"
    created_at = utc_now_iso()
    file_id = compute_content_id({
        "type": "file", "area_id": area.area_id, "filename": name, "sha256": sha256,
        "uploader": f"ftn:{network.domain}:{label}", "created_at": created_at,
    })
    db.connection.execute(
        """
        INSERT INTO files
            (file_id, area_id, filename, description, size_bytes, sha256, storage_path,
             uploader_user_id, uploader_label, uploader_fingerprint, created_at, status,
             ftn_network_id, ftn_file_echo, ftn_origin, ftn_crc32, ftn_replaces)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, area.id, name, description, len(data), sha256, str(path), label, created_at, status,
         network.id, tag, origin, f"{crc:08X}", "\n".join(replaces) or None),
    )
    if commit:
        db.connection.commit()
        reindex_file(db, area.id, file_id)
    return get_file(db, file_id)


def apply_replaces(db: Database, file_row_id: int) -> int:
    """Remove what a visible TIC file's `Replaces` covers: files in the same
    area that arrived by TIC, never itself. Returns how many; each gets a
    node-log line. A file with nothing to replace is left alone."""
    row = db.connection.execute(
        "SELECT * FROM files WHERE id = ?", (file_row_id,)
    ).fetchone()
    if row is None or "ftn_replaces" not in row.keys() or not row["ftn_replaces"]:
        return 0
    patterns = row["ftn_replaces"].split("\n")
    victims = [
        candidate for candidate in db.connection.execute(
            "SELECT id, file_id, filename FROM files WHERE area_id = ? AND id != ? AND ftn_file_echo IS NOT NULL",
            (row["area_id"], row["id"]),
        ).fetchall()
        if any(matches_replaces(pattern, candidate["filename"]) for pattern in patterns)
    ]
    for victim in victims:
        db.connection.execute("DELETE FROM files WHERE id = ?", (victim["id"],))
    db.connection.execute("UPDATE files SET ftn_replaces = NULL WHERE id = ?", (row["id"],))
    db.connection.commit()
    for victim in victims:
        reindex_file(db, row["area_id"], victim["file_id"])
        _logger.info("FTN: %s replaced %s in file area %s", row["filename"], victim["filename"], row["area_id"])
    return len(victims)


def _nodelist_feed(db: Database, network: FtnNetwork, tag: str, name: str, path: Path) -> bool:
    """Hand a passed file to the nodelist import (issue #1464); False when
    it isn't the network's list or diff."""
    try:
        return bool(nodelist_feed.handle_received_file(db, network, tag, name, path))
    except Exception:  # noqa: BLE001 -- a nodelist failure must not lose the file the area stored
        _logger.exception("FTN %s: the nodelist import of %s failed", network.name, name)
        return False


def _feed_without_area(db: Database, network: FtnNetwork, verdict: Verdict, data: bytes) -> bool:
    """A nodelist echo no file area carries: the file goes to the nodelist
    import from a staging copy, which is removed again."""
    if not verdict.nodelist_echo:
        return False
    staging = new_incoming_temp_path(db)
    try:
        staging.write_bytes(data)
        return _nodelist_feed(db, network, verdict.tag, verdict.name, staging)
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - a leftover is purged at the next start
            _logger.warning("could not remove %s: %s", staging, exc)


# --- the held screen's [S]ave and [E]xport (issue #1460) -------------------


@dataclass(frozen=True)
class HeldFile:
    name: str
    data: bytes
    description: str | None


def held_file_for_saving(db: Database, held_id: int) -> HeldFile:
    """A held item's bytes, the name it would be saved under, and the
    description its TIC gives, if it was held with one."""
    item = get_held(db, held_id)
    content = held_content(db, held_id)
    if item is None or content is None:
        raise HeldItemError("That held item is gone.")
    name, description = item.file_name, None
    tic_bytes = held_tic(db, held_id)
    if tic_bytes is not None:
        try:
            tic = parse_tic(tic_bytes)
            name = stored_name(tic)
            description = fit_description(tic.description) if tic.description else None
        except FtnFormatError:
            pass
    if name_problem(name) is not None:
        # A bundle member keeps its own name, which nothing has cleaned:
        # BinkP's safe form of it, so no path is ever built from the raw one.
        name = safe_file_name(name)
    return HeldFile(name, content, description)


def save_held_to_area(
    db: Database, held_id: int, area: FileArea, actor: User, *, name: str, description: str | None,
) -> FileEntry:
    """Store a held item in `area` as `actor`'s own upload, through the
    area's normal path, and drop it from the held store. A name already in
    the area is refused."""
    problem = name_problem(name)
    if problem is not None:
        raise HeldItemError(f"{name!r} can't be a file's name: {problem}.")
    if db.connection.execute(
        "SELECT 1 FROM files WHERE area_id = ? AND filename = ? COLLATE NOCASE AND status IN ('approved', 'pending')",
        (area.id, name),
    ).fetchone() is not None:
        raise HeldItemError(f"A file named {name} is already in {area.name}; nothing was saved.")
    content = held_content(db, held_id)
    if content is None:
        raise HeldItemError("That held item is gone.")
    item = get_held(db, held_id)
    entry = upload_file(db, area, actor, name, content, description=fit_description(description) if description
                        else None)
    # It came over FTN, whoever saved it: never hatched back (issue #1476).
    db.connection.execute("UPDATE files SET ftn_network_id = ?, ftn_origin = ? WHERE id = ?",
                          (item.network_id if item is not None else None,
                           item.remote_address if item is not None else "held", entry.id))
    db.connection.execute("DELETE FROM ftn_held_inbound WHERE id = ?", (held_id,))
    db.connection.commit()
    record_action(db, actor=actor, action="save_ftn_held", object_type="file_area", object_id=area.id,
                  detail=f"saved held {name} ({len(content)} bytes) to {area.name}")
    return entry


def export_held(data: bytes, destination: Path) -> Path:
    """Write `data` to `destination` on the host, by the rules for SysOp
    uploads (§6.2): written beside it and moved into place, never over a
    file that exists, never through a symbolic link. Plain file I/O: run it
    off the event loop."""
    problem = name_problem(destination.name)
    if problem is not None or "\x00" in str(destination):
        raise HeldItemError(f"{destination.name!r} can't be a file's name: {problem or 'it has a NUL byte'}.")
    if destination.is_symlink():
        raise HeldItemError(f"{destination} is a symbolic link; nothing is written through it.")
    if os.path.lexists(destination):
        raise HeldItemError(f"{destination} already exists; nothing was overwritten.")
    parent = destination.parent
    if parent.is_symlink() or not parent.is_dir():
        raise HeldItemError(f"{parent} is not a folder on this machine.")
    partial = parent / f".{destination.name}.export-{secrets.token_hex(4)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(partial, flags, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(partial, destination)  # fails, rather than replaces, when the name was taken meanwhile
        except FileExistsError:
            raise HeldItemError(f"{destination} appeared meanwhile; nothing was overwritten.") from None
        except OSError:
            if os.path.lexists(destination):
                raise HeldItemError(f"{destination} appeared meanwhile; nothing was overwritten.") from None
            os.replace(partial, destination)  # a filesystem without hard links
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover
            _logger.warning("could not remove %s: %s", partial, exc)
    return destination
