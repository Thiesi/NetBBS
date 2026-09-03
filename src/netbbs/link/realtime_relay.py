"""
Live (real-time) relay for NetBBS Link sessions -- issue #168, design doc
§8.10.3 and §16 "Issue #168" Decisions 1-4.

Two nodes that cannot dial each other (both outgoing-only, §8.4) get a
live Noise XX session anyway by meeting at a third node that both *can*
reach: the relay. The relay is a **raw-socket proxy below the Noise
layer** (Decision 1) -- it pumps opaque bytes between two TCP legs and
never holds any key material, so the two parties run the exact same
mutual authentication they would run if adjacent
(`netbbs.link.transport.establish_noise_xx_*`, unchanged), and the
relay is as invisible to Noise as any router hop. All new code here is
connection *setup*; nothing touches the confidentiality-critical path.

Rendezvous (Decision 4) rides over each party's ordinary, already-
authenticated `LinkRealtimeSession` to the relay:

    A -> R  relay_request  {target: B, requester: A}
    R -> A  relay_waiting  {target: B}            (B hasn't agreed yet)
    R -> B  relay_request  {target: B, requester: A}  (the invitation:
                                                  target == B's own fp)
    B -> R  relay_request  {target: A, requester: B}  (B agrees)  or
    B -> R  relay_reject   {target: A, reason: declined}
    R -> A, R -> B  relay_ready {bridge_id, peer, role, attach_token,
                                 attach_address, attach_port}
    A, B  each open a fresh TCP connection to attach_address:port, send
          one BRIDGE_ATTACH preamble record carrying their own token,
          then run Noise XX with each other (A initiator, B responder)
          through the spliced sockets, verifying the peer fingerprint
          `relay_ready` named.

The relay bridges only pairs that are *both* currently connected live to
it: a party's authenticated session with the relay is its consent to be
reachable through it, the reliable-nodes roster (§16 issue #219 Decision
4) being the v1 way an outgoing-only node knows which relays to stay
connected to. The relay makes no trust decision about the pair
(Decision 5 of #219 / Decision 3 here): each party applies its own
Phase-4 `REALTIME` policy to the *other party* before agreeing and again
after the handshake, exactly as for a direct session.

Bounds (Decision 2), every one remotely influenced and every breach
visible as an explicit close/reject rather than degradation:

- `max_concurrent_pairs` -- a relay holds two live legs per bridge for
  the whole conversation; `at_capacity` rejects beyond this.
- `max_pending` + `rendezvous_timeout_seconds` -- a party that shows up
  first waits, bounded in count (`pending_full`) and time (`timeout`,
  reported back, never a silently forgotten request).
- `max_bytes_per_second` per direction per bridge -- the relay never
  parses frames, so frame-rate bounds are meaningless here; exceeding
  the byte rate closes the bridge (the "drop rather than degrade"
  precedent `LinkRealtimeSession.send()` sets for a full queue).
- `idle_timeout_seconds` -- a dumb "zero bytes either direction" timer;
  the endpoints' own (relay-invisible) ping/pong keeps a live bridge
  from ever tripping it.
- One leg closing tears down the other: a bridge with one live end is
  not a bridge, and the endpoints' existing per-session bounds compose
  across the two legs for free.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from netbbs.link.protocol import (
    RealtimeFrame,
    build_relay_ready_frame,
    build_relay_reject_frame,
    build_relay_request_frame,
    build_relay_waiting_frame,
)
from netbbs.link.transport import (
    LinkRealtimeSession,
    LinkRealtimeSessionRegistry,
    LinkTransportError,
)

_logger = logging.getLogger(__name__)

RELAY_FRAME_TYPES = frozenset({"relay_request", "relay_waiting", "relay_ready", "relay_reject"})

LIVE_RELAY_DEFAULT_MAX_CONCURRENT_PAIRS = 8
LIVE_RELAY_DEFAULT_MAX_PENDING_RENDEZVOUS = 32
LIVE_RELAY_DEFAULT_RENDEZVOUS_TIMEOUT_SECONDS = 30.0
LIVE_RELAY_DEFAULT_IDLE_TIMEOUT_SECONDS = 120.0
LIVE_RELAY_DEFAULT_MAX_BYTES_PER_SECOND = 64 * 1024
_PUMP_CHUNK_BYTES = 16 * 1024


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


@dataclass
class _Rendezvous:
    bridge_id: str
    requester: str
    target: str
    created_at: float
    # The requester's own relay_request message_id, echoed on every
    # answer so a late answer never lands on a fresh attempt.
    request_id: str | None = None
    target_agreed: bool = False
    tokens: dict[str, str] = field(default_factory=dict)  # fingerprint -> attach token
    legs: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = field(default_factory=dict)
    timer: asyncio.Task | None = None


@dataclass
class _Bridge:
    bridge_id: str
    pair: tuple[str, str]
    legs: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]]
    last_activity: float
    tasks: list[asyncio.Task] = field(default_factory=list)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    close_reason: str | None = None


class RealtimeRelay:
    """The relay-side half (module docstring): rendezvous bookkeeping,
    attach matching, and the byte pumps. One instance per node, owned by
    `netbbs.__main__` alongside the real-time listener, which hands it
    every bridge-attach connection via `attach()` and every relay frame
    that names *another* node as target via `handle_frame()`."""

    def __init__(
        self,
        *,
        own_fingerprint: str,
        registry: LinkRealtimeSessionRegistry,
        serving_enabled: bool,
        attach_address: str | None,
        attach_port: int | None,
        max_concurrent_pairs: int = LIVE_RELAY_DEFAULT_MAX_CONCURRENT_PAIRS,
        max_pending: int = LIVE_RELAY_DEFAULT_MAX_PENDING_RENDEZVOUS,
        rendezvous_timeout_seconds: float = LIVE_RELAY_DEFAULT_RENDEZVOUS_TIMEOUT_SECONDS,
        idle_timeout_seconds: float = LIVE_RELAY_DEFAULT_IDLE_TIMEOUT_SECONDS,
        max_bytes_per_second: int = LIVE_RELAY_DEFAULT_MAX_BYTES_PER_SECOND,
        decide_peer_allowed: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._own = own_fingerprint
        self._registry = registry
        # §8.10.2's "checked again" principle for the relay half: a
        # requester's session can outlive a SysOp block; every request is
        # re-decided against local REALTIME policy before it costs anything.
        self._decide_peer_allowed = decide_peer_allowed
        # Serving needs an address the parties can attach to -- an
        # outgoing-only node has none to give, so it never serves.
        self._serving = serving_enabled and attach_address is not None and attach_port is not None
        self._attach_address = attach_address
        self._attach_port = attach_port
        self._max_pairs = max_concurrent_pairs
        self._max_pending = max_pending
        self._rendezvous_timeout = rendezvous_timeout_seconds
        self._idle_timeout = idle_timeout_seconds
        self._max_bytes_per_second = max_bytes_per_second
        self._pending: dict[tuple[str, str], _Rendezvous] = {}
        self._by_token: dict[str, _Rendezvous] = {}
        self._bridges: dict[str, _Bridge] = {}

    # -- introspection (SysOp Link status screen) --------------------------

    @property
    def serving(self) -> bool:
        return self._serving

    @property
    def active_pairs(self) -> int:
        return len(self._bridges)

    @property
    def pending_rendezvous(self) -> int:
        return len(self._pending)

    @property
    def max_concurrent_pairs(self) -> int:
        return self._max_pairs

    # -- rendezvous ----------------------------------------------------------

    def owns_frame(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> bool:
        """Whether `frame` (already validated) is addressed to this node
        *as a relay*: a request whose requester is the sender itself
        (rather than an invitation forwarded by a relay, where the target
        is this node), or a reject answering one of our invitations."""
        if frame.type == "relay_request":
            return frame.payload["requester_fingerprint"] == session.remote_fingerprint
        if frame.type == "relay_reject":
            if frame.payload["origin"] != "party":
                return False
            pending = self._pending.get(_pair_key(session.remote_fingerprint, frame.payload["target_fingerprint"]))
            return pending is not None and pending.target == session.remote_fingerprint
        return False

    async def handle_frame(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        if frame.type == "relay_request":
            await self._handle_request(session, frame.payload["target_fingerprint"], request_id=frame.message_id)
        elif frame.type == "relay_reject":
            await self._handle_party_reject(session, frame.payload["target_fingerprint"])

    async def _handle_request(
        self, session: LinkRealtimeSession, target: str, *, request_id: str | None = None,
    ) -> None:
        requester = session.remote_fingerprint
        if not self._serving:
            await self._reject(session, target, "not_serving", request_id=request_id)
            return
        if self._decide_peer_allowed is not None and not await self._decide_peer_allowed(requester):
            await self._reject(session, target, "policy_refused", request_id=request_id)
            return
        if target == self._own or target == requester:
            await self._reject(session, target, "invalid_target", request_id=request_id)
            return
        key = _pair_key(requester, target)
        pending = self._pending.get(key)
        if pending is not None:
            if pending.target == requester and not pending.target_agreed:
                # The invited party agreeing: both sides are now present.
                pending.target_agreed = True
                await self._issue_ready(pending)
            else:
                # A repeat from the original requester (or a duplicate
                # agreement): restate the wait, never a second rendezvous.
                await self._send(session, build_relay_waiting_frame(
                    target_fingerprint=target, request_id=pending.request_id,
                ))
            return
        target_session = self._registry.get(target)
        if target_session is None:
            await self._reject(session, target, "target_unreachable", request_id=request_id)
            return
        if len(self._bridges) >= self._max_pairs:
            await self._reject(session, target, "at_capacity", request_id=request_id)
            return
        if len(self._pending) >= self._max_pending:
            await self._reject(session, target, "pending_full", request_id=request_id)
            return
        loop = asyncio.get_running_loop()
        rendezvous = _Rendezvous(
            bridge_id=secrets.token_hex(16), requester=requester, target=target, created_at=loop.time(),
            request_id=request_id,
        )
        self._pending[key] = rendezvous
        rendezvous.timer = loop.create_task(self._expire(rendezvous))
        await self._send(session, build_relay_waiting_frame(target_fingerprint=target, request_id=request_id))
        try:
            await target_session.send(
                build_relay_request_frame(target_fingerprint=target, requester_fingerprint=requester)
            )
        except LinkTransportError:
            await self._fail_rendezvous(rendezvous, reason="target_unreachable")

    async def _handle_party_reject(self, session: LinkRealtimeSession, other: str) -> None:
        pending = self._pending.get(_pair_key(session.remote_fingerprint, other))
        if pending is None or pending.target != session.remote_fingerprint:
            return
        await self._fail_rendezvous(pending, reason="declined")

    async def _issue_ready(self, rendezvous: _Rendezvous) -> None:
        assert self._attach_address is not None and self._attach_port is not None
        for fingerprint in (rendezvous.requester, rendezvous.target):
            token = secrets.token_hex(16)
            rendezvous.tokens[fingerprint] = token
            self._by_token[token] = rendezvous
        for fingerprint, role in ((rendezvous.requester, "initiator"), (rendezvous.target, "responder")):
            session = self._registry.get(fingerprint)
            peer = rendezvous.target if fingerprint == rendezvous.requester else rendezvous.requester
            frame = build_relay_ready_frame(
                bridge_id=rendezvous.bridge_id, peer_fingerprint=peer, role=role,
                attach_token=rendezvous.tokens[fingerprint], attach_address=self._attach_address,
                attach_port=self._attach_port,
                request_id=rendezvous.request_id if fingerprint == rendezvous.requester else None,
            )
            if session is None:
                await self._fail_rendezvous(rendezvous, reason="target_unreachable")
                return
            try:
                await session.send(frame)
            except LinkTransportError:
                await self._fail_rendezvous(rendezvous, reason="target_unreachable")
                return
        # The attach legs are now expected within the same timeout window
        # the rendezvous itself had -- the timer keeps running.

    async def _expire(self, rendezvous: _Rendezvous) -> None:
        try:
            await asyncio.sleep(self._rendezvous_timeout)
        except asyncio.CancelledError:
            raise
        await self._fail_rendezvous(rendezvous, reason="timeout", from_timer=True)

    async def _fail_rendezvous(self, rendezvous: _Rendezvous, *, reason: str, from_timer: bool = False) -> None:
        key = _pair_key(rendezvous.requester, rendezvous.target)
        if self._pending.get(key) is not rendezvous:
            return
        del self._pending[key]
        for token in rendezvous.tokens.values():
            self._by_token.pop(token, None)
        if rendezvous.timer is not None and not from_timer:
            rendezvous.timer.cancel()
        for _, (_reader, writer) in rendezvous.legs.items():
            _close_writer(writer)
        # Fail clearly to whoever is still waiting: the requester always,
        # the target too once it has agreed.
        for fingerprint, other in ((rendezvous.requester, rendezvous.target), (rendezvous.target, rendezvous.requester)):
            if fingerprint == rendezvous.target and not rendezvous.target_agreed and reason != "timeout":
                continue
            session = self._registry.get(fingerprint)
            if session is None:
                continue
            await self._reject(
                session, other, reason,
                request_id=rendezvous.request_id if fingerprint == rendezvous.requester else None,
            )

    async def _reject(
        self, session: LinkRealtimeSession, target: str, reason: str, *, request_id: str | None = None,
    ) -> None:
        await self._send(session, build_relay_reject_frame(
            target_fingerprint=target, reason=reason, origin="relay", request_id=request_id,
        ))

    async def _send(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        try:
            await session.send(frame)
        except LinkTransportError:
            pass

    # -- attach + pump -------------------------------------------------------

    async def attach(self, token: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """The real-time listener's hand-off for a connection that opened
        with a bridge-attach preamble. Returns whether the connection was
        accepted (the caller closes it otherwise). An accepted connection
        is owned here from now on; this coroutine returns as soon as the
        leg is recorded, never blocking the listener's accept task on the
        conversation."""
        rendezvous = self._by_token.get(token)
        if rendezvous is None:
            return False
        fingerprint = next(fp for fp, t in rendezvous.tokens.items() if t == token)
        if fingerprint in rendezvous.legs:
            return False  # a second attach for the same side is not a retry we honour
        rendezvous.legs[fingerprint] = (reader, writer)
        if len(rendezvous.legs) == 2:
            await self._start_bridge(rendezvous)
        return True

    async def _start_bridge(self, rendezvous: _Rendezvous) -> None:
        # The pair cap is re-checked here, not only when the rendezvous was
        # created: up to max_pending rendezvous can be waiting at once, and
        # every one of them passed the request-time check against a count
        # that has since grown. Exceeding it here is an explicit reject,
        # never a silently oversized relay.
        if len(self._bridges) >= self._max_pairs:
            await self._fail_rendezvous(rendezvous, reason="at_capacity")
            return
        key = _pair_key(rendezvous.requester, rendezvous.target)
        self._pending.pop(key, None)
        for token in rendezvous.tokens.values():
            self._by_token.pop(token, None)
        if rendezvous.timer is not None:
            rendezvous.timer.cancel()
        loop = asyncio.get_running_loop()
        bridge = _Bridge(
            bridge_id=rendezvous.bridge_id, pair=key, legs=dict(rendezvous.legs), last_activity=loop.time(),
        )
        self._bridges[bridge.bridge_id] = bridge
        (fp_a, (reader_a, writer_a)), (fp_b, (reader_b, writer_b)) = bridge.legs.items()
        bridge.tasks = [
            loop.create_task(self._pump(bridge, reader_a, writer_b, label=f"{fp_a[:8]}->{fp_b[:8]}")),
            loop.create_task(self._pump(bridge, reader_b, writer_a, label=f"{fp_b[:8]}->{fp_a[:8]}")),
            loop.create_task(self._idle_watch(bridge)),
        ]
        _logger.info("live relay bridge %s opened between %s and %s", bridge.bridge_id[:8], fp_a[:12], fp_b[:12])

    async def _pump(
        self, bridge: _Bridge, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, label: str,
    ) -> None:
        loop = asyncio.get_running_loop()
        window_start = loop.time()
        window_bytes = 0
        try:
            while True:
                data = await reader.read(_PUMP_CHUNK_BYTES)
                if not data:
                    await self._close_bridge(bridge, reason="leg_closed")
                    return
                now = loop.time()
                bridge.last_activity = now
                if now - window_start >= 1.0:
                    window_start = now
                    window_bytes = 0
                window_bytes += len(data)
                if window_bytes > self._max_bytes_per_second:
                    await self._close_bridge(bridge, reason="byte_rate_exceeded")
                    return
                writer.write(data)
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError):
            await self._close_bridge(bridge, reason="leg_error")
        except Exception as exc:  # never let one bridge's surprise kill the relay
            _logger.warning("live relay pump %s failed: %s", label, exc)
            await self._close_bridge(bridge, reason="leg_error")

    async def _idle_watch(self, bridge: _Bridge) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                remaining = self._idle_timeout - (loop.time() - bridge.last_activity)
                if remaining <= 0:
                    await self._close_bridge(bridge, reason="idle_timeout")
                    return
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            raise

    async def _close_bridge(self, bridge: _Bridge, *, reason: str) -> None:
        if bridge.closed.is_set():
            return
        bridge.closed.set()
        bridge.close_reason = reason
        self._bridges.pop(bridge.bridge_id, None)
        current = asyncio.current_task()
        for task in bridge.tasks:
            if task is not current:
                task.cancel()
        for _reader, writer in bridge.legs.values():
            _close_writer(writer)
        _logger.info("live relay bridge %s closed: %s", bridge.bridge_id[:8], reason)

    async def close(self) -> None:
        for rendezvous in list(self._pending.values()):
            await self._fail_rendezvous(rendezvous, reason="attach_failed")
        bridges = list(self._bridges.values())
        tasks = [task for bridge in bridges for task in bridge.tasks]
        for bridge in bridges:
            await self._close_bridge(bridge, reason="local_shutdown")
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _close_writer(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:
        pass


# ---------------------------------------------------------------- party side


class RelayRendezvousError(LinkTransportError):
    """A rendezvous through a relay failed for a stated reason -- the
    relay's reject code, a local timeout, or an attach/handshake failure.
    Callers only ever surface Decision 3's deliberately reason-free
    "can't be reached for live chat right now" to a caller; the reason is
    for logs and tests."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"relay rendezvous failed: {reason}{(' -- ' + detail) if detail else ''}")
        self.reason = reason


class RealtimeRelayClient:
    """The party-side half (module docstring): asks a relay for a bridge
    to a target, answers a relay's invitation, and turns `relay_ready`
    into a fresh, fully authenticated `LinkRealtimeSession` with the
    counterpart that the registry then holds exactly like a direct one.

    Every relay frame this node honours is *correlated*: a `relay_ready`
    or `relay_reject` counts only if it comes from the relay this node
    asked (for a target it asked about) or the relay whose invitation it
    accepted (for that inviter), within the rendezvous timeout. Anything
    else from any peer is ignored -- an authenticated peer must never be
    able to make this node open outbound connections to an address of
    its choosing, nor cancel a rendezvous it is not part of. At most one
    attach is ever in flight per counterpart.

    `establish_session` is injected: it opens the attach connection,
    runs the Noise handshake in the given role against the expected
    peer, applies local trust policy, and admits the session -- the
    transport-layer recipe lives in `netbbs.link.transport.
    attach_relayed_session`; keeping it a parameter is what lets tests
    exercise this bookkeeping without sockets and lets `netbbs.link`
    stay free of a circular import.
    """

    def __init__(
        self,
        *,
        own_fingerprint: str,
        registry: LinkRealtimeSessionRegistry,
        establish_session: Callable[..., Awaitable[LinkRealtimeSession]],
        decide_peer_allowed: Callable[[str], Awaitable[bool]],
        on_session: Callable[[LinkRealtimeSession], Awaitable[None]] | None = None,
        rendezvous_timeout_seconds: float = LIVE_RELAY_DEFAULT_RENDEZVOUS_TIMEOUT_SECONDS,
        allowed_attach_addresses: Callable[[str], list[tuple[str, int]]] | None = None,
    ) -> None:
        self._own = own_fingerprint
        self._registry = registry
        self._establish = establish_session
        self._decide_peer_allowed = decide_peer_allowed
        self._on_session = on_session
        self._timeout = rendezvous_timeout_seconds
        # A relay's `relay_ready` names where to attach. Correlation only
        # proves *which* authenticated peer said so; this pins the address
        # to that relay's own advertised real-time addresses, so a
        # malicious relay cannot make this node open a connection (and
        # send the plaintext preamble) to an arbitrary internal or
        # external service. `None` (tests only) skips the pin.
        self._allowed_attach_addresses = allowed_attach_addresses
        # target fingerprint -> (relay fingerprint asked, future resolved
        # with the established session or failed with RelayRendezvousError,
        # the relay_request message_id this attempt used)
        self._waiting: dict[str, tuple[str, asyncio.Future, str]] = {}
        # inviter fingerprint -> (relay fingerprint the invitation came
        # over, deadline) for invitations this node agreed to and is now
        # expecting a relay_ready for.
        self._accepted: dict[str, tuple[str, float]] = {}
        # counterpart fingerprint -> the one attach task in flight for it.
        self._attaching: dict[str, asyncio.Task] = {}

    async def request_bridge(self, relay: LinkRealtimeSession, target: str) -> LinkRealtimeSession:
        """Ask `relay` to bridge this node to `target`; returns the
        authenticated session with `target` or raises
        `RelayRendezvousError`. Concurrent callers for the same target
        share one request; each waits under its own timeout, and a
        timeout fails the shared future with a typed error so every
        waiter sees the same `RelayRendezvousError`, never a bare
        cancellation."""
        loop = asyncio.get_running_loop()
        existing = self._waiting.get(target)
        if existing is None:
            future: asyncio.Future = loop.create_future()
            request = build_relay_request_frame(target_fingerprint=target, requester_fingerprint=self._own)
            self._waiting[target] = (relay.remote_fingerprint, future, request.message_id)
            try:
                await relay.send(request)
            except LinkTransportError as exc:
                self._settle(target, RelayRendezvousError("relay_unreachable", str(exc)))
                raise RelayRendezvousError("relay_unreachable", str(exc)) from exc
        else:
            _, future, _ = existing
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
        except TimeoutError as exc:
            self._settle(target, RelayRendezvousError("timeout"))
            raise RelayRendezvousError("timeout") from exc

    def _settle(self, target: str, error: BaseException | None = None, result=None) -> None:
        entry = self._waiting.pop(target, None)
        if entry is None:
            return
        _, future, _ = entry
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
            # The settling caller raises its own copy; mark the shared
            # future's exception retrieved so the last waiter leaving does
            # not leave asyncio an "exception was never retrieved" report.
            future.exception()
        else:
            future.set_result(result)

    def owns_frame(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> bool:
        sender = session.remote_fingerprint
        payload = frame.payload
        if frame.type == "relay_request":
            return payload["target_fingerprint"] == self._own
        if frame.type == "relay_waiting":
            return self._answers_current_wait(sender, payload["target_fingerprint"], payload.get("request_id"))
        if frame.type == "relay_reject":
            if payload["origin"] != "relay":
                return False
            return self._answers_current_wait(sender, payload["target_fingerprint"], payload.get("request_id"))
        if frame.type == "relay_ready":
            peer = payload["peer_fingerprint"]
            if self._answers_current_wait(sender, peer, payload.get("request_id")):
                return True
            accepted = self._accepted.get(peer)
            return accepted is not None and accepted[0] == sender
        return False

    def _answers_current_wait(self, sender: str, target: str, request_id: str | None) -> bool:
        """Whether a relay's answer belongs to the request this node is
        *currently* waiting on for `target`: from the relay asked, and --
        when the relay echoes one -- for this attempt's own request id, so
        a late answer to an earlier, timed-out attempt is ignored."""
        entry = self._waiting.get(target)
        if entry is None or entry[0] != sender:
            return False
        return request_id is None or request_id == entry[2]

    async def handle_frame(self, session: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        self._expire_accepted()
        if frame.type == "relay_request":
            await self._handle_invitation(session, frame.payload["requester_fingerprint"])
        elif frame.type == "relay_waiting":
            pass  # informational; the request's own timeout bounds the wait
        elif frame.type == "relay_reject":
            self._settle(frame.payload["target_fingerprint"], RelayRendezvousError(frame.payload["reason"]))
        elif frame.type == "relay_ready":
            # Defensive re-check: handle_frame must be as strict as
            # owns_frame even if a caller routes a frame here directly.
            if self.owns_frame(session, frame):
                self._spawn_attach(frame.payload)

    def _expire_accepted(self) -> None:
        now = asyncio.get_running_loop().time()
        for peer, (_relay, deadline) in list(self._accepted.items()):
            if now > deadline:
                del self._accepted[peer]

    async def _handle_invitation(self, relay: LinkRealtimeSession, requester: str) -> None:
        if requester == self._own or self._registry.get(requester) is not None:
            await self._reply(relay, build_relay_reject_frame(
                target_fingerprint=requester, reason="declined", origin="party",
            ))
            return
        if not await self._decide_peer_allowed(requester):
            await self._reply(relay, build_relay_reject_frame(
                target_fingerprint=requester, reason="declined", origin="party",
            ))
            return
        self._accepted[requester] = (relay.remote_fingerprint, asyncio.get_running_loop().time() + self._timeout)
        await self._reply(relay, build_relay_request_frame(target_fingerprint=requester, requester_fingerprint=self._own))

    async def _reply(self, relay: LinkRealtimeSession, frame: RealtimeFrame) -> None:
        try:
            await relay.send(frame)
        except LinkTransportError:
            pass

    def _spawn_attach(self, payload: dict) -> None:
        peer = payload["peer_fingerprint"]
        if peer in self._attaching:
            return  # one attach per counterpart; a duplicate ready is not a retry
        relay = self._relay_for(peer)
        if relay is not None and not self._attach_address_allowed(relay, payload):
            _logger.warning(
                "relay %s named an attach address it does not advertise (%s:%s); refusing",
                relay[:12], payload["attach_address"], payload["attach_port"],
            )
            self._accepted.pop(peer, None)
            self._settle(peer, RelayRendezvousError("attach_failed", "attach address not advertised by the relay"))
            return
        self._accepted.pop(peer, None)
        task = asyncio.get_running_loop().create_task(self._attach(payload))
        self._attaching[peer] = task
        task.add_done_callback(lambda _t, peer=peer: self._attaching.pop(peer, None))

    def _relay_for(self, peer: str) -> str | None:
        entry = self._waiting.get(peer)
        if entry is not None:
            return entry[0]
        accepted = self._accepted.get(peer)
        return accepted[0] if accepted is not None else None

    def _attach_address_allowed(self, relay: str, payload: dict) -> bool:
        if self._allowed_attach_addresses is None:
            return True
        wanted = (str(payload["attach_address"]).lower(), int(payload["attach_port"]))
        return any((str(host).lower(), int(port)) == wanted for host, port in self._allowed_attach_addresses(relay))

    async def _attach(self, payload: dict) -> None:
        peer = payload["peer_fingerprint"]
        try:
            session = await asyncio.wait_for(
                self._establish(
                    host=payload["attach_address"], port=payload["attach_port"],
                    attach_token=payload["attach_token"], role=payload["role"], expected_fingerprint=peer,
                ),
                timeout=self._timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = str(exc)
            _logger.info("relayed session with %s could not be established: %s", peer[:12], detail)
            self._settle(peer, RelayRendezvousError("attach_failed", detail))
            return
        if self._on_session is not None:
            await self._on_session(session)
        self._settle(peer, result=session)

    async def close(self) -> None:
        for target in list(self._waiting):
            self._settle(target, RelayRendezvousError("local_shutdown"))
        tasks = list(self._attaching.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def frame_is_relay_traffic(frame: RealtimeFrame) -> bool:
    return frame.type in RELAY_FRAME_TYPES


__all__ = [
    "RELAY_FRAME_TYPES",
    "RealtimeRelay",
    "RealtimeRelayClient",
    "RelayRendezvousError",
    "frame_is_relay_traffic",
]

