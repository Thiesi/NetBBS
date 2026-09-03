"""
Link-wide live direct messages (issue #168, design doc §8.10.3): the
first caller-facing use of a real-time session that is not a linked
channel -- one private line from a user on this node to a user currently
online on another node, delivered live or not at all, over a session this
module establishes on demand: reusing the registry's existing session,
dialing the peer directly when it advertises a real-time address, or
rendezvousing through a live relay (`netbbs.link.realtime_relay`) when
neither node can dial the other.

Decision 3 (v1 fallback UX): when no path works, the caller gets an
explicit, reason-free refusal -- `DirectChatUnreachable` -- that the UI
turns into "<user> can't be reached for live chat right now" plus a
pointer at Link mail. Which of the possible reasons applied (peer
offline, no relay, relay at capacity, rendezvous timeout) is deliberately
not surfaced to the caller (§12: operational detail about a *remote* node
isn't a caller's to see); it is logged.

Delivery on the receiving node is a plain injected callable
(`deliver`) built by `netbbs.__main__` from the same hub/mailbox/session
registry the local `/msg` uses -- this module stays in `netbbs.link` and
never imports `netbbs.net`. A received `direct_message` is re-checked
against local Phase-4 `REALTIME` policy for the sending node (§8.10.2's
"checked again at delivery") before it is handed over.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from netbbs.link.enforcement import LinkPolicyAction, decide_node_action
from netbbs.link.node_identity import NodeIdentity
from netbbs.link.protocol import LinkNode, RealtimeFrame, RealtimeProtocolVersionError, build_direct_message_frame
from netbbs.link.realtime_relay import RealtimeRelayClient, RelayRendezvousError
from netbbs.link.reliable_nodes import ReliableNode, effective_reliable_nodes
from netbbs.link.transport import (
    LinkRealtimeSession,
    LinkRealtimeSessionRegistry,
    dial_realtime_session,
    dialable_realtime_addresses_for_peer,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_logger = logging.getLogger(__name__)

DIRECT_CHAT_DEFAULT_DIAL_TIMEOUT_SECONDS = 10.0


class DirectChatUnreachable(Exception):
    """No live path to the peer node right now (Decision 3). `reason` is
    for logs/tests only; the caller-facing text never distinguishes."""

    def __init__(self, fingerprint: str, reason: str) -> None:
        super().__init__(f"no live path to {fingerprint}: {reason}")
        self.fingerprint = fingerprint
        self.reason = reason


@dataclass(frozen=True)
class IncomingDirectMessage:
    """One delivered `direct_message`, attributed to the authenticated
    sending node -- what `deliver` receives."""

    from_node_fingerprint: str
    from_user_id: str
    from_display_label: str
    to_user_id: str
    body: str
    created_at: str


def _url_host_port(url: str) -> tuple[str, int | None]:
    """`(hostname, port)` for a roster URL, or `("", None)` for one that
    does not parse (a non-numeric or out-of-range port, unbalanced IPv6
    brackets): one malformed roster entry must never abort an anchor
    pass or a relay lookup."""
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        return "", None
    if not hostname:
        return "", None
    if port is None:
        port = 443 if parts.scheme == "https" else 80 if parts.scheme == "http" else None
    return hostname.lower(), port


def reliable_node_fingerprints(node: LinkNode, roster: list[ReliableNode]) -> list[str]:
    """Which known peers are reliable nodes: a roster entry names a Link
    base URL, a completed hello records the peer's advertised HTTP
    address -- match host and port. Order follows the roster."""
    wanted = [hp for hp in (_url_host_port(entry.url) for entry in roster) if hp[0]]
    found: list[str] = []
    for fingerprint, peer in node.peers.items():
        addresses = peer.descriptor.payload.get("addresses") or []
        for address in addresses:
            if address.get("protocol") not in ("http", "https"):
                continue
            host_port = (str(address.get("address", "")).lower(), address.get("port"))
            if host_port in wanted and fingerprint not in found:
                found.append(fingerprint)
    found.sort(key=lambda fp: next(
        (i for i, entry in enumerate(roster) if _url_host_port(entry.url) in _peer_http_addresses(node, fp)), 999
    ))
    return found


def _peer_http_addresses(node: LinkNode, fingerprint: str) -> set[tuple[str, int | None]]:
    peer = node.peers.get(fingerprint)
    if peer is None:
        return set()
    return {
        (str(a.get("address", "")).lower(), a.get("port"))
        for a in (peer.descriptor.payload.get("addresses") or []) if a.get("protocol") in ("http", "https")
    }


# A descriptor is signed but its *contents* are the peer's claim: cap how
# many relays this node will ever try on a peer's say-so, and reject any
# entry that could not be a fingerprint (the wire bound for a real-time
# ID is 128 bytes).
MAX_ADVERTISED_LIVE_RELAYS = 8
_MAX_FINGERPRINT_BYTES = 128


def advertised_live_relays(node: LinkNode, fingerprint: str) -> list[str]:
    """The live relays `fingerprint` advertises standing by at (issue
    #270, `EndpointDescriptor.payload["live_relays"]`), from whichever of
    its completed peer record or peer-list candidate descriptor is newer
    -- an outgoing-only node's descriptor may only ever be learned
    secondhand. Bounded and validated: a malformed field reads as empty,
    never as an error, and at most `MAX_ADVERTISED_LIVE_RELAYS` entries
    are ever returned."""
    peer = node.peers.get(fingerprint)
    candidates = [d for d in (
        peer.descriptor if peer is not None else None, node.candidate_descriptors.get(fingerprint),
    ) if d is not None]
    if not candidates:
        return []
    descriptor = max(candidates, key=lambda d: str(d.payload.get("created_at", "")))
    relays = descriptor.payload.get("live_relays")
    if not isinstance(relays, list):
        return []
    valid: list[str] = []
    for entry in relays:
        if not isinstance(entry, str) or not entry or len(entry.encode("utf-8")) > _MAX_FINGERPRINT_BYTES:
            continue
        if entry not in valid:
            valid.append(entry)
        if len(valid) >= MAX_ADVERTISED_LIVE_RELAYS:
            break
    return valid


class AnchorState:
    """Issue #270: which reliable nodes this node is currently standing
    by at -- maintained by `run_reliable_anchor_connectors`, read by the
    hello provider to advertise `live_relays` (filtered to those with a
    session actually up right now)."""

    def __init__(self) -> None:
        self.anchored: set[str] = set()

    def live(self, registry: LinkRealtimeSessionRegistry) -> list[str]:
        return sorted(fp for fp in self.anchored if registry.get(fp) is not None)


class LiveDirectChat:
    """See module docstring. One per node, owned by `netbbs.__main__`
    alongside the `LiveChannelBridge`, which routes `direct_message`
    frames here via `owns_frame`/`handle_frame`."""

    def __init__(
        self,
        *,
        node_identity: NodeIdentity,
        link_node: LinkNode,
        lane: DatabaseLane,
        registry: LinkRealtimeSessionRegistry,
        on_frame: Callable[[LinkRealtimeSession, RealtimeFrame], Awaitable[None]],
        track_session: Callable[[LinkRealtimeSession], Awaitable[None]],
        relay_client: RealtimeRelayClient | None,
        deliver: Callable[[IncomingDirectMessage], Awaitable[bool]] | None,
        dial_timeout_seconds: float = DIRECT_CHAT_DEFAULT_DIAL_TIMEOUT_SECONDS,
    ) -> None:
        self._identity = node_identity
        self._node = link_node
        self._lane = lane
        self._registry = registry
        self._on_frame = on_frame
        self._track_session = track_session
        self._relay_client = relay_client
        self._deliver = deliver
        self._dial_timeout = dial_timeout_seconds

    # -- outbound ------------------------------------------------------------

    async def ensure_session(self, fingerprint: str) -> LinkRealtimeSession:
        """A live session with `fingerprint`, established if needed, in
        this order (design doc §8.10.3): the registry; a direct dial of
        every advertised real-time address; a single-hop rendezvous at
        each relay the target advertises standing by at (reusing a
        session or dialing that relay directly -- it is a full peer);
        a single-hop rendezvous at each of this node's own anchors; and
        finally a chained rendezvous, asking each own anchor to forward
        toward each of the target's advertised relays (issue #270).
        Raises `DirectChatUnreachable`."""
        if fingerprint == self._identity.fingerprint:
            raise DirectChatUnreachable(fingerprint, "self")
        # A session can outlive a trust change (§8.10.2's "checked again"
        # principle): a SysOp who blocks or quarantines a node must stop a
        # private message to it immediately, not at the next reconnect.
        # The dial/attach paths run this same gate inside the transport.
        if not await self._peer_allowed(fingerprint):
            raise DirectChatUnreachable(fingerprint, "policy")
        session = self._registry.get(fingerprint)
        if session is not None:
            return session
        for host, port in dialable_realtime_addresses_for_peer(self._node, fingerprint):
            try:
                session = await asyncio.wait_for(
                    dial_realtime_session(
                        host, port, self._identity, on_frame=self._on_frame, registry=self._registry,
                        lane=self._lane, enforce_trust_policy=True, expected_fingerprint=fingerprint,
                    ),
                    timeout=self._dial_timeout,
                )
            except RealtimeProtocolVersionError:
                # An authenticated version mismatch is an upgrade
                # requirement, not transient unreachability -- the caller
                # gets the explicit notice, never Decision 3's refusal.
                raise
            except Exception as exc:
                winner = self._registry.get(fingerprint)
                if winner is not None:
                    return winner  # a simultaneous inbound session won the tiebreak
                _logger.info("direct real-time dial of %s at %s:%d failed: %s", fingerprint[:12], host, port, exc)
                continue
            await self._track_session(session)
            return session
        if self._relay_client is None:
            raise DirectChatUnreachable(fingerprint, "no_relay_client")
        last_reason = "no_relay"
        target_relays = [fp for fp in advertised_live_relays(self._node, fingerprint) if fp != self._identity.fingerprint]
        own_anchors = await self._relay_sessions()
        # 1. Meet at a relay the target stands by at, reaching it ourselves.
        unreachable_relays: list[str] = []
        for relay_fingerprint in target_relays:
            relay_session = await self._reach_relay(relay_fingerprint)
            if relay_session is None:
                unreachable_relays.append(relay_fingerprint)
                continue
            try:
                return await self._relay_client.request_bridge(relay_session, fingerprint)
            except RelayRendezvousError as exc:
                last_reason = exc.reason
                _logger.info("relayed session with %s via its anchor %s failed: %s", fingerprint[:12], relay_fingerprint[:12], exc)
        # 2. Meet at one of our own anchors (the target may stand by there too).
        for relay_session in own_anchors:
            if relay_session.remote_fingerprint in target_relays:
                continue  # already tried above
            try:
                return await self._relay_client.request_bridge(relay_session, fingerprint)
            except RelayRendezvousError as exc:
                last_reason = exc.reason
                _logger.info("relayed session with %s via %s failed: %s", fingerprint[:12], relay_session.remote_fingerprint[:12], exc)
        # 3. Chain: ask our own anchor to forward toward a target relay we
        #    could not reach ourselves.
        for relay_fingerprint in unreachable_relays:
            for relay_session in own_anchors:
                if relay_session.remote_fingerprint == relay_fingerprint:
                    continue
                try:
                    return await self._relay_client.request_bridge(relay_session, fingerprint, via_relay=relay_fingerprint)
                except RelayRendezvousError as exc:
                    last_reason = exc.reason
                    _logger.info(
                        "chained session with %s via %s -> %s failed: %s",
                        fingerprint[:12], relay_session.remote_fingerprint[:12], relay_fingerprint[:12], exc,
                    )
        raise DirectChatUnreachable(fingerprint, last_reason)

    async def _peer_allowed(self, fingerprint: str) -> bool:
        return await self._lane.run(
            lambda db: decide_node_action(db, fingerprint, LinkPolicyAction.REALTIME).allowed
        )

    async def _reach_relay(self, fingerprint: str) -> LinkRealtimeSession | None:
        """A session with relay `fingerprint`: the registry's, or a fresh
        dial of its advertised real-time address (a relay is a full peer).
        `None` if this node cannot reach it -- the chained path's cue."""
        session = self._registry.get(fingerprint)
        if session is not None:
            return session
        for host, port in dialable_realtime_addresses_for_peer(self._node, fingerprint):
            try:
                session = await asyncio.wait_for(
                    dial_realtime_session(
                        host, port, self._identity, on_frame=self._on_frame, registry=self._registry,
                        lane=self._lane, enforce_trust_policy=True, expected_fingerprint=fingerprint,
                    ),
                    timeout=self._dial_timeout,
                )
            except Exception as exc:
                # A simultaneous inbound session from that relay can win
                # the duplicate-session tiebreak against this dial; the
                # admitted winner is exactly the session we wanted.
                winner = self._registry.get(fingerprint)
                if winner is not None:
                    return winner
                _logger.info("could not reach relay %s at %s:%d: %s", fingerprint[:12], host, port, exc)
                continue
            await self._track_session(session)
            return session
        return self._registry.get(fingerprint)

    async def connect_relay(self, fingerprint: str) -> LinkRealtimeSession | None:
        """The relay-server half's hook for reaching an upstream relay when
        forwarding (issue #270) -- the same recipe, exposed."""
        return await self._reach_relay(fingerprint)

    async def _relay_sessions(self) -> list[LinkRealtimeSession]:
        """Live sessions with reliable nodes (§16 issue #219 Decision 4:
        the same roster serves as the live-relay anchor), roster order --
        dialed on demand when this node holds none yet. A full peer runs
        no standing anchor unless participation is accepted, and even an
        anchored node may be between reconnects; a relay is a full peer,
        so reaching it is an ordinary direct dial."""
        roster = await self._lane.run(effective_reliable_nodes)
        sessions: list[LinkRealtimeSession] = []
        for fingerprint in reliable_node_fingerprints(self._node, roster):
            session = self._registry.get(fingerprint)
            if session is None:
                session = await self._dial_relay(fingerprint)
            if session is not None:
                sessions.append(session)
        return sessions

    async def _dial_relay(self, fingerprint: str) -> LinkRealtimeSession | None:
        for host, port in dialable_realtime_addresses_for_peer(self._node, fingerprint):
            try:
                session = await asyncio.wait_for(
                    dial_realtime_session(
                        host, port, self._identity, on_frame=self._on_frame, registry=self._registry,
                        lane=self._lane, enforce_trust_policy=True, expected_fingerprint=fingerprint,
                    ),
                    timeout=self._dial_timeout,
                )
            except Exception as exc:
                winner = self._registry.get(fingerprint)
                if winner is not None:
                    return winner
                _logger.info("could not reach relay %s at %s:%d: %s", fingerprint[:12], host, port, exc)
                continue
            await self._track_session(session)
            return session
        return self._registry.get(fingerprint)

    async def send_direct_message(
        self, fingerprint: str, *, to_user_id: str, from_user_id: str, from_display_label: str,
        body: str, created_at: str,
    ) -> None:
        """Deliver one private line to `to_user_id` on node `fingerprint`,
        live. Raises `DirectChatUnreachable` (no path) or
        `LinkTransportError` (the session dropped mid-send)."""
        session = await self.ensure_session(fingerprint)
        await session.send(build_direct_message_frame(
            to_user_id=to_user_id, from_user_id=from_user_id, from_display_label=from_display_label,
            body=body, created_at=created_at,
        ))

    # -- inbound -------------------------------------------------------------

    def owns_frame(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> bool:
        return frame.type == "direct_message"

    async def handle_frame(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        if self._deliver is None:
            return
        allowed = await self._lane.run(
            lambda db: decide_node_action(db, session.remote_fingerprint, LinkPolicyAction.REALTIME).allowed
        )
        if not allowed:
            return
        payload = frame.payload
        message = IncomingDirectMessage(
            from_node_fingerprint=session.remote_fingerprint,
            from_user_id=payload["from_user_id"], from_display_label=payload["from_display_label"],
            to_user_id=payload["to_user_id"], body=payload["body"], created_at=payload["created_at"],
        )
        try:
            await self._deliver(message)
        except Exception:
            _logger.exception("delivering a live direct message from %s failed", session.remote_fingerprint[:12])


async def run_reliable_anchor_connectors(
    *,
    node_identity: NodeIdentity,
    link_node: LinkNode,
    lane: DatabaseLane,
    registry: LinkRealtimeSessionRegistry,
    on_frame: Callable[[LinkRealtimeSession, RealtimeFrame], Awaitable[None]],
    track_session: Callable[[LinkRealtimeSession], Awaitable[None]],
    participation_accepted: Callable[[Database], bool],
    start_connector: Callable[..., object],
    interval_seconds: float = 60.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop_event: asyncio.Event | None = None,
    state: AnchorState | None = None,
) -> None:
    """Keep a participating node standing by at every reliable node it
    knows (design doc §8.10.3): a live session to a relay is what makes
    an outgoing-only node *reachable* for a rendezvous (nobody can dial
    it first), and what lets a full peer *reach* an outgoing-only node
    through the same relay without a dial per message. One
    `LinkRealtimeConnector` per reliable-node peer with an advertised
    real-time address, cycling through every advertised address across
    attempts, started once and left to its own reconnect loop; re-checked
    every `interval_seconds` as hellos complete and the roster refreshes.
    Only while reliable-node participation is accepted -- declining it
    means never contacting project infrastructure for this either."""
    # fingerprint -> (connector, (host, port)) -- reconciled every pass
    # against what participation, the roster, and the peer's current
    # advertised address say *now*: a declined participation, a node
    # dropped from the roster, or a changed address stops (and, for the
    # last, restarts) that connector, never only future ones.
    started: dict[str, tuple[object, tuple[str, int]]] = {}
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                desired: dict[str, tuple[str, int]] = {}
                if await lane.run(participation_accepted):
                    roster = await lane.run(effective_reliable_nodes)
                    for fingerprint in reliable_node_fingerprints(link_node, roster):
                        addresses = dialable_realtime_addresses_for_peer(link_node, fingerprint)
                        if addresses:
                            desired[fingerprint] = tuple(addresses)
                for fingerprint, (connector, address) in list(started.items()):
                    if desired.get(fingerprint) != address:
                        await connector.stop()  # type: ignore[attr-defined]
                        del started[fingerprint]
                        _logger.info("no longer standing by at %s for live relay", fingerprint[:12])
                if state is not None:
                    state.anchored = set(desired)
                for fingerprint, addresses in desired.items():
                    if fingerprint in started:
                        continue
                    host, port = addresses[0]
                    connector = start_connector(
                        host=host, port=port, identity=node_identity, on_frame=on_frame, registry=registry,
                        lane=lane, enforce_trust_policy=True, expected_fingerprint=fingerprint,
                        track_session=track_session, addresses=list(addresses),
                    )
                    started[fingerprint] = (connector, addresses)
                    _logger.info(
                        "standing by at reliable node %s (%s:%d, %d address(es)) for live relay",
                        fingerprint[:12], host, port, len(addresses),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("reliable-node anchor pass failed")
            await sleep(interval_seconds)
    finally:
        if state is not None:
            state.anchored = set()
        for connector, _address in started.values():
            try:
                await connector.stop()  # type: ignore[attr-defined]
            except Exception:
                pass


__all__ = [
    "AnchorState",
    "DirectChatUnreachable",
    "advertised_live_relays",
    "IncomingDirectMessage",
    "LiveDirectChat",
    "reliable_node_fingerprints",
    "run_reliable_anchor_connectors",
]

