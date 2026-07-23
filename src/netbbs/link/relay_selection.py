"""
Relay selection and self-healing (design doc §12, issue #58) --
pure, synchronous, `db`-first functions deciding which reachable full
peers an outgoing-only node should ask to relay for it, and detecting
when an already-serving relay's observed reliability has dropped enough
to warrant replacing it.

Deliberately separate from `netbbs.link.sync`'s own periodic scheduling
-- this module only ever decides *what* to do next, never *when*, nor
does it dial anyone or touch `LinkNode` state itself. See `netbbs.link.
sync` for the loop that actually calls these on a schedule, requests
consent from whatever they return (`netbbs.link.transport.request_
relay_consent`), drops a self-healing casualty from `node.relays_
serving_me`, and republishes the resulting `endpoint_descriptor` (issue
#58 task #25) -- the same "decide here, act there" split `netbbs.link.
reliability` and `netbbs.link.protocol`'s relay-consent methods already
apply.
"""

from __future__ import annotations

from netbbs.link.protocol import LinkNode
from netbbs.link.reliability import rank_by_reliability, reliability_score
from netbbs.storage.database import Database

# Design doc §12: "a node picks a small redundant set (3) of
# candidate relays."
TARGET_RELAY_COUNT = 3

# Self-healing threshold (design doc §12: "a relay whose reliability
# drops ... is automatically replaced"). Deliberately below `netbbs.
# link.reliability`'s own neutral prior (0.5) -- a never-dialed
# candidate is exactly as eligible as ever, but a relay actually
# *observed* to fail more often than it succeeds has demonstrated a
# real problem, not just an unlucky attempt or two.
_RELIABILITY_FLOOR = 0.3


def _reachable_full_peer_candidates(node: LinkNode) -> list[str]:
    """
    Fingerprints this node could plausibly ask to relay for it: a
    reachable full peer (either of the two deployment modes -- has at
    least one address, is not itself outgoing-only) among either a
    completed peer or an unverified candidate descriptor (the
    "a weak prior worth trying" framing, extended here from hello-
    bootstrap to relay selection) -- never this node's own fingerprint,
    never a fingerprint already granted or already asked
    (`LinkNode.relays_serving_me`/`pending_own_relay_requests`).
    """
    seen: set[str] = set()
    for fingerprint, peer in node.peers.items():
        if fingerprint == node.identity.fingerprint:
            continue
        if peer.descriptor.payload.get("outgoing_only"):
            continue
        seen.add(fingerprint)
    for fingerprint, descriptor in node.candidate_descriptors.items():
        if fingerprint == node.identity.fingerprint:
            continue
        if descriptor.payload.get("outgoing_only"):
            continue
        seen.add(fingerprint)
    return [
        fingerprint
        for fingerprint in seen
        if fingerprint not in node.relays_serving_me and fingerprint not in node.pending_own_relay_requests
    ]


def select_relay_candidates(db: Database, node: LinkNode, *, count: int = TARGET_RELAY_COUNT) -> list[str]:
    """
    Up to `count` reachable-full-peer fingerprints this node should ask
    to relay for it next, ranked most-reliable-first (design doc §12:
    candidates "ranked by observed reliability") -- already excludes
    anyone currently serving or already asked.

    Returns an empty list once `node.relays_serving_me` already holds
    `count` entries -- topping up on top of an already-full set is the
    caller's own call to make (issue #58 task #25), e.g. only after
    `relays_needing_replacement` below flags a drop and the caller has
    removed the casualty from `relays_serving_me` first.
    """
    if len(node.relays_serving_me) >= count:
        return []
    candidates = _reachable_full_peer_candidates(node)
    ranked = rank_by_reliability(db, candidates)
    return ranked[: count - len(node.relays_serving_me)]


def relays_needing_replacement(db: Database, node: LinkNode, *, floor: float = _RELIABILITY_FLOOR) -> list[str]:
    """
    Fingerprints among `node.relays_serving_me` whose observed
    reliability has dropped to or below `floor` -- design doc §12's
    self-healing signal, covering "silently dropping traffic instead of
    relaying" exactly as well as outright going offline, since both
    show up identically as failed dial attempts (`netbbs.link.
    reliability.record_dial_outcome`).

    The caller (issue #58 task #25) is responsible for actually
    dropping these from `relays_serving_me`, selecting replacements
    (`select_relay_candidates`, once these are excluded first, which
    happens automatically since a dropped fingerprint is no longer in
    `relays_serving_me` by the time it's called), and republishing this
    node's own `endpoint_descriptor` to reflect the change.
    """
    return [
        fingerprint for fingerprint in node.relays_serving_me if reliability_score(db, fingerprint) <= floor
    ]
