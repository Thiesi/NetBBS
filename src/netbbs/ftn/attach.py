"""
Inbound netmail file attaches (design doc §6.8, "File attach"; issue
#1470).

A netmail with FTS-0001's FileAttach attribute names its files in its
subject; the files travel beside it in the same session, or in another.
When such a netmail is delivered here (`netbbs.ftn.tosser`), each name is
remembered against the letter (`record_waits_without_commit`), and the
file that arrives under that name is

- stored in the network's attach area (`FtnNetwork.attach_area_id`)
  through the area's own moderation, as an FTN file whose listing shows the
  sender, and pointed at from the letter with a file ref (§6.4), the way a
  letter points at any file here; or
- held for the SysOp, with the reason, when no attach area is set, the area
  is gone, a file of that name is already in it, or the file is over the
  limit a file echo's file meets (the node's upload limit, at most 64 MiB).

Pairing reuses the stage A held store. A file no TIC and no netmail has
claimed waits there (`fileecho.hold_waiting_file`), so whichever of the
two arrives first, the other finds it: `claim_received` looks for a waiting
name as a file arrives, and `claim_held` looks in the held store as a
netmail is delivered. A name is checked by the rules a TIC's `File` meets
(`tic.name_problem`, after any folder part the sender's system wrote is
cut off). A wait unclaimed after `queue.UNPAIRED_DAYS` is forgotten
(`expire_attach_waits`); the file, if it comes later, is held as any
unclaimed file is.

Security: only a secure session's files ever reach here. A file from an
unsecure session is held unopened before any of this (`exchange`), and is
never marked waiting, so a netmail released from the held store can't
claim it; the SysOp [S]aves it by hand.
"""

from __future__ import annotations

import datetime
import logging

from netbbs.file_refs import MAX_FILE_REFS, mail_refs, ref_for_entry, write_mail_refs_without_commit
from netbbs.files.areas import _row_to_file_area
from netbbs.ftn import FtnFormatError
from netbbs.ftn.fileecho import pair_key, store_ftn_file
from netbbs.ftn.netmail import attach_limit
from netbbs.ftn.networks import FtnNetwork
from netbbs.ftn.queue import HELD_FILE, UNPAIRED_DAYS, held_content, hold_inbound, waiting_halves
from netbbs.ftn.tic import crc32, name_problem
from netbbs.search import reindex_file
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso, utc_now_iso

_logger = logging.getLogger(__name__)

#: The most waits kept at once, over every network: what a hub's netmail
#: can make this node remember.
MAX_WAITS = 200


def attach_names(subject: str) -> list[str]:
    """The files a FileAttach netmail's subject names: each word, without
    the folder the sender's system kept it in, at most `MAX_FILE_REFS`."""
    names: list[str] = []
    for word in subject.split():
        base = word.replace("\\", "/").rsplit("/", 1)[-1]
        if base and base.casefold() not in {name.casefold() for name in names}:
            names.append(base)
    return names[:MAX_FILE_REFS]


def attach_note(network: FtnNetwork, names: list[str], unsafe: list[str]) -> str:
    """The line a FileAttach netmail's letter opens with."""
    files = ", ".join(names)
    if network.attach_area_id is None:
        note = (f"[Files were sent with this netmail: {files}. This BBS keeps no netmail files for "
                f"{network.name}, so they are held for the SysOp.]")
    else:
        note = f"[Files were sent with this netmail: {files}. Each one is listed below once it has arrived.]"
    if unsafe:
        note += f"\n[Not kept, as their names are not safe here: {', '.join(unsafe)}.]"
    return note


def split_names(names: list[str]) -> tuple[list[str], list[str]]:
    """`(safe, unsafe)`: the names a stored file may have, and the rest."""
    safe = [name for name in names if name_problem(name) is None]
    return safe, [name for name in names if name not in safe]


def record_waits_without_commit(
    db: Database, network: FtnNetwork, mail_id: int, names: list[str], *, sender_label: str, origin: str,
) -> None:
    """Remember that letter `mail_id` waits for these files. Past
    `MAX_WAITS` the oldest waits are forgotten first."""
    now = utc_now_iso()
    for name in names:
        db.connection.execute(
            "INSERT INTO ftn_attach_waits (network_id, mail_id, file_key, file_name, sender_label, origin, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (network.id, mail_id, pair_key(name), name, sender_label, origin, now),
        )
    over = db.connection.execute("SELECT COUNT(*) FROM ftn_attach_waits").fetchone()[0] - MAX_WAITS
    if over > 0:
        db.connection.execute(
            "DELETE FROM ftn_attach_waits WHERE id IN (SELECT id FROM ftn_attach_waits ORDER BY id LIMIT ?)", (over,)
        )
        _logger.warning("FTN: %d netmail file wait(s) dropped at the %d-wait cap", over, MAX_WAITS)


def _wait_for(db: Database, network: FtnNetwork, name: str):
    return db.connection.execute(
        "SELECT * FROM ftn_attach_waits WHERE network_id = ? AND file_key = ? ORDER BY id LIMIT 1",
        (network.id, pair_key(name)),
    ).fetchone()


def claim_received(db: Database, network: FtnNetwork, remote: str, name: str, data: bytes, result) -> bool:
    """A file a secure session brought: when a delivered netmail waits for
    its name, store it (or hold it with the reason) and return True;
    False when no netmail waits for it. Counts into `result`."""
    wait = _wait_for(db, network, name)
    if wait is None:
        return False
    _settle(db, network, wait, remote, data, result)
    return True


def claim_held(db: Database, network: FtnNetwork, result) -> None:
    """The netmail came after its files: claim this network's waiting files
    in the held store that a wait names. Run once a netmail's waits are
    committed."""
    for half in waiting_halves(db, network.id, HELD_FILE):
        wait = _wait_for(db, network, half.file_name)
        data = held_content(db, half.id) if wait is not None else None
        if data is None:
            continue
        row = db.connection.execute("SELECT remote_address FROM ftn_held_inbound WHERE id = ?", (half.id,)).fetchone()
        _settle(db, network, wait, row["remote_address"] if row is not None else "?", data, result, held_id=half.id)


def _settle(db: Database, network: FtnNetwork, wait, remote: str, data: bytes, result, *,
            held_id: int | None = None) -> None:
    """Store one attached file for its letter, or hold it with the reason;
    the wait is used up either way. A file already in the held store
    (`held_id`) leaves it when stored, and otherwise stays there, no longer
    waiting, with the new reason -- so a full store can't lose it. What a
    full store can't keep of a file just received is counted lost."""
    name = wait["file_name"]
    reason = _refusal(db, network, name, data)
    if reason is None:
        try:
            entry = store_ftn_file(
                db, network, area_id=network.attach_area_id, name=name, data=data, description=None,
                label=wait["sender_label"], origin=wait["origin"], crc=crc32(data), commit=False,
            )
            area = _area(db, network.attach_area_id)
            if _letter_exists(db, wait["mail_id"]):
                refs = mail_refs(db, wait["mail_id"])
                db.connection.execute("DELETE FROM mail_file_refs WHERE mail_id = ?", (wait["mail_id"],))
                write_mail_refs_without_commit(db, wait["mail_id"], [*refs, ref_for_entry(entry, area)])
            db.connection.execute("DELETE FROM ftn_attach_waits WHERE id = ?", (wait["id"],))
            if held_id is not None:
                db.connection.execute("DELETE FROM ftn_held_inbound WHERE id = ?", (held_id,))
        except FtnFormatError as exc:
            db.connection.rollback()
            reason = f"could not be stored: {exc}"
        else:
            db.connection.commit()
            reindex_file(db, entry.area_id, entry.file_id)
            result.files += 1
            _logger.info("FTN %s: netmail file %s from %s stored in %s (%s)", network.name, name,
                         wait["sender_label"], area.name,
                         "waiting for approval" if entry.status == "pending" else "visible")
            return
    reason = f"sent with a netmail from {wait['sender_label']}: {reason}"
    db.connection.execute("DELETE FROM ftn_attach_waits WHERE id = ?", (wait["id"],))
    if held_id is not None:
        db.connection.execute("UPDATE ftn_held_inbound SET reason = ?, waiting = 0 WHERE id = ?", (reason, held_id))
        db.connection.commit()
        result.held += 1
        _logger.warning("FTN %s: netmail file %s held: %s", network.name, name, reason)
        return
    db.connection.commit()
    if hold_inbound(db, network_id=network.id, remote_address=remote, file_name=name, content=data,
                    reason=reason, kind=HELD_FILE):
        result.held += 1
        _logger.warning("FTN %s: netmail file %s held: %s", network.name, name, reason)
    else:
        result.lost += 1
        _logger.error("FTN %s: netmail file %s was lost (held store full): %s", network.name, name, reason)


def _area(db: Database, area_id: int | None):
    if area_id is None:
        return None
    row = db.connection.execute("SELECT * FROM file_areas WHERE id = ?", (area_id,)).fetchone()
    # A hidden area (issue #683) is as good as gone: no reader could open the file.
    return _row_to_file_area(row) if row is not None and row["link_hidden_at"] is None else None


def _letter_exists(db: Database, mail_id: int) -> bool:
    return db.connection.execute("SELECT 1 FROM mail_messages WHERE id = ?", (mail_id,)).fetchone() is not None


def attach_area_problem(db: Database, area_id: int | None) -> str | None:
    """Why file area `area_id` can't take netmail files, or None (and None
    for no area). A Linked area can't: an FTN file never becomes a Link
    file (§16 "Issue #166", Decision 3, extended in "Issue #1154")."""
    from netbbs.link.files import is_area_linked  # netbbs.link imports widely; keep it local

    if area_id is None:
        return None
    area = _area(db, area_id)
    if area is None:
        return "That file area is gone or hidden."
    if is_area_linked(db, area):
        return f"File area {area.name!r} is Linked; netmail files go in a local area."
    return None


def _refusal(db: Database, network: FtnNetwork, name: str, data: bytes) -> str | None:
    """Why an attached file can't be stored, or None."""
    area = _area(db, network.attach_area_id)
    if area is None:
        return (f"no file area takes {network.name}'s netmail files (set Attach area in the network editor; "
                "an area that was removed or hidden counts as none)")
    problem = attach_area_problem(db, area.id)
    if problem is not None:
        return problem.rstrip(".")
    problem = name_problem(name)
    if problem is not None:
        return f"{name!r} can't be a file's name: {problem}"
    limit = attach_limit(db)
    if len(data) > limit:
        return f"it is {len(data)} bytes, over this node's limit of {limit}"
    if db.connection.execute(
        "SELECT 1 FROM files WHERE area_id = ? AND filename = ? COLLATE NOCASE AND status IN ('approved', 'pending')",
        (area.id, name),
    ).fetchone() is not None:
        return f"a file named {name} is already in {area.name}"
    return None


def expire_attach_waits(db: Database, *, now: datetime.datetime | None = None) -> int:
    """Forget waits older than `UNPAIRED_DAYS`, as unpaired halves are;
    returns how many."""
    cutoff = utc_iso((now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=UNPAIRED_DAYS))
    cursor = db.connection.execute("DELETE FROM ftn_attach_waits WHERE created_at < ?", (cutoff,))
    db.connection.commit()
    if cursor.rowcount:
        _logger.warning("FTN: %d netmail file(s) never arrived within %d days", cursor.rowcount, UNPAIRED_DAYS)
    return cursor.rowcount
