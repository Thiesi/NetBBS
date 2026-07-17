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
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from aiohttp import ClientSession

from netbbs.link.boards import load_own_board_events
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError
from netbbs.link.transport import LinkTransportError, dial_hello, push_events
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
    easy to follow), then sleeps `interval_seconds` before the next
    pass. The first pass runs immediately on entry, not after an
    initial sleep — a node should try to reach the network as soon as
    it's up, not wait a full interval first.

    `own_hello_provider` is the same callable shape `netbbs.link.
    transport.LinkServer` takes (design doc round 117/118) — reused
    here rather than duplicating the addresses/outgoing_only-from-
    config logic a second time.
    """
    while True:
        for seed_url in seeds:
            await _sync_one_seed(node, session, seed_url, own_hello_provider, lane)
        await asyncio.sleep(interval_seconds)


async def _sync_one_seed(
    node: LinkNode,
    session: ClientSession,
    seed_url: str,
    own_hello_provider: Callable[[], HelloMessage],
    lane: DatabaseLane,
) -> None:
    try:
        await dial_hello(node, session, seed_url, own_hello_provider(), lane)
    except (LinkTransportError, LinkProtocolError) as exc:
        _logger.warning("Link sync: could not complete hello with seed %s: %s", seed_url, exc)
        return

    own_events = list(node.identity.transitions) + await lane.run(load_own_board_events)
    try:
        await push_events(node, session, seed_url, own_events)
    except LinkTransportError as exc:
        _logger.warning("Link sync: could not push events to seed %s: %s", seed_url, exc)
