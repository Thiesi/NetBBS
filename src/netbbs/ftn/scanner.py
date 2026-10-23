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
  would read as a new message;
- sits on a board that sends real names (`sends_real_names`, issue
  #1443) without an author whose verified real name fits the From field.

Each exported post gets a MSGID, stored on the post so replies from
elsewhere thread under it, and a REPLY when it answers a post that has
one. SEEN-BY carries this node and its uplink and PATH this node (a point
adds neither: its boss does it for it, FSC-0074).

A copy of a post written to several boards at once (issue #1298, design doc
§6.8) is its own echomail message with its own MSGID. Its first body line,
`* Crossposted in <ECHO>, <ECHO>`, names the other echoes of the same network
that get a copy; local and Link boards are never named to an FTN network.
"""

from __future__ import annotations

import datetime
import logging

from netbbs import __version__
from netbbs.boards import Board
from netbbs.boards.posts import Post, crosspost_copies
from netbbs.config import get_node_display_name
from netbbs.auth.users import SYSOP_LEVEL, User
from netbbs.ftn.chrs import MAX_NAME_BYTES, choose_outbound_charset
from netbbs.ftn.message import (
    FtnMessage,
    build_origin,
    encode_message,
    encoded_name_bytes,
    format_msgid,
    format_tzutc,
    outbound_charset,
)
from netbbs.ftn.networks import FtnNetwork, board_area, get_network
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
    goes to the node log, where the SysOp sees why it didn't go out. A post
    on a board that sends real names (`sends_real_names`) whose author has
    no verified real name, or one too long for the From field, stays local
    the same way: it never goes out under the username, or cut short."""
    if post.status != "approved":
        return False
    area = board_area(db, board)
    if area is None:
        return False
    network = get_network(db, area.network_id)
    if network is None or not network.enabled:
        return False
    row = db.connection.execute(
        "SELECT ftn_inbound, edit_of_post_id, parent_post_id, author_user_id, author_verified_name "
        "FROM posts WHERE post_id = ?",
        (post.post_id,),
    ).fetchone()
    if row is None or row["ftn_inbound"] or row["edit_of_post_id"] is not None:
        return False

    real_names = sends_real_names(db, board)
    from_name = post.author_label
    if real_names:
        # The name frozen on the post when it was written, which is what the
        # board shows it under (design doc §5.5): the network copy and the
        # local view always agree, whatever has happened to the attestation
        # since -- an approval on a moderated board can come much later.
        verified = _frozen_real_name(row)
        if verified is None:
            _logger.warning(
                "FTN %s: post %s on %s was not exported: the echo takes real names, and its author has no "
                "verified real name", network.name, post.post_id, area.tag,
            )
            return False
        from_name = verified
    to_name, reply_msgid = TO_ALL, None
    if row["parent_post_id"] is not None:
        parent = db.connection.execute(
            "SELECT ftn_msgid, ftn_inbound, author_label, author_user_id, author_verified_name "
            "FROM posts WHERE post_id = ?",
            (row["parent_post_id"],),
        ).fetchone()
        if parent is not None:
            reply_msgid = parent["ftn_msgid"] or None
            to_name = _reply_to_name(db, parent, real_names=real_names)
    local_time, offset = _local_time(db, post.created_at)
    message = _echomail(
        db, network, area.tag, to_name=to_name, from_name=from_name, subject=post.subject,
        body=_crossposted_line(db, post, area.network_id, area.tag) + post.body, date=local_time,
        origin_text=area.origin_text,
    )
    if real_names:
        size = encoded_name_bytes(from_name, outbound_charset(message))
        if size > MAX_NAME_BYTES:
            _logger.warning(
                "FTN %s: post %s on %s was not exported: its author's verified real name is %d bytes, and the "
                "From field holds %d", network.name, post.post_id, area.tag, size, MAX_NAME_BYTES,
            )
            return False

    serial = next_msgid_serial_without_commit(db)
    msgid = format_msgid(network.our_address, serial)
    message.kludges = [("MSGID", msgid)]
    if reply_msgid is not None:
        message.kludges.append(("REPLY", reply_msgid))
    message.kludges += [("PID", PRODUCT), ("TZUTC", format_tzutc(offset))]
    try:
        queued = enqueue_outbound_without_commit(
            db, network.id, kind="echomail", reference_id=post.post_id, destination=str(network.uplink_address),
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


def _echomail(
    db: Database, network: FtnNetwork, tag: str, *, to_name: str, from_name: str, subject: str, body: str,
    date: datetime.datetime | None, origin_text: str = "",
) -> FtnMessage:
    """The echomail message a post on echo `tag` leaves as, but for its
    kludges: everything that decides its character set. `origin_text` is
    the board's own Origin (issue #1474); empty, the network's is used, and
    without one the BBS's name."""
    ours, uplink = network.our_address, network.uplink_address
    return FtnMessage(
        to_name=to_name,
        from_name=from_name,
        subject=subject,
        body=body,
        area=tag,
        date=date,
        tear_line=PRODUCT,
        origin=build_origin(origin_text or network.origin_text or get_node_display_name(db), ours),
        seen_by=[uplink.net_node] if ours.point else [ours.net_node, uplink.net_node],
        path=[] if ours.point else [ours.net_node],
        orig_net=ours.net,
        orig_node=ours.node,
        dest_net=uplink.net,
        dest_node=uplink.node,
    )


# -- real-name echoes (issue #1443) -------------------------------------------
#
# Some echoes take posts under real names only (nixNet's NIX_ADMIN). A board
# carrying an echo whose name requirement is "verified and displayed" (design
# doc §18) sends its author's verified real name as the From name instead of
# the username, and only that board does: every other echo, Link and chat
# keep the username. A post that cannot go out that way -- no verified name,
# or one longer than the From field -- is refused before it is written
# (`real_name_refusal`, asked by `netbbs.boards.posts`), and is never sent
# under the username or cut short (`export_post_if_ftn`).


def sends_real_names(db: Database, board: Board) -> bool:
    """Whether `board` carries an echo and asks for verified and displayed
    names (its own requirement, else its Community's), so its echomail goes
    out under its authors' verified real names."""
    from netbbs.communities import get_effective_name_requirement

    # The requirement first: it reads nothing FTN keeps, so a post on a
    # schema older than the gateway is never asked about an echo.
    return get_effective_name_requirement(db, board) == "verified_and_displayed" and board_area(db, board) is not None


def real_name_refusal(
    db: Database, board: Board, author: User, *, subject: str | None = None, body: str | None = None,
    parent_post_id: str | None = None, short: bool = False,
) -> str | None:
    """Why a post by `author` on `board` could not go out under their
    verified real name, or `None` when it could or `board` does not send
    real names. A SysOp is asked too: the gate bypass lets them onto the
    board, but the network still needs a name.

    With `subject` and `body` the length is judged in the character set the
    whole message would be written in; without them, in the smallest one the
    name allows -- all that is known before a post is written. `short` gives
    the phrase a board picker shows."""
    if not sends_real_names(db, board):
        return None
    area = board_area(db, board)
    network = get_network(db, area.network_id)
    if network is None:
        return None
    name = _verified_real_name(db, author.id)
    if name is None:
        if short:
            return f"sends real names to {network.name}; yours is not verified"
        # A SysOp verifies themselves (maintainer decision 2026-10-23).
        how = (
            "Verify it yourself with [V]erify on the main menu." if author.user_level >= SYSOP_LEVEL
            else "Ask the SysOp to verify it."
        )
        return f"{board.name} sends real names to {network.name}, and your account has no verified real name. {how}"
    if subject is None or body is None:
        charset = choose_outbound_charset(name)
    else:
        to_name = TO_ALL
        if parent_post_id is not None:
            parent = db.connection.execute(
                "SELECT ftn_inbound, author_label, author_user_id, author_verified_name FROM posts WHERE post_id = ?",
                (parent_post_id,),
            ).fetchone()
            if parent is not None:
                to_name = _reply_to_name(db, parent, real_names=True)
        charset = outbound_charset(_echomail(
            db, network, area.tag, to_name=to_name, from_name=name, subject=subject, body=body, date=None,
            origin_text=area.origin_text,
        ))
    size = encoded_name_bytes(name, charset)
    if size <= MAX_NAME_BYTES:
        return None
    if short:
        return f"your verified real name is too long for {network.name}"
    return (
        f"Your verified real name is {size} bytes; {network.name} allows {MAX_NAME_BYTES}. "
        + ("Verify a shorter form of it." if author.user_level >= SYSOP_LEVEL else "Ask a SysOp to verify a shorter form.")
    )


def _verified_real_name(db: Database, user_id: int | None) -> str | None:
    """The verified real name of local account `user_id`, or `None`."""
    from netbbs.attestation import get_attestation
    from netbbs.auth.users import get_user_by_id

    user = get_user_by_id(db, user_id) if user_id is not None else None
    attestation = get_attestation(db, user, "name") if user is not None else None
    return attestation.attested_value if attestation is not None else None


def _frozen_real_name(row) -> str | None:
    """The verified real name frozen on posts `row` when its local author
    wrote it (design doc §5.5), or `None`: a carried post's frozen name is
    not this node's to send, and a post written before its board displayed
    names has none."""
    if row["author_user_id"] is None:
        return None
    return row["author_verified_name"]


def _reply_to_name(db: Database, parent, *, real_names: bool) -> str:
    """The To name of a reply to `parent` (a posts row): the name the network
    knows its author by. That is the verified real name frozen on a local
    author's post on a board that sends real names -- the From it went out
    under -- else the author's label: the username, or the name in an FTN
    author's `Name (zone:net/node)`."""
    if real_names and not parent["ftn_inbound"]:
        name = _frozen_real_name(parent)
        if name is not None:
            return name
    return _name_of(parent["author_label"])


#: How a cross-posted copy's first line begins (issue #1298).
CROSSPOSTED_PREFIX = "* Crossposted in "


def _crossposted_line(db: Database, post: Post, network_id: int, own_tag: str) -> str:
    """`* Crossposted in ECHO, ECHO` and a line break, naming the echoes of
    network `network_id` other than `own_tag` that carry another copy of
    `post`'s cross-post -- or "" for a post on one board, or one whose other
    copies are on no echo of this network."""
    tags: list[str] = []
    for board, _copy in crosspost_copies(db, post):
        area = board_area(db, board)
        if area is None or area.network_id != network_id or area.tag == own_tag or area.tag in tags:
            continue
        tags.append(area.tag)
    if not tags:
        return ""
    return f"{CROSSPOSTED_PREFIX}{', '.join(tags)}\n"


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
