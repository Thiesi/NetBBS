"""
The AreaFix and FileFix server: this node's robots answering its points
and downlinks (design doc §6.8, "Points and downlinks"; issue #1479, §16
"Issue #1154", Decision 17).

A link asks for echoes (AreaFix) or file echoes (FileFix) the way this node
asks its own uplink (`netbbs.ftn.areafix`): a netmail to the robot's name
at this node's address -- the network's AreaFix and FileFix names -- with
the link's robot password as the first word of its subject and one command
per line. The tosser hands such a netmail here only when it came from a
point or downlink in a session that link's own password proved, and was
written at that link's address; anyone else's goes to the SysOp, not acted
on. Nothing a robot request asks is ever done for an unproven caller.

- **Password.** AreaFix requests carry the link's AreaFix password, FileFix
  requests its FileFix password, else the AreaFix one; compared without
  case. A link with none set, or a wrong one, gets a reply saying so and
  nothing is done.
- **What a link may link.** The areas the SysOp offers to links on the
  network (`set_offered`; nothing by default): an echo a board carries, or
  a file echo a file area carries, that the SysOp marked on the Links
  screen's **[R]obot areas**. The SysOp can give any link any carried
  area from the link's own areas screen, offered or not; a link can unlink
  any area it has. A new link is read-write; one the SysOp made read-only
  stays read-only when the link asks for it again.
- **Commands** (FSC-0057-style, as common tossers read them; at most
  `MAX_COMMANDS` per request):
  - `+TAG` or a bare `TAG` links an area, `-TAG` unlinks one; `*` and `?`
    match several (offered areas for `+`, the link's own for `-`);
  - `%LIST`: every area offered to the link, marked when linked;
    `%QUERY` (or `%LINKED`): the link's areas; `%UNLINKED`: offered areas
    it doesn't have; `%AVAIL`: the same as `%LIST`, since this node never
    asks its uplink for an area on a link's behalf; `%HELP`;
  - `%RESCAN [TAG] [n]` (AreaFix only): the newest `n` messages
    (`DEFAULT_RESCAN`, at most `MAX_RESCAN` per echo and
    `MAX_RESCAN_TOTAL` per request) of the named echo, or of each echo the
    same request linked, sent again to the link, each with a `^ARESCANNED`
    line;
  - `%PAUSE` stops the link's echomail and file echoes (its status becomes
    *paused*, `netbbs.ftn.links.links_carrying` leaves it out) and `%RESUME`
    starts them again; netmail goes on either way. A link the SysOp
    disabled can't resume itself;
  - `%PWD new` sets that robot's password for the link. When AreaFix's
    changes and FileFix had none of its own, FileFix keeps the old one, as
    this node's own client does (§6.8, "Robots");
  - anything else is answered as unknown.
- **The reply** is netmail from the robot's name to the name the request
  came from, at the link's address, queued for the link like any netmail to
  it, and split into parts of at most `MAX_REPLY_CHARS`. A request is
  answered once: its MSGID goes through the netmail dupe check first.
"""

from __future__ import annotations

import datetime
import fnmatch
import hashlib
import logging
import re
from dataclasses import replace

from netbbs.config import get_node_display_name
from netbbs.ftn.address import FtnAddress, find_address
from netbbs.ftn.links import ACTIVE, DISABLED, ECHO, FILE_ECHO, PAUSED, FtnLink, link_areas
from netbbs.ftn.message import FtnMessage, build_origin, encode_message
from netbbs.ftn.netmail import encode_netmail
from netbbs.ftn.networks import (
    MAX_SESSION_PASSWORD,
    FtnNetwork,
    FtnNetworkError,
    area_mappings,
    board_id_for_area,
    file_area_tic_mappings,
    normalise_area_tag,
)
from netbbs.ftn.packet import ATTR_PRIVATE, pack_message
from netbbs.ftn.queue import FtnQueueFullError, enqueue_outbound_without_commit
from netbbs.storage.database import Database
from netbbs.timeutil import parse_utc_iso, utc_now_iso

_logger = logging.getLogger(__name__)

AREAFIX, FILEFIX = "areafix", "filefix"
MAX_COMMANDS = 50
MAX_REPLY_CHARS = 8_000
MAX_REPLY_PARTS = 10
DEFAULT_RESCAN = 50
MAX_RESCAN = 200
MAX_RESCAN_TOTAL = 500
_PRINTABLE = re.compile(r"[\x21-\x7e]*")
_KIND = {AREAFIX: ECHO, FILEFIX: FILE_ECHO}
_WHAT = {AREAFIX: "echo", FILEFIX: "file echo"}


# --- what is offered to links -------------------------------------------------------


def offered_tags(db: Database, network_id: int, kind: str) -> set[str]:
    """The tags of `kind` (`ECHO` or `FILE_ECHO`) the SysOp offers to links,
    whether or not an area still carries them."""
    rows = db.connection.execute(
        "SELECT tag FROM ftn_link_offers WHERE network_id = ? AND kind = ?", (network_id, kind)
    ).fetchall()
    return {row["tag"].upper() for row in rows}


def set_offered(db: Database, network_id: int, kind: str, tag: str, offered: bool, *, commit: bool = True) -> None:
    """Offer an area to the network's links, or stop offering it. Links that
    have it keep it; only new requests are refused."""
    if kind not in (ECHO, FILE_ECHO):
        raise FtnNetworkError(f"area kind {kind!r} is unknown")
    tag = normalise_area_tag(tag)
    if offered:
        db.connection.execute(
            "INSERT OR IGNORE INTO ftn_link_offers (network_id, kind, tag, created_at) VALUES (?, ?, ?, ?)",
            (network_id, kind, tag, utc_now_iso()),
        )
    else:
        db.connection.execute("DELETE FROM ftn_link_offers WHERE network_id = ? AND kind = ? AND tag = ?",
                              (network_id, kind, tag))
    if commit:
        db.connection.commit()


def carried_areas(db: Database, network_id: int, kind: str) -> list[tuple[str, str]]:
    """`(tag, name)` for every area of `kind` this node carries on the
    network -- an echo a board carries, a file echo a file area carries --
    by tag. Only these can be given to a link."""
    if kind == ECHO:
        ids = area_mappings(db, network_id)
        table = "boards"
    else:
        ids = file_area_tic_mappings(db, network_id)
        table = "file_areas"
    found = []
    for tag, area_id in ids.items():
        row = db.connection.execute(f"SELECT name FROM {table} WHERE id = ?", (area_id,)).fetchone()  # noqa: S608
        found.append((tag.upper(), row["name"] if row is not None else ""))
    return sorted(found)


def available_areas(db: Database, network_id: int, kind: str) -> list[tuple[str, str]]:
    """The carried areas of `kind` the SysOp offers to links."""
    offered = offered_tags(db, network_id, kind)
    return [(tag, name) for tag, name in carried_areas(db, network_id, kind) if tag in offered]


# --- who a robot request is for -----------------------------------------------------


def robot_for(network: FtnNetwork, to_name: str) -> str | None:
    """`AREAFIX` or `FILEFIX` when a netmail's To is that robot's name on
    the network, compared without case; None otherwise."""
    name = " ".join(to_name.split()).casefold()
    if name == network.areafix_robot.casefold():
        return AREAFIX
    if name == network.filefix_robot.casefold():
        return FILEFIX
    return None


def robot_label(network: FtnNetwork, robot: str) -> str:
    return network.areafix_robot if robot == AREAFIX else network.filefix_robot


def not_acted_note(network: FtnNetwork, robot: str, origin: FtnAddress) -> str:
    """The line a robot request from anyone but a proven link reaches the
    SysOp with."""
    return (f"[This netmail was addressed to {robot_label(network, robot)}, this node's robot. It came from "
            f"{origin.four_d}, which is not a point or downlink of this node calling with its password, so it was "
            "not acted on.]")


# --- answering a request ----------------------------------------------------------------


def answer_request_without_commit(db: Database, network: FtnNetwork, robot: str, link: FtnLink,
                                  message: FtnMessage, dupe_key: str) -> list[str]:
    """Carry out a proven link's request and queue the reply; returns the
    reply's lines. Raises `FtnQueueFullError` when the reply can't be
    queued, and the caller rolls back: nothing the request asked stays
    done without its answer."""
    lines = _carry_out(db, network, robot, link, message, dupe_key)
    _queue_reply(db, network, robot, link, message, lines, dupe_key)
    _logger.info("FTN %s: %s request from %s answered (%d lines)", network.name, robot_label(network, robot),
                 link.address.four_d, len(lines))
    return lines


def _carry_out(db: Database, network: FtnNetwork, robot: str, link: FtnLink, message: FtnMessage,
               dupe_key: str) -> list[str]:
    label = robot_label(network, robot)
    password = link.areafix_password if robot == AREAFIX else link.effective_filefix_password
    words = message.subject.split()
    given = words[0] if words else ""
    if not password:
        return [f"No {label} password is set for {link.address.four_d} on this node, so {label} can't act for "
                "you. Ask the SysOp to set one."]
    if given.upper() != password.upper():
        _logger.warning("FTN %s: %s request from %s with a wrong password", network.name, label, link.address.four_d)
        return [f"The password in the subject is not your {label} password here; nothing was done."]
    commands = _commands(message.body)
    if not commands:
        return [f"No commands found. Send %HELP to {label} for the list."]
    out: list[str] = []
    if len(commands) > MAX_COMMANDS:
        out.append(f"Only the first {MAX_COMMANDS} commands were read.")
        commands = commands[:MAX_COMMANDS]
    kind = _KIND[robot]
    linked_now: list[str] = []
    rescans: list[tuple[str | None, int]] = []
    state = {"link": link}
    for command in commands:
        out.append(f"> {command}")
        out += _one(db, network, robot, kind, state, command, linked_now, rescans)
        out.append("")
    rescanned = 0
    for tag, count in rescans:
        tags = [tag] if tag is not None else linked_now
        if not tags:
            out.append("%RESCAN: name an echo, or link one in the same request.")
        for name in tags:
            room = MAX_RESCAN_TOTAL - rescanned
            if room <= 0:
                out.append(f"%RESCAN {name}: at most {MAX_RESCAN_TOTAL} messages are rescanned per request.")
                continue
            sent = _rescan_without_commit(db, network, state["link"], name, min(count, room), dupe_key)
            rescanned += sent
            out.append(f"%RESCAN {name}: {sent} messages sent again.")
    return out


def _commands(body: str) -> list[str]:
    commands = []
    for raw in body.split("\n"):
        line = " ".join(raw.split())
        if line.startswith("---") or line.startswith("* Origin:"):
            break
        if line:
            commands.append(line[:200])
    return commands


def _one(db: Database, network: FtnNetwork, robot: str, kind: str, state: dict, command: str,
         linked_now: list[str], rescans: list) -> list[str]:
    link: FtnLink = state["link"]
    words = command.split()
    name = words[0].upper()
    what = _WHAT[robot]
    if name.startswith("%"):
        if name in ("%HELP", "%?"):
            return _help(network, robot)
        if name in ("%LIST", "%AVAIL"):
            return _listing(db, network, link, kind, only_unlinked=False)
        if name == "%UNLINKED":
            return _listing(db, network, link, kind, only_unlinked=True)
        if name in ("%QUERY", "%LINKED"):
            areas = link_areas(db, link.id, kind)
            if not areas:
                return [f"You carry no {what}s from this node."]
            return [f"  {area.tag}{'  (read-only)' if area.mode != 'rw' else ''}" for area in areas]
        if name == "%PAUSE":
            if link.status == PAUSED:
                return ["Already paused."]
            if link.status == DISABLED:
                return ["The SysOp has switched this link off; it can't be paused or resumed by request."]
            state["link"] = _set_status(db, link, PAUSED)
            return ["Paused: no echomail or file echoes are sent to you until %RESUME. Netmail still is."]
        if name == "%RESUME":
            if link.status == ACTIVE:
                return ["Not paused."]
            if link.status == DISABLED:
                return ["The SysOp has switched this link off; it can't be paused or resumed by request."]
            state["link"] = _set_status(db, link, ACTIVE)
            return ["Resumed: echomail and file echoes are sent to you again."]
        if name in ("%PWD", "%PASSWORD"):
            return _change_password(db, network, robot, state, words[1:])
        if name == "%RESCAN":
            if robot != AREAFIX:
                return ["FileFix doesn't rescan here; ask the SysOp to send a file again."]
            tag, count = _rescan_arguments(words[1:])
            if tag is not None:
                carried = {area.tag.upper() for area in link_areas(db, link.id, ECHO)}
                if tag not in carried:
                    return [f"You don't carry {tag}; link it first."]
            rescans.append((tag, count))
            return [f"Rescan of {tag or 'the echoes linked above'} queued (up to {count} messages)."]
        return [f"{name} is not a command this robot knows. Send %HELP for the list."]
    if name.startswith("-"):
        return _unlink(db, link, kind, name[1:], what)
    return _link(db, network, link, kind, name.removeprefix("+"), what, linked_now)


def _help(network: FtnNetwork, robot: str) -> list[str]:
    label = robot_label(network, robot)
    what = _WHAT[robot]
    lines = [
        f"{label} at {network.our_address.four_d}. Write to {label}, with your {label} password as the subject",
        "and one command per line:",
        f"  +TAG or TAG    link a {what} (* and ? match several)",
        f"  -TAG           unlink a {what}",
        f"  %LIST          the {what}s offered to you, * marking those you carry",
        f"  %QUERY         the {what}s you carry",
        f"  %UNLINKED      offered {what}s you don't carry",
        "  %AVAIL         the same as %LIST",
    ]
    if robot == AREAFIX:
        lines.append(f"  %RESCAN [TAG] [n]  send the newest n messages again (default {DEFAULT_RESCAN}, "
                     f"at most {MAX_RESCAN})")
    lines += [
        "  %PAUSE         stop echomail and file echoes for a while; netmail still comes",
        "  %RESUME        start them again",
        f"  %PWD new       change your {label} password",
        "  %HELP          this text",
    ]
    return lines


def _listing(db: Database, network: FtnNetwork, link: FtnLink, kind: str, *, only_unlinked: bool) -> list[str]:
    carried = {area.tag.upper(): area.mode for area in link_areas(db, link.id, kind)}
    offered = available_areas(db, network.id, kind)
    rows = [(tag, name) for tag, name in offered if not (only_unlinked and tag in carried)]
    if not rows:
        return ["Nothing is offered to you here." if not offered else "You carry everything offered to you."]
    width = min(max(len(tag) for tag, _name in rows), 30)
    return [f"{'*' if tag in carried else ' '} {tag.ljust(width)}  {name}".rstrip() for tag, name in rows]


def _matching(pattern: str, tags: list[str]) -> list[str]:
    if "*" in pattern or "?" in pattern:
        return [tag for tag in tags if fnmatch.fnmatchcase(tag, pattern)]
    return [tag for tag in tags if tag == pattern]


def _link(db: Database, network: FtnNetwork, link: FtnLink, kind: str, pattern: str, what: str,
          linked_now: list[str]) -> list[str]:
    from netbbs.ftn.links import set_link_area_without_commit

    pattern = pattern.strip().upper()
    if not pattern or pattern.startswith("%"):
        return [f"Nothing to link in {pattern!r}."]
    carried = {area.tag.upper() for area in link_areas(db, link.id, kind)}
    offered = [tag for tag, _name in available_areas(db, network.id, kind)]
    matches = _matching(pattern, offered)
    if not matches:
        if pattern in carried:
            return [f"{pattern}: you carry it already."]
        return [f"{pattern}: no {what} by that name is offered to you here."]
    out = []
    for tag in matches:
        if tag in carried:
            out.append(f"{tag}: you carry it already.")
            continue
        set_link_area_without_commit(db, link.id, kind, tag, "rw")
        linked_now.append(tag)
        out.append(f"{tag}: linked.")
    return out


def _unlink(db: Database, link: FtnLink, kind: str, pattern: str, what: str) -> list[str]:
    from netbbs.ftn.links import remove_link_area_without_commit

    pattern = pattern.strip().upper()
    carried = [area.tag.upper() for area in link_areas(db, link.id, kind)]
    matches = _matching(pattern, carried)
    if not matches:
        return [f"{pattern}: you don't carry that {what}."]
    for tag in matches:
        remove_link_area_without_commit(db, link.id, kind, tag)
    return [f"{tag}: unlinked." for tag in matches]


def _set_status(db: Database, link: FtnLink, status: str) -> FtnLink:
    db.connection.execute("UPDATE ftn_links SET status = ? WHERE id = ?", (status, link.id))
    return replace(link, status=status)


def _change_password(db: Database, network: FtnNetwork, robot: str, state: dict, words: list[str]) -> list[str]:
    link: FtnLink = state["link"]
    label = robot_label(network, robot)
    if len(words) != 1:
        return [f"Write %PWD and the new {label} password, with no spaces in it."]
    new = words[0]
    if len(new) > MAX_SESSION_PASSWORD or not _PRINTABLE.fullmatch(new):
        return [f"The new password must be at most {MAX_SESSION_PASSWORD} printable characters without spaces; "
                "nothing was changed."]
    if robot == AREAFIX:
        # A FileFix with no password of its own was using AreaFix's: it
        # keeps the old one, as the link's own client does.
        filefix = link.filefix_password or link.areafix_password
        db.connection.execute("UPDATE ftn_links SET areafix_password = ?, filefix_password = ? WHERE id = ?",
                              (new, filefix, link.id))
        state["link"] = replace(link, areafix_password=new, filefix_password=filefix)
    else:
        db.connection.execute("UPDATE ftn_links SET filefix_password = ? WHERE id = ?", (new, link.id))
        state["link"] = replace(link, filefix_password=new)
    _logger.info("FTN %s: %s changed its %s password", network.name, link.address.four_d, label)
    return [f"Your {label} password is changed. Use the new one from your next request."]


def _rescan_arguments(words: list[str]) -> tuple[str | None, int]:
    tag, count = None, DEFAULT_RESCAN
    for word in words:
        upper = word.upper().lstrip("/")
        if upper.startswith("R="):
            upper = upper[2:]
        if upper.isdigit() and upper.isascii():
            count = max(1, min(int(upper), MAX_RESCAN))
        elif tag is None and "=" not in upper:
            try:
                tag = normalise_area_tag(upper)
            except FtnNetworkError:
                continue
    return tag, count


# --- %RESCAN ---------------------------------------------------------------------------------


def _rescan_without_commit(db: Database, network: FtnNetwork, link: FtnLink, tag: str, count: int,
                           dupe_key: str) -> int:
    """Queue the newest `count` messages of echo `tag` for `link` again,
    oldest first; returns how many were queued. A post with no MSGID (a
    local post written before the board carried the echo) is left out: a
    receiver's dupe check would have nothing to go by."""
    from netbbs.ftn.links import enqueue_for_link_without_commit
    from netbbs.ftn.scanner import PRODUCT

    board_id = board_id_for_area(db, network.id, tag)
    if board_id is None:
        return 0
    rows = db.connection.execute(
        "SELECT post_id, ftn_msgid, ftn_inbound, author_label, author_user_id, author_verified_name, subject, body, "
        "created_at, parent_post_id FROM posts WHERE board_id = ? AND status = 'approved' AND edit_of_post_id IS NULL "
        "AND tombstoned_at IS NULL AND withdrawn = 0 AND ftn_msgid IS NOT NULL AND ftn_msgid != '' "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (board_id, count),
    ).fetchall()
    key = hashlib.sha256(dupe_key.encode("utf-8")).hexdigest()[:12]
    sent = 0
    for row in reversed(rows):
        message = _rescanned_message(db, network, link, tag, row, PRODUCT)
        if message is None:
            continue
        try:
            packed = pack_message(encode_message(message))
            if enqueue_for_link_without_commit(db, link, kind="echomail",
                                               reference_id=f"rescan:{key}:{row['post_id']}>{link.id}",
                                               packed=packed):
                sent += 1
        except FtnQueueFullError as exc:
            _logger.warning("FTN %s: rescan of %s for %s stopped: %s", network.name, tag, link.address.four_d, exc)
            break
        except Exception as exc:  # noqa: BLE001 -- one unwritable post must not end the rescan
            _logger.warning("FTN %s: post %s left out of a rescan: %s", network.name, row["post_id"], exc)
    return sent


def _rescanned_message(db: Database, network: FtnNetwork, link: FtnLink, tag: str, row, product: str):
    from netbbs.ftn.scanner import _frozen_real_name, _name_of, sends_real_names
    from netbbs.boards.boards import get_board_by_id

    ours = network.our_address
    if row["ftn_inbound"]:
        author = find_address(row["author_label"]) if "(" in row["author_label"] else None
        if author is None:
            return None
        from_name = _name_of(row["author_label"])
        origin = build_origin(f"rescanned at {ours.four_d}", author)
    else:
        board = get_board_by_id(db, board_id_for_area(db, network.id, tag))
        from_name = row["author_label"]
        if board is not None and sends_real_names(db, board):
            from_name = _frozen_real_name(row)
            if from_name is None:
                return None
        origin = build_origin(network.origin_text or get_node_display_name(db), ours)
    kludges = [("MSGID", row["ftn_msgid"])]
    if row["parent_post_id"] is not None:
        parent = db.connection.execute("SELECT ftn_msgid FROM posts WHERE post_id = ?",
                                       (row["parent_post_id"],)).fetchone()
        if parent is not None and parent["ftn_msgid"]:
            kludges.append(("REPLY", parent["ftn_msgid"]))
    kludges += [("PID", product), ("TZUTC", "0000"), ("RESCANNED", ours.four_d)]
    moment = parse_utc_iso(row["created_at"]).astimezone(datetime.timezone.utc).replace(tzinfo=None)
    seen_by = [ours.net_node] + ([] if link.is_point else [link.address.net_node])
    return FtnMessage(
        to_name="All", from_name=from_name, subject=row["subject"], body=row["body"], area=tag, date=moment,
        kludges=kludges, tear_line=product, origin=origin, seen_by=seen_by, path=[ours.net_node],
        orig_net=ours.net, orig_node=ours.node, dest_net=link.address.net, dest_node=link.address.node,
    )


# --- the reply ---------------------------------------------------------------------------------


def _queue_reply(db: Database, network: FtnNetwork, robot: str, link: FtnLink, message: FtnMessage,
                 lines: list[str], dupe_key: str) -> None:
    parts = _split(lines)
    label = robot_label(network, robot)
    to_name = message.from_name.strip() or "SysOp"
    for index, part in enumerate(parts, start=1):
        subject = f"{label} reply" + (f" ({index} of {len(parts)})" if len(parts) > 1 else "")
        packed = encode_netmail(db, network, from_name=label, to_name=to_name, address=link.address,
                                subject=subject, body="\n".join(part), attributes=ATTR_PRIVATE, reply=message.msgid)
        enqueue_outbound_without_commit(
            db, network.id, kind="netmail", reference_id=f"robot:{dupe_key}:{index}", destination=str(link.address),
            packed=packed, route="direct", link_id=link.id,
        )


def _split(lines: list[str]) -> list[list[str]]:
    parts: list[list[str]] = [[]]
    size = 0
    for line in lines:
        if size + len(line) + 1 > MAX_REPLY_CHARS and parts[-1]:
            if len(parts) >= MAX_REPLY_PARTS:
                parts[-1].append("[The rest of this reply was left out: it was too long.]")
                break
            parts.append([])
            size = 0
        parts[-1].append(line)
        size += len(line) + 1
    while parts[-1] and not parts[-1][-1]:
        parts[-1].pop()
    return parts
