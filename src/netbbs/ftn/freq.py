"""
FTN file requests, made and answered (design doc §6.8, "File requests";
issue #1475, §16 "Issue #1154", Decision 13).

A request is a WaZOO `.REQ` file (FTS-0006): one line per file, a name or
a pattern with `*` and `?`, optionally followed by ` !password` and by an
update word (`+time` or `-time`, Unix seconds). It travels as an ordinary
file in a BinkP session, under a name FTS-5005 gives it: the node's net and
node numbers as eight hex digits, `.REQ`.

**Making one.** `queue_request` writes the `.REQ` into the node's spool
folder and queues it on `netbbs.ftn.outbound`'s files queue, crash, to the
uplink or direct to a node the nodelist lists as answering BinkP: a request
goes at once, and never via the uplink to someone else. The file is
deleted once the remote confirmed it. What comes back, in the same
session's later batch or in a later session, is received like any other
file and held (`reply_reason`): the SysOp [S]aves it to a file area from
Held items, as a held file-echo file is saved. A requested file is held
without expiry, unlike a file waiting for its TIC.

**Answering.** Off by default, per network (`FreqSettings`). When on, a
`.REQ` received in a binkp/1.1 multi-batch session is answered in the
session's next batch, through the sessions' batch hook (`FreqAnswerer`).
A binkp/1.0 session has no next batch, and its request is not answered and
not queued for later: it is logged as refused (`take_requests`). Served:

- the files of the file areas the SysOp offers (`set_area_offered`) that
  anyone may read (`offer_problem`): no read level, age gate, name
  requirement or hidden Community; not Linked (Link content never enters
  FTN); carrying no other network's file echo; approved and not expired;
- the magic names: `FILES`, a listing of those files, generated; `ABOUT`,
  a file the SysOp names; `NODELIST`, a file the SysOp names, else the
  network's nodelist as this node imported it.

A request line naming a folder, a drive, `..` or control characters is
refused, and so is a pattern of wildcards alone (the listing is `FILES`).
Wildcards match only the files above, so nothing restricted is ever
enumerated. A password is never stored, logged or shown; nothing here asks
for one, so a line carrying one is served like any other. An update word
keeps the files newer than (`+`) or at most as new as (`-`) its time.

Bounds: who may request (secure links only, by default, or anyone); per
session, files and bytes; per requesting node per day, files and bytes;
per network per day, `NETWORK_DAY_FILES` and `NETWORK_DAY_BYTES` whatever
the settings, since an unproven caller can claim any address. A `.REQ` is
read up to `MAX_REQ_BYTES` and `MAX_REQ_LINES` lines.

**Logging.** Every request, answered or refused, is a node-log line and a
row in `ftn_freq_requests`, the status screen's recent requests, kept
`LOG_DAYS`.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from netbbs.files.areas import FileArea, list_file_areas
from netbbs.ftn.address import FtnAddress, parse_address
from netbbs.ftn.binkp import MAX_FILE_BYTES, BatchContext, OutgoingFile, SessionResult, safe_file_name
from netbbs.ftn.networks import FtnNetwork
from netbbs.ftn.tic import matches_replaces
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso, utc_now_iso

_logger = logging.getLogger(__name__)

REQ_SUFFIX = ".REQ"
MAX_REQ_BYTES = 16 * 1024
MAX_REQ_LINES = 50
MAX_REQUEST_NAMES = 20  # what one request made here names
MAX_NAME = 64
REQUEST_DAYS = 14  # how long a request made here waits for its files
LOG_DAYS = 30
MAX_LOG_ROWS = 2000
RECENT_SHOWN = 5
FILES_NAME = "FILES"
ABOUT_NAME = "ABOUT"
NODELIST_NAME = "NODELIST"
MAGIC_NAMES = (FILES_NAME, ABOUT_NAME, NODELIST_NAME)
LISTING_NAME = "FILES.TXT"
MAX_LISTING_BYTES = 4 * 1024 * 1024
NETWORK_DAY_FILES = 1000
NETWORK_DAY_BYTES = 1024 * 1024 * 1024
MIB = 1024 * 1024
# What the settings may be set to.
SESSION_FILES_RANGE = (1, 100)
SESSION_MIB_RANGE = (1, 256)
DAY_FILES_RANGE = (1, 1000)
DAY_MIB_RANGE = (1, 1024)
_UPDATE = re.compile(r"[+-]\d{1,12}")
_REQUEST_REFERENCE = "freq"
_HOOK_STATE_KEY = "freq"


class FreqError(ValueError):
    """A request or a setting the SysOp can't make, with the reason."""


def is_req_name(name: str) -> bool:
    return name.upper().endswith(REQ_SUFFIX)


def req_send_name(node: FtnAddress) -> str:
    """FTS-5005's name for a request to `node`: net and node in hex."""
    return f"{node.net:04X}{node.node:04X}{REQ_SUFFIX}"


# --- settings ----------------------------------------------------------------


@dataclass(frozen=True)
class FreqSettings:
    network_id: int
    answer: bool = False
    secure_only: bool = True
    session_files: int = 10
    session_bytes: int = 50 * MIB
    day_files: int = 25
    day_bytes: int = 100 * MIB
    about_path: str = ""
    serve_nodelist: bool = True
    nodelist_path: str = ""  # empty: the network's nodelist as imported


def get_settings(db: Database, network_id: int) -> FreqSettings:
    row = db.connection.execute("SELECT * FROM ftn_freq_settings WHERE network_id = ?", (network_id,)).fetchone()
    if row is None:
        return FreqSettings(network_id)
    return FreqSettings(
        network_id=network_id, answer=bool(row["answer"]), secure_only=bool(row["secure_only"]),
        session_files=row["session_files"], session_bytes=row["session_bytes"], day_files=row["day_files"],
        day_bytes=row["day_bytes"], about_path=row["about_path"], serve_nodelist=bool(row["serve_nodelist"]),
        nodelist_path=row["nodelist_path"],
    )


def _within(value: int, bounds: tuple[int, int], label: str) -> None:
    if not bounds[0] <= value <= bounds[1]:
        raise FreqError(f"{label} must be {bounds[0]}-{bounds[1]}.")


def validate_settings(settings: FreqSettings) -> FreqSettings:
    """The settings as stored; raises `FreqError` with the reason. A path
    is checked to be a file only while answering is on."""
    _within(settings.session_files, SESSION_FILES_RANGE, "Files per session")
    _within(settings.session_bytes // MIB, SESSION_MIB_RANGE, "MiB per session")
    _within(settings.day_files, DAY_FILES_RANGE, "Files per node a day")
    _within(settings.day_bytes // MIB, DAY_MIB_RANGE, "MiB per node a day")
    about, nodelist = settings.about_path.strip(), settings.nodelist_path.strip()
    if settings.answer:
        for path, label in ((about, "ABOUT file"), (nodelist, "NODELIST file")):
            if path and _regular_file(path) is None:
                raise FreqError(f"{label}: {path} is not a file on this machine (a symbolic link is not served).")
    return FreqSettings(
        network_id=settings.network_id, answer=settings.answer, secure_only=settings.secure_only,
        session_files=settings.session_files, session_bytes=settings.session_bytes, day_files=settings.day_files,
        day_bytes=settings.day_bytes, about_path=about, serve_nodelist=settings.serve_nodelist,
        nodelist_path=nodelist,
    )


def save_settings_without_commit(db: Database, settings: FreqSettings) -> FreqSettings:
    settings = validate_settings(settings)
    db.connection.execute(
        "INSERT INTO ftn_freq_settings (network_id, answer, secure_only, session_files, session_bytes, day_files, "
        "day_bytes, about_path, serve_nodelist, nodelist_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (network_id) DO UPDATE SET answer = excluded.answer, secure_only = excluded.secure_only, "
        "session_files = excluded.session_files, session_bytes = excluded.session_bytes, "
        "day_files = excluded.day_files, day_bytes = excluded.day_bytes, about_path = excluded.about_path, "
        "serve_nodelist = excluded.serve_nodelist, nodelist_path = excluded.nodelist_path",
        (settings.network_id, int(settings.answer), int(settings.secure_only), settings.session_files,
         settings.session_bytes, settings.day_files, settings.day_bytes, settings.about_path,
         int(settings.serve_nodelist), settings.nodelist_path),
    )
    return settings


# --- the areas offered ---------------------------------------------------------


def offer_problem(db: Database, area: FileArea) -> str | None:
    """Why `area` can't be served to file requests, or None: only an area
    any caller could read is, since an FTN node has no account here."""
    from netbbs.communities import (
        get_community,
        get_effective_min_age,
        get_effective_min_read_level,
        get_effective_name_requirement,
    )
    from netbbs.link.files import is_area_linked

    if is_area_linked(db, area):
        return "it is Linked, and Link content never goes to FTN"
    if get_effective_min_read_level(db, area) > 0:
        return "it has a read level"
    if get_effective_min_age(db, area) is not None:
        return "it has an age requirement"
    if get_effective_name_requirement(db, area) is not None:
        return "it requires a verified name"
    community = get_community(db, area.community_id)
    if community is not None and community.hidden:
        return "its Community is hidden"
    return None


def is_area_offered(db: Database, area: FileArea) -> bool:
    row = db.connection.execute("SELECT 1 FROM ftn_freq_areas WHERE area_id = ?", (area.area_id,)).fetchone()
    return row is not None


def set_area_offered(db: Database, area: FileArea, offered: bool) -> None:
    """Offer `area` to file requests, or stop; raises `FreqError` when it
    can't be offered."""
    if offered:
        problem = offer_problem(db, area)
        if problem is not None:
            raise FreqError(f"{area.name} can't be offered to file requests: {problem}.")
        db.connection.execute("INSERT OR IGNORE INTO ftn_freq_areas (area_id, offered_at) VALUES (?, ?)",
                              (area.area_id, utc_now_iso()))
    else:
        db.connection.execute("DELETE FROM ftn_freq_areas WHERE area_id = ?", (area.area_id,))
    db.connection.commit()


def servable_areas(db: Database, network: FtnNetwork) -> list[FileArea]:
    """The areas a request on `network` is served from: offered, readable by
    anyone, and carrying no other network's file echo -- checked now, so an
    area that gained a read level since it was offered is not served."""
    offered = {row["area_id"] for row in db.connection.execute("SELECT area_id FROM ftn_freq_areas")}
    areas = []
    for area in list_file_areas(db):
        if area.area_id not in offered or offer_problem(db, area) is not None:
            continue
        row = db.connection.execute("SELECT ftn_network_id FROM file_areas WHERE id = ?", (area.id,)).fetchone()
        if row is not None and row["ftn_network_id"] is not None and row["ftn_network_id"] != network.id:
            continue
        areas.append(area)
    return areas


@dataclass(frozen=True)
class _Servable:
    area: str
    name: str
    size: int
    created_at: str
    description: str | None
    path: str


def _servable_files(db: Database, areas: list[FileArea]) -> list[_Servable]:
    now = utc_now_iso()
    files = []
    for area in areas:
        rows = db.connection.execute(
            "SELECT f.filename, f.size_bytes, f.created_at, f.description, f.storage_path FROM files f "
            "JOIN file_areas a ON a.id = f.area_id WHERE f.area_id = ? AND f.status = 'approved' AND ("
            "f.exempt_from_expiry = 1 OR a.max_file_age_days IS NULL "
            "OR julianday(f.created_at) >= julianday(?) - a.max_file_age_days) "
            "ORDER BY f.filename COLLATE NOCASE",
            (area.id, now),
        ).fetchall()
        files.extend(_Servable(area.name, row["filename"], row["size_bytes"], row["created_at"], row["description"],
                               row["storage_path"]) for row in rows)
    return files


# --- request lines -------------------------------------------------------------


@dataclass(frozen=True)
class RequestLine:
    name: str
    password: str = field(default="", repr=False)
    update: str = ""  # "+<unix time>" or "-<unix time>"

    def as_req_line(self) -> str:
        parts = [self.name]
        if self.password:
            parts.append("!" + self.password)
        if self.update:
            parts.append(self.update)
        return " ".join(parts)


def line_problem(name: str) -> str | None:
    """Why a requested name is refused, or None (§6.8)."""
    if not name or len(name) > MAX_NAME:
        return f"a name is 1-{MAX_NAME} characters"
    if any(not 0x21 <= ord(c) <= 0x7E for c in name):
        return "it has spaces or characters that are not printable ASCII"
    if any(part in name for part in ("/", "\\", ":", "..")):
        return "it names a folder or a drive"
    if name.startswith("."):
        return "it starts with a dot"
    if not any(c.isalnum() for c in name):
        return "a pattern of wildcards alone names nothing; request FILES for the list"
    return None


def parse_req(data: bytes) -> tuple[list[RequestLine], int]:
    """A `.REQ`'s lines, and how many were cut by the bounds. Words after
    the name: `!password`, an update word, and anything else ignored."""
    cut = 0
    if len(data) > MAX_REQ_BYTES:
        data = data[:MAX_REQ_BYTES]
        cut += 1
    lines = []
    for raw in re.split(rb"\r\n|\r|\n", data.split(b"\x1a", 1)[0]):
        words = raw.decode("latin-1").split()
        if not words or words[0].startswith(";"):
            continue
        if len(lines) >= MAX_REQ_LINES:
            cut += 1
            continue
        password = update = ""
        for word in words[1:]:
            if word.startswith("!") and not password:
                password = word[1:]
            elif _UPDATE.fullmatch(word) and not update:
                update = word
        lines.append(RequestLine(words[0], password, update))
    return lines, cut


def format_req(lines: list[RequestLine]) -> bytes:
    return "".join(line.as_req_line() + "\r\n" for line in lines).encode("latin-1")


def request_lines_from_text(text: str) -> list[RequestLine]:
    """What the SysOp typed: names or magic names separated by spaces, each
    optionally followed by `!password`. Raises `FreqError`."""
    lines: list[RequestLine] = []
    for word in text.split():
        if word.startswith("!"):
            if not lines or lines[-1].password:
                raise FreqError("A !password follows the file name it is for.")
            if len(word) < 2 or any(not 0x21 <= ord(c) <= 0x7E for c in word):
                raise FreqError("A password is printable ASCII without spaces.")
            last = lines.pop()
            lines.append(RequestLine(last.name, word[1:], last.update))
            continue
        problem = line_problem(word)
        if problem is not None:
            raise FreqError(f"{word[:40]!r} can't be requested: {problem}.")
        lines.append(RequestLine(word.upper() if word.upper() in MAGIC_NAMES else word))
    if not lines:
        raise FreqError("Name at least one file, or FILES for the list.")
    if len(lines) > MAX_REQUEST_NAMES:
        raise FreqError(f"One request names at most {MAX_REQUEST_NAMES} files.")
    return lines


def _shown_names(lines: list[RequestLine]) -> str:
    """The names for the log and the screen: never a password, only
    printable ASCII, bounded."""
    names = " ".join("".join(c if 0x21 <= ord(c) <= 0x7E else "?" for c in line.name[:MAX_NAME]) + (
        f" {line.update}" if line.update else "") for line in lines)
    return names[:500] or "(nothing)"


# --- the log -------------------------------------------------------------------


@dataclass(frozen=True)
class FreqRecord:
    id: int
    network_id: int
    direction: str  # "in" or "out"
    node: str
    names: str
    secure: bool
    send_name: str | None
    outcome: str
    files: int
    bytes: int
    created_at: str


def _record_from_row(row) -> FreqRecord:
    return FreqRecord(row["id"], row["network_id"], row["direction"], row["node"], row["names"], bool(row["secure"]),
                      row["send_name"], row["outcome"], row["files"], row["bytes"], row["created_at"])


def _record_without_commit(db: Database, network_id: int, direction: str, node: str, names: str, *,
                           secure: bool = False, send_name: str | None = None, outcome: str = "",
                           files: int = 0, size: int = 0) -> int:
    cutoff = utc_iso(_utcnow() - datetime.timedelta(days=LOG_DAYS))
    db.connection.execute("DELETE FROM ftn_freq_requests WHERE created_at < ?", (cutoff,))
    db.connection.execute(
        "DELETE FROM ftn_freq_requests WHERE id NOT IN (SELECT id FROM ftn_freq_requests ORDER BY id DESC LIMIT ?)",
        (MAX_LOG_ROWS - 1,),
    )
    cursor = db.connection.execute(
        "INSERT INTO ftn_freq_requests (network_id, direction, node, names, secure, send_name, outcome, files, bytes, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (network_id, direction, node, names, int(secure), send_name, outcome, files, size, utc_now_iso()),
    )
    return cursor.lastrowid


def recent_requests(db: Database, network_id: int, limit: int = RECENT_SHOWN) -> list[FreqRecord]:
    rows = db.connection.execute(
        "SELECT * FROM ftn_freq_requests WHERE network_id = ? ORDER BY id DESC LIMIT ?", (network_id, limit)
    ).fetchall()
    return [_record_from_row(row) for row in rows]


def request_state(db: Database, record: FreqRecord) -> str:
    """How a request made here stands, for the status screen."""
    if record.outcome:
        return record.outcome
    waiting = db.connection.execute(
        "SELECT 1 FROM ftn_outbound_files WHERE reference_kind = ? AND reference_id = ?",
        (_REQUEST_REFERENCE, str(record.id)),
    ).fetchone()
    got = f"; {record.files} files back ({_size(record.bytes)}), in Held items" if record.files else ""
    return ("waiting for the next call" if waiting else "sent") + got


def note_request_dropped(db: Database, reference_id: str, reason: str) -> None:
    """A queued request the outbound queue dropped (a crash call that kept
    failing, say): its record says why. Called by
    `netbbs.ftn.outbound.report_undelivered`."""
    if reference_id.isdigit():
        reason = re.sub(r"^its file \S+ (?:was not sent|could not be sent): ", "", reason)
        db.connection.execute("UPDATE ftn_freq_requests SET outcome = ? WHERE id = ? AND direction = 'out'",
                              (f"not sent: {reason}"[:300], int(reference_id)))
        db.connection.commit()


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < MIB:
        return f"{size / 1024:.1f} KiB"
    return f"{size / MIB:.1f} MiB"


# --- making a request ------------------------------------------------------------


def request_target_problem(db: Database, network: FtnNetwork, node: FtnAddress) -> str | None:
    """Why this node can't be sent a request, or None: the uplink, or a node
    the nodelist lists as answering BinkP."""
    from netbbs.ftn.nodelist import direct_route

    if node.same_node(network.our_address):
        return "that is this BBS's own address"
    if node.same_node(network.uplink_address):
        return None
    if node.point:
        return "a point is reached through its boss node; request from the boss, or via the uplink"
    if direct_route(db, network.id, node) is None:
        return (f"the {network.name} nodelist does not list {node.four_d} as answering BinkP; "
                f"request from the uplink ({network.uplink_address.four_d}) or a listed node")
    return None


def queue_request(db: Database, network: FtnNetwork, node: FtnAddress, lines: list[RequestLine]) -> FreqRecord:
    """Write the `.REQ` and queue it, crash, for `node`; returns the record.
    Raises `FreqError`, or `netbbs.ftn.queue.FtnQueueFullError`."""
    from netbbs.ftn.outbound import CRASH, KILL, queue_file_without_commit, spool_dir

    problem = request_target_problem(db, network, node)
    if problem is not None:
        raise FreqError(f"{problem[:1].upper()}{problem[1:]}.")
    uplink = node.same_node(network.uplink_address)
    # FTS-5005's name, unless a request for the node already waits under it.
    send_name = req_send_name(node)
    taken = db.connection.execute(
        "SELECT 1 FROM ftn_outbound_files WHERE network_id = ? AND reference_kind = ? AND UPPER(send_name) = ?",
        (network.id, _REQUEST_REFERENCE, send_name),
    ).fetchone()
    if taken:
        send_name = f"{int.from_bytes(os.urandom(4), 'big'):08X}{REQ_SUFFIX}"
    content = format_req(lines)
    folder = spool_dir(db) / "freq"
    folder.mkdir(parents=True, exist_ok=True)
    _sweep_spool(db, folder)
    path = folder / f"{network.id}-{os.urandom(6).hex()}-{send_name}"
    path.write_bytes(content)
    try:
        record_id = _record_without_commit(db, network.id, "out", node.four_d, _shown_names(lines), secure=uplink,
                                           send_name=send_name)
        queue_file_without_commit(
            db, network.id, destination=node, route="uplink" if uplink else "direct", flavour=CRASH,
            send_name=send_name, size_bytes=len(content), reference_kind=_REQUEST_REFERENCE,
            reference_id=str(record_id), path=path, after_sent=KILL)
        db.connection.commit()
    except BaseException:
        db.connection.rollback()
        path.unlink(missing_ok=True)
        raise
    _logger.info("FTN %s: file request to %s queued: %s", network.name, node.four_d, _shown_names(lines))
    row = db.connection.execute("SELECT * FROM ftn_freq_requests WHERE id = ?", (record_id,)).fetchone()
    return _record_from_row(row)


def _sweep_spool(db: Database, folder: Path) -> None:
    """Delete the spooled `.REQ` files no queued row points at any more: a
    request the queue dropped (its crash calls kept failing, say)."""
    queued = {row["path"] for row in db.connection.execute(
        "SELECT path FROM ftn_outbound_files WHERE reference_kind = ? AND path IS NOT NULL", (_REQUEST_REFERENCE,))}
    for path in folder.iterdir():
        if str(path) not in queued and path.is_file() and not path.is_symlink():
            path.unlink(missing_ok=True)


# --- what comes back ---------------------------------------------------------------


def _magic_prefixes(network: FtnNetwork, name: str) -> tuple[str, ...]:
    """The file names a magic name's answer usually goes by."""
    if name == FILES_NAME:
        return ("FILES", "ALLFILES", "FILELIST")
    if name == NODELIST_NAME:
        return tuple({"NODELIST", network.nodelist_name.upper() or "NODELIST"})
    return (name,)


def _answers(network: FtnNetwork, names: str, received: str) -> bool:
    for word in names.split():
        if word[:1] in "+-" and word[1:].isdigit():
            continue
        if word.upper() in MAGIC_NAMES:
            if received.upper().startswith(_magic_prefixes(network, word.upper())):
                return True
        elif matches_replaces(word, received):
            return True
    return False


def reply_reason(db: Database, network: FtnNetwork, result: SessionResult, name: str, size: int) -> str | None:
    """Whether a received file that would otherwise wait for a TIC answers a
    request made here, and the held reason when it does: the session
    carried a request of ours to this remote (its reply comes in a later
    batch), or the name matches an open request to it, within
    `REQUEST_DAYS`. Counts the file on the request's record."""
    cutoff = utc_iso(_utcnow() - datetime.timedelta(days=REQUEST_DAYS))
    rows = db.connection.execute(
        "SELECT * FROM ftn_freq_requests WHERE network_id = ? AND direction = 'out' AND created_at >= ? "
        "ORDER BY id DESC", (network.id, cutoff),
    ).fetchall()
    sent = {item.upper() for item in result.sent}
    for row in rows:
        record = _record_from_row(row)
        try:
            node = parse_address(record.node)
        except Exception:  # noqa: BLE001 -- a record nothing can parse answers nothing
            continue
        if not any(address.same_node(node) for address in result.remote_addresses):
            continue
        in_session = record.send_name is not None and record.send_name.upper() in sent
        if in_session or _answers(network, record.names, name):
            db.connection.execute("UPDATE ftn_freq_requests SET files = files + 1, bytes = bytes + ? WHERE id = ?",
                                  (size, record.id))
            db.connection.commit()
            return f"requested from {record.node} (file request of {record.created_at[:16]})"
    return None


# --- answering -------------------------------------------------------------------------


class _OnDemand:
    """A served file's bytes as BinkP's sender reads them, opened for each
    slice, so nothing stays open or in memory after the session."""

    def __init__(self, path: Path, size: int):
        self._path = path
        self._size = size

    def __len__(self) -> int:
        return self._size

    def __getitem__(self, key: slice) -> bytes:
        start, stop, _step = key.indices(self._size)
        if stop <= start:
            return b""
        with open(self._path, "rb") as handle:
            handle.seek(start)
            return handle.read(stop - start)


@dataclass
class _Tally:
    """One session's answers so far: what bounds the next."""

    files: int = 0
    bytes: int = 0
    handled: set[int] = field(default_factory=set)  # id() of the .REQ files answered or refused
    names: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _Planned:
    name: str
    size: int
    mtime: int
    path: str | None = None
    data: bytes | None = None


def _tally(result: SessionResult) -> _Tally:
    return result.hook_state.setdefault(_HOOK_STATE_KEY, _Tally())


def _regular_file(path_text: str) -> Path | None:
    path = Path(os.path.expanduser(path_text))
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path
    except OSError:
        return None


def _usage(db: Database, network_id: int, node: str | None) -> tuple[int, int]:
    cutoff = utc_iso(_utcnow() - datetime.timedelta(days=1))
    clause, params = ("", ()) if node is None else (" AND node = ?", (node,))
    row = db.connection.execute(
        "SELECT COALESCE(SUM(files), 0) AS files, COALESCE(SUM(bytes), 0) AS bytes FROM ftn_freq_requests "
        f"WHERE network_id = ? AND direction = 'in' AND created_at >= ?{clause}",
        (network_id, cutoff, *params),
    ).fetchone()
    return row["files"], row["bytes"]


def _listing(db: Database, network: FtnNetwork, files: list[_Servable]) -> bytes:
    """`FILES`: the files a request is served from, area by area, as CP437
    text with CR LF line ends."""
    from netbbs.config import get_node_display_name

    def clean(text: str) -> str:
        return "".join(c if c.isprintable() else " " for c in text)

    lines = [f"Files available by file request from {clean(get_node_display_name(db))} "
             f"({network.our_address.four_d}, {network.name})",
             f"Made {utc_now_iso()[:16].replace('T', ' ')} UTC. Request a file by its name.", ""]
    current = None
    for item in files:
        if item.area != current:
            current = item.area
            lines += ["", clean(item.area), "-" * min(len(clean(item.area)), 79)]
        description = clean((item.description or "").split("\n", 1)[0])[:40]
        lines.append(f"{safe_file_name(item.name):<24} {_size(item.size):>10} {item.created_at[:10]}  {description}")
    if not files:
        lines.append("No files are offered to file requests at the moment.")
    data = "\r\n".join(lines).encode("cp437", "replace") + b"\r\n"
    if len(data) > MAX_LISTING_BYTES:
        note = b"\r\n(The list is cut here: it is longer than this BBS sends.)\r\n"
        data = data[:MAX_LISTING_BYTES - len(note)].rsplit(b"\r\n", 1)[0] + note
    return data


def _magic(db: Database, network: FtnNetwork, settings: FreqSettings, name: str,
           files: list[_Servable]) -> _Planned | None:
    now = int(_utcnow().timestamp())
    if name == FILES_NAME:
        data = _listing(db, network, files)
        return _Planned(LISTING_NAME, len(data), now, data=data)
    if name == ABOUT_NAME:
        path = _regular_file(settings.about_path) if settings.about_path else None
    elif settings.nodelist_path:
        path = _regular_file(settings.nodelist_path) if settings.serve_nodelist else None
    else:
        path = None
        if settings.serve_nodelist:
            from netbbs.ftn.nodelist import nodelist_status

            row = db.connection.execute("SELECT content FROM ftn_nodelist_raw WHERE network_id = ?",
                                        (network.id,)).fetchone()
            if row is None:
                return None
            status = nodelist_status(db, network.id)
            day = f"{status.day:03d}" if status is not None and status.day is not None else "TXT"
            data = bytes(row["content"])
            return _Planned(f"{network.nodelist_name or NODELIST_NAME}.{day}", len(data), now, data=data)
    if path is None:
        return None
    stat = path.stat()
    return _Planned(safe_file_name(path.name), stat.st_size, int(stat.st_mtime), path=str(path))


def _update_keeps(update: str, created_at: str) -> bool:
    if not update:
        return True
    try:
        when = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return True
    limit = int(update[1:])
    return when > limit if update[0] == "+" else when <= limit


def answer_request(db: Database, network: FtnNetwork, requester: FtnAddress | None, secure: bool, data: bytes,
                   tally: _Tally) -> list[_Planned]:
    """Answer one received `.REQ` within the limits; record it and log it.
    Returns the files to send."""
    node = requester.four_d if requester is not None else "?"
    lines, cut = parse_req(data)
    names = _shown_names(lines)
    settings = get_settings(db, network.id)
    refusal = None
    if not settings.answer:
        refusal = f"{network.name} answers no file requests here"
    elif settings.secure_only and not secure:
        refusal = "only secure links may request files"
    if refusal is not None:
        return _finish(db, network, node, names, secure, [], f"refused: {refusal}")
    files = _servable_files(db, servable_areas(db, network))
    day_files, day_bytes = _usage(db, network.id, node)
    net_files, net_bytes = _usage(db, network.id, None)
    planned: list[_Planned] = []
    missing: list[str] = []
    refused: list[str] = []
    over: list[str] = []

    def room(size: int) -> bool:
        files_now = tally.files + len(planned)
        bytes_now = tally.bytes + sum(p.size for p in planned)
        return (files_now < settings.session_files and bytes_now + size <= settings.session_bytes
                and day_files + len(planned) < settings.day_files and day_bytes + sum(p.size for p in planned)
                + size <= settings.day_bytes and net_files + len(planned) < NETWORK_DAY_FILES
                and net_bytes + sum(p.size for p in planned) + size <= NETWORK_DAY_BYTES
                and size <= MAX_FILE_BYTES)

    for line in lines:
        shown = _shown_names([line])
        upper = line.name.upper()
        if upper in MAGIC_NAMES:
            candidates = [found] if (found := _magic(db, network, settings, upper, files)) is not None else []
        else:
            problem = line_problem(line.name)
            if problem is not None:
                refused.append(shown)
                continue
            candidates = [
                _Planned(safe_file_name(item.name), item.size, 0, path=item.path)
                for item in files
                if (matches_replaces(line.name, item.name) or matches_replaces(line.name, safe_file_name(item.name)))
                and _update_keeps(line.update, item.created_at)
            ]
        if not candidates:
            missing.append(shown)
            continue
        hit = False
        for candidate in candidates:
            key = candidate.name.casefold()
            if key in tally.names or any(p.name.casefold() == key for p in planned):
                hit = True
                continue  # already sent in this session
            if candidate.path is not None:
                path = _regular_file(candidate.path)
                if path is None:
                    continue
                stat = path.stat()
                candidate = _Planned(candidate.name, stat.st_size, int(stat.st_mtime), path=str(path))
            if not room(candidate.size):
                if shown not in over:
                    over.append(shown)
                continue
            planned.append(candidate)
            hit = True
        if not hit and shown not in over:
            missing.append(shown)
    parts = [f"sent {len(planned)} files ({_size(sum(p.size for p in planned))})"]
    if missing:
        parts.append("not found: " + " ".join(missing))
    if over:
        parts.append("over the limits: " + " ".join(over))
    if refused:
        parts.append("refused (folder, drive or wildcards alone): " + " ".join(refused))
    if cut:
        parts.append(f"the rest of the request ignored (over {MAX_REQ_LINES} lines or {MAX_REQ_BYTES} bytes)")
    tally.files += len(planned)
    tally.bytes += sum(p.size for p in planned)
    tally.names.update(p.name.casefold() for p in planned)
    return _finish(db, network, node, names, secure, planned, "; ".join(parts))


def _finish(db: Database, network: FtnNetwork, node: str, names: str, secure: bool, planned: list[_Planned],
            outcome: str) -> list[_Planned]:
    _record_without_commit(db, network.id, "in", node, names, secure=secure, outcome=outcome[:1000],
                           files=len(planned), size=sum(p.size for p in planned))
    db.connection.commit()
    _logger.info("FTN %s: file request from %s (%s): %s -- %s", network.name, node,
                 "secure" if secure else "non-secure", names, outcome)
    return planned


def _outgoing(planned: list[_Planned]) -> list[OutgoingFile]:
    files = []
    for item in planned:
        data = item.data if item.data is not None else _OnDemand(Path(item.path), item.size)
        files.append(OutgoingFile(item.name, data, item.mtime))  # type: ignore[arg-type]
    return files


class FreqAnswerer:
    """The sessions' batch hook (`FtnMailer.batch_hook`,
    `FtnListener.batch_hook`): answers each `.REQ` received since the last
    batch with files for the next one."""

    def __init__(self, lane):
        self._lane = lane

    async def __call__(self, network: FtnNetwork | None, context: BatchContext) -> list[OutgoingFile]:
        requests = [received for received in context.received if is_req_name(received.name)]
        if network is None or not requests:
            return []
        tally = _tally(context.result)
        requester = context.result.remote_addresses[0] if context.result.remote_addresses else None
        planned: list[_Planned] = []
        for received in requests:
            planned += await self._lane.run(answer_request, network, requester, context.result.secure,
                                            received.data, tally)
            tally.handled.add(id(received))
        return _outgoing(planned)


async def take_requests(lane, network: FtnNetwork, result: SessionResult) -> SessionResult:
    """`result` without the `.REQ` files it received: each was answered by
    the batch hook, or is refused here -- a binkp/1.0 session has no batch
    to answer in, and nothing is queued for later. A request is never
    held."""
    tally = result.hook_state.get(_HOOK_STATE_KEY)
    handled = tally.handled if isinstance(tally, _Tally) else set()
    kept = []
    for received in result.received:
        if not is_req_name(received.name):
            kept.append(received)
            continue
        if id(received) in handled:
            continue
        reason = ("refused: the session was binkp/1.0, and a request is answered only in a binkp/1.1 "
                  "multi-batch session" if not result.multi_batch else
                  "refused: the session ended before it could be answered")
        requester = result.remote_addresses[0].four_d if result.remote_addresses else "?"
        lines, _cut = parse_req(received.data)
        await lane.run(_finish, network, requester, _shown_names(lines), result.secure, [], reason)
    if len(kept) == len(result.received):
        return result
    from dataclasses import replace

    return replace(result, received=kept)


async def hold_reply_or_none(lane, network: FtnNetwork, result: SessionResult, name: str, data: bytes) -> str | None:
    """The held reason for a file answering a request made here, or None."""
    return await lane.run(reply_reason, network, result, name, len(data))
