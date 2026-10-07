"""
The scanner: local posts on FTN boards out to the uplink (design doc §6.8).

`export_post_if_ftn` is called wherever a post becomes visible on its
board -- written on an unmoderated board, or approved on a moderated one
-- beside the Link equivalent (`queue_board_post_if_linked`). It queues
the post once, as an encoded echomail message for the network's uplink;
the mailer packs and sends what is queued.

Nothing is exported that:

- is not approved;
- sits on a board carrying no echo, or on a network that is not enabled;
- arrived from FTN (`posts.ftn_inbound`): a gateway that re-exported what
  it tossed would loop mail back into the network;
- is an edit of an earlier post. FTN has no edits, and a second copy
  would read as a new message.

Each exported post gets a MSGID, stored on the post so replies from
elsewhere thread under it, and a REPLY when it answers a post that has
one. SEEN-BY carries this node and its uplink and PATH this node (a point
adds neither: its boss does it for it, FSC-0074).
"""

from __future__ import annotations

import datetime
import logging

from netbbs import __version__
from netbbs.boards import Board
from netbbs.boards.posts import Post
from netbbs.config import get_node_display_name
from netbbs.ftn.message import FtnMessage, build_origin, encode_message, format_msgid, format_tzutc
from netbbs.ftn.networks import board_area, get_network
from netbbs.ftn.packet import pack_message
from netbbs.ftn.queue import FtnQueueFullError, enqueue_outbound_without_commit, next_msgid_serial_without_commit
from netbbs.storage.database import Database
from netbbs.timeutil import get_node_timezone, parse_utc_iso

_logger = logging.getLogger(__name__)

PRODUCT = f"NetBBS {__version__}"
TO_ALL = "All"


def export_post_if_ftn(db: Database, post: Post, board: Board) -> bool:
    """Queue `post` for its board's FTN network; True if it was queued now.

    Never raises for a full queue: the post stays local, and the warning
    goes to the node log, where the SysOp sees why it didn't go out."""
    if post.status != "approved":
        return False
    area = board_area(db, board)
    if area is None:
        return False
    network = get_network(db, area.network_id)
    if network is None or not network.enabled:
        return False
    row = db.connection.execute(
        "SELECT ftn_inbound, edit_of_post_id, parent_post_id FROM posts WHERE post_id = ?", (post.post_id,)
    ).fetchone()
    if row is None or row["ftn_inbound"] or row["edit_of_post_id"] is not None:
        return False

    serial = next_msgid_serial_without_commit(db)
    msgid = format_msgid(network.our_address, serial)
    kludges = [("MSGID", msgid)]
    to_name = TO_ALL
    if row["parent_post_id"] is not None:
        parent = db.connection.execute(
            "SELECT ftn_msgid, author_label FROM posts WHERE post_id = ?", (row["parent_post_id"],)
        ).fetchone()
        if parent is not None:
            if parent["ftn_msgid"]:
                kludges.append(("REPLY", parent["ftn_msgid"]))
            to_name = _name_of(parent["author_label"])
    local_time, offset = _local_time(db, post.created_at)
    kludges += [("PID", PRODUCT), ("TZUTC", format_tzutc(offset))]

    ours, uplink = network.our_address, network.uplink_address
    message = FtnMessage(
        to_name=to_name,
        from_name=post.author_label,
        subject=post.subject,
        body=post.body,
        area=area.tag,
        date=local_time,
        kludges=kludges,
        tear_line=PRODUCT,
        origin=build_origin(network.origin_text or get_node_display_name(db), ours),
        seen_by=[uplink.net_node] if ours.point else [ours.net_node, uplink.net_node],
        path=[] if ours.point else [ours.net_node],
        orig_net=ours.net,
        orig_node=ours.node,
        dest_net=uplink.net,
        dest_node=uplink.node,
    )
    try:
        queued = enqueue_outbound_without_commit(
            db, network.id, kind="echomail", reference_id=post.post_id, destination=str(uplink),
            packed=_pack(message),
        )
    except FtnQueueFullError as exc:
        db.connection.rollback()
        _logger.warning("FTN %s: post %s on %s was not exported: %s", network.name, post.post_id, area.tag, exc)
        return False
    if queued:
        db.connection.execute("UPDATE posts SET ftn_msgid = ? WHERE post_id = ?", (msgid, post.post_id))
    db.connection.commit()
    return queued


def _pack(message: FtnMessage) -> bytes:
    """The encoded packed message, as the queue stores it."""
    return pack_message(encode_message(message))


def _name_of(label: str) -> str:
    """The name in `Name (zone:net/node)`, or the label itself."""
    if label.endswith(")") and " (" in label:
        return label.rsplit(" (", 1)[0]
    return label


def _local_time(db: Database, created_at: str) -> tuple[datetime.datetime, datetime.timedelta]:
    """The post's date in the node's timezone (naive, as FTS-0001 writes
    it) and that timezone's offset then, for TZUTC."""
    moment = parse_utc_iso(created_at).astimezone(get_node_timezone(db))
    return moment.replace(tzinfo=None), moment.utcoffset() or datetime.timedelta(0)
