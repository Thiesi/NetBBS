"""
Returning netmail no account here can take (design doc §6.8, "Inbound
delivery"; §16 "Issue #1154", Decisions 12 and 26; issue #1472).

A network bounces by default (`FtnNetwork.bounce_unknown`). A netmail for
this node that matches no account, whose To is a verified real name two
accounts share, or whose account takes no mail goes back to its sender as
a netmail from this node: `Mailer-Daemon` writes to the sender's name and
address, with the reason and the original's first lines quoted. With
bouncing off it goes to the SysOp, as before (§16 "Issue #166", Decision 7).

The loop guard (`bounce_refusal`) keeps a bounce from ever starting a
loop. Nothing is bounced

- to a robot name (`ROBOT_NAMES`, and the network's own AreaFix and
  FileFix names): a robot's message is never answered by a robot;
- that is a bounce itself: from a robot name, or with a subject that a
  bounce is written with (`Undeliverable:` and the like) -- so a bounce
  this node sends, which comes from `Mailer-Daemon` and says
  `Undeliverable:`, is never bounced by a node that runs these rules;
- to this node's own address;
- to an address that already got `MAX_BOUNCES_PER_DAY` bounces from this
  node in the last day.

Such a netmail goes to the SysOp instead, with a line saying why it was not
returned. A bounce is queued as a normal netmail (`reference_id`
`bounce:<key>`), so it counts against the outbound queue's bound and is
pruned with it; when that queue is full the netmail goes to the SysOp.
Netmail to `SysOp` or `Sysop` never reaches here: that name is an alias
that always reaches the SysOp (`netbbs.ftn.tosser`).
"""

from __future__ import annotations

import datetime

from netbbs.config import get_node_display_name
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.message import FtnMessage
from netbbs.ftn.netmail import encode_netmail, route_for
from netbbs.ftn.networks import FtnNetwork
from netbbs.ftn.packet import ATTR_FILE_ATTACHED, ATTR_PRIVATE
from netbbs.ftn.queue import enqueue_outbound_without_commit
from netbbs.storage.database import Database
from netbbs.timeutil import utc_iso

BOUNCE_FROM = "Mailer-Daemon"
BOUNCE_PREFIX = "bounce:"
MAX_BOUNCES_PER_DAY = 5
QUOTED_LINES = 10
# Names robots and mail daemons write as, compared without case. The
# network's own robot names are added per network.
ROBOT_NAMES = frozenset({
    "areafix", "areamgr", "areamanager", "filefix", "filemgr", "filemanager", "allfix", "raid", "mailer-daemon",
    "mailer daemon", "mailerdaemon", "netmail daemon", "postmaster", "ping", "pong", "tracker", "echomail robot",
})
BOUNCE_SUBJECTS = ("undeliverable", "returned mail", "bounced", "bounce:", "unknown user", "delivery failure")


def _robot_names(network: FtnNetwork) -> set[str]:
    return set(ROBOT_NAMES) | {network.areafix_robot.casefold(), network.filefix_robot.casefold()}


def is_robot_name(network: FtnNetwork, name: str) -> bool:
    return " ".join(name.split()).casefold() in _robot_names(network)


def bounce_refusal(db: Database, network: FtnNetwork, message: FtnMessage, origin: FtnAddress) -> str | None:
    """Why this netmail must not be bounced (the loop guard), or None."""
    if is_robot_name(network, message.from_name):
        return f"it came from {message.from_name.strip() or 'nobody'}, a robot's name"
    if message.subject.strip().casefold().startswith(BOUNCE_SUBJECTS):
        return "it is itself a returned netmail"
    if origin.same_node(network.our_address):
        return "it came from this node's own address"
    cutoff = utc_iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
    sent = db.connection.execute(
        "SELECT COUNT(*) FROM ftn_outbound WHERE network_id = ? AND kind = 'netmail' AND reference_id LIKE ? "
        "AND destination = ? AND created_at >= ?",
        (network.id, BOUNCE_PREFIX + "%", str(origin), cutoff),
    ).fetchone()[0]
    if sent >= MAX_BOUNCES_PER_DAY:
        return f"{origin.four_d} already got {sent} returned netmails from this node today"
    return None


def bounce_body(db: Database, network: FtnNetwork, message: FtnMessage, reason: str) -> str:
    """The returned netmail's text: who it was for, why it could not be
    delivered, and the original's first lines quoted."""
    written = message.date.strftime("%Y-%m-%d %H:%M") if message.date is not None else "an unknown date"
    lines = message.body.split("\n")
    quoted = [f"> {line}".rstrip() for line in lines[:QUOTED_LINES]]
    if len(lines) > QUOTED_LINES:
        quoted.append(f"> [{len(lines) - QUOTED_LINES} more lines]")
    parts = [
        f"Your netmail to {message.to_name.strip() or '(no name)'} at {network.our_address.four_d} "
        f"({get_node_display_name(db)}) could not be delivered: {reason}.",
        "",
        f"It was written on {written}, with the subject \"{message.subject.strip()}\".",
    ]
    if message.attributes & ATTR_FILE_ATTACHED:
        parts.append("The files sent with it were not kept.")
    parts += ["", "It began:", "", *quoted]
    return "\n".join(parts)


def queue_bounce_without_commit(
    db: Database, network: FtnNetwork, message: FtnMessage, origin: FtnAddress, reason: str, dupe_key: str,
) -> None:
    """Queue the returned netmail, in the transaction that records the
    original as seen. Raises `FtnQueueFullError` when the queue is full."""
    subject = f"Undeliverable: {message.subject.strip()}" if message.subject.strip() else "Undeliverable netmail"
    packed = encode_netmail(
        db, network, from_name=BOUNCE_FROM, to_name=message.from_name.strip() or "SysOp", address=origin,
        subject=subject, body=bounce_body(db, network, message, reason), attributes=ATTR_PRIVATE,
        reply=message.msgid,
    )
    enqueue_outbound_without_commit(
        db, network.id, kind="netmail", reference_id=f"{BOUNCE_PREFIX}{dupe_key}", destination=str(origin),
        packed=packed, route=route_for(db, network, origin),
    )
