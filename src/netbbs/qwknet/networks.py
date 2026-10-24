"""
QWKnet network records, the conference <-> board mapping, and the node
accounts a hub serves (issues #1481 and #1482, design doc §6.11).

**A network** says who this node is on one QWK network (`our_id`, the QWK
ID it logs in with and that routes name it by) and how it reaches its hub:
the hub's own QWK ID (which names the packets: `VERT.QWK` down, `VERT.REP`
up), host, port, transport and password. A network with no hub ID has no
uplink: this node is that network's top hub, and only serves nodes.
Records are edited live from the console and are off until enabled.

**Conferences** map to boards one to one, by the board's content id. A
board carries at most one conference of one network, and is then neither
Linked nor FTN: a board is local, Linked, FTN or QWKnet, never two (§16
"Issue #166", Decision 3, extended by "Issue #1154"). `map_conference`
refuses a Linked or FTN board; `netbbs.ftn.networks.set_board_area` and
`netbbs.link.boards.link_board` refuse a board that carries a conference.

**Node accounts** (the hub, #1482) log in with their QWK ID and a password
only its Argon2id hash of is kept. Each carries a set of the network's
conferences, and has its own limits: how many messages a packet holds and
how large a reply packet may be.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace

from netbbs.boards import Board
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

TRANSPORT_HTTPS, TRANSPORT_HTTP, TRANSPORT_FTP, TRANSPORT_FTPS = "https", "http", "ftp", "ftps"
TRANSPORTS = (TRANSPORT_HTTPS, TRANSPORT_HTTP, TRANSPORT_FTP, TRANSPORT_FTPS)
DEFAULT_PORTS = {TRANSPORT_HTTPS: 443, TRANSPORT_HTTP: 80, TRANSPORT_FTP: 21, TRANSPORT_FTPS: 21}

MAX_NAME = 40
MAX_PASSWORD = 64
MAX_TAGLINE = 70
MIN_POLL_MINUTES = 15
MAX_POLL_MINUTES = 24 * 60
DEFAULT_POLL_MINUTES = 240  # DOVE-Net asks its nodes to call four times a day

MIN_PACKET_MESSAGES, MAX_PACKET_MESSAGES = 50, 10_000
MIN_REP_KIB, MAX_REP_KIB = 64, 32 * 1024

#: QWK IDs (Synchronet's rule): 2-8 letters and digits, starting with a
#: letter. SYSOP and NETMAIL name addresses, not systems.
_QWK_ID = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")
_RESERVED_IDS = frozenset({"SYSOP", "NETMAIL", "ALL"})


class QwknetError(ValueError):
    """A record or mapping a SysOp can't save, with the reason."""


def normalise_qwk_id(value: str, label: str = "QWK ID") -> str:
    """A QWK ID as stored: upper case. Raises `QwknetError` for one that is
    not 2-8 letters and digits starting with a letter, or a reserved name."""
    text = value.strip().upper()
    if not _QWK_ID.match(text):
        raise QwknetError(f"{label} must be 2-8 letters and digits, starting with a letter (e.g. VERT).")
    if text in _RESERVED_IDS:
        raise QwknetError(f"{label} can't be {text}: QWKnet uses that name for an address.")
    return text


def is_qwk_id(value: str) -> bool:
    return bool(_QWK_ID.match(value)) and value not in _RESERVED_IDS


@dataclass(frozen=True)
class QwknetNetwork:
    name: str
    our_id: str
    hub_id: str = ""
    transport: str = TRANSPORT_HTTPS
    host: str = ""
    port: int = 0  # 0: the transport's usual port
    password: str = ""
    poll_minutes: int = DEFAULT_POLL_MINUTES
    tagline: str = ""
    enabled: bool = False
    serve_nodes: bool = False
    uplink_seq: int = 0
    last_poll_at: str | None = None
    last_poll_outcome: str = ""
    id: int | None = None

    @property
    def has_uplink(self) -> bool:
        return bool(self.hub_id)

    @property
    def effective_port(self) -> int:
        return self.port or DEFAULT_PORTS[self.transport]


def validate_network(network: QwknetNetwork) -> QwknetNetwork:
    name = " ".join(network.name.split())
    if not name or len(name) > MAX_NAME or not name.isprintable():
        raise QwknetError(f"Network name must be 1-{MAX_NAME} printable characters.")
    our_id = normalise_qwk_id(network.our_id, "Our QWK ID")
    hub_id = normalise_qwk_id(network.hub_id, "Hub QWK ID") if network.hub_id.strip() else ""
    if hub_id and hub_id == our_id:
        raise QwknetError("This node's QWK ID and the hub's must differ.")
    if network.transport not in TRANSPORTS:
        raise QwknetError("Transport must be HTTPS, HTTP, FTP or FTPS.")
    host = network.host.strip()
    if any(character.isspace() for character in host) or "/" in host or "@" in host:
        raise QwknetError("Host must be a single host name or address, without a path.")
    if not 0 <= network.port <= 65535:
        raise QwknetError("Port must be between 1 and 65535, or 0 for the transport's usual port.")
    if len(network.password) > MAX_PASSWORD or not network.password.isprintable():
        raise QwknetError(f"Password must be at most {MAX_PASSWORD} printable characters.")
    if not MIN_POLL_MINUTES <= network.poll_minutes <= MAX_POLL_MINUTES:
        raise QwknetError(f"Poll interval must be {MIN_POLL_MINUTES}-{MAX_POLL_MINUTES} minutes.")
    tagline = " ".join(network.tagline.split())
    if len(tagline) > MAX_TAGLINE or not tagline.isprintable():
        raise QwknetError(f"Tagline must be at most {MAX_TAGLINE} printable characters.")
    if network.enabled and hub_id and not host:
        raise QwknetError("An enabled network with a hub needs the hub's host.")
    if network.enabled and hub_id and not network.password:
        raise QwknetError("An enabled network with a hub needs the password of this node's account there.")
    if not hub_id and not network.serve_nodes and network.enabled:
        raise QwknetError("A network without a hub has nothing to do unless it serves nodes: "
                          "set the hub's QWK ID, or turn Serve nodes on.")
    return replace(network, name=name, our_id=our_id, hub_id=hub_id, host=host, tagline=tagline)


_NETWORK_COLUMNS = ("name", "our_id", "hub_id", "transport", "host", "port", "password", "poll_minutes",
                    "tagline", "enabled", "serve_nodes")


def save_network(db: Database, network: QwknetNetwork, *, commit: bool = True) -> QwknetNetwork:
    """Create (no `id`) or update a record; returns it as stored. Changing
    a network's own QWK ID changes how routes name this node from then on;
    nothing already sent changes."""
    validated = validate_network(network)
    values = (validated.name, validated.our_id, validated.hub_id, validated.transport, validated.host,
              validated.port, validated.password, validated.poll_minutes, validated.tagline,
              int(validated.enabled), int(validated.serve_nodes))
    try:
        if validated.id is None:
            cursor = db.connection.execute(
                f"INSERT INTO qwknet_networks ({', '.join(_NETWORK_COLUMNS)}, created_at) "
                f"VALUES ({', '.join('?' * (len(_NETWORK_COLUMNS) + 1))})",
                (*values, utc_now_iso()),
            )
            validated = replace(validated, id=cursor.lastrowid)
        else:
            cursor = db.connection.execute(
                f"UPDATE qwknet_networks SET {', '.join(f'{c} = ?' for c in _NETWORK_COLUMNS)} WHERE id = ?",
                (*values, validated.id),
            )
            if cursor.rowcount == 0:
                raise QwknetError("That network no longer exists.")
    except sqlite3.IntegrityError:
        db.connection.rollback()
        raise QwknetError(f"A QWKnet network called {validated.name!r} already exists.") from None
    if commit:
        db.connection.commit()
    return get_network(db, validated.id) or validated


def delete_network(db: Database, network_id: int, *, commit: bool = True) -> None:
    """Remove a record. Its boards become local again and keep their posts;
    its node accounts, held messages and queue go with it."""
    db.connection.execute("DELETE FROM qwknet_networks WHERE id = ?", (network_id,))
    if commit:
        db.connection.commit()


def _network_from_row(row: sqlite3.Row) -> QwknetNetwork:
    return QwknetNetwork(
        id=row["id"], name=row["name"], our_id=row["our_id"], hub_id=row["hub_id"], transport=row["transport"],
        host=row["host"], port=row["port"], password=row["password"], poll_minutes=row["poll_minutes"],
        tagline=row["tagline"], enabled=bool(row["enabled"]), serve_nodes=bool(row["serve_nodes"]),
        uplink_seq=row["uplink_seq"], last_poll_at=row["last_poll_at"], last_poll_outcome=row["last_poll_outcome"],
    )


def list_networks(db: Database) -> list[QwknetNetwork]:
    rows = db.connection.execute("SELECT * FROM qwknet_networks ORDER BY name COLLATE NOCASE").fetchall()
    return [_network_from_row(row) for row in rows]


def get_network(db: Database, network_id: int | None) -> QwknetNetwork | None:
    if network_id is None:
        return None
    row = db.connection.execute("SELECT * FROM qwknet_networks WHERE id = ?", (network_id,)).fetchone()
    return _network_from_row(row) if row is not None else None


def record_poll(db: Database, network_id: int, outcome: str) -> None:
    """What the network's last call to its hub came to, for the console."""
    db.connection.execute(
        "UPDATE qwknet_networks SET last_poll_at = ?, last_poll_outcome = ? WHERE id = ?",
        (utc_now_iso(), outcome[:500], network_id),
    )
    db.connection.commit()


# --- conference <-> board -------------------------------------------------------


@dataclass(frozen=True)
class ConferenceMapping:
    network_id: int
    number: int
    board_key: str


def _board_by_key(db: Database, board_key: str) -> Board | None:
    from netbbs.boards.boards import BoardError, get_board_by_id

    row = db.connection.execute("SELECT id FROM boards WHERE board_id = ?", (board_key,)).fetchone()
    if row is None:
        return None
    try:
        return get_board_by_id(db, row["id"])
    except BoardError:
        return None


def map_conference(db: Database, board: Board, network_id: int, number: int, *,
                   commit: bool = True) -> ConferenceMapping:
    """Make `board` carry conference `number` of the network. Refuses a
    Linked or FTN board, a missing network, conference 0 (mail) or one out
    of QWK's range, and a conference another board already carries.
    Remapping a board moves it; its posts stay."""
    from netbbs.ftn.networks import is_board_ftn
    from netbbs.link.boards import is_board_linked  # netbbs.link imports widely; keep it local

    if not 1 <= number <= 65535:
        raise QwknetError("A conference number is 1-65535; 0 is mail, which no board carries.")
    if is_board_linked(db, board):
        raise QwknetError(f"Board {board.name!r} is Linked; a board carries Link or a QWKnet conference, not both.")
    if is_board_ftn(db, board):
        raise QwknetError(f"Board {board.name!r} carries an FTN echo; a board carries one network, not two.")
    if get_network(db, network_id) is None:
        raise QwknetError("That network no longer exists.")
    holder = db.connection.execute(
        "SELECT board_key FROM qwknet_conferences WHERE network_id = ? AND number = ?", (network_id, number)
    ).fetchone()
    if holder is not None and holder["board_key"] != board.board_id:
        other = _board_by_key(db, holder["board_key"])
        if other is not None:
            raise QwknetError(f"Conference {number} is already carried by board {other.name!r}.")
        # The board that carried it was deleted: the number is free again.
        db.connection.execute("DELETE FROM qwknet_conferences WHERE board_key = ?", (holder["board_key"],))
    db.connection.execute("DELETE FROM qwknet_conferences WHERE board_key = ?", (board.board_id,))
    db.connection.execute(
        "INSERT INTO qwknet_conferences (network_id, number, board_key, mapped_at) VALUES (?, ?, ?, ?)",
        (network_id, number, board.board_id, utc_now_iso()),
    )
    if commit:
        db.connection.commit()
    return ConferenceMapping(network_id, number, board.board_id)


def unmap_board(db: Database, board: Board, *, commit: bool = True) -> None:
    """Make `board` local again; its posts stay."""
    db.connection.execute("DELETE FROM qwknet_conferences WHERE board_key = ?", (board.board_id,))
    if commit:
        db.connection.commit()


def board_conference(db: Database, board: Board) -> ConferenceMapping | None:
    """The conference `board` carries, or None. Reads nothing when QWKnet's
    tables are missing (an older schema in a test)."""
    try:
        row = db.connection.execute(
            "SELECT network_id, number, board_key FROM qwknet_conferences WHERE board_key = ?", (board.board_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return ConferenceMapping(row["network_id"], row["number"], row["board_key"]) if row is not None else None


def is_board_qwknet(db: Database, board: Board) -> bool:
    return board_conference(db, board) is not None


def network_conferences(db: Database, network_id: int) -> dict[int, Board]:
    """The network's conferences with the board each is carried by. A
    mapping whose board is gone is left out."""
    out: dict[int, Board] = {}
    for row in db.connection.execute(
        "SELECT number, board_key FROM qwknet_conferences WHERE network_id = ? ORDER BY number", (network_id,)
    ).fetchall():
        board = _board_by_key(db, row["board_key"])
        if board is not None:
            out[row["number"]] = board
    return out


# --- node accounts (the hub, #1482) ------------------------------------------------


@dataclass(frozen=True)
class QwknetNode:
    network_id: int
    qwk_id: str
    name: str = ""
    enabled: bool = True
    packet_messages: int = 2000
    rep_kib: int = 4096
    sent_seq: int = 0
    pending_seq: int | None = None
    last_fetch_at: str | None = None
    last_upload_at: str | None = None
    last_outcome: str = ""
    id: int | None = None


def _node_from_row(row: sqlite3.Row) -> QwknetNode:
    return QwknetNode(
        id=row["id"], network_id=row["network_id"], qwk_id=row["qwk_id"], name=row["name"],
        enabled=bool(row["enabled"]), packet_messages=row["packet_messages"], rep_kib=row["rep_kib"],
        sent_seq=row["sent_seq"], pending_seq=row["pending_seq"], last_fetch_at=row["last_fetch_at"],
        last_upload_at=row["last_upload_at"], last_outcome=row["last_outcome"],
    )


def validate_node(db: Database, node: QwknetNode) -> QwknetNode:
    qwk_id = normalise_qwk_id(node.qwk_id, "The node's QWK ID")
    network = get_network(db, node.network_id)
    if network is None:
        raise QwknetError("That network no longer exists.")
    if qwk_id in (network.our_id, network.hub_id):
        raise QwknetError(f"{qwk_id} is {'this node' if qwk_id == network.our_id else 'the hub'} on {network.name}.")
    name = " ".join(node.name.split())
    if len(name) > MAX_NAME or not name.isprintable():
        raise QwknetError(f"Name must be at most {MAX_NAME} printable characters.")
    if not MIN_PACKET_MESSAGES <= node.packet_messages <= MAX_PACKET_MESSAGES:
        raise QwknetError(f"Packet size must be {MIN_PACKET_MESSAGES}-{MAX_PACKET_MESSAGES} messages.")
    if not MIN_REP_KIB <= node.rep_kib <= MAX_REP_KIB:
        raise QwknetError(f"Reply packet limit must be {MIN_REP_KIB}-{MAX_REP_KIB} KiB.")
    return replace(node, qwk_id=qwk_id, name=name)


def save_node(db: Database, node: QwknetNode, *, password_hash: str | None = None,
              conferences: set[int] | None = None, commit: bool = True) -> QwknetNode:
    """Create (no `id`; `password_hash` required) or update a node account.
    `password_hash` replaces the password when given. `conferences`, when
    given, replaces the set it carries; a new account with none carries
    every conference the network has now."""
    validated = validate_node(db, node)
    try:
        if validated.id is None:
            if not password_hash:
                raise QwknetError("A new node account needs a password.")
            cursor = db.connection.execute(
                "INSERT INTO qwknet_nodes (network_id, qwk_id, name, password_hash, enabled, packet_messages, "
                "rep_kib, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (validated.network_id, validated.qwk_id, validated.name, password_hash, int(validated.enabled),
                 validated.packet_messages, validated.rep_kib, utc_now_iso()),
            )
            validated = replace(validated, id=cursor.lastrowid)
            if conferences is None:
                conferences = set(network_conferences(db, validated.network_id))
            # A new node starts from now: what waits is not its backlog.
            db.connection.execute(
                "UPDATE qwknet_nodes SET sent_seq = (SELECT COALESCE(MAX(seq), 0) FROM qwknet_outbound "
                "WHERE network_id = ?) WHERE id = ?", (validated.network_id, validated.id),
            )
        else:
            cursor = db.connection.execute(
                "UPDATE qwknet_nodes SET qwk_id = ?, name = ?, enabled = ?, packet_messages = ?, rep_kib = ? "
                "WHERE id = ?",
                (validated.qwk_id, validated.name, int(validated.enabled), validated.packet_messages,
                 validated.rep_kib, validated.id),
            )
            if cursor.rowcount == 0:
                raise QwknetError("That node account no longer exists.")
            if password_hash:
                db.connection.execute("UPDATE qwknet_nodes SET password_hash = ? WHERE id = ?",
                                      (password_hash, validated.id))
        if conferences is not None:
            db.connection.execute("DELETE FROM qwknet_node_conferences WHERE node_id = ?", (validated.id,))
            db.connection.executemany(
                "INSERT INTO qwknet_node_conferences (node_id, number) VALUES (?, ?)",
                [(validated.id, number) for number in sorted(conferences) if 1 <= number <= 65535],
            )
            # A packet built for the old set is built again.
            db.connection.execute(
                "UPDATE qwknet_nodes SET pending_packet = NULL, pending_seq = NULL, pending_at = NULL WHERE id = ?",
                (validated.id,),
            )
    except sqlite3.IntegrityError:
        db.connection.rollback()
        raise QwknetError(f"A node account {validated.qwk_id} already exists.") from None
    except BaseException:
        db.connection.rollback()
        raise
    if commit:
        db.connection.commit()
    return get_node(db, validated.id) or validated


def delete_node(db: Database, node_id: int, *, commit: bool = True) -> None:
    db.connection.execute("DELETE FROM qwknet_nodes WHERE id = ?", (node_id,))
    if commit:
        db.connection.commit()


def list_nodes(db: Database, network_id: int | None = None) -> list[QwknetNode]:
    if network_id is None:
        rows = db.connection.execute("SELECT * FROM qwknet_nodes ORDER BY qwk_id").fetchall()
    else:
        rows = db.connection.execute(
            "SELECT * FROM qwknet_nodes WHERE network_id = ? ORDER BY qwk_id", (network_id,)
        ).fetchall()
    return [_node_from_row(row) for row in rows]


def get_node(db: Database, node_id: int | None) -> QwknetNode | None:
    if node_id is None:
        return None
    row = db.connection.execute("SELECT * FROM qwknet_nodes WHERE id = ?", (node_id,)).fetchone()
    return _node_from_row(row) if row is not None else None


def node_login(db: Database, qwk_id: str) -> tuple[QwknetNode, str] | None:
    """The account `qwk_id` logs in to, and its password hash; None for no
    such account."""
    row = db.connection.execute(
        "SELECT * FROM qwknet_nodes WHERE qwk_id = ? COLLATE NOCASE", (qwk_id.strip(),)
    ).fetchone()
    return (_node_from_row(row), row["password_hash"]) if row is not None else None


def node_conferences(db: Database, node_id: int) -> set[int]:
    return {row["number"] for row in db.connection.execute(
        "SELECT number FROM qwknet_node_conferences WHERE node_id = ?", (node_id,)
    )}


def record_node_outcome(db: Database, node_id: int, outcome: str, *, fetched: bool = False,
                        uploaded: bool = False) -> None:
    sets = ["last_outcome = ?"]
    values: list = [outcome[:500]]
    now = utc_now_iso()
    if fetched:
        sets.append("last_fetch_at = ?")
        values.append(now)
    if uploaded:
        sets.append("last_upload_at = ?")
        values.append(now)
    db.connection.execute(f"UPDATE qwknet_nodes SET {', '.join(sets)} WHERE id = ?", (*values, node_id))
    db.connection.commit()
