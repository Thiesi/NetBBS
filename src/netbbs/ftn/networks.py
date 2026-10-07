"""
FTN network records and the board <-> echo area mapping (design doc §6.8).

A network record says who this node is on one FTN network and how it
reaches its uplink. Records are edited live from the SysOp console, like
MRC's settings: nothing here needs a restart, and a record is off until
the SysOp enables it.

A board carries at most one echo area, and an area maps to at most one
board per network. A board is local, Linked or FTN, never two (§16 "Issue
#166", Decision 3): `set_board_area` refuses a Linked board, and
`netbbs.link.boards.link_board` refuses a board that carries an area.
"""

from __future__ import annotations

import codecs
import re
import sqlite3
from dataclasses import dataclass, replace

from netbbs.auth.users import SYSOP_LEVEL, is_valid_level
from netbbs.boards import Board
from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress, parse_address
from netbbs.ftn.chrs import DEFAULT_CHARSET
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

DEFAULT_PORT = 24554
MAX_NAME = 40
MAX_ORIGIN_TEXT = 60
MAX_SESSION_PASSWORD = 40
MAX_PACKET_PASSWORD = 8
MIN_POLL_MINUTES = 5
MAX_POLL_MINUTES = 24 * 60

_AREA_TAG = re.compile(r"[\x21-\x60\x7b-\x7e]{1,60}")
_DOMAIN = re.compile(r"[a-z0-9_-]{1,8}")
_PRINTABLE = re.compile(r"[\x21-\x7e]*")


class FtnNetworkError(ValueError):
    """A network record or area mapping a SysOp can't save, with the reason."""


@dataclass(frozen=True)
class FtnNetwork:
    name: str
    domain: str
    our_address: FtnAddress
    uplink_address: FtnAddress
    uplink_host: str = ""
    uplink_port: int = DEFAULT_PORT
    session_password: str = ""
    packet_password: str = ""
    areafix_password: str = ""
    poll_minutes: int = 60
    answers_calls: bool = False
    enabled: bool = False
    default_charset: str = DEFAULT_CHARSET
    origin_text: str = ""
    netmail_min_level: int = SYSOP_LEVEL
    id: int | None = None


def validate_network(network: FtnNetwork) -> FtnNetwork:
    """The record as it will be stored; raises `FtnNetworkError` with a
    SysOp-readable reason."""
    name = " ".join(network.name.split())
    if not name or len(name) > MAX_NAME or not name.isprintable():
        raise FtnNetworkError(f"Network name must be 1-{MAX_NAME} printable characters.")
    domain = network.domain.strip().lower()
    if not _DOMAIN.fullmatch(domain):
        raise FtnNetworkError("Domain must be 1-8 letters, digits, - or _ (e.g. fsxnet).")
    our = network.our_address.with_domain(domain)
    uplink = network.uplink_address.with_domain(domain)
    if our.same_node(uplink):
        raise FtnNetworkError("This node's address and the uplink's must differ.")
    host = network.uplink_host.strip()
    if any(character.isspace() for character in host):
        raise FtnNetworkError("Uplink host must be a single host name or address.")
    if network.enabled and not host:
        raise FtnNetworkError("An enabled network needs the uplink's host.")
    if not 1 <= network.uplink_port <= 65535:
        raise FtnNetworkError("Uplink port must be between 1 and 65535.")
    for value, limit, label in (
        (network.session_password, MAX_SESSION_PASSWORD, "Session password"),
        (network.packet_password, MAX_PACKET_PASSWORD, "Packet password"),
        (network.areafix_password, MAX_SESSION_PASSWORD, "AreaFix password"),
    ):
        if len(value) > limit or not _PRINTABLE.fullmatch(value):
            raise FtnNetworkError(f"{label} must be at most {limit} printable characters without spaces.")
    if not MIN_POLL_MINUTES <= network.poll_minutes <= MAX_POLL_MINUTES:
        raise FtnNetworkError(
            f"Poll interval must be {MIN_POLL_MINUTES}-{MAX_POLL_MINUTES} minutes; hubs ask for at least daily."
        )
    try:
        codecs.lookup(network.default_charset)
    except LookupError:
        raise FtnNetworkError(f"{network.default_charset!r} is not a known character set.") from None
    origin_text = " ".join(network.origin_text.split())
    if len(origin_text) > MAX_ORIGIN_TEXT or not origin_text.isprintable():
        raise FtnNetworkError(f"Origin text must be at most {MAX_ORIGIN_TEXT} printable characters.")
    if not is_valid_level(network.netmail_min_level):
        raise FtnNetworkError("Netmail level must be a valid level.")
    return replace(network, name=name, domain=domain, our_address=our, uplink_address=uplink,
                   uplink_host=host, origin_text=origin_text)


def save_network(db: Database, network: FtnNetwork) -> FtnNetwork:
    """Create (no `id`) or update a record; returns it as stored."""
    validated = validate_network(network)
    values = (
        validated.name, validated.domain, str(validated.our_address), str(validated.uplink_address),
        validated.uplink_host, validated.uplink_port, validated.session_password, validated.packet_password,
        validated.areafix_password, validated.poll_minutes, int(validated.answers_calls), int(validated.enabled),
        validated.default_charset, validated.origin_text, validated.netmail_min_level,
    )
    try:
        if validated.id is None:
            cursor = db.connection.execute(
                "INSERT INTO ftn_networks (name, domain, our_address, uplink_address, uplink_host, uplink_port, "
                "session_password, packet_password, areafix_password, poll_minutes, answers_calls, enabled, "
                "default_charset, origin_text, netmail_min_level, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, utc_now_iso()),
            )
            validated = replace(validated, id=cursor.lastrowid)
        else:
            cursor = db.connection.execute(
                "UPDATE ftn_networks SET name = ?, domain = ?, our_address = ?, uplink_address = ?, "
                "uplink_host = ?, uplink_port = ?, session_password = ?, packet_password = ?, "
                "areafix_password = ?, poll_minutes = ?, answers_calls = ?, enabled = ?, default_charset = ?, "
                "origin_text = ?, netmail_min_level = ? WHERE id = ?",
                (*values, validated.id),
            )
            if cursor.rowcount == 0:
                raise FtnNetworkError("That network no longer exists.")
    except sqlite3.IntegrityError:
        db.connection.rollback()
        raise FtnNetworkError(f"A network called {validated.name!r} already exists.") from None
    db.connection.commit()
    return validated


def delete_network(db: Database, network_id: int) -> None:
    """Remove a record. Its boards become local again and keep their posts;
    its queue, dupe history and held packets go with it."""
    db.connection.execute(
        "UPDATE boards SET ftn_area_tag = NULL WHERE ftn_network_id = ?", (network_id,)
    )
    db.connection.execute("DELETE FROM ftn_networks WHERE id = ?", (network_id,))
    db.connection.commit()


def list_networks(db: Database) -> list[FtnNetwork]:
    rows = db.connection.execute("SELECT * FROM ftn_networks ORDER BY name COLLATE NOCASE").fetchall()
    return [_network_from_row(row) for row in rows]


def get_network(db: Database, network_id: int) -> FtnNetwork | None:
    row = db.connection.execute("SELECT * FROM ftn_networks WHERE id = ?", (network_id,)).fetchone()
    return _network_from_row(row) if row is not None else None


def _network_from_row(row: sqlite3.Row) -> FtnNetwork:
    try:
        our = parse_address(row["our_address"])
        uplink = parse_address(row["uplink_address"])
    except FtnFormatError as exc:  # only a hand-edited row can get here
        raise FtnNetworkError(f"network {row['name']!r} has an unreadable address: {exc}") from exc
    return FtnNetwork(
        id=row["id"], name=row["name"], domain=row["domain"], our_address=our, uplink_address=uplink,
        uplink_host=row["uplink_host"], uplink_port=row["uplink_port"],
        session_password=row["session_password"], packet_password=row["packet_password"],
        areafix_password=row["areafix_password"], poll_minutes=row["poll_minutes"],
        answers_calls=bool(row["answers_calls"]), enabled=bool(row["enabled"]),
        default_charset=row["default_charset"], origin_text=row["origin_text"],
        netmail_min_level=row["netmail_min_level"],
    )


# --- board <-> echo area ----------------------------------------------------


@dataclass(frozen=True)
class AreaMapping:
    network_id: int
    tag: str


def normalise_area_tag(tag: str) -> str:
    """An echo tag as stored and compared: upper case, as echolists print
    them. Raises `FtnNetworkError` if it is not a valid tag."""
    tag = tag.strip().upper()
    if not _AREA_TAG.fullmatch(tag):
        raise FtnNetworkError("An echo tag is 1-60 characters, no spaces (e.g. FSX_GEN).")
    return tag


def set_board_area(db: Database, board: Board, network_id: int, tag: str) -> AreaMapping:
    """Make `board` carry echo area `tag` of the network. Refuses a Linked
    board, a missing network, and a tag another board already carries."""
    from netbbs.link.boards import is_board_linked  # netbbs.link imports widely; keep it local

    tag = normalise_area_tag(tag)
    if is_board_linked(db, board):
        raise FtnNetworkError(f"Board {board.name!r} is Linked; a board carries Link or an FTN echo, not both.")
    if get_network(db, network_id) is None:
        raise FtnNetworkError("That network no longer exists.")
    holder = db.connection.execute(
        "SELECT name FROM boards WHERE ftn_network_id = ? AND ftn_area_tag = ? COLLATE NOCASE AND id != ?",
        (network_id, tag, board.id),
    ).fetchone()
    if holder is not None:
        raise FtnNetworkError(f"Echo {tag} is already carried by board {holder['name']!r}.")
    db.connection.execute(
        "UPDATE boards SET ftn_network_id = ?, ftn_area_tag = ? WHERE id = ?", (network_id, tag, board.id)
    )
    db.connection.commit()
    return AreaMapping(network_id, tag)


def clear_board_area(db: Database, board: Board) -> None:
    """Make `board` local again. Its posts stay."""
    db.connection.execute("UPDATE boards SET ftn_network_id = NULL, ftn_area_tag = NULL WHERE id = ?", (board.id,))
    db.connection.commit()


def board_area(db: Database, board: Board) -> AreaMapping | None:
    row = db.connection.execute(
        "SELECT ftn_network_id, ftn_area_tag FROM boards WHERE id = ?", (board.id,)
    ).fetchone()
    if row is None or row["ftn_network_id"] is None or not row["ftn_area_tag"]:
        return None
    return AreaMapping(row["ftn_network_id"], row["ftn_area_tag"])


def is_board_ftn(db: Database, board: Board) -> bool:
    return board_area(db, board) is not None


def board_id_for_area(db: Database, network_id: int, tag: str) -> int | None:
    """The board carrying `tag` on the network, or None."""
    row = db.connection.execute(
        "SELECT id FROM boards WHERE ftn_network_id = ? AND ftn_area_tag = ? COLLATE NOCASE",
        (network_id, tag.strip()),
    ).fetchone()
    return row["id"] if row is not None else None


def area_mappings(db: Database, network_id: int) -> dict[str, int]:
    """Every tag the network carries, mapped to its board id."""
    rows = db.connection.execute(
        "SELECT ftn_area_tag, id FROM boards WHERE ftn_network_id = ? AND ftn_area_tag IS NOT NULL",
        (network_id,),
    ).fetchall()
    return {row["ftn_area_tag"]: row["id"] for row in rows}
