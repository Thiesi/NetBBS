"""
What moves on a QWK network (issues #1481 and #1482, design doc §6.11).

**In.** A hub's packet (`toss_hub_packet`, this node as a node) and a
node's reply packet (`toss_node_rep`, this node as a hub) are tossed message
by message, each in its own transaction:

- **Untrusted.** Everything comes through the strict readers in
  `netbbs.qwk`, bounded; text is CP437 (or UTF-8 where HEADERS.DAT says so)
  with control characters removed.
- **Where.** A message goes to the board carrying its conference. A
  conference no board carries is held per conference, bounded, for the SysOp
  to map and re-toss (`retoss_conference`) or delete, like FTN's bad echoes.
  A packet that can't be read at all is held whole.
- **Who.** The author is labelled `Name (ORIGIN)`: the name the message
  carries and the QWK ID of the system it was written on -- the far end of
  its route -- never with `@`, which names a Link address. The board's
  moderation applies (`create_labelled_post`). Its date is the message's,
  never later than now.
- **Route and loops.** A message's route is the system it came from, then
  what its `SenderNetAddr` (HEADERS.DAT) or `@VIA:` kludge adds: the path
  back to where it was written, nearest first, as Synchronet keeps it. A
  message whose route names this node already has been here: it is dropped
  as a loop.
- **Duplicates** are dropped by Message-ID (HEADERS.DAT `Message-ID` or
  the `@MSGID:` kludge), or by a digest of the message when it has none,
  per network, and a message carrying a Message-ID this node gave out is its
  own post coming back.
- **Threads** follow `In-Reply-To`/`@REPLY:` to the post with that
  Message-ID on the same board, else the header's reference number: the
  hub's message number for a hub's packet, this node's for a node's reply.
- **Mail.** A hub's conference 0 is mail for this node: to `SYSOP` or this
  node's QWK ID it goes to the SysOp, to an account's name to that account,
  and anything else is held. Conference 0 in a node's reply packet (Synchronet
  control messages, routed netmail) is not carried; votes (status `V`) are
  skipped too.

**Out.** Every post that becomes visible on a mapped board -- written here,
approved here, or tossed in -- gets a row in `qwknet_outbound`
(`queue_post_if_qwknet`), numbered in order. The hub's reply packet
(`build_uplink_rep`) and each node's packet (`build_node_packet`) take the
rows after their own cursor, skipping a post's own source (what came from
the hub never goes back up, what came from a node never goes back to it)
and any post whose route names the destination. A post written here gets a
Message-ID, a `---` line and the network's tagline; a post passed on keeps
its text, and its route goes with it as `SenderNetAddr`. The cursor moves
only when the far end confirms: the hub accepting the upload, the node
confirming its download (`confirm_node_packet`).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import sqlite3
import zipfile
import zlib
from dataclasses import dataclass, field

from netbbs import __version__
from netbbs.auth.renames import get_recipient_by_name
from netbbs.auth.users import AuthError, User, is_usable_sysop, list_users
from netbbs.boards import Board
from netbbs.boards.limits import MAX_BODY_BYTES
from netbbs.boards.posts import Post, PostError, create_labelled_post, visible_post
from netbbs.config import get_node_display_name
from netbbs.qwk.format import QwkFormatError, split_kludges
from netbbs.qwk.packet import Conference, PacketInfo, PacketMessage, build_packet, read_packet
from netbbs.qwk.rep import OutgoingReply, RepLimits, build_rep, parse_rep
from netbbs.qwknet.networks import (
    QwknetNetwork,
    QwknetNode,
    board_conference,
    get_network,
    network_conferences,
    node_conferences,
)
from netbbs.rendering import sanitize_text
from netbbs.search import reindex_post
from netbbs.storage.database import Database
from netbbs.timeutil import get_node_timezone, parse_utc_iso, utc_iso, utc_now_iso

_logger = logging.getLogger(__name__)

MAIL_CONFERENCE = 0
PRODUCT = "NetBBS"
#: A tagline line starts with this, as Synchronet's does (CP437 0xFE).
TAGLINE_MARK = "■"

MAX_HELD_MESSAGES = 2000  # per network; the oldest go first
MAX_HELD_PACKETS = 10  # whole packets per network that could not be read
HELD_DAYS = 30
SEEN_DAYS = 365
OUTBOUND_DAYS = 90  # a row no destination took by then is dropped
MAX_REP_MESSAGES = 1000  # per upload to the hub; the rest go next call
MAX_PACKET_TEXT_BYTES = 16 * 1024 * 1024
MAX_ROUTE_HOPS = 32
MAX_NAME = 60
_MAX_MESSAGE_NUMBER = 9_999_999
_CUT_NOTE = "\n\n[The rest of this message was cut: it was longer than this node keeps.]"
_ROUTE_ID = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,7}$")
_WHEN = re.compile(r"^(\d{14})(?:\s*([+-])(\d{2})(\d{2}))?")


@dataclass
class TossResult:
    posts: int = 0
    mail: int = 0
    duplicates: int = 0
    loops: int = 0
    held: int = 0
    skipped: int = 0  # votes, and mail a node's reply packet can't carry
    held_packet: str | None = None  # why the whole packet was held
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.held_packet:
            return f"packet held: {self.held_packet}"
        parts = [f"{self.posts} posts"]
        if self.mail:
            parts.append(f"{self.mail} letters")
        for count, word in ((self.duplicates, "duplicates"), (self.loops, "loops dropped"),
                            (self.held, "held"), (self.skipped, "skipped")):
            if count:
                parts.append(f"{count} {word}")
        if self.problems:
            parts.append(f"{len(self.problems)} unreadable")
        return ", ".join(parts)


@dataclass(frozen=True)
class Incoming:
    """One message on its way in, whichever packet it came in."""

    conference: int
    number: int | None
    when: datetime.datetime | None
    to: str
    sender: str
    subject: str
    body: str
    reference: int | None
    private: bool
    headers: dict[str, str]
    status: str = " "

    def as_json(self) -> bytes:
        return json.dumps({
            "conference": self.conference, "number": self.number,
            "when": self.when.isoformat() if self.when is not None else None,
            "to": self.to, "sender": self.sender, "subject": self.subject, "body": self.body,
            "reference": self.reference, "private": self.private, "headers": self.headers, "status": self.status,
        }).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> Incoming:
        value = json.loads(data.decode("utf-8"))
        when = value.get("when")
        return cls(
            conference=int(value["conference"]), number=value.get("number"),
            when=datetime.datetime.fromisoformat(when) if when else None,
            to=str(value.get("to", "")), sender=str(value.get("sender", "")), subject=str(value.get("subject", "")),
            body=str(value.get("body", "")), reference=value.get("reference"), private=bool(value.get("private")),
            headers={str(k): str(v) for k, v in dict(value.get("headers") or {}).items()},
            status=str(value.get("status", " "))[:1] or " ",
        )


# --- routes, names, identifiers --------------------------------------------------


def parse_route(text: str) -> list[str]:
    """A `SenderNetAddr`/`@VIA:` route as QWK IDs, nearest first. Read up to
    the first part that is not a QWK ID, and at most `MAX_ROUTE_HOPS`."""
    out: list[str] = []
    for part in text.strip().upper().split("/"):
        part = part.strip()
        if not _ROUTE_ID.match(part) or len(out) >= MAX_ROUTE_HOPS:
            break
        out.append(part)
    return out


def clean_name(name: str) -> str:
    """A name from a packet as a label may carry it: printable, no `@`, runs
    of spaces as one, at most `MAX_NAME` characters."""
    clean = " ".join("".join(c for c in name if c.isprintable()).replace("@", " at ").split())
    return clean[:MAX_NAME].strip() or "Unknown"


def author_label(name: str, origin: str) -> str:
    return f"{clean_name(name)} ({origin})"


def name_of_label(label: str) -> str:
    """The name in `Name (ORIGIN)`, or the label itself."""
    if label.endswith(")") and " (" in label:
        return label.rsplit(" (", 1)[0]
    return label


def make_msgid(post: Post, our_id: str) -> str:
    """The Message-ID a post written here goes out under."""
    return f"<{post.post_id[:16]}.{post.id}@{our_id}>"


def _digest(message: Incoming) -> str:
    joined = "\x00".join([
        str(message.conference), message.sender.casefold(), message.subject, message.body,
        message.when.isoformat() if message.when else "",
    ])
    return "hash:" + hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()[:40]


def _fit(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    room = max_bytes - len(_CUT_NOTE.encode("utf-8"))
    return text.encode("utf-8")[:room].decode("utf-8", errors="ignore") + _CUT_NOTE


def _written_at(db: Database, message: Incoming) -> str:
    """The post's date: HEADERS.DAT's `WhenWritten` with its offset, else the
    header's date read in this node's timezone; never later than now."""
    now = datetime.datetime.now(datetime.timezone.utc)
    moment = None
    match = _WHEN.match(message.headers.get("whenwritten", ""))
    if match is not None:
        try:
            naive = datetime.datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
            if match.group(2):
                offset = datetime.timedelta(hours=int(match.group(3)), minutes=int(match.group(4)))
                tz = datetime.timezone(offset if match.group(2) == "+" else -offset)
                moment = naive.replace(tzinfo=tz)
            else:
                moment = naive.replace(tzinfo=get_node_timezone(db))
        except ValueError:
            moment = None
    if moment is None and message.when is not None:
        moment = message.when.replace(tzinfo=get_node_timezone(db))
    if moment is None or moment > now:
        return utc_now_iso()
    return utc_iso(moment.astimezone(datetime.timezone.utc))


def _first_sysop(db: Database) -> User | None:
    sysops = sorted((u for u in list_users(db) if is_usable_sysop(u)), key=lambda u: u.id)
    return sysops[0] if sysops else None


# --- tossing ------------------------------------------------------------------------


class _Hold(Exception):
    """This message is held, with this reason."""


def _seen(db: Database, network_id: int, key: str) -> bool:
    if db.connection.execute(
        "SELECT 1 FROM qwknet_seen WHERE network_id = ? AND key = ?", (network_id, key)
    ).fetchone() is not None:
        return True
    return db.connection.execute(
        "SELECT 1 FROM qwknet_posts WHERE network_id = ? AND msgid = ?", (network_id, key)
    ).fetchone() is not None


def _remember_without_commit(db: Database, network_id: int, key: str) -> None:
    db.connection.execute(
        "INSERT OR IGNORE INTO qwknet_seen (network_id, key, seen_at) VALUES (?, ?, ?)",
        (network_id, key, utc_now_iso()),
    )


def toss_message(
    db: Database, network: QwknetNetwork, message: Incoming, *, node: QwknetNode | None, result: TossResult,
) -> None:
    """Toss one message from the hub (`node` None) or from `node`; see the
    module docstring. Never raises for the message itself: what can't be
    stored is held."""
    source = node.qwk_id if node is not None else network.hub_id
    try:
        _toss(db, network, message, node=node, source=source, result=result)
    except _Hold as exc:
        db.connection.rollback()
        hold_message(db, network.id, source, message, str(exc))
        result.held += 1


def _toss(db: Database, network: QwknetNetwork, message: Incoming, *, node: QwknetNode | None, source: str,
          result: TossResult) -> None:
    if message.status == "V":  # a Synchronet vote or poll; NetBBS carries neither
        result.skipped += 1
        return
    kludges = split_kludges(message.body.split("\n"))
    headers = message.headers
    msgid = (headers.get("message-id") or kludges.values.get("msgid") or "").strip()[:200]
    reply_to = (headers.get("in-reply-to") or kludges.values.get("reply") or "").strip()[:200]
    via = headers.get("sendernetaddr") or kludges.values.get("via") or ""
    route = [source, *parse_route(via)]
    if network.our_id in route:
        result.loops += 1
        return
    key = msgid or _digest(message)
    if _seen(db, network.id, key):
        result.duplicates += 1
        return
    body = "\n".join(kludges.lines).rstrip("\n")
    if message.conference == MAIL_CONFERENCE:
        if node is not None:
            result.skipped += 1  # control messages and routed netmail: not carried
            return
        _deliver_mail(db, network, message, body, route, key)
        result.mail += 1
        return
    boards = network_conferences(db, network.id)
    board = boards.get(message.conference)
    if board is None:
        raise _Hold(f"conference {message.conference}: no board carries it")
    parent_post_id = _parent(db, network, board, reply_to, message.reference, from_hub=node is None)
    label = author_label(message.sender, route[-1])
    subject = " ".join(message.subject.split()) or "(no subject)"
    try:
        post = create_labelled_post(
            db, board, label, subject, _fit(body or "(no text)", MAX_BODY_BYTES),
            commit=False, parent_post_id=parent_post_id, created_at=_written_at(db, message),
        )
    except PostError as exc:
        raise _Hold(f"board {board.name!r} refused it: {exc}") from exc
    db.connection.execute(
        "INSERT OR REPLACE INTO qwknet_posts (post_id, network_id, msgid, route, hub_number, source_node_id, "
        "from_uplink) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (post.post_id, network.id, key, "/".join(route),
         message.number if node is None else None, node.id if node is not None else None, int(node is None)),
    )
    _remember_without_commit(db, network.id, key)
    if post.status == "approved":
        _queue_without_commit(db, network.id, post, message.conference, node.id if node is not None else None,
                              node is None)
    db.connection.commit()
    reindex_post(db, board.id, post.post_id)
    result.posts += 1


def _parent(db: Database, network: QwknetNetwork, board: Board, reply_to: str, reference: int | None, *,
            from_hub: bool) -> str | None:
    """The post a message answers, on the same board, or None."""
    row = None
    if reply_to:
        row = db.connection.execute(
            "SELECT p.post_id FROM qwknet_posts q JOIN posts p ON p.post_id = q.post_id "
            "WHERE q.network_id = ? AND q.msgid = ? AND p.board_id = ?",
            (network.id, reply_to, board.id),
        ).fetchone()
    if row is None and reference:
        if from_hub:
            row = db.connection.execute(
                "SELECT p.post_id FROM qwknet_posts q JOIN posts p ON p.post_id = q.post_id "
                "WHERE q.network_id = ? AND q.hub_number = ? AND p.board_id = ? ORDER BY p.id DESC LIMIT 1",
                (network.id, reference, board.id),
            ).fetchone()
        else:
            row = db.connection.execute(
                "SELECT post_id FROM posts WHERE id = ? AND board_id = ? AND post_id = root_post_id",
                (reference, board.id),
            ).fetchone()
    if row is None:
        return None
    parent = visible_post(db, row["post_id"])
    return parent.root_post_id if parent is not None and parent.tombstoned_at is None else None


def _deliver_mail(db: Database, network: QwknetNetwork, message: Incoming, body: str, route: list[str],
                  key: str) -> None:
    """A hub's conference 0: a letter for the SysOp or an account here."""
    from netbbs.mail import MAX_MAIL_BODY_BYTES, MailboxFullError, MailError, deliver_external_mail_without_commit

    to = message.to.strip()
    if to.upper() in ("SYSOP", network.our_id):
        recipient = _first_sysop(db)
        if recipient is None:
            raise _Hold("mail for the SysOp, and this node has no SysOp")
    elif to.upper() == "NETMAIL":
        raise _Hold("routed netmail, which this node does not route")
    else:
        try:
            recipient = get_recipient_by_name(db, to)
        except AuthError:
            raise _Hold(f"mail for {to!r}, who has no account here") from None
    subject = " ".join(message.subject.split()) or "(no subject)"
    try:
        deliver_external_mail_without_commit(
            db, recipient, sender_label=author_label(message.sender, route[-1]),
            subject=subject[:200], body=_fit(body or "(no text)", MAX_MAIL_BODY_BYTES),
            created_at=_written_at(db, message),
        )
    except (MailboxFullError, MailError) as exc:
        raise _Hold(f"could not deliver to {recipient.username}: {exc}") from exc
    _remember_without_commit(db, network.id, key)
    db.connection.commit()


def toss_hub_packet(db: Database, network: QwknetNetwork, data: bytes) -> TossResult:
    """Toss the packet this node fetched from its hub. One that can't be
    read, or that another BBS made, is held whole."""
    result = TossResult()
    try:
        packet = read_packet(data)
    except (QwkFormatError, zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError,
            EOFError, OSError, zlib.error) as exc:
        result.held_packet = f"unreadable: {exc}"
        hold_packet(db, network.id, network.hub_id, data, result.held_packet)
        return result
    if packet.bbs_id.upper() != network.hub_id:
        result.held_packet = f"made by {packet.bbs_id or 'an unnamed BBS'!r}, not by the hub {network.hub_id}"
        hold_packet(db, network.id, network.hub_id, data, result.held_packet)
        return result
    for message in packet.messages:
        toss_message(db, network, Incoming(
            conference=message.conference, number=message.number, when=message.when, to=message.to,
            sender=message.sender, subject=message.subject, body=message.body, reference=message.reference,
            private=message.private, headers=message.headers, status=message.status,
        ), node=None, result=result)
    return result


def toss_node_rep(db: Database, network: QwknetNetwork, node: QwknetNode, data: bytes) -> TossResult:
    """Toss a reply packet `node` uploaded. Raises `RepError` when it can't
    be read at all (nothing is used). The From each message carries is the
    name at the node -- a QWKnet node speaks for its own callers -- with
    the node's QWK ID as its origin, or the route's far end."""
    limits = RepLimits(archive_bytes=node.rep_kib * 1024, msg_bytes=node.rep_kib * 1024 * 4, members=64,
                       messages=5000, body_bytes=MAX_BODY_BYTES * 4)
    packet = parse_rep(data, bbs_id=network.our_id, limits=limits)
    result = TossResult(problems=[problem.reason for problem in packet.problems])
    for message in packet.messages:
        toss_message(db, network, Incoming(
            conference=message.conference, number=None, when=message.when,
            to=message.headers.get("to") or message.to,
            sender=message.headers.get("sender") or message.claimed_from or node.qwk_id,
            subject=message.headers.get("subject") or message.subject, body=message.body,
            reference=message.reference, private=message.private, headers=message.headers, status=message.status,
        ), node=node, result=result)
    return result


# --- the held store ---------------------------------------------------------------


def hold_message(db: Database, network_id: int, source: str, message: Incoming, reason: str) -> None:
    db.connection.execute(
        "INSERT INTO qwknet_held (network_id, source, conference, reason, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (network_id, source, message.conference, reason[:300], message.as_json(), utc_now_iso()),
    )
    db.connection.execute(
        "DELETE FROM qwknet_held WHERE network_id = ? AND conference IS NOT NULL AND id NOT IN ("
        "SELECT id FROM qwknet_held WHERE network_id = ? AND conference IS NOT NULL ORDER BY id DESC LIMIT ?)",
        (network_id, network_id, MAX_HELD_MESSAGES),
    )
    db.connection.commit()


def hold_packet(db: Database, network_id: int, source: str, data: bytes, reason: str) -> None:
    db.connection.execute(
        "INSERT INTO qwknet_held (network_id, source, conference, reason, content, created_at) "
        "VALUES (?, ?, NULL, ?, ?, ?)",
        (network_id, source, reason[:300], data, utc_now_iso()),
    )
    db.connection.execute(
        "DELETE FROM qwknet_held WHERE network_id = ? AND conference IS NULL AND id NOT IN ("
        "SELECT id FROM qwknet_held WHERE network_id = ? AND conference IS NULL ORDER BY id DESC LIMIT ?)",
        (network_id, network_id, MAX_HELD_PACKETS),
    )
    db.connection.commit()
    _logger.warning("QWKnet: a packet from %s was held: %s", source, reason)


@dataclass(frozen=True)
class HeldGroup:
    """Held messages of one conference (`conference` None: whole packets)."""

    network_id: int
    conference: int | None
    count: int
    reason: str
    newest_at: str


def list_held(db: Database, network_id: int) -> list[HeldGroup]:
    rows = db.connection.execute(
        "SELECT conference, COUNT(*) AS n, MAX(created_at) AS newest, "
        "(SELECT reason FROM qwknet_held h2 WHERE h2.network_id = h.network_id "
        " AND h2.conference IS h.conference ORDER BY id DESC LIMIT 1) AS reason "
        "FROM qwknet_held h WHERE network_id = ? GROUP BY conference ORDER BY conference IS NULL, conference",
        (network_id,),
    ).fetchall()
    return [HeldGroup(network_id, row["conference"], row["n"], row["reason"] or "", row["newest"]) for row in rows]


def delete_held(db: Database, network_id: int, conference: int | None) -> int:
    cursor = db.connection.execute(
        "DELETE FROM qwknet_held WHERE network_id = ? AND conference IS ?", (network_id, conference)
    )
    db.connection.commit()
    return cursor.rowcount


def retoss_conference(db: Database, network: QwknetNetwork, conference: int) -> TossResult:
    """Toss a conference's held messages again, oldest first, once the SysOp
    has mapped it. A message still unmapped comes straight back."""
    from netbbs.qwknet.networks import get_node

    result = TossResult()
    rows = db.connection.execute(
        "SELECT id, source, content FROM qwknet_held WHERE network_id = ? AND conference = ? ORDER BY id",
        (network.id, conference),
    ).fetchall()
    nodes = {row["id"]: row for row in db.connection.execute(
        "SELECT id, qwk_id FROM qwknet_nodes WHERE network_id = ?", (network.id,))}
    by_id = {row["qwk_id"]: row["id"] for row in nodes.values()}
    for row in rows:
        db.connection.execute("DELETE FROM qwknet_held WHERE id = ?", (row["id"],))
        db.connection.commit()
        try:
            message = Incoming.from_json(row["content"])
        except (ValueError, KeyError, TypeError):
            result.problems.append("a held message could not be read back")
            continue
        node = get_node(db, by_id.get(row["source"])) if row["source"] != network.hub_id else None
        toss_message(db, network, message, node=node, result=result)
    return result


# --- out: the queue ---------------------------------------------------------------


def _queue_without_commit(db: Database, network_id: int, post: Post, number: int, source_node_id: int | None,
                          from_uplink: bool) -> None:
    db.connection.execute(
        "INSERT OR IGNORE INTO qwknet_outbound (network_id, post_id, number, source_node_id, from_uplink, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (network_id, post.post_id, number, source_node_id, int(from_uplink), utc_now_iso()),
    )


def queue_post_if_qwknet(db: Database, post: Post, board: Board) -> bool:
    """Queue `post` for its board's QWK network, once: called wherever a
    post becomes visible -- written on an unmoderated board, approved on a
    moderated one -- beside `export_post_if_ftn`. A post written here is
    given its Message-ID now; one that came from the network keeps where it
    came from, so it never goes back there. True when queued now."""
    if post.status != "approved":
        return False
    mapping = board_conference(db, board)
    if mapping is None:
        return False
    network = get_network(db, mapping.network_id)
    if network is None or not network.enabled:
        return False
    row = db.connection.execute(
        "SELECT edit_of_post_id FROM posts WHERE post_id = ?", (post.post_id,)
    ).fetchone()
    if row is None or row["edit_of_post_id"] is not None:
        return False  # QWK has no edits: a second copy would read as a new message
    origin = db.connection.execute(
        "SELECT source_node_id, from_uplink, network_id FROM qwknet_posts WHERE post_id = ?", (post.post_id,)
    ).fetchone()
    if origin is None:
        db.connection.execute(
            "INSERT INTO qwknet_posts (post_id, network_id, msgid, route) VALUES (?, ?, ?, '')",
            (post.post_id, network.id, make_msgid(post, network.our_id)),
        )
        source_node_id, from_uplink = None, False
    elif origin["network_id"] != network.id:
        return False  # carried in from another network; never passed between networks
    else:
        source_node_id, from_uplink = origin["source_node_id"], bool(origin["from_uplink"])
    before = db.connection.total_changes
    _queue_without_commit(db, network.id, post, mapping.number, source_node_id, from_uplink)
    queued = db.connection.total_changes > before
    db.connection.commit()
    return queued


@dataclass(frozen=True)
class _Out:
    """A queued post as it leaves: the fields every packet needs."""

    seq: int
    number: int
    post: Post
    to: str
    sender: str
    subject: str
    body: str
    msgid: str
    reply_msgid: str
    route: str
    local: bool
    parent_row_id: int | None
    parent_hub_number: int | None


def _outgoing(db: Database, network: QwknetNetwork, row: sqlite3.Row, boards: dict[int, Board],
              destination: str) -> _Out | None:
    """The queued row as it leaves for `destination` (a QWK ID), or None for
    a post gone since, a board no longer carrying the conference, or a post
    whose route already names the destination."""
    from netbbs.qwk.offline import packet_post_text

    post = visible_post(db, row["post_id"])
    board = boards.get(row["number"])
    if post is None or board is None or board.id != post.board_id:
        return None
    info = db.connection.execute(
        "SELECT msgid, route FROM qwknet_posts WHERE post_id = ?", (post.post_id,)
    ).fetchone()
    route = info["route"] if info is not None else ""
    if destination in route.split("/"):
        return None
    local = not route
    to, reply_msgid, parent_row_id, parent_hub_number = "All", "", None, None
    if post.parent_post_id is not None:
        parent = db.connection.execute(
            "SELECT p.id, p.author_label, q.msgid, q.hub_number FROM posts p "
            "LEFT JOIN qwknet_posts q ON q.post_id = p.post_id WHERE p.post_id = ?",
            (post.parent_post_id,),
        ).fetchone()
        if parent is not None:
            to = name_of_label(sanitize_text(parent["author_label"]))
            reply_msgid = parent["msgid"] or ""
            parent_row_id = parent["id"]
            parent_hub_number = parent["hub_number"]
    body = packet_post_text(db, board, post)
    if local:
        tagline = network.tagline or get_node_display_name(db)
        body = body.rstrip("\n") + f"\n---\n {TAGLINE_MARK} {PRODUCT} {TAGLINE_MARK} {sanitize_text(tagline)}"
    return _Out(
        seq=row["seq"], number=row["number"], post=post, to=to,
        sender=name_of_label(sanitize_text(post.author_label)) if not local else sanitize_text(post.author_label),
        subject=sanitize_text(post.subject), body=body,
        msgid=info["msgid"] if info is not None else make_msgid(post, network.our_id), reply_msgid=reply_msgid,
        route=route, local=local, parent_row_id=parent_row_id, parent_hub_number=parent_hub_number,
    )


def _headers(out: _Out) -> dict[str, str]:
    fields = {"Message-ID": out.msgid, "Sender": out.sender, "To": out.to, "Subject": out.subject}
    if out.reply_msgid:
        fields["In-Reply-To"] = out.reply_msgid
    if out.route:
        fields["SenderNetAddr"] = out.route
    return fields


def _local_time(db: Database, iso: str) -> datetime.datetime:
    try:
        moment = parse_utc_iso(iso)
    except (ValueError, TypeError):
        return datetime.datetime(1980, 1, 1)
    return moment.astimezone(get_node_timezone(db)).replace(tzinfo=None)


@dataclass
class UplinkRep:
    """What the next upload to the hub carries: `data` None when nothing
    goes, though `upto` may still move past rows that had nothing to send."""

    data: bytes | None
    upto: int
    messages: int


def build_uplink_rep(db: Database, network: QwknetNetwork) -> UplinkRep:
    """The reply packet for the hub: queued posts after `uplink_seq` that
    did not come from the hub, up to `MAX_REP_MESSAGES`."""
    rows = db.connection.execute(
        "SELECT * FROM qwknet_outbound WHERE network_id = ? AND seq > ? AND from_uplink = 0 ORDER BY seq LIMIT ?",
        (network.id, network.uplink_seq, MAX_REP_MESSAGES),
    ).fetchall()
    boards = network_conferences(db, network.id)
    replies: list[OutgoingReply] = []
    upto = network.uplink_seq
    for row in rows:
        upto = row["seq"]
        out = _outgoing(db, network, row, boards, network.hub_id)
        if out is None:
            continue
        replies.append(OutgoingReply(
            conference=out.number, to=out.to, sender=out.sender, subject=out.subject, body=out.body,
            reference=out.parent_hub_number, when=_local_time(db, out.post.created_at),
            headers=_headers(out), tagline=out.local,
        ))
    data = build_rep(network.hub_id, replies) if replies else None
    return UplinkRep(data, upto, len(replies))


def confirm_uplink(db: Database, network_id: int, upto: int) -> None:
    """The hub took the upload: its cursor moves to `upto`."""
    db.connection.execute(
        "UPDATE qwknet_networks SET uplink_seq = MAX(uplink_seq, ?) WHERE id = ?", (upto, network_id)
    )
    db.connection.commit()


def pending_for_uplink(db: Database, network: QwknetNetwork) -> int:
    return db.connection.execute(
        "SELECT COUNT(*) FROM qwknet_outbound WHERE network_id = ? AND seq > ? AND from_uplink = 0",
        (network.id, network.uplink_seq),
    ).fetchone()[0]


def pending_for_node(db: Database, node: QwknetNode) -> int:
    carried = node_conferences(db, node.id)
    if not carried:
        return 0
    marks = ", ".join("?" * len(carried))
    return db.connection.execute(
        f"SELECT COUNT(*) FROM qwknet_outbound WHERE network_id = ? AND seq > ? "
        f"AND (source_node_id IS NULL OR source_node_id != ?) AND number IN ({marks})",
        (node.network_id, node.sent_seq, node.id, *sorted(carried)),
    ).fetchone()[0]


def build_node_packet(db: Database, network: QwknetNetwork, node: QwknetNode) -> bytes | None:
    """`node`'s packet: the one built earlier and not yet confirmed, or a new
    one of queued posts after its cursor in the conferences it carries, up
    to its message limit and `MAX_PACKET_TEXT_BYTES`. None when nothing
    waits; rows that had nothing for it move its cursor at once."""
    row = db.connection.execute(
        "SELECT pending_packet FROM qwknet_nodes WHERE id = ?", (node.id,)
    ).fetchone()
    if row is not None and row["pending_packet"] is not None:
        return bytes(row["pending_packet"])
    carried = node_conferences(db, node.id)
    boards = {number: board for number, board in network_conferences(db, network.id).items() if number in carried}
    if not boards:
        return None
    marks = ", ".join("?" * len(boards))
    rows = db.connection.execute(
        f"SELECT * FROM qwknet_outbound WHERE network_id = ? AND seq > ? "
        f"AND (source_node_id IS NULL OR source_node_id != ?) AND number IN ({marks}) ORDER BY seq LIMIT ?",
        (network.id, node.sent_seq, node.id, *sorted(boards), node.packet_messages),
    ).fetchall()
    messages: list[PacketMessage] = []
    upto = node.sent_seq
    budget = MAX_PACKET_TEXT_BYTES
    for queued in rows:
        out = _outgoing(db, network, queued, boards, node.qwk_id)
        if out is not None and out.post.id <= _MAX_MESSAGE_NUMBER:
            cost = 512 + len(out.body.encode("utf-8", errors="replace"))
            if messages and cost > budget:
                break
            budget -= cost
            messages.append(PacketMessage(
                conference=out.number, number=out.post.id, when=_local_time(db, out.post.created_at), to=out.to,
                sender=out.sender, subject=out.subject, body=out.body,
                reference=out.parent_row_id if out.parent_row_id and out.parent_row_id <= _MAX_MESSAGE_NUMBER
                else None,
                headers=_headers(out), tagline=out.local,
            ))
        upto = queued["seq"]
    if not messages:
        if upto != node.sent_seq:
            db.connection.execute("UPDATE qwknet_nodes SET sent_seq = ? WHERE id = ?", (upto, node.id))
            db.connection.commit()
        return None
    info = PacketInfo(
        bbs_id=network.our_id, bbs_name=sanitize_text(get_node_display_name(db)), user_name=node.qwk_id,
        created_at=_local_time(db, utc_now_iso()), door=PRODUCT, version=__version__,
    )
    conferences = [Conference(0, "E-mail", "pP")] + [
        Conference(number, sanitize_text(board.name), "aOE") for number, board in sorted(boards.items())
    ]
    data = build_packet(info, conferences, messages)
    db.connection.execute(
        "UPDATE qwknet_nodes SET pending_packet = ?, pending_seq = ?, pending_at = ? WHERE id = ?",
        (data, upto, utc_now_iso(), node.id),
    )
    db.connection.commit()
    return data


def confirm_node_packet(db: Database, node_id: int, size: int) -> bool:
    """The node says it received `size` bytes: when that is its pending
    packet's size, the cursor moves past what the packet held. False for a
    wrong size or no packet waiting (nothing changes)."""
    row = db.connection.execute(
        "SELECT pending_packet, pending_seq FROM qwknet_nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if row is None or row["pending_packet"] is None or len(row["pending_packet"]) != size:
        return False
    db.connection.execute(
        "UPDATE qwknet_nodes SET sent_seq = MAX(sent_seq, ?), pending_packet = NULL, pending_seq = NULL, "
        "pending_at = NULL WHERE id = ?",
        (row["pending_seq"], node_id),
    )
    db.connection.commit()
    return True


# --- upkeep ---------------------------------------------------------------------------


def prune(db: Database) -> None:
    """Daily: forget old Message-IDs and held items, and drop queued rows
    every destination has taken (or none took for `OUTBOUND_DAYS`)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    db.connection.execute("DELETE FROM qwknet_seen WHERE seen_at < ?",
                          (utc_iso(now - datetime.timedelta(days=SEEN_DAYS)),))
    db.connection.execute("DELETE FROM qwknet_held WHERE created_at < ?",
                          (utc_iso(now - datetime.timedelta(days=HELD_DAYS)),))
    db.connection.execute("DELETE FROM qwknet_outbound WHERE created_at < ?",
                          (utc_iso(now - datetime.timedelta(days=OUTBOUND_DAYS)),))
    for network in db.connection.execute("SELECT id, hub_id, uplink_seq FROM qwknet_networks").fetchall():
        cursors = [row["sent_seq"] for row in db.connection.execute(
            "SELECT sent_seq FROM qwknet_nodes WHERE network_id = ?", (network["id"],))]
        if network["hub_id"]:
            cursors.append(network["uplink_seq"])
        if cursors:
            db.connection.execute("DELETE FROM qwknet_outbound WHERE network_id = ? AND seq <= ?",
                                  (network["id"], min(cursors)))
    db.connection.commit()


__all__ = [
    "Incoming",
    "TossResult",
    "build_node_packet",
    "build_uplink_rep",
    "confirm_node_packet",
    "confirm_uplink",
    "queue_post_if_qwknet",
    "retoss_conference",
    "toss_hub_packet",
    "toss_node_rep",
]
