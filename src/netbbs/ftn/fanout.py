"""
Echomail fan-out: a hub sending each echomail on to the other systems that
carry its echo (design doc §6.8, "Points and downlinks"; issue #1479, §16
"Issue #1154", Decision 17; FTS-0004, FSC-0074).

`fan_out_without_commit` takes one echomail, as received or as written here,
and queues one copy for each system it goes to:

- **Who gets it.** Every active point and downlink carrying the echo
  (`netbbs.ftn.links.links_carrying`; a paused or disabled link gets
  nothing), and the uplink when the message did not come from it. Never
  the system it came from, and never a downlink whose net/node is in its
  SEEN-BY already: that one has seen it (FTS-0004). A point is never in a
  SEEN-BY, so only the sender rule keeps a point from its own message.
- **Loops.** A message whose PATH names this node passed here before and
  goes nowhere: forwarding it again would loop it.
- **SEEN-BY and PATH.** Every copy carries the same SEEN-BY: what it came
  with, this node, and the net/node of every downlink and uplink it goes
  to -- never a point's, which its boss stands for (FSC-0074). PATH gets
  this node at the end, unless already last. A point's own message comes
  with no SEEN-BY or PATH worth keeping: its boss writes them as if it were
  its own, so whatever the point's software wrote is dropped first.
- **Bytes.** A copy is the received message with only its trailing SEEN-BY
  and PATH lines and its packed header's net/node fields changed
  (`netbbs.ftn.route.forwarded_copy`), never decoded and re-encoded.
- **Bounds.** A copy for a link that is at its queue bound is not queued:
  a node-log line says so, and the other copies go. A full network queue
  stops the uplink's copy the same way.

The tosser calls this once a received message is stored and visible on its
board (a link's message on a moderated board waits for its approval first,
`netbbs.ftn.links.hold_link_echomail_without_commit`), and duplicates never
get here: the dupe check comes first.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from netbbs.ftn import FtnFormatError
from netbbs.ftn.networks import FtnNetwork
from netbbs.ftn.packet import PackedMessage, pack_message
from netbbs.ftn.queue import FtnQueueFullError, enqueue_outbound_without_commit
from netbbs.ftn.route import forwarded_copy, seen_by_and_path
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)


@dataclass
class FanOut:
    """What one fan-out queued: the links' copies, whether the uplink got
    one, and the links skipped at their bound."""

    links: list[str] = field(default_factory=list)  # the addresses queued for
    uplink: bool = False
    full: list[str] = field(default_factory=list)
    loop: bool = False

    @property
    def copies(self) -> int:
        return len(self.links) + int(self.uplink)


def fan_out_without_commit(
    db: Database, network: FtnNetwork, packed: PackedMessage, tag: str, reference_id: str, *,
    sender=None, from_uplink: bool = False, to_uplink: bool | None = None,
) -> FanOut:
    """Queue `packed`, an echomail on echo `tag`, for every system it goes
    to (see the module docstring). `sender` is the point or downlink it came
    from (`netbbs.ftn.links.FtnLink`), `from_uplink` says it came from the
    uplink; neither, it was written here. `to_uplink` defaults to whether it
    came from anywhere but the uplink. `reference_id` is the post's id: the
    uplink's copy is queued under it and each link's under
    `fanout_reference`. Raises nothing for a full queue."""
    from netbbs.ftn.links import ECHO, enqueue_for_link_without_commit, fanout_reference, links_carrying

    ours, uplink_address = network.our_address, network.uplink_address
    outcome = FanOut()
    if ours.point:
        return outcome  # a point feeds nobody
    point_sender = sender is not None and sender.is_point
    if point_sender:
        seen_by, path = [], []
    else:
        seen_by, path = seen_by_and_path(packed.text)
        if ours.net_node in path:
            outcome.loop = True
            _logger.warning("FTN %s: echomail %s in %s names this node in its PATH; not sent on (a loop)",
                            network.name, reference_id, tag)
            return outcome
    seen = set(seen_by)
    try:
        candidates = links_carrying(db, network.id, ECHO, tag)
    except sqlite3.OperationalError:  # a schema older than links
        candidates = []
    targets = []
    for link in candidates:
        if sender is not None and link.id == sender.id:
            continue
        if not link.is_point and link.address.net_node in seen:
            continue
        targets.append(link)
    if to_uplink is None:
        to_uplink = not from_uplink
    to_uplink = bool(to_uplink) and uplink_address.net_node not in seen
    new_seen = [*seen_by, ours.net_node, *(link.address.net_node for link in targets if not link.is_point)]
    if to_uplink:
        new_seen.append(uplink_address.net_node)
    new_path = path if path and path[-1] == ours.net_node else [*path, ours.net_node]
    for link in targets:
        try:
            copy = pack_message(forwarded_copy(packed, new_seen, new_path, orig=ours, dest=link.address))
            if enqueue_for_link_without_commit(db, link, kind="echomail",
                                               reference_id=fanout_reference(reference_id, link), packed=copy):
                outcome.links.append(link.address.four_d)
        except FtnQueueFullError as exc:
            outcome.full.append(link.address.four_d)
            _logger.warning("FTN %s: echomail %s in %s not sent to %s: %s", network.name, reference_id, tag,
                            link.address.four_d, exc)
        except FtnFormatError as exc:
            _logger.warning("FTN %s: echomail %s in %s not sent on: %s", network.name, reference_id, tag, exc)
            return outcome
    if to_uplink:
        try:
            copy = pack_message(forwarded_copy(packed, new_seen, new_path, orig=ours, dest=uplink_address))
            outcome.uplink = enqueue_outbound_without_commit(
                db, network.id, kind="echomail", reference_id=reference_id, destination=str(uplink_address),
                packed=copy,
            )
        except (FtnQueueFullError, FtnFormatError) as exc:
            outcome.full.append(uplink_address.four_d)
            _logger.warning("FTN %s: echomail %s in %s not sent to the uplink: %s", network.name, reference_id, tag,
                            exc)
    return outcome


def link_copies_without_commit(db: Database, network: FtnNetwork, packed: PackedMessage, tag: str,
                               reference_id: str, links: list) -> list[str]:
    """Queue a message written here, already carrying the SEEN-BY and PATH
    every copy shares, for each of `links` (`netbbs.ftn.scanner`'s local
    posts): only the packed header's destination changes. Returns the
    addresses queued for; a link at its bound is skipped with a node-log
    line."""
    from dataclasses import replace

    from netbbs.ftn.links import enqueue_for_link_without_commit, fanout_reference

    queued = []
    for link in links:
        copy = pack_message(replace(packed, dest_net=link.address.net, dest_node=link.address.node))
        try:
            if enqueue_for_link_without_commit(db, link, kind="echomail",
                                               reference_id=fanout_reference(reference_id, link), packed=copy):
                queued.append(link.address.four_d)
        except FtnQueueFullError as exc:
            _logger.warning("FTN %s: post %s in %s not sent to %s: %s", network.name, reference_id, tag,
                            link.address.four_d, exc)
    return queued


def seen_by_for_links(links: list) -> list[tuple[int, int]]:
    """The net/node a local post's SEEN-BY gains for the links it goes to:
    each downlink's, never a point's (FSC-0074)."""
    return [link.address.net_node for link in links if not link.is_point]
