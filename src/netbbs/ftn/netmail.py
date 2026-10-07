"""
Caller netmail: personal mail to an FTN address (design doc §6.8,
Decisions 4 and 6).

A caller writes to `Name (zone:net/node[.point])` on Mail's To line -- the
form an FTN author is shown with on a board and a netmail sender in the
Inbox, so Reply fills it in. `@` stays Link's (`user@node`).

- The letter goes out on the enabled network whose address is in the
  recipient's zone (the first by name, if several are).
- Sending needs the network's `netmail_min_level`, SysOp only until the
  SysOp lowers it: netmail leaves under the node's address.
- `send_netmail` keeps the sender's Sent copy (`recipient_remote_address`
  holds the FTN form, never `@`, so nothing Link reads it as a Link
  address) and queues the encoded netmail for the uplink in the same
  transaction, with INTL, FMPT/TOPT where points need them, MSGID, PID,
  TZUTC and CHRS. Netmail goes to one person at a time.
- It is routed direct when the network's nodelist lists where the
  destination (a point's boss node) answers BinkP, and via the uplink
  otherwise (Decision 5); the mailer falls back to the uplink when direct
  calls keep failing.
"""

from __future__ import annotations

import datetime
import re

from netbbs.auth.users import User
from netbbs.ftn import FtnFormatError
from netbbs.ftn.address import FtnAddress, parse_address
from netbbs.ftn.chrs import MAX_NAME_BYTES
from netbbs.ftn.message import FtnMessage, encode_message, format_msgid, format_tzutc
from netbbs.ftn.networks import FtnNetwork, list_networks
from netbbs.ftn.nodelist import direct_route
from netbbs.ftn.packet import ATTR_PRIVATE, pack_message
from netbbs.ftn.queue import FtnQueueFullError, enqueue_outbound_without_commit, next_msgid_serial_without_commit
from netbbs.ftn.scanner import PRODUCT
from netbbs.mail import MailError, MailMessage, get_mail, validate_mail_fields
from netbbs.permissions.levels import meets_level
from netbbs.search import index_mail_without_commit
from netbbs.storage.database import Database
from netbbs.timeutil import get_node_timezone, utc_now_iso

_RECIPIENT = re.compile(r"(?P<name>.*?)\s*\((?P<address>\d+:\d+/\d+(?:\.\d+)?)\)\s*")


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
    return None


def netmail_notice(db: Database, text: str) -> str | None:
    """The compose screen's line under To for a netmail, or None."""
    parsed = parse_ftn_recipient(text)
    network = network_for(db, parsed[1]) if parsed else None
    if network is None:
        return None
    return f"Netmail via {network.name}. Not private: every system it passes through can read it."


def send_netmail(db: Database, sender: User, text: str, subject: str, body: str) -> MailMessage:
    """Keep the Sent copy and queue the netmail; raises `MailError` with a
    reason the caller is shown."""
    refusal = netmail_refusal(db, sender, text)
    if refusal is not None:
        raise MailError(refusal)
    subject = validate_mail_fields(subject, body)
    name, address = parse_ftn_recipient(text)
    network = network_for(db, address)
    label = format_ftn_recipient(name, address)
    created_at = utc_now_iso()
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
        packed = _encode(db, network, sender, name, address, subject, body)
        # Decision 5: direct when the nodelist lists where the node (a
        # point's boss) answers, unless that node is the uplink anyway.
        direct = direct_route(db, network.id, address)
        route = "direct" if direct is not None and not direct[0].same_node(network.uplink_address) else "uplink"
        enqueue_outbound_without_commit(
            db, network.id, kind="netmail", reference_id=str(mail_id), destination=str(address), packed=packed,
            route=route,
        )
    except FtnQueueFullError as exc:
        db.connection.rollback()
        raise MailError(f"netmail to {network.name} can't be queued now: {exc}") from exc
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()
    return get_mail(db, sender, mail_id)


def _encode(db, network: FtnNetwork, sender: User, name: str, address: FtnAddress, subject: str, body: str) -> bytes:
    ours = network.our_address
    moment = datetime.datetime.now(datetime.timezone.utc).astimezone(get_node_timezone(db))
    kludges = [("INTL", f"{address.zone}:{address.net}/{address.node} {ours.zone}:{ours.net}/{ours.node}")]
    if ours.point:
        kludges.append(("FMPT", str(ours.point)))
    if address.point:
        kludges.append(("TOPT", str(address.point)))
    kludges += [
        ("MSGID", format_msgid(ours, next_msgid_serial_without_commit(db))),
        ("PID", PRODUCT),
        ("TZUTC", format_tzutc(moment.utcoffset() or datetime.timedelta(0))),
    ]
    message = FtnMessage(
        to_name=name, from_name=sender.username, subject=subject, body=body, kludges=kludges,
        date=moment.replace(tzinfo=None), tear_line=PRODUCT, attributes=ATTR_PRIVATE,
        orig_net=ours.net, orig_node=ours.node, dest_net=address.net, dest_node=address.node,
    )
    return pack_message(encode_message(message))
