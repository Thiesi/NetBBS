"""
Caller netmail: personal mail to an FTN address (design doc §6.8,
Decisions 4 and 6; issues #1470 and #1471).

A caller writes to `Name (zone:net/node[.point])` on Mail's To line -- the
form an FTN author is shown with on a board and a netmail sender in the
Inbox, so Reply fills it in. `@` stays Link's (`user@node`).

- The letter goes out on the enabled network whose address is in the
  recipient's zone (the first by name, if several are).
- Sending needs the network's `netmail_min_level`, SysOp only until the
  SysOp lowers it: netmail leaves under the node's address.
- `send_netmail` keeps the sender's Sent copy (`recipient_remote_address`
  holds the FTN form, never `@`, so nothing Link reads it as a Link
  address) and queues the encoded netmail in the same transaction, with
  INTL, FMPT/TOPT where points need them, MSGID, PID, TZUTC and CHRS.
  Netmail goes to one person at a time.
- **Flavour** (issue #1471, `netbbs.ftn.outbound`): `normal` is routed
  direct when the network's nodelist lists where the destination (a
  point's boss node) answers BinkP, and via the uplink otherwise; the
  mailer falls back to the uplink when direct calls keep failing. `crash`
  goes direct only, and is refused when the nodelist lists no BinkP
  address for the destination (unless it is the uplink itself). `hold`
  waits for the destination to call.
- **Points and downlinks** (issues #1478, #1479, `netbbs.ftn.links`): a
  netmail to a point of this node, or to a downlink or one of its points,
  waits in that link's own outbound for a session its password proves --
  any flavour, since the link calls in (or a polled downlink is called).
  A point this node does not serve is refused.
- **File attach** (issue #1470): a SysOp's netmail sends the files it
  points at (§6.4's file refs) with it, as FTS-0001's FileAttach: the
  netmail's subject on the network is the files' names, and each file is
  queued in `ftn_outbound_files` on the netmail's route and flavour. A
  file is at most the node's upload limit and BinkP's 64 MiB. Anyone
  else's file refs go as text lines, as a letter to another BBS does.
"""

from __future__ import annotations

import datetime
import re

from netbbs.auth.users import User, is_usable_sysop
from netbbs.config import get_max_upload_bytes
from netbbs.file_refs import FileRef, write_mail_refs_without_commit
from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress, parse_address
from netbbs.ftn.binkp import MAX_FILE_BYTES, safe_file_name
from netbbs.ftn.chrs import MAX_NAME_BYTES, MAX_SUBJECT_BYTES
from netbbs.ftn.links import is_own_point, link_for_destination
from netbbs.ftn.message import FtnMessage, encode_message, format_msgid, format_tzutc
from netbbs.ftn.networks import FtnNetwork, list_networks
from netbbs.ftn.nodelist import direct_route
from netbbs.ftn.outbound import CRASH, FLAVOURS, HOLD, NORMAL, queue_file_without_commit
from netbbs.ftn.packet import ATTR_CRASH, ATTR_FILE_ATTACHED, ATTR_HOLD, ATTR_PRIVATE, pack_message
from netbbs.ftn.queue import FtnQueueFullError, enqueue_outbound_without_commit, next_msgid_serial_without_commit
from netbbs.ftn.scanner import PRODUCT
from netbbs.mail import MailError, MailMessage, get_mail, validate_mail_fields
from netbbs.permissions.levels import meets_level
from netbbs.search import index_mail_without_commit
from netbbs.storage.database import Database
from netbbs.timeutil import get_node_timezone, utc_now_iso

_RECIPIENT = re.compile(r"(?P<name>.*?)\s*\((?P<address>\d+:\d+/\d+(?:\.\d+)?)\)\s*")

FLAVOUR_LABELS = {NORMAL: "Normal", CRASH: "Crash", HOLD: "Hold"}


def parse_ftn_recipient(text: str) -> tuple[str, FtnAddress] | None:
    """`(name, address)` for `Name (zone:net/node[.point])`, else None."""
    match = _RECIPIENT.fullmatch(text.strip())
    if match is None or "@" in text:
        return None
    try:
        address = parse_address(match["address"])
    except FtnFormatError:
        return None
    name = " ".join(match["name"].split())
    return (name, address) if name else None


def format_ftn_recipient(name: str, address: FtnAddress) -> str:
    """The To and Sent form: `Name (zone:net/node[.point])`."""
    return f"{name} ({address.four_d})"


def is_ftn_recipient(text: str | None) -> bool:
    return text is not None and parse_ftn_recipient(text) is not None


def network_for(db: Database, address: FtnAddress) -> FtnNetwork | None:
    """The enabled network a netmail to `address` goes out on."""
    for network in list_networks(db):
        if network.enabled and network.our_address.zone == address.zone:
            return network
    return None


def netmail_refusal(db: Database, sender: User, text: str) -> str | None:
    """Why `sender` can't send netmail to `text`, in the words the To prompt
    shows, or None."""
    parsed = parse_ftn_recipient(text)
    if parsed is None:
        return "That is not an FTN address: write it as Name (zone:net/node)."
    name, address = parsed
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        return f"An FTN name is at most {MAX_NAME_BYTES} characters."
    network = network_for(db, address)
    if network is None:
        return f"This BBS sends no netmail to zone {address.zone}."
    if not meets_level(sender, network.netmail_min_level):
        return f"Sending netmail on {network.name} needs level {network.netmail_min_level}."
    if is_own_point(network, address) and link_for_destination(db, network.id, address) is None:
        return f"This BBS has no point {address.four_d} on {network.name}."
    return None


def netmail_notice(db: Database, text: str) -> str | None:
    """The compose screen's line under To for a netmail, or None."""
    parsed = parse_ftn_recipient(text)
    network = network_for(db, parsed[1]) if parsed else None
    if network is None:
        return None
    return f"Netmail via {network.name}. Not private: every system it passes through can read it."


def may_attach_files(sender: User) -> bool:
    """Whether `sender`'s netmail sends its files rather than naming them
    (issue #1470): the SysOp's only."""
    return is_usable_sysop(sender)


def attach_limit(db: Database) -> int:
    """The most one attached file may be: the node's upload limit, at most
    BinkP's per-file 64 MiB -- the limit a file echo's file meets too (§16
    "Issue #1154", Decision 4)."""
    return min(get_max_upload_bytes(db), MAX_FILE_BYTES)


def attach_send_name(ref: FileRef) -> str:
    """The name an attached file goes out under, and is named by in the
    netmail's subject."""
    return safe_file_name(ref.filename)


def attach_refusal(db: Database, files: list[FileRef]) -> str | None:
    """Why these files can't all go with one netmail, or None: one too
    large or gone, two under the same name, or names too long together
    for FTS-0001's 71-byte subject that carries them."""
    if not files:
        return None
    limit = attach_limit(db)
    names: list[str] = []
    for ref in files:
        row = db.connection.execute(
            "SELECT size_bytes, status FROM files WHERE file_id = ?", (ref.file_id,)
        ).fetchone()
        if row is None or row["status"] != "approved":
            return f"{ref.filename} is no longer available."
        if row["size_bytes"] > limit:
            return f"{ref.filename} is {row['size_bytes']} bytes; a file sent with netmail is at most {limit}."
        name = attach_send_name(ref)
        if name.casefold() in {other.casefold() for other in names}:
            return f"Two of the files would go out as {name}; attach one of them."
        names.append(name)
    if len(" ".join(names).encode("ascii")) > MAX_SUBJECT_BYTES:
        return (f"The files' names carry the netmail's subject, which holds {MAX_SUBJECT_BYTES} characters: "
                "attach fewer files.")
    return None


def route_for(db: Database, network: FtnNetwork, address: FtnAddress) -> str:
    """`direct` when the nodelist lists where the node (a point's boss)
    answers, unless that node is the uplink anyway; `uplink` otherwise."""
    direct = direct_route(db, network.id, address)
    return "direct" if direct is not None and not direct[0].same_node(network.uplink_address) else "uplink"


def _is_uplink(network: FtnNetwork, address: FtnAddress) -> bool:
    return FtnAddress(address.zone, address.net, address.node).same_node(network.uplink_address)


def flavour_refusal(db: Database, text: str, flavour: str) -> str | None:
    """Why a netmail to `text` can't go with `flavour`, or None. Crash goes
    straight to its destination, so the nodelist must say where that
    answers -- unless the destination is the uplink itself."""
    if flavour not in FLAVOURS:
        return f"{flavour!r} is not a netmail flavour."
    parsed = parse_ftn_recipient(text)
    network = network_for(db, parsed[1]) if parsed else None
    if flavour != CRASH or network is None:
        return None
    address = parsed[1]
    if link_for_destination(db, network.id, address) is not None:
        return None  # it waits for its point or downlink, whatever the flavour
    if _is_uplink(network, address) or route_for(db, network, address) == "direct":
        return None
    return (f"Crash netmail goes straight to {address.four_d}, and the {network.name} nodelist lists no BinkP "
            "address for it. Send it Normal, through the hub.")


def flavour_route(db: Database, network: FtnNetwork, address: FtnAddress, flavour: str) -> str:
    """Where a netmail of `flavour` to `address` is queued: `hold` waits for
    the destination itself to call, so it is routed direct unless the
    destination is the uplink; crash and normal as `route_for` says."""
    if link_for_destination(db, network.id, address) is not None:
        return "direct"  # the link's own outbound (issues #1478, #1479)
    if flavour == HOLD:
        return "uplink" if _is_uplink(network, address) else "direct"
    if flavour == CRASH and _is_uplink(network, address):
        return "uplink"
    return route_for(db, network, address)


def send_netmail(
    db: Database, sender: User, text: str, subject: str, body: str, *, flavour: str = NORMAL,
    attach: list[FileRef] = (),
) -> MailMessage:
    """Keep the Sent copy and queue the netmail; raises `MailError` with a
    reason the caller is shown. `attach` are files that go with it (a
    SysOp's only, `may_attach_files`); the Sent copy points at them."""
    refusal = netmail_refusal(db, sender, text) or flavour_refusal(db, text, flavour)
    attach = list(attach)
    if refusal is None and attach:
        refusal = ("Only the SysOp sends files with netmail." if not may_attach_files(sender)
                   else attach_refusal(db, attach))
    if refusal is not None:
        raise MailError(refusal)
    subject = validate_mail_fields(subject, body)
    name, address = parse_ftn_recipient(text)
    network = network_for(db, address)
    label = format_ftn_recipient(name, address)
    created_at = utc_now_iso()
    route = flavour_route(db, network, address, flavour)
    try:
        db.connection.execute(
            """
            INSERT INTO mail_messages
                (sender_user_id, sender_label, recipient_remote_address, recipient_label, subject, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sender.id, sender.username, label, label, subject, body, created_at),
        )
        mail_id = db.connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        index_mail_without_commit(db, mail_id)
        names = [attach_send_name(ref) for ref in attach]
        attributes = ATTR_PRIVATE | {NORMAL: 0, CRASH: ATTR_CRASH, HOLD: ATTR_HOLD}[flavour]
        if attach:
            write_mail_refs_without_commit(db, mail_id, attach)
            attributes |= ATTR_FILE_ATTACHED
        packed = encode_netmail(
            db, network, from_name=sender.username, to_name=name, address=address,
            # FTS-0001: a FileAttach netmail's subject is the files' names.
            subject=" ".join(names) if attach else subject, body=body, attributes=attributes,
        )
        enqueue_outbound_without_commit(
            db, network.id, kind="netmail", reference_id=str(mail_id), destination=str(address), packed=packed,
            route=route, flavour=flavour,
        )
        for ref, send_name in zip(attach, names):
            queue_file_without_commit(
                db, network.id, destination=address, route=route, flavour=flavour, file_id=ref.file_id,
                send_name=send_name, size_bytes=ref.size_bytes, reference_kind="netmail",
                reference_id=str(mail_id),
            )
    except FtnQueueFullError as exc:
        db.connection.rollback()
        raise MailError(f"netmail to {network.name} can't be queued now: {exc}") from exc
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    return get_mail(db, sender, mail_id)


def sent_outcome(db: Database, text: str, flavour: str, files: int) -> str:
    """What the compose screen says once a netmail is queued."""
    parsed = parse_ftn_recipient(text)
    where = parsed[1].four_d if parsed else "its destination"
    with_files = f" with {files} file{'s' if files != 1 else ''}" if files else ""
    network = network_for(db, parsed[1]) if parsed else None
    link = link_for_destination(db, network.id, parsed[1]) if network is not None else None
    if link is not None:
        if link.polled:
            return f"Netmail queued{with_files} for {link.address.four_d}: this BBS calls it shortly."
        return f"Netmail queued{with_files}: it waits for {link.address.four_d} to call this BBS."
    if flavour == CRASH:
        return f"Crash netmail queued{with_files}: this BBS calls {where} now."
    if flavour == HOLD:
        return f"Netmail on hold{with_files}: it waits for {where} to call this BBS."
    return f"Netmail queued{with_files}. It goes out with this BBS's next call to its hub."


def encode_netmail(
    db: Database, network: FtnNetwork, *, from_name: str, to_name: str, address: FtnAddress, subject: str,
    body: str, attributes: int = ATTR_PRIVATE, reply: str | None = None,
) -> bytes:
    """A netmail from this node to `to_name` at `address`, packed, with
    INTL, FMPT/TOPT where points need them, MSGID, REPLY when given, PID
    and TZUTC."""
    ours = network.our_address
    moment = datetime.datetime.now(datetime.timezone.utc).astimezone(get_node_timezone(db))
    kludges = [("INTL", f"{address.zone}:{address.net}/{address.node} {ours.zone}:{ours.net}/{ours.node}")]
    if ours.point:
        kludges.append(("FMPT", str(ours.point)))
    if address.point:
        kludges.append(("TOPT", str(address.point)))
    kludges.append(("MSGID", format_msgid(ours, next_msgid_serial_without_commit(db))))
    if reply:
        kludges.append(("REPLY", reply))
    kludges += [
        ("PID", PRODUCT),
        ("TZUTC", format_tzutc(moment.utcoffset() or datetime.timedelta(0))),
    ]
    message = FtnMessage(
        to_name=to_name, from_name=from_name, subject=subject, body=body, kludges=kludges,
        date=moment.replace(tzinfo=None), tear_line=PRODUCT, attributes=attributes,
        orig_net=ours.net, orig_node=ours.node, dest_net=address.net, dest_node=address.node,
    )
    return pack_message(encode_message(message))
