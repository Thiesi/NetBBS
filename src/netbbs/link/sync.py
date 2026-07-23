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

**Round 97: the per-pass seed list is operator-configured `seeds` plus
whatever `netbbs.link.seedlist.run_scheduled_seed_refresh` has most
recently cached**, re-merged every pass (not just once at startup) so a
live-fetched seed takes effect without a restart.

**Candidate fallback**: if every seed in a given pass fails (or none
were configured/cached at all), `_try_candidate_fallback` tries a small
random sample of `node.candidate_descriptors` (peer-list-discovered,
unverified addresses) before giving up for that pass -- closing the gap
earlier rounds' own docstrings named as still open ("not yet consumed
by anything"). Never a first resort: the normal seed list is always
tried first, every pass, regardless of whether the previous pass had to
fall back.

**Automatic relay selection, pickup, and send-via-relay (design doc
§12 round 95, issue #58)**, both gated on this node's own hello
currently claiming `outgoing_only` (a full peer never needs relays --
checked via `own_hello_provider()` itself rather than a new parameter,
since that callable already encodes the addresses/outgoing_only
decision, this module's own long-standing convention):
`_maintain_relay_selection` runs right after the seed loop/fallback
above, dropping any self-healing casualty (`netbbs.link.relay_
selection.relays_needing_replacement`) and requesting consent from
freshly-ranked candidates (`select_relay_candidates`) to top back up
toward a redundant set -- no separate "republish my descriptor" step
exists, since `LinkNode.build_hello` already reads `relays_serving_me`
live, so the very next hello this node sends (including the seed-
dialing loop that already ran earlier in the *same* pass) carries any
change automatically. `_pickup_relay_mail` then collects whatever each
currently-serving relay is holding and runs it through the exact same
`LinkNode.handle_events` acceptance path a directly-arrived event
already goes through (never trusting the relay itself -- see `netbbs.
link.relay_mailbox`'s own docstring). Finally, `_push_pending_link_
mail`'s existing per-message loop falls back to depositing at a
recipient's own published relay(s) (`_relay_base_urls_for_peer`) when
no direct address is dialable -- checking `node.candidate_descriptors`
as well as `node.peers`, since a genuinely outgoing-only recipient can
never become a completed peer of a sender who can never dial it in the
first place; the *only* way such a sender ever learns that recipient's
`relays` field is secondhand, via ordinary peer-list exchange with
someone who has met them directly (see that function's own docstring
for why this is safe: a wrong/stale candidate address costs a failed
deposit, never a confidentiality issue, since the payload is already
sealed to the real recipient's own key). Only `link_message` gets this
fallback, never an acknowledgement -- `netbbs.link.relay_mailbox`'s own
documented boundary this round.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable

from aiohttp import ClientSession

from netbbs.link.boards import load_own_board_events
from netbbs.link.events import LINK_MESSAGE_OBJECT_TYPE, EndpointDescriptor, LinkMessage
from netbbs.link.mail import (
    deliver_link_message,
    expire_link_message_delivery,
    get_link_mail_acknowledgement,
    get_link_message_for_delivery,
)
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError
from netbbs.link.relay_selection import relays_needing_replacement, select_relay_candidates
from netbbs.link.reliability import record_dial_outcome
from netbbs.link.seedlist import get_cached_supplementary_seeds
from netbbs.link.store import delete_relay_consent, save_event, save_peer
from netbbs.link.transport import (
    LinkTransportError,
    deposit_into_relay_mailbox,
    dial_hello,
    pickup_from_relay_mailbox,
    push_events,
    request_peer_list,
    request_relay_consent,
)
from netbbs.link.work_items import (
    KIND_LINK_MAIL_ACK,
    KIND_LINK_MAIL_DELIVERY,
    load_due_work_items,
    record_failure,
    record_success,
)
from netbbs.storage.execution import DatabaseLane

_logger = logging.getLogger(__name__)

# Round 95: how many discovered-but-unverified candidates to try, at
# most, when every configured/cached seed has failed for a pass -- a
# small, bounded number matching relay selection's own "pick a small
# number" precedent (no reliability ranking exists yet to pick more
# cleverly), not every entry in node.candidate_descriptors.
_MAX_CANDIDATE_FALLBACK_ATTEMPTS = 5


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

    `seeds` is the *operator-configured* list only (explicit intent
    always wins, round 97) — each pass also merges in whatever `netbbs.
    link.seedlist.run_scheduled_seed_refresh` has most recently cached
    (empty until/unless that task is running and Link is enabled), so a
    node started with zero configured seeds still eventually reaches
    the network once a live fetch succeeds, without needing a restart.
    Re-read from the lane every pass, not captured once at startup, or
    "live" refresh would only ever take effect after a restart.
    """
    while True:
        supplementary = await lane.run(get_cached_supplementary_seeds)
        # De-duplicated, order-preserving: operator-configured first.
        pass_seeds = list(dict.fromkeys(seeds + supplementary))
        reached_network = False
        for seed_url in pass_seeds:
            succeeded = await _sync_one_seed(node, session, seed_url, own_hello_provider, lane)
            reached_network = reached_network or succeeded
        if not reached_network:
            # Round 95's own-stated resilience path: every configured/
            # cached seed failed this pass (or none were configured at
            # all) -- fall back to a discovered candidate rather than
            # sitting isolated until the next pass tries the same seeds
            # again. Never a first resort: an operator's explicit seed
            # configuration and a genuinely live supplementary list
            # always take priority when either actually works.
            await _try_candidate_fallback(node, session, own_hello_provider, lane)
        # Round 95/issue #58: relay selection/pickup only makes sense
        # for an outgoing-only node -- a full peer is directly dialable
        # by definition, so it has nothing to gain from seeking relays
        # (design doc §12: "an outgoing-only node selects its own
        # relays automatically," never a full peer). Checked via this
        # node's own current hello rather than a separate parameter --
        # `own_hello_provider` already encodes the addresses/outgoing_
        # only decision (this method's own docstring), so there's
        # nothing new to thread through from node startup config.
        if own_hello_provider().descriptor.payload.get("outgoing_only"):
            # Maintain the outgoing relay set and pick up anything held
            # *before* pushing pending mail below -- so a message that
            # only just became deliverable via a freshly-selected relay
            # still gets its own send-via-relay attempt in the same
            # pass, not one whole interval later.
            await _maintain_relay_selection(node, session, own_hello_provider, lane)
            await _pickup_relay_mail(node, session, own_hello_provider, lane)
        await _push_pending_link_mail(node, session, lane)
        await asyncio.sleep(interval_seconds)


async def _sync_one_seed(
    node: LinkNode,
    session: ClientSession,
    seed_url: str,
    own_hello_provider: Callable[[], HelloMessage],
    lane: DatabaseLane,
) -> bool:
    """Returns whether the hello itself succeeded -- the bar `run_link_
    sync` uses to decide "did this node reach the network at all this
    pass" (round 95's candidate-fallback trigger). A failed push/peer-
    list-request afterward doesn't downgrade a successful hello back to
    failure; those are secondary, independently-tolerated steps, not
    the "are we isolated" signal."""
    try:
        seed_peer = await dial_hello(node, session, seed_url, own_hello_provider(), lane)
    except (LinkTransportError, LinkProtocolError) as exc:
        _logger.warning("Link sync: could not complete hello with seed %s: %s", seed_url, exc)
        return False

    own_events = list(node.identity.transitions) + await lane.run(
        load_own_board_events, node.identity.fingerprint
    )
    try:
        await push_events(node, session, seed_url, own_events)
    except LinkTransportError as exc:
        _logger.warning("Link sync: could not push events to seed %s: %s", seed_url, exc)

    # Round 95: also ask this seed who else it knows -- feeds the
    # candidate pool `_try_candidate_fallback` (below) draws from.
    try:
        await request_peer_list(node, session, seed_url, seed_peer.fingerprint, lane)
    except LinkTransportError as exc:
        _logger.warning("Link sync: could not request a peer list from seed %s: %s", seed_url, exc)

    return True


def _dialable_addresses(descriptor: EndpointDescriptor) -> list[str]:
    """Every advertised address in `descriptor`, as dialable base URLs,
    in the order the descriptor itself lists them (design doc §12:
    "peers try them in order", issue #58 -- previously only `addresses[0]`
    was ever attempted anywhere in this transport layer; callers now try
    each of these in turn, stopping at the first that works). Empty for
    an outgoing-only descriptor (round 12: by design, genuinely
    undialable directly -- see `netbbs.link.relay_mailbox`/this
    module's own `_relay_base_urls_for_peer` for how such a peer is
    still reachable, via `payload["relays"]`, once it has any)."""
    addresses = descriptor.payload.get("addresses")
    if not addresses:
        return []
    return [f"{a['protocol']}://{a['address']}:{a['port']}" for a in addresses]


def _dialable_addresses_for_peer(node: LinkNode, target_fingerprint: str) -> list[str]:
    """Every advertised address on file for `target_fingerprint`, or an
    empty list if this node has never said hello to that fingerprint
    (or it's outgoing-only with none)."""
    peer = node.peers.get(target_fingerprint)
    if peer is None:
        return []
    return _dialable_addresses(peer.descriptor)


def _candidate_dialable_addresses(node: LinkNode, fingerprint: str) -> list[str]:
    """Every advertised address on file for `fingerprint`, whether it's
    a completed peer or merely an unverified candidate (round 95/issue
    #58's relay selection draws from both -- see `netbbs.link.relay_
    selection.select_relay_candidates`'s own docstring for why). Checks
    `node.peers` first since a completed peer's own descriptor is more
    current than a possibly-stale candidate entry for the same
    fingerprint."""
    peer = node.peers.get(fingerprint)
    if peer is not None:
        return _dialable_addresses(peer.descriptor)
    descriptor = node.candidate_descriptors.get(fingerprint)
    if descriptor is not None:
        return _dialable_addresses(descriptor)
    return []


def _relay_base_urls_for_peer(node: LinkNode, target_fingerprint: str) -> list[str]:
    """
    Base URLs of every relay `target_fingerprint` has itself published
    as serving it (`EndpointDescriptor.payload["relays"]`, round 95/
    issue #58) that *this* node can also directly dial -- a relay this
    node has never itself met is skipped (round 116's own "no relay
    from a stranger" boundary, extended here: reaching a relay to
    deposit at it needs the same direct-address knowledge reaching
    anyone else does). Empty if `target_fingerprint` is unknown, or
    known but has published no relays.

    Checks `node.candidate_descriptors` as well as `node.peers`,
    **deliberately unlike** `_dialable_addresses_for_peer`'s own
    peers-only check -- a genuinely outgoing-only recipient can never
    complete a direct hello with this node at all (there's no address
    to dial it *at*), so a completed-peer record for one will simply
    never exist here; the *only* way this node ever learns such a
    recipient's `relays` field is secondhand, via peer-list exchange
    with someone who has directly met them (round 95's own "worth
    trying, not trusting outright" framing, applied here to routing
    rather than trust: a wrong or stale candidate address just costs a
    failed deposit attempt, never a confidentiality issue, since the
    payload is already sealed to the real recipient's own key
    regardless of where it ends up)."""
    peer = node.peers.get(target_fingerprint)
    descriptor = peer.descriptor if peer is not None else node.candidate_descriptors.get(target_fingerprint)
    if descriptor is None:
        return []
    relay_fingerprints = descriptor.payload.get("relays") or []
    urls: list[str] = []
    for relay_fingerprint in relay_fingerprints:
        urls.extend(_candidate_dialable_addresses(node, relay_fingerprint))
    return urls


async def _try_addresses_via(base_urls: list[str], attempt: Callable[[str], Awaitable[bool]]) -> bool:
    """Try `attempt(base_url)` for each of `base_urls` in order,
    stopping at the first that returns `True` (design doc §12: "peers
    try them in order", issue #58). Returns `False` if every address was
    tried and none succeeded, or if `base_urls` is empty."""
    for base_url in base_urls:
        if await attempt(base_url):
            return True
    return False


async def _try_candidate_fallback(
    node: LinkNode,
    session: ClientSession,
    own_hello_provider: Callable[[], HelloMessage],
    lane: DatabaseLane,
) -> None:
    """
    Round 95's own-stated resilience path, closing the gap named in
    earlier rounds' own docstrings: "a node isn't perpetually dependent
    on the seed list." Only ever called once every configured/cached
    seed has already failed this pass (see `run_link_sync`'s own
    caller). Tries a small, randomly-sampled subset of `node.candidate_
    descriptors` -- round 95's own "pick a small number" precedent for
    relay selection, reused here for the same reason: no reliability
    ranking exists yet to pick more cleverly (that's automatic relay
    selection's own still-open reliability-metric question, not
    answered by this round), and trying every known candidate every
    pass would be excessive at this project's declared scale (§14).
    Random rather than insertion order, so a consistently-unreachable
    early candidate doesn't get retried every single pass at the
    expense of ones never tried at all.

    Stops at the first successful hello -- one reconnection is enough
    to end this pass's isolation; the next pass tries the normal seed
    list again first, as always. On success, `LinkNode.handle_hello`
    already promotes the candidate out of `candidate_descriptors` into
    a real peer (see that method's own docstring), so there is no
    separate bookkeeping to do here. A candidate with no dialable
    address (outgoing-only, or a malformed entry) is skipped without
    counting against the sample size, the same "genuinely cannot dial
    it" reasoning `_dialable_addresses` already documents. Every one of
    a multi-address candidate's addresses is tried, in order, before
    moving to the next candidate (issue #58) -- still one "attempt"
    against the sample-size bound, not one per address.

    Every candidate this function actually attempts (successful or not,
    including the one that finally ends the loop) has its outcome
    recorded via `netbbs.link.reliability.record_dial_outcome` (issue
    #58) -- a failed attempt is exactly as informative for scoring
    purposes as a successful one, and this is the direct-observation
    sample relay selection later ranks candidates by (see that module's
    own docstring for why nothing existing could be reused here). Still
    stops at the first success, as before -- a candidate never reached
    this call at all (beyond the attempt cap, or simply never picked in
    this pass's random sample) has nothing recorded for it here.
    """
    candidates = list(node.candidate_descriptors.items())
    if not candidates:
        return
    random.shuffle(candidates)

    attempted = 0
    for fingerprint, descriptor in candidates:
        if attempted >= _MAX_CANDIDATE_FALLBACK_ATTEMPTS:
            return
        base_urls = _dialable_addresses(descriptor)
        if not base_urls:
            continue
        attempted += 1
        succeeded = await _try_addresses_via(
            base_urls, lambda url: _sync_one_seed(node, session, url, own_hello_provider, lane)
        )
        await lane.run(record_dial_outcome, fingerprint, succeeded=succeeded)
        if succeeded:
            _logger.info(
                "Link sync: every configured seed failed this pass -- reached the network "
                "via fallback candidate %s instead",
                fingerprint,
            )
            return


async def _request_one_relay_consent(
    node: LinkNode,
    session: ClientSession,
    base_url: str,
    relay_fingerprint: str,
    own_hello_provider: Callable[[], HelloMessage],
    lane: DatabaseLane,
) -> bool:
    """One relay-consent attempt against a single `base_url`, collapsed
    to a bool for `_try_addresses_via`'s own contract. A completed hello
    always precedes the actual consent request -- required the first
    time a candidate is only in `node.candidate_descriptors` (relay
    consent's own acceptance rule needs the requester to already be a
    completed peer, see `LinkNode.handle_relay_consent_request`'s own
    docstring), and a harmless refresh otherwise. Both
    `LinkTransportError` (transport-level failure) and
    `LinkProtocolError` (the relay's returned response failed
    verification) are treated as "this candidate didn't work out," the
    same tolerance `_sync_one_seed` already applies to its own
    `dial_hello` call -- one bad or hostile candidate must not abort the
    rest of this pass."""
    try:
        await dial_hello(node, session, base_url, own_hello_provider(), lane)
        response = await request_relay_consent(node, session, base_url, relay_fingerprint, lane)
    except (LinkTransportError, LinkProtocolError) as exc:
        _logger.warning(
            "Link sync: relay consent request to %s (%s) failed: %s", relay_fingerprint, base_url, exc
        )
        return False
    return bool(response.payload["accepted"])


async def _maintain_relay_selection(
    node: LinkNode,
    session: ClientSession,
    own_hello_provider: Callable[[], HelloMessage],
    lane: DatabaseLane,
) -> None:
    """
    Round 95/issue #58's automatic relay selection. `run_link_sync`
    only calls this for an outgoing-only node (design doc §12: a full
    peer never needs relays, see that caller's own comment for why) --
    this function itself has no such guard, so a future caller wiring
    it in some other context is responsible for the same gate.

    Two steps: **self-healing** first -- drop any currently-serving
    relay whose observed reliability has fallen to or below `netbbs.
    link.relay_selection`'s floor, both in memory and on disk (`netbbs.
    link.store.delete_relay_consent`, so a restart doesn't resurrect a
    relay this node already gave up on) -- then **top up** back toward
    `TARGET_RELAY_COUNT` by requesting consent from freshly-ranked
    candidates (unlike `_try_candidate_fallback`'s "stop at the first
    success," every returned candidate is tried, since the goal is a
    *redundant set*, not just one working relay).

    **No explicit "republish my descriptor" step is needed here** --
    `LinkNode.build_hello` already reads `relays_serving_me` live (see
    that method's own docstring), so the very next hello this node
    sends to anyone -- the seed-dialing loop that already runs earlier
    in the same pass -- carries the updated `relays` field automatically.
    This is what this module's own docstring means by "self-healing
    republication" needing no separate mechanism.
    """
    for stale_fingerprint in await lane.run(relays_needing_replacement, node):
        node.relays_serving_me.pop(stale_fingerprint, None)
        await lane.run(delete_relay_consent, stale_fingerprint, role="relay_for_me")
        _logger.info(
            "Link sync: dropping relay %s -- its observed reliability has fallen below the "
            "self-healing floor",
            stale_fingerprint,
        )

    for candidate_fingerprint in await lane.run(select_relay_candidates, node):
        base_urls = _candidate_dialable_addresses(node, candidate_fingerprint)
        if not base_urls:
            continue
        await _try_addresses_via(
            base_urls,
            lambda url: _request_one_relay_consent(
                node, session, url, candidate_fingerprint, own_hello_provider, lane
            ),
        )


async def _pickup_one_relay_mailbox(
    session: ClientSession, base_urls: list[str], own_hello_provider: Callable[[], HelloMessage]
) -> list[LinkMessage]:
    """Try each of `base_urls` in turn (issue #58's own multi-address
    "peers try them in order" convention), returning whatever the first
    reachable one hands back. Raises `LinkTransportError` only once
    every address has failed."""
    last_error: LinkTransportError | None = None
    for url in base_urls:
        try:
            return await pickup_from_relay_mailbox(session, url, own_hello_provider())
        except LinkTransportError as exc:
            last_error = exc
    raise last_error or LinkTransportError("no addresses to try")


async def _pickup_relay_mail(
    node: LinkNode,
    session: ClientSession,
    own_hello_provider: Callable[[], HelloMessage],
    lane: DatabaseLane,
) -> None:
    """
    Round 95/issue #58: for every relay currently serving this node
    (`node.relays_serving_me`), pick up whatever mail it's holding and
    feed each envelope through the exact same `LinkNode.handle_events`
    acceptance path a directly-arrived `link_message` already goes
    through, keyed by that message's own claimed sender -- never the
    relay that happened to hand it over (see `netbbs.link.relay_mailbox.
    pickup_relay_mailbox_envelopes`'s own docstring for why the relay
    itself never verifies anything). A sender this node has no
    completed hello with is rejected here exactly as it would be for a
    directly-arrived message -- relaying doesn't relax "no relay from a
    stranger," it just changes which node performs the check.

    Persistence after acceptance mirrors `LinkServer._handle_events`
    exactly (`save_event` then `deliver_link_message`) -- this is the
    one place in this module that duplicates transport-layer bookkeeping
    rather than calling through `netbbs.link.transport`, since pickup
    has no equivalent existing entry point to reuse (it isn't an inbound
    HTTP request `LinkServer` ever sees).
    """
    for relay_fingerprint in list(node.relays_serving_me):
        base_urls = _candidate_dialable_addresses(node, relay_fingerprint)
        if not base_urls:
            continue
        try:
            messages = await _pickup_one_relay_mailbox(session, base_urls, own_hello_provider)
        except LinkTransportError as exc:
            _logger.warning("Link sync: could not pick up mail from relay %s: %s", relay_fingerprint, exc)
            continue

        for message in messages:
            claimed_sender = message.payload.get("sender", {}).get("home_node_fingerprint")
            if claimed_sender is None:
                continue
            raw = message.to_dict()
            try:
                accepted = node.handle_events(claimed_sender, [raw])
            except LinkProtocolError as exc:
                _logger.warning(
                    "Link sync: rejected a link_message picked up from relay %s: %s",
                    relay_fingerprint,
                    exc,
                )
                continue
            for content_id in accepted:
                envelope = node.events[content_id]
                await lane.run(
                    save_event,
                    sender_fingerprint=claimed_sender,
                    content_id=content_id,
                    object_type=LINK_MESSAGE_OBJECT_TYPE,
                    envelope=envelope,
                )
                await lane.run(deliver_link_message, envelope, node_identity=node.identity)
            if accepted:
                await lane.run(save_peer, node.peers[claimed_sender])


async def _deposit_one(
    session: ClientSession, base_url: str, recipient_fingerprint: str, message: LinkMessage
) -> bool:
    """One relay-mailbox deposit attempt against a single `base_url`,
    collapsed to a bool for `_try_addresses_via`'s own contract -- a
    failure here just means "try the next relay," not this pending
    message's own final outcome."""
    try:
        await deposit_into_relay_mailbox(session, base_url, recipient_fingerprint, message)
        return True
    except LinkTransportError:
        return False


async def _push_one(node: LinkNode, session: ClientSession, base_url: str, events: list) -> bool:
    """One `push_events` attempt against a single `base_url`, collapsed
    to a bool for `_try_addresses_via`'s own contract -- a failure here
    just means "try the next address," not this pass's own final
    outcome, so the exception is swallowed, not logged, at this level.
    """
    try:
        await push_events(node, session, base_url, events)
        return True
    except LinkTransportError:
        return False


async def _push_pending_link_mail(node: LinkNode, session: ClientSession, lane: DatabaseLane) -> None:
    """
    Attempts every currently-due `link_mail_delivery`/`link_mail_ack`
    work item (design doc §13.7, issue #60's second operational slice)
    -- replaces this function's old "reload and resend every pending
    row, unconditionally, every pass, forever" shape. A work item not
    yet due (still backing off after an earlier failure) is simply not
    returned by `load_due_work_items` this pass; it'll be picked up
    again once `next_attempt_at` has passed.
    """
    for work_item in await lane.run(load_due_work_items, kind=KIND_LINK_MAIL_DELIVERY):
        result = await lane.run(get_link_message_for_delivery, work_item.reference_id)
        if result is None:
            # mail_messages rows are never deleted by this delivery path
            # -- shouldn't happen, but a work item outliving its target
            # is tolerated as "nothing left to push," not a crash.
            await lane.run(record_success, work_item)
            continue
        message, delivery_status = result
        if delivery_status != "pending":
            # Already resolved -- a genuine accepted/bounced event (or,
            # for 'expired', an earlier dead-letter) arrived through some
            # other path. Further pushes serve no purpose.
            await lane.run(record_success, work_item)
            continue

        # Issue #69: compose_link_message (netbbs.link.mail) is DB-only
        # and never touches a live LinkNode, so a composed message isn't
        # in node.events yet -- register it here, the one point every
        # caller of compose_link_message funnels through before this
        # message ever actually leaves the node, and strictly before any
        # possible push attempt below. Without this, _resolve_own_link_
        # message (netbbs.link.protocol) can never recognize this node's
        # own message when the recipient's accepted/bounced acknowledgement
        # comes back, and rejects it every time. Guarded by known_event_ids
        # so a work item still retrying across passes doesn't re-persist.
        if message.content_id not in node.known_event_ids:
            node.known_event_ids.add(message.content_id)
            node.events[message.content_id] = message.to_dict()
            await lane.run(
                save_event,
                sender_fingerprint=node.identity.fingerprint,
                content_id=message.content_id,
                object_type=LINK_MESSAGE_OBJECT_TYPE,
                envelope=message.to_dict(),
            )

        target_fingerprint = work_item.target_fingerprint
        base_urls = _dialable_addresses_for_peer(node, target_fingerprint)
        delivered = False
        if base_urls:
            delivered = await _try_addresses_via(base_urls, lambda url: _push_one(node, session, url, [message]))
        if not delivered:
            # Round 95/issue #58: send-via-relay -- the recipient is
            # either unknown-as-directly-dialable or genuinely outgoing-
            # only. Only `link_message` gets this fallback (never an
            # acknowledgement, below) -- `netbbs.link.relay_mailbox`'s
            # own documented boundary; see that module's docstring.
            relay_urls = _relay_base_urls_for_peer(node, target_fingerprint)
            if relay_urls:
                delivered = await _try_addresses_via(
                    relay_urls, lambda url: _deposit_one(session, url, target_fingerprint, message)
                )

        if delivered:
            await lane.run(record_success, work_item)
        else:
            updated = await lane.run(
                record_failure, work_item, error="could not push on any known or relayed address"
            )
            if updated.status == "dead_lettered":
                await lane.run(expire_link_message_delivery, work_item.reference_id)
                _logger.warning(
                    "Link sync: dead-lettered mail to %s after %s attempts",
                    target_fingerprint, updated.attempts,
                )

    for work_item in await lane.run(load_due_work_items, kind=KIND_LINK_MAIL_ACK):
        ack = await lane.run(get_link_mail_acknowledgement, work_item.reference_id)
        if ack is None:
            await lane.run(record_success, work_item)
            continue

        target_fingerprint = work_item.target_fingerprint
        base_urls = _dialable_addresses_for_peer(node, target_fingerprint)
        delivered = False
        if base_urls:
            delivered = await _try_addresses_via(base_urls, lambda url: _push_one(node, session, url, [ack]))

        if delivered:
            await lane.run(record_success, work_item)
        else:
            updated = await lane.run(record_failure, work_item, error="could not push on any known address")
            if updated.status == "dead_lettered":
                _logger.warning(
                    "Link sync: dead-lettered acknowledgement to %s after %s attempts",
                    target_fingerprint, updated.attempts,
                )
