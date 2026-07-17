"""
NetBBS Link background sync (design doc §12, round 119) — the piece
that makes a running node *originate* outbound Link activity, not just
answer it (round 118 wired up the inbound side only). Periodically
dials every configured seed via `netbbs.link.transport.dial_hello`,
then pushes this node's own `key_transition`s (and, since round 128,
its own `board_genesis`/`board_post` events) via `netbbs.link.
transport.push_events`.

Pushes *all* of `node.identity.transitions`, not just the `"signing"`-
purpose subset `build_hello`'s own bundle carries — round 116 excluded
`"transport"`-purpose transitions from the hello bundle specifically
because live transport-key *authentication* is Noise's own concern
(§11), but the transition record itself is still an ordinary event
(design doc round 90) that needs to reach other nodes via Link like
any other, so a node's transport-key rotations get gossiped too. No
per-peer "what have I already pushed" tracking — `handle_events` on
the receiving end already dedups via its own `known_event_ids` (§7:
"transport-level dedup... is a pure performance optimization"), so
re-pushing everything every interval is simply a harmless no-op for
whatever a peer has already seen, and keeps this module's own state
to nothing worth persisting.

**Round 128** extends the same "re-push everything every pass"
treatment to `netbbs.link.boards.load_own_board_events` — this node's
own Linked boards' genesis events and its own posts' board_post events,
read fresh off the `boards`/`posts` tables each pass rather than
tracked in any in-memory list of "what's pending push," the same
"nothing here worth persisting separately" reasoning as `identity.
transitions` above.

Deliberately minimal, matching what this round set out to fill: a
single interval, no per-seed backoff/retry state, no peer-list
exchange (a peer that has only ever *dialed this node*, never been
dialed by it, is not re-contacted here — round 118's own design-doc
sign-off note already flagged this as the next real gap), and no pull
("what am I missing") request — `push_events` is the only gossip
direction this module drives. A single unreachable or misbehaving seed
logs a warning and is skipped; it never aborts the rest of that pass
or the loop itself.

**Round 120**: `dial_hello` now persists the resulting `PeerRecord`
via a `DatabaseLane`, so `run_link_sync` takes one and threads it
through unchanged -- this module has no storage concerns of its own.
Round 128 reuses the same `lane` to read `load_own_board_events`
(a plain, synchronous, `db`-first function, dispatched the same way
every other lane-run function already is).

**Link messages (design doc round 93) get their own pass, not folded
into the per-seed loop above.** A `board_post`/`key_transition` is
correctly pushed to *every* configured seed (round 116's flood-to-
peers model); a `link_message` has exactly one intended recipient node
and must reach *that node specifically* (round 93's confirmed routing
decision) -- pushing it to an unrelated seed would just have that seed
correctly refuse it (`LinkNode.handle_events`'s own "not addressed to
me" rule). `_push_pending_link_mail` dials a pending item's target
directly using whatever address this node already knows for it from a
prior hello (`node.peers`), never a configured seed URL. A target this
node has never said hello to is skipped this pass -- nothing here
discovers new peers, matching round 116's own "no relay from a
stranger" boundary applied to composing/delivering too. Same "push
everything every pass, dedup handles idempotency" model as the rest of
this module for a pending *message* (`link_delivery_status` only
changes when a real `link_message_accepted`/`_bounced` arrives, never
just because a transport push succeeded -- round 93's own "a transport
ACK only means the bytes arrived" distinction); a pending
*acknowledgement* is simpler and genuinely one-shot, marked sent as
soon as the push itself succeeds, since nothing acknowledges an
acknowledgement.

**Round 95: `_sync_one_seed` also requests each seed's own peer list**,
right after its hello completes -- `netbbs.link.protocol.LinkNode.
handle_peer_list` records what comes back as unverified candidates
(`node.candidate_descriptors`), persisted the same way `dial_hello`
already persists its own `PeerRecord`. **Deliberately not consumed by
anything yet** -- this round only closes the *exchange* half of §12's
"a node isn't perpetually dependent on the seed list" resilience path;
actually falling back to dialing a candidate when every configured seed
is unavailable is real behavior this module doesn't have, named here
rather than silently assumed done. A failed peer-list request logs and
is skipped, same tolerance every other per-seed step in this loop
already has.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from aiohttp import ClientSession

from netbbs.link.boards import load_own_board_events
from netbbs.link.mail import (
    load_pending_link_mail,
    load_pending_link_mail_acknowledgements,
    mark_link_mail_acknowledgement_sent,
)
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError
from netbbs.link.transport import LinkTransportError, dial_hello, push_events, request_peer_list
from netbbs.storage.execution import DatabaseLane

_logger = logging.getLogger(__name__)


async def run_link_sync(
    node: LinkNode,
    session: ClientSession,
    seeds: list[str],
    own_hello_provider: Callable[[], HelloMessage],
    lane: DatabaseLane,
    *,
    interval_seconds: float,
) -> None:
    """
    Runs until cancelled: each pass dials every seed in `seeds` (in
    order, one at a time — this project's declared scale, §14, doesn't
    need concurrent dialing, and sequential keeps failures/logging
    easy to follow), pushes any pending Link mail directly to its own
    known recipients (see module docstring), then sleeps
    `interval_seconds` before the next pass. The first pass runs
    immediately on entry, not after an initial sleep — a node should
    try to reach the network as soon as it's up, not wait a full
    interval first.

    `own_hello_provider` is the same callable shape `netbbs.link.
    transport.LinkServer` takes (design doc round 117/118) — reused
    here rather than duplicating the addresses/outgoing_only-from-
    config logic a second time.
    """
    while True:
        for seed_url in seeds:
            await _sync_one_seed(node, session, seed_url, own_hello_provider, lane)
        await _push_pending_link_mail(node, session, lane)
        await asyncio.sleep(interval_seconds)


async def _sync_one_seed(
    node: LinkNode,
    session: ClientSession,
    seed_url: str,
    own_hello_provider: Callable[[], HelloMessage],
    lane: DatabaseLane,
) -> None:
    try:
        seed_peer = await dial_hello(node, session, seed_url, own_hello_provider(), lane)
    except (LinkTransportError, LinkProtocolError) as exc:
        _logger.warning("Link sync: could not complete hello with seed %s: %s", seed_url, exc)
        return

    own_events = list(node.identity.transitions) + await lane.run(load_own_board_events)
    try:
        await push_events(node, session, seed_url, own_events)
    except LinkTransportError as exc:
        _logger.warning("Link sync: could not push events to seed %s: %s", seed_url, exc)

    # Round 95: also ask this seed who else it knows -- the resilience
    # path for when every *configured* seed is eventually unavailable
    # (not yet consumed anywhere; see module docstring's own note on
    # what this round deliberately doesn't build yet).
    try:
        await request_peer_list(node, session, seed_url, seed_peer.fingerprint, lane)
    except LinkTransportError as exc:
        _logger.warning("Link sync: could not request a peer list from seed %s: %s", seed_url, exc)


def _dialable_address(node: LinkNode, target_fingerprint: str) -> str | None:
    """The first advertised address on file for `target_fingerprint`
    (§12: "peers try them in order" -- multi-address fallback isn't
    built anywhere in this transport layer yet, for any caller, so this
    doesn't attempt it either), or `None` if this node has never said
    hello to that fingerprint, or that peer is outgoing-only (round 12:
    it publishes no address at all, by design -- this node genuinely
    cannot dial it)."""
    peer = node.peers.get(target_fingerprint)
    if peer is None:
        return None
    addresses = peer.descriptor.payload.get("addresses")
    if not addresses:
        return None
    first = addresses[0]
    return f"{first['protocol']}://{first['address']}:{first['port']}"


async def _push_pending_link_mail(node: LinkNode, session: ClientSession, lane: DatabaseLane) -> None:
    pending_messages = await lane.run(load_pending_link_mail)
    for target_fingerprint, message in pending_messages:
        base_url = _dialable_address(node, target_fingerprint)
        if base_url is None:
            continue
        try:
            await push_events(node, session, base_url, [message])
        except LinkTransportError as exc:
            _logger.warning("Link sync: could not push pending mail to %s: %s", target_fingerprint, exc)

    pending_acks = await lane.run(load_pending_link_mail_acknowledgements)
    for target_fingerprint, ack in pending_acks:
        base_url = _dialable_address(node, target_fingerprint)
        if base_url is None:
            continue
        try:
            await push_events(node, session, base_url, [ack])
        except LinkTransportError as exc:
            _logger.warning("Link sync: could not push pending acknowledgement to %s: %s", target_fingerprint, exc)
            continue
        await lane.run(mark_link_mail_acknowledgement_sent, ack)
