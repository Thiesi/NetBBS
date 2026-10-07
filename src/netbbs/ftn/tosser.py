"""
The tosser: inbound FTN packets into boards and Mail (design doc §6.8).

`toss_packet` takes one packet a session received for a network:

- **Packet checks.** A packet from an unsecure session, one addressed to
  another node, or one whose packet password doesn't match is held for
  the SysOp whole (`netbbs.ftn.queue.hold_inbound`), not tossed.
- **Echomail** becomes a post on the board carrying its area:
  - the author is labelled `Name (zone:net/node[.point])`, never with `@`;
  - the board's own moderation applies, through `create_labelled_post`;
  - `^AREPLY` threads it under the post with that MSGID;
  - its date, corrected by `^ATZUTC`, is the post's date. A date in the
    future is today, so a wrong clock elsewhere can't pin a message to
    the top.

  An area no board carries is skipped and counted, which is normal while
  a hub still sends an echo the SysOp has unmapped.
- **Netmail** addressed to this node is delivered to the account whose
  username matches its To name, case-insensitively. A name matching no
  account, or an account that takes no mail, goes to the SysOp with a
  line saying who it was for (Decision 7). Netmail for another node is
  not routed: this node is not a hub. It is skipped and counted.
- **Duplicates** are dropped by MSGID, or by a hash of the message when
  it has none, per area (netmail has its own).
- **A message that can't be stored** (a full mailbox, a closed board) is
  held as a one-message packet with the reason, so nothing is lost. Its
  MSGID is not recorded, so releasing it later tosses it.

A packet's messages are tossed in one transaction each, so a failure part
way through keeps what came before it.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from dataclasses import dataclass, field

from netbbs.auth.users import AuthError, User, get_user_by_username, is_usable_sysop, list_users
from netbbs.boards.boards import get_board_by_id
from netbbs.boards.limits import MAX_BODY_BYTES
from netbbs.boards.posts import PostError, create_labelled_post
from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.message import FtnMessage, decode_message
from netbbs.ftn.networks import FtnNetwork, board_id_for_area
from netbbs.ftn.packet import PackedMessage, PacketHeader, build_packet, parse_packet
from netbbs.ftn.queue import hold_inbound, record_seen_msgid_without_commit
from netbbs.mail import (
    MAX_MAIL_BODY_BYTES,
    MAX_MAIL_SUBJECT_BYTES,
    MailboxFullError,
    MailError,
    deliver_external_mail_without_commit,
    mail_recipient_refusal,
)
from netbbs.search import reindex_post
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso, utc_now_iso

_logger = logging.getLogger(__name__)

NETMAIL_AREA = ""  # the dupe history's area for netmail
_CUT_NOTE = "\n\n[The rest of this message was cut: it was longer than this node keeps.]"


@dataclass
class TossResult:
    posts: int = 0
    netmail: int = 0
    duplicates: int = 0
    unknown_areas: dict[str, int] = field(default_factory=dict)
    not_for_us: int = 0
    held: int = 0  # messages, or a whole packet, held for the SysOp
    lost: int = 0  # held store full: nothing could keep them
    truncated_packet: bool = False
    refused_packet: str | None = None  # why the whole packet was held


def toss_packet(
    db: Database, network: FtnNetwork, data: bytes, *, secure: bool, remote_address: str, file_name: str,
) -> TossResult:
    """Toss one packet received for `network`; see the module docstring."""
    result = TossResult()
    if not secure:
        return _hold_packet(db, network, data, remote_address, file_name, "unsecure session", result)
    try:
        packet = parse_packet(data)
    except FtnFormatError as exc:
        return _hold_packet(db, network, data, remote_address, file_name, f"unreadable packet: {exc}", result)
    header = packet.header
    if not _addressed_to(header.dest, network.our_address):
        return _hold_packet(db, network, data, remote_address, file_name,
                            f"addressed to {header.dest.four_d}, not to this node", result)
    if network.packet_password and header.password.upper() != network.packet_password.upper():
        return _hold_packet(db, network, data, remote_address, file_name, "packet password does not match", result)

    result.truncated_packet = packet.truncated
    for packed in packet.messages:
        try:
            message = decode_message(packed, default_charset=network.default_charset)
            if message.is_echomail:
                _toss_echomail(db, network, message, packed, header, result)
            else:
                _toss_netmail(db, network, message, packed, header, result)
        except (_Unstorable, FtnFormatError) as exc:
            db.connection.rollback()
            _hold_message(db, network, header, packed, data, remote_address, file_name, str(exc), result)
    if result.unknown_areas or result.not_for_us or result.held or result.truncated_packet:
        _logger.warning(
            "FTN %s: packet %s from %s -- %d posts, %d netmail, %d duplicates, unknown areas %s, "
            "%d netmail for other nodes, %d held%s",
            network.name, file_name, remote_address, result.posts, result.netmail, result.duplicates,
            dict(result.unknown_areas), result.not_for_us, result.held,
            "; the packet was cut off, so messages after the break were not received"
            if result.truncated_packet else "",
        )
    return result


class _Unstorable(Exception):
    """This message can't be stored here; it is held with this reason."""


def _toss_echomail(db, network, message: FtnMessage, packed, header, result: TossResult) -> None:
    board_id = board_id_for_area(db, network.id, message.area)
    if board_id is None:
        tag = message.area.upper()
        result.unknown_areas[tag] = result.unknown_areas.get(tag, 0) + 1
        return
    board = get_board_by_id(db, board_id)
    if not record_seen_msgid_without_commit(db, network.id, message.area.upper(), _dupe_key(message, packed)):
        db.connection.rollback()
        result.duplicates += 1
        return
    parent_post_id = None
    if message.reply:
        row = db.connection.execute(
            "SELECT post_id FROM posts WHERE board_id = ? AND ftn_msgid = ?", (board.id, message.reply)
        ).fetchone()
        parent_post_id = row["post_id"] if row is not None else None
    label, body, written_at = author_label(message, network), _fit(message.body, MAX_BODY_BYTES), _written_at(message)
    # The same author, text and date as a post already here is the same
    # message, whatever its MSGID says (it would get the same content id).
    if db.connection.execute(
        "SELECT 1 FROM posts WHERE board_id = ? AND author_label = ? AND subject = ? AND body = ? AND created_at = ?",
        (board.id, label, message.subject, body, written_at),
    ).fetchone() is not None:
        db.connection.rollback()
        result.duplicates += 1
        return
    try:
        post = create_labelled_post(
            db, board, label, message.subject, body,
            commit=False, parent_post_id=parent_post_id, created_at=written_at,
        )
    except PostError as exc:
        raise _Unstorable(f"board {board.name!r} refused it: {exc}") from exc
    db.connection.execute(
        "UPDATE posts SET ftn_msgid = ?, ftn_inbound = 1 WHERE post_id = ?", (message.msgid, post.post_id)
    )
    db.connection.commit()
    reindex_post(db, board.id, post.post_id)
    result.posts += 1


def _toss_netmail(db, network, message: FtnMessage, packed, header: PacketHeader, result: TossResult) -> None:
    destination = netmail_destination(message, header)
    if not _addressed_to(destination, network.our_address):
        result.not_for_us += 1
        return
    if not record_seen_msgid_without_commit(db, network.id, NETMAIL_AREA, _dupe_key(message, packed)):
        db.connection.rollback()
        result.duplicates += 1
        return
    recipient, note = _netmail_recipient(db, message.to_name)
    body = message.body if note is None else f"{note}\n\n{message.body}"
    try:
        deliver_external_mail_without_commit(
            db, recipient,
            sender_label=_label(message.from_name, netmail_origin(message, header)),
            subject=_fit(message.subject.strip() or "(no subject)", MAX_MAIL_SUBJECT_BYTES, note=""),
            body=_fit(body, MAX_MAIL_BODY_BYTES),
            created_at=_written_at(message),
        )
    except (MailboxFullError, MailError) as exc:
        raise _Unstorable(f"could not deliver to {recipient.username}: {exc}") from exc
    db.connection.commit()
    result.netmail += 1


def _netmail_recipient(db: Database, to_name: str) -> tuple[User, str | None]:
    """The account a netmail is for, or the SysOp with a note saying who it
    was for (Decision 7)."""
    name = to_name.strip()
    try:
        user = get_user_by_username(db, name)
    except AuthError:
        user = None
    if user is not None and mail_recipient_refusal(db, user) is None:
        return user, None
    sysops = sorted((u for u in list_users(db) if is_usable_sysop(u)), key=lambda u: u.id)
    if not sysops:
        raise _Unstorable(f"no account named {name!r} and no SysOp to give it to")
    return sysops[0], f"[This netmail was addressed to {name!r}, who has no mailbox on this node.]"


def author_label(message: FtnMessage, network: FtnNetwork) -> str:
    """`Name (zone:net/node[.point])` for an echomail's author."""
    address = message.origin_address
    if address is None:
        address = FtnAddress(network.our_address.zone, message.orig_net, message.orig_node)
    return _label(message.from_name, address)


def netmail_origin(message: FtnMessage, header: PacketHeader) -> FtnAddress:
    """Where a netmail came from: INTL's origin zone and net/node (else the
    message header's, in the packet's zone), with FMPT's point."""
    zone, net, node = header.orig.zone, message.orig_net, message.orig_node
    intl = (message.kludge("INTL") or "").split()
    if len(intl) == 2:
        zone, net, node = _zone_net_node(intl[1], (zone, net, node))
    return FtnAddress(zone, net, node, _point(message.kludge("FMPT")))


def netmail_destination(message: FtnMessage, header: PacketHeader) -> FtnAddress:
    """Where a netmail is going: INTL's destination (else the message
    header's, in the packet's zone), with TOPT's point."""
    zone, net, node = header.dest.zone, message.dest_net, message.dest_node
    intl = (message.kludge("INTL") or "").split()
    if len(intl) == 2:
        zone, net, node = _zone_net_node(intl[0], (zone, net, node))
    return FtnAddress(zone, net, node, _point(message.kludge("TOPT")))


def _label(name: str, address: FtnAddress) -> str:
    # `@` would make the label read as a Link address (`user@node`), and
    # control characters have no business in a name.
    clean = " ".join("".join(c for c in name if c.isprintable()).replace("@", " at ").split()) or "Unknown"
    return f"{clean} ({address.four_d})"


def _addressed_to(address: FtnAddress, ours: FtnAddress) -> bool:
    # A stone-age Type 2 header may carry zone 0: net/node/point decide then.
    if address.zone and ours.zone and address.zone != ours.zone:
        return False
    return (address.net, address.node, address.point) == (ours.net, ours.node, ours.point)


def _zone_net_node(text: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        zone, _, rest = text.partition(":")
        net, _, node = rest.partition("/")
        return int(zone), int(net), int(node.split(".")[0])
    except ValueError:
        return fallback


def _point(value: str | None) -> int:
    try:
        point = int((value or "0").split()[0])
    except (ValueError, IndexError):
        return 0
    return point if 0 <= point <= 0xFFFF else 0


def _dupe_key(message: FtnMessage, packed: PackedMessage) -> str:
    if message.msgid:
        return message.msgid
    digest = hashlib.sha256(b"\0".join((packed.from_name, packed.subject, packed.date, packed.text)))
    return "hash:" + digest.hexdigest()[:32]


def _written_at(message: FtnMessage) -> str:
    """The post's date: the message's own, in UTC when TZUTC says how and
    read as UTC when nothing does; never later than now."""
    now = datetime.datetime.now(datetime.timezone.utc)
    moment = message.utc_date()
    if moment is None and message.date is not None:
        moment = message.date.replace(tzinfo=datetime.timezone.utc)
    if moment is None or moment > now:
        return utc_now_iso()
    return utc_iso(moment)


def _fit(text: str, max_bytes: int, *, note: str = _CUT_NOTE) -> str:
    """`text`, cut with `note` if its UTF-8 is over `max_bytes`. A CP437
    subject of 71 bytes can be over 200 in UTF-8."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    room = max_bytes - len(note.encode("utf-8"))
    return text.encode("utf-8")[:room].decode("utf-8", errors="ignore") + note


def _hold_packet(db, network, data, remote_address, file_name, reason, result: TossResult) -> TossResult:
    result.refused_packet = reason
    if hold_inbound(db, network_id=network.id, remote_address=remote_address, file_name=file_name,
                    content=data, reason=reason):
        result.held += 1
    else:
        result.lost += 1
    _logger.warning("FTN %s: packet %s from %s held: %s", network.name, file_name, remote_address, reason)
    return result


def _hold_message(db, network, header, packed, data, remote_address, file_name, reason, result: TossResult) -> None:
    try:
        single = build_packet(header, [packed])
    except (FtnFormatError, ValueError):
        # What can't be written back as a packet of its own is held as the
        # whole packet it came in; what was tossed from it is a duplicate then.
        single = data
    if hold_inbound(db, network_id=network.id, remote_address=remote_address, file_name=file_name,
                    content=single, reason=reason):
        result.held += 1
    else:
        result.lost += 1
        _logger.error("FTN %s: a message in %s was lost (held store full): %s", network.name, file_name, reason)
