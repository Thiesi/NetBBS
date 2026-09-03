"""
Tests for `netbbs.link.realtime_relay` + `netbbs.link.realtime_direct`
(issue #168, design doc §8.10.3): three real nodes on loopback -- a
relay `R` that serves live relay, and two parties `A` and `B` that hold
ordinary Noise sessions with `R` but cannot dial each other -- proving
the rendezvous, the raw-proxy bridge, the end-to-end Noise handshake
*through* the relay, direct-message delivery, and every bound the design
locks in. Nothing is mocked below the socket.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.chat.hub import ChatHub
from netbbs.chat.presence import PresenceRegistry
from netbbs.link.enforcement import LinkPolicyAction, decide_node_action, ensure_node_subject
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import (
    LinkNode,
    LinkProtocolError,
    RealtimeFrame,
    build_direct_message_frame,
    build_relay_ready_frame,
    build_relay_reject_frame,
    build_relay_request_frame,
    validate_realtime_frame_payload,
)
from netbbs.link.realtime_channels import LiveChannelBridge
from netbbs.link.realtime_direct import (
    DirectChatUnreachable,
    IncomingDirectMessage,
    LiveDirectChat,
    reliable_node_fingerprints,
)
from netbbs.link.realtime_relay import RealtimeRelay, RealtimeRelayClient, RelayRendezvousError
from netbbs.link.reliable_nodes import ReliableNode
from netbbs.link.transport import (
    LINK_REALTIME_PROTOCOL_TAG,
    LinkRealtimeServer,
    LinkRealtimeSessionRegistry,
    attach_relayed_session,
    dial_realtime_session,
    decode_bridge_attach_record,
    encode_bridge_attach_record,
)
from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, set_trust_override
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso


def _establish_trust(db: Database, fingerprint: str) -> None:
    ensure_node_subject(db, fingerprint)
    subject = TrustSubject.node(fingerprint)
    for dimension in (TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR):
        set_trust_override(
            db, subject, dimension, TrustState.ESTABLISHED,
            reason="pre-established for test", now_iso="2026-09-03T12:00:00+00:00",
        )


class _Node:
    """One node: db/lane, identity, registry, bridge, and -- wired exactly
    the way `netbbs.__main__` wires them -- a relay server half, a relay
    client half, and the direct-message layer."""

    def __init__(self, tmp_path, name: str, *, relay_kwargs: dict | None = None) -> None:
        self.name = name
        self.db = Database(tmp_path / f"{name}.db")
        self.lane = DatabaseLane(self.db.path)
        self.identity = bootstrap_node_identity(name)
        self.link_node = LinkNode(identity=self.identity)
        self.hub = ChatHub()
        self.presence = PresenceRegistry()
        self.registry = LinkRealtimeSessionRegistry(own_fingerprint=self.identity.fingerprint)
        self.bridge = LiveChannelBridge(hub=self.hub, lane=self.lane, presence=self.presence, registry=self.registry)
        self.server: LinkRealtimeServer | None = None
        self.relay: RealtimeRelay | None = None
        self.delivered: list[IncomingDirectMessage] = []
        self._relay_kwargs = relay_kwargs or {}

    async def start_server(self, *, serving: bool) -> None:
        # Bind first so the relay can advertise the real port.
        self.server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=self.identity, registry=self.registry,
            on_frame=self.bridge.on_frame, lane=self.lane, enforce_trust_policy=True,
            bridge_attach=self._attach,
        )
        await self.server.start()
        self.relay = RealtimeRelay(
            own_fingerprint=self.identity.fingerprint, registry=self.registry, serving_enabled=serving,
            attach_address="127.0.0.1", attach_port=self.server.port, **self._relay_kwargs,
        )
        self.bridge.register_frame_handler(self.relay.owns_frame, self.relay.handle_frame)
        self._wire_client()

    async def _attach(self, token, reader, writer):
        assert self.relay is not None
        return await self.relay.attach(token, reader, writer)

    def wire_party(self) -> None:
        """A party that serves nothing (outgoing-only in spirit: no
        listener, no relay) but can ask relays and answer invitations."""
        self._wire_client()

    def _wire_client(self) -> None:
        async def _allowed(fingerprint: str) -> bool:
            return await self.lane.run(
                lambda db: decide_node_action(db, fingerprint, LinkPolicyAction.REALTIME).allowed
            )

        async def _establish(**kw):
            return await attach_relayed_session(
                kw["host"], kw["port"], self.identity, attach_token=kw["attach_token"], role=kw["role"],
                expected_fingerprint=kw["expected_fingerprint"], on_frame=self.bridge.on_frame,
                registry=self.registry, lane=self.lane, enforce_trust_policy=True,
            )

        self.relay_client = RealtimeRelayClient(
            own_fingerprint=self.identity.fingerprint, registry=self.registry, establish_session=_establish,
            decide_peer_allowed=_allowed, on_session=self.bridge.track_session, rendezvous_timeout_seconds=3.0,
        )

        async def _deliver(message: IncomingDirectMessage) -> bool:
            self.delivered.append(message)
            return True

        self.direct = LiveDirectChat(
            node_identity=self.identity, link_node=self.link_node, lane=self.lane, registry=self.registry,
            on_frame=self.bridge.on_frame, track_session=self.bridge.track_session,
            relay_client=self.relay_client, deliver=_deliver, dial_timeout_seconds=2.0,
        )
        self.bridge.register_frame_handler(self.relay_client.owns_frame, self.relay_client.handle_frame)
        self.bridge.register_frame_handler(self.direct.owns_frame, self.direct.handle_frame)

    def trust(self, *others: "_Node") -> None:
        for other in others:
            _establish_trust(self.db, other.identity.fingerprint)

    def know_relay(self, relay: "_Node", *, http_port: int = 7862) -> None:
        """Record `relay` as a completed peer advertising its HTTP base
        address (so it can be matched against the reliable-nodes roster)
        and its real-time port."""
        assert relay.server is not None
        hello = relay.link_node.build_hello(
            addresses=[
                {"protocol": "http", "address": "127.0.0.1", "port": http_port},
                {"protocol": LINK_REALTIME_PROTOCOL_TAG, "address": "127.0.0.1", "port": relay.server.port},
            ],
            outgoing_only=False, created_at=utc_now_iso(),
        )
        self.link_node.handle_hello(hello)

    async def connect_to(self, relay: "_Node") -> None:
        assert relay.server is not None
        session = await dial_realtime_session(
            "127.0.0.1", relay.server.port, self.identity, on_frame=self.bridge.on_frame,
            registry=self.registry, lane=self.lane, enforce_trust_policy=True,
            expected_fingerprint=relay.identity.fingerprint,
        )
        await self.bridge.track_session(session)

    async def teardown(self) -> None:
        if hasattr(self, "relay_client"):
            await self.relay_client.close()
        if self.relay is not None:
            await self.relay.close()
        await self.registry.close_all(reason="test_done")
        if self.server is not None:
            await self.server.stop()
        await self.bridge.close()
        self.lane.close()
        self.db.close()


ROSTER = [ReliableNode(name="Relay", url="http://127.0.0.1:7862")]


async def _three_nodes(tmp_path, *, serving: bool = True, relay_kwargs: dict | None = None):
    relay = _Node(tmp_path, "relay", relay_kwargs=relay_kwargs)
    a = _Node(tmp_path, "party-a")
    b = _Node(tmp_path, "party-b")
    await relay.start_server(serving=serving)
    a.wire_party()
    b.wire_party()
    relay.trust(a, b)
    a.trust(relay, b)
    b.trust(relay, a)
    a.know_relay(relay)
    b.know_relay(relay)
    # The roster the parties consult when looking for relays (the
    # config-table cache, exactly as a real refresh would leave it).
    from netbbs.link.reliable_nodes import set_cached_reliable_nodes
    set_cached_reliable_nodes(a.db, ROSTER)
    set_cached_reliable_nodes(b.db, ROSTER)
    await a.connect_to(relay)
    await b.connect_to(relay)
    return relay, a, b


async def _wait_until(predicate, *, timeout: float = 3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


# -- wire-level ----------------------------------------------------------------


def test_bridge_attach_record_round_trips_and_is_distinct_from_noise():
    token = "ab" * 16
    record = encode_bridge_attach_record(token)
    assert decode_bridge_attach_record(record[2:]) == token
    assert decode_bridge_attach_record(b"\x01" * 48) is None
    with pytest.raises(Exception):
        encode_bridge_attach_record("not-hex")


def test_relay_and_direct_message_frames_validate_their_shapes():
    ready = build_relay_ready_frame(
        bridge_id="b1", peer_fingerprint="peer", role="responder", attach_token="0" * 32,
        attach_address="127.0.0.1", attach_port=7863,
    )
    validate_realtime_frame_payload(ready)
    assert build_relay_reject_frame(target_fingerprint="x", reason="declined").payload["reason"] == "declined"
    with pytest.raises(LinkProtocolError):
        build_relay_reject_frame(target_fingerprint="x", reason="because")
    with pytest.raises(LinkProtocolError):
        build_relay_request_frame(target_fingerprint="same", requester_fingerprint="same")
    with pytest.raises(LinkProtocolError):
        validate_realtime_frame_payload(RealtimeFrame(
            type="relay_ready", message_id="m",
            payload={**ready.payload, "role": "relay"},
        ))
    with pytest.raises(LinkProtocolError):
        build_direct_message_frame(
            to_user_id="bob", from_user_id="alice", from_display_label="alice", body="   ",
            created_at=utc_now_iso(),
        )


def test_reliable_node_fingerprints_matches_peers_by_advertised_http_address(tmp_path):
    node = _Node(tmp_path, "matcher")
    relay = _Node(tmp_path, "matched-relay")
    hello = relay.link_node.build_hello(
        addresses=[{"protocol": "http", "address": "ReLink.NetBBS.org", "port": 7862}],
        outgoing_only=False, created_at=utc_now_iso(),
    )
    node.link_node.handle_hello(hello)
    roster = [ReliableNode(name="Reliable Link", url="http://relink.netbbs.org:7862")]
    assert reliable_node_fingerprints(node.link_node, roster) == [relay.identity.fingerprint]
    assert reliable_node_fingerprints(node.link_node, [ReliableNode(name="x", url="http://other:1")]) == []
    node.lane.close(); node.db.close(); relay.lane.close(); relay.db.close()


# -- the real thing --------------------------------------------------------------


def test_two_parties_that_cannot_dial_each_other_meet_through_the_relay(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path)
        try:
            session = await a.direct.ensure_session(b.identity.fingerprint)
            assert session.remote_fingerprint == b.identity.fingerprint
            assert session.is_initiator
            assert await _wait_until(lambda: b.registry.get(a.identity.fingerprint) is not None)
            peer_side = b.registry.get(a.identity.fingerprint)
            assert peer_side is not None and not peer_side.is_initiator
            assert relay.relay.active_pairs == 1
            assert relay.relay.pending_rendezvous == 0
            # The relay holds no session with the pair's *bridge* -- only its
            # two ordinary sessions with A and B; the bridge is opaque bytes.
            assert set(relay.registry._sessions) == {a.identity.fingerprint, b.identity.fingerprint}

            # A live direct message rides the relayed session end to end.
            await a.direct.send_direct_message(
                b.identity.fingerprint, to_user_id="bob", from_user_id="alice", from_display_label="Alice",
                body="hello through the relay", created_at=utc_now_iso(),
            )
            assert await _wait_until(lambda: len(b.delivered) == 1)
            got = b.delivered[0]
            assert got.from_node_fingerprint == a.identity.fingerprint
            assert (got.to_user_id, got.from_user_id, got.body) == ("bob", "alice", "hello through the relay")

            # And a second ensure_session reuses it -- no second rendezvous.
            again = await a.direct.ensure_session(b.identity.fingerprint)
            assert again is session
            assert relay.relay.active_pairs == 1
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_closing_one_leg_tears_down_the_bridge_and_the_other_side_notices(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path)
        try:
            session = await a.direct.ensure_session(b.identity.fingerprint)
            assert await _wait_until(lambda: b.registry.get(a.identity.fingerprint) is not None)
            other = b.registry.get(a.identity.fingerprint)
            await session.close(reason="test_close")
            assert await _wait_until(lambda: relay.relay.active_pairs == 0)
            assert await _wait_until(lambda: other.closed.is_set())
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_relay_that_is_not_serving_rejects_with_a_reason(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path, serving=False)
        try:
            with pytest.raises(DirectChatUnreachable) as excinfo:
                await a.direct.ensure_session(b.identity.fingerprint)
            assert excinfo.value.reason == "not_serving"
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_target_not_connected_to_the_relay_is_unreachable(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path)
        try:
            await b.registry.close_all(reason="b_goes_away")
            assert await _wait_until(lambda: relay.registry.get(b.identity.fingerprint) is None)
            with pytest.raises(DirectChatUnreachable) as excinfo:
                await a.direct.ensure_session(b.identity.fingerprint)
            assert excinfo.value.reason == "target_unreachable"
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_invited_party_that_declines_by_policy_fails_the_rendezvous(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path)
        try:
            # B no longer trusts A for real-time traffic: it declines the invitation.
            subject = TrustSubject.node(a.identity.fingerprint)
            set_trust_override(
                b.db, subject, TrustDimension.RESOURCE_BEHAVIOR, TrustState.PROBATIONARY,
                reason="test", now_iso="2026-09-03T12:00:00+00:00",
            )
            with pytest.raises(DirectChatUnreachable) as excinfo:
                await a.direct.ensure_session(b.identity.fingerprint)
            assert excinfo.value.reason == "declined"
            assert relay.relay.pending_rendezvous == 0
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_concurrent_pair_cap_rejects_at_capacity(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path, relay_kwargs={"max_concurrent_pairs": 1})
        c = _Node(tmp_path, "party-c")
        c.wire_party()
        relay.trust(c); c.trust(relay, a); a.trust(c)
        c.know_relay(relay)
        from netbbs.link.reliable_nodes import set_cached_reliable_nodes
        set_cached_reliable_nodes(c.db, ROSTER)
        await c.connect_to(relay)
        try:
            await a.direct.ensure_session(b.identity.fingerprint)
            assert relay.relay.active_pairs == 1
            with pytest.raises(DirectChatUnreachable) as excinfo:
                await c.direct.ensure_session(a.identity.fingerprint)
            assert excinfo.value.reason == "at_capacity"
        finally:
            await c.teardown(); await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_pending_rendezvous_times_out_and_is_reported(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path, relay_kwargs={"rendezvous_timeout_seconds": 0.3})
        try:
            # B's client never answers invitations (handler removed) -> A waits, relay expires it.
            b.bridge._frame_handlers = [h for h in b.bridge._frame_handlers if getattr(h[0], "__self__", None) is not b.relay_client]
            with pytest.raises(DirectChatUnreachable) as excinfo:
                await a.direct.ensure_session(b.identity.fingerprint)
            assert excinfo.value.reason == "timeout"
            assert relay.relay.pending_rendezvous == 0
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_idle_bridge_is_closed_by_the_relay(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path, relay_kwargs={"idle_timeout_seconds": 0.4})
        try:
            session = await a.direct.ensure_session(b.identity.fingerprint)
            assert await _wait_until(lambda: b.registry.get(a.identity.fingerprint) is not None)
            # Silence both ends' heartbeats so the bridge really goes idle.
            for s in (session, b.registry.get(a.identity.fingerprint)):
                for task in s._tasks:
                    if "heartbeat" in (task.get_name() or ""):
                        task.cancel()
            assert await _wait_until(lambda: relay.relay.active_pairs == 0, timeout=3.0)
            assert await _wait_until(lambda: session.closed.is_set(), timeout=3.0)
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_byte_rate_breach_closes_the_bridge(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path, relay_kwargs={"max_bytes_per_second": 2048})
        try:
            session = await a.direct.ensure_session(b.identity.fingerprint)
            big = "x" * 3000
            for _ in range(4):
                try:
                    await session.send(build_direct_message_frame(
                        to_user_id="bob", from_user_id="alice", from_display_label="Alice", body=big,
                        created_at=utc_now_iso(),
                    ))
                except Exception:
                    break
                await asyncio.sleep(0.01)
            assert await _wait_until(lambda: relay.relay.active_pairs == 0, timeout=3.0)
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_attach_with_an_unknown_token_is_refused(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.server.port)
            writer.write(encode_bridge_attach_record("f" * 32))
            await writer.drain()
            data = await asyncio.wait_for(reader.read(1), timeout=3.0)
            assert data == b""  # closed without a byte back
            writer.close()
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_relay_cannot_pair_a_party_with_the_wrong_counterpart(tmp_path):
    """The binding gap the worklog records for any relay design: the
    party checks the authenticated fingerprint against the one the
    rendezvous named, on both roles."""
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path)
        c = _Node(tmp_path, "party-c")
        c.wire_party()
        relay.trust(c); c.trust(relay, a, b); a.trust(c); b.trust(c)
        try:
            # Hand A a ready frame naming B, but wire the attach so C shows up instead.
            from netbbs.link.realtime_relay import _Rendezvous
            loop = asyncio.get_running_loop()
            rv = _Rendezvous(bridge_id="forged", requester=a.identity.fingerprint,
                             target=c.identity.fingerprint, created_at=loop.time(), target_agreed=True)
            rv.tokens = {a.identity.fingerprint: "1" * 32, c.identity.fingerprint: "2" * 32}
            relay.relay._by_token.update({"1" * 32: rv, "2" * 32: rv})
            relay.relay._pending[tuple(sorted((a.identity.fingerprint, c.identity.fingerprint)))] = rv

            async def c_attaches():
                return await attach_relayed_session(
                    "127.0.0.1", relay.server.port, c.identity, attach_token="2" * 32, role="responder",
                    expected_fingerprint=a.identity.fingerprint, on_frame=c.bridge.on_frame,
                    registry=c.registry, lane=c.lane, enforce_trust_policy=True,
                )

            c_task = asyncio.create_task(c_attaches())
            with pytest.raises(LinkProtocolError):
                await attach_relayed_session(
                    "127.0.0.1", relay.server.port, a.identity, attach_token="1" * 32, role="initiator",
                    expected_fingerprint=b.identity.fingerprint, on_frame=a.bridge.on_frame,
                    registry=a.registry, lane=a.lane, enforce_trust_policy=True,
                )
            c_task.cancel()
            try:
                await c_task
            except (asyncio.CancelledError, Exception):
                pass
            assert a.registry.get(c.identity.fingerprint) is None
        finally:
            await c.teardown(); await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_relay_client_request_is_rejected_by_a_relay_reject_frame_without_sockets():
    async def scenario():
        registry = LinkRealtimeSessionRegistry(own_fingerprint="me")
        sent = []

        class _FakeSession:
            remote_fingerprint = "relay"

            async def send(self, frame):
                sent.append(frame)

        client = RealtimeRelayClient(
            own_fingerprint="me", registry=registry, establish_session=None,  # never reached
            decide_peer_allowed=lambda fp: asyncio.sleep(0, result=True), rendezvous_timeout_seconds=1.0,
        )
        session = _FakeSession()
        task = asyncio.create_task(client.request_bridge(session, "peer"))
        await asyncio.sleep(0)
        assert sent and sent[0].type == "relay_request"
        await client.handle_frame(session, build_relay_reject_frame(target_fingerprint="peer", reason="at_capacity"))
        with pytest.raises(RelayRendezvousError) as excinfo:
            await task
        assert excinfo.value.reason == "at_capacity"

    asyncio.run(scenario())
