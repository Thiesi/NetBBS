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
        self.relay._decide_peer_allowed = self._peer_allowed

    async def _attach(self, token, reader, writer):
        assert self.relay is not None
        return await self.relay.attach(token, reader, writer)

    def wire_party(self) -> None:
        """A party that serves nothing (outgoing-only in spirit: no
        listener, no relay) but can ask relays and answer invitations."""
        self._wire_client()

    async def _peer_allowed(self, fingerprint: str) -> bool:
        return await self.lane.run(
            lambda db: decide_node_action(db, fingerprint, LinkPolicyAction.REALTIME).allowed
        )

    def _wire_client(self) -> None:
        _allowed = self._peer_allowed

        async def _establish(**kw):
            return await attach_relayed_session(
                kw["host"], kw["port"], self.identity, attach_token=kw["attach_token"], role=kw["role"],
                expected_fingerprint=kw["expected_fingerprint"], on_frame=self.bridge.on_frame,
                registry=self.registry, lane=self.lane, enforce_trust_policy=True,
            )

        from netbbs.link.transport import dialable_realtime_addresses_for_peer

        self.relay_client = RealtimeRelayClient(
            own_fingerprint=self.identity.fingerprint, registry=self.registry, establish_session=_establish,
            decide_peer_allowed=_allowed, on_session=self.bridge.track_session, rendezvous_timeout_seconds=3.0,
            allowed_attach_addresses=lambda fp: dialable_realtime_addresses_for_peer(self.link_node, fp),
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
    assert build_relay_reject_frame(target_fingerprint="x", reason="declined", origin="party").payload["reason"] == "declined"
    with pytest.raises(LinkProtocolError):
        build_relay_reject_frame(target_fingerprint="x", reason="because", origin="relay")
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
        await client.handle_frame(session, build_relay_reject_frame(target_fingerprint="peer", reason="at_capacity", origin="relay"))
        with pytest.raises(RelayRendezvousError) as excinfo:
            await task
        assert excinfo.value.reason == "at_capacity"

    asyncio.run(scenario())


# -- code review (PR #269) -------------------------------------------------------


class _RecordingSession:
    def __init__(self, fingerprint: str) -> None:
        self.remote_fingerprint = fingerprint
        self.sent: list = []

    async def send(self, frame):
        self.sent.append(frame)


def _client(establish_calls: list, *, timeout: float = 1.0) -> RealtimeRelayClient:
    async def _establish(**kw):
        establish_calls.append(kw)
        raise RuntimeError("no socket in this test")

    return RealtimeRelayClient(
        own_fingerprint="me", registry=LinkRealtimeSessionRegistry(own_fingerprint="me"),
        establish_session=_establish, decide_peer_allowed=lambda fp: asyncio.sleep(0, result=True),
        rendezvous_timeout_seconds=timeout,
    )


def test_client_ignores_relay_ready_it_never_asked_for_or_agreed_to():
    """An authenticated peer must not be able to make this node open an
    outbound connection to an address of its choosing."""
    async def scenario():
        calls: list = []
        client = _client(calls)
        stranger = _RecordingSession("stranger")
        ready = build_relay_ready_frame(
            bridge_id="x", peer_fingerprint="victim", role="responder", attach_token="0" * 32,
            attach_address="10.0.0.5", attach_port=22,
        )
        assert client.owns_frame(stranger, ready) is False
        # Even if routed anyway, nothing attaches.
        await client.handle_frame(stranger, ready)
        await asyncio.sleep(0.05)
        assert calls == []
        # A reject from a peer that is not the relay we asked is ignored too.
        relay = _RecordingSession("relay")
        task = asyncio.create_task(client.request_bridge(relay, "peer"))
        await asyncio.sleep(0)
        reject = build_relay_reject_frame(target_fingerprint="peer", reason="declined", origin="relay")
        assert client.owns_frame(stranger, reject) is False
        assert client.owns_frame(relay, reject) is True
        await client.handle_frame(relay, reject)
        with pytest.raises(RelayRendezvousError) as excinfo:
            await task
        assert excinfo.value.reason == "declined"

    asyncio.run(scenario())


def test_client_honours_relay_ready_only_from_the_relay_it_asked_and_once():
    async def scenario():
        calls: list = []
        client = _client(calls)
        relay = _RecordingSession("relay")
        task = asyncio.create_task(client.request_bridge(relay, "peer"))
        await asyncio.sleep(0)
        ready = build_relay_ready_frame(
            bridge_id="x", peer_fingerprint="peer", role="initiator", attach_token="0" * 32,
            attach_address="127.0.0.1", attach_port=1,
        )
        assert client.owns_frame(_RecordingSession("other"), ready) is False
        assert client.owns_frame(relay, ready) is True
        await client.handle_frame(relay, ready)
        await client.handle_frame(relay, ready)  # duplicate: no second attach
        with pytest.raises(RelayRendezvousError) as excinfo:
            await task
        assert excinfo.value.reason == "attach_failed"
        assert len(calls) == 1 and calls[0]["expected_fingerprint"] == "peer"

    asyncio.run(scenario())


def test_client_accepted_invitation_authorizes_a_ready_from_that_relay_within_the_timeout():
    async def scenario():
        calls: list = []
        client = _client(calls, timeout=0.2)
        relay = _RecordingSession("relay")
        await client.handle_frame(relay, build_relay_request_frame(target_fingerprint="me", requester_fingerprint="alice"))
        assert relay.sent and relay.sent[-1].type == "relay_request"
        ready = build_relay_ready_frame(
            bridge_id="x", peer_fingerprint="alice", role="responder", attach_token="0" * 32,
            attach_address="127.0.0.1", attach_port=1,
        )
        assert client.owns_frame(_RecordingSession("other-relay"), ready) is False
        assert client.owns_frame(relay, ready) is True
        await asyncio.sleep(0.3)  # past the deadline: the acceptance expires
        client._expire_accepted()
        assert client.owns_frame(relay, ready) is False
        assert calls == []

    asyncio.run(scenario())


def test_two_concurrent_waiters_both_get_a_typed_timeout_not_a_cancellation():
    async def scenario():
        client = _client([], timeout=0.2)
        relay = _RecordingSession("relay")
        first = asyncio.create_task(client.request_bridge(relay, "peer"))
        await asyncio.sleep(0)
        second = asyncio.create_task(client.request_bridge(relay, "peer"))
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(r, RelayRendezvousError) and r.reason == "timeout" for r in results), results
        assert client._waiting == {}

    asyncio.run(scenario())


def test_pair_cap_is_enforced_when_the_bridge_would_start_not_only_at_request_time(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path, relay_kwargs={"max_concurrent_pairs": 1})
        try:
            # Fake an already-running bridge so the request-time check passes
            # (0 < 1 at request time is what the relay would see with a
            # concurrent rendezvous racing it) but the start-time check fails.
            from netbbs.link.realtime_relay import _Bridge
            loop = asyncio.get_running_loop()
            relay.relay._bridges["occupied"] = _Bridge(bridge_id="occupied", pair=("x", "y"), legs={}, last_activity=loop.time())
            with pytest.raises(DirectChatUnreachable) as excinfo:
                await a.direct.ensure_session(b.identity.fingerprint)
            assert excinfo.value.reason == "at_capacity"
        finally:
            relay.relay._bridges.pop("occupied", None)
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_presence_from_a_dialed_in_session_is_tracked_and_cleared_on_close(tmp_path):
    """A session that only ever dials in (no subscribe) still gets tracked
    on the receiving side, so its presence is answered and later cleared."""
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path)
        try:
            assert await _wait_until(lambda: a.identity.fingerprint in relay.bridge.remote_node_presence())
            # ... and the relay answered with its own snapshot, so A knows R's roster too.
            assert await _wait_until(lambda: relay.identity.fingerprint in a.bridge.remote_node_presence())
            await a.registry.close_all(reason="a_leaves")
            assert await _wait_until(lambda: a.identity.fingerprint not in relay.bridge.remote_node_presence())
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_anchor_connectors_are_reconciled_when_participation_or_roster_changes(tmp_path):
    from netbbs.link.onboarding import Participation, set_participation
    from netbbs.link.realtime_direct import run_reliable_anchor_connectors
    from netbbs.link.reliable_nodes import set_cached_reliable_nodes

    node = _Node(tmp_path, "anchoring")
    relay = _Node(tmp_path, "anchor-relay")
    relay_hello = relay.link_node.build_hello(
        addresses=[
            {"protocol": "http", "address": "127.0.0.1", "port": 7862},
            {"protocol": LINK_REALTIME_PROTOCOL_TAG, "address": "127.0.0.1", "port": 9001},
        ],
        outgoing_only=False, created_at=utc_now_iso(),
    )
    node.link_node.handle_hello(relay_hello)
    set_cached_reliable_nodes(node.db, ROSTER)
    set_participation(node.db, Participation.ACCEPTED)
    events: list = []

    class _Connector:
        def __init__(self, host, port):
            self.address = (host, port)
            self.stopped = False

        async def stop(self):
            self.stopped = True
            events.append(("stop", self.address))

    def start_connector(**kw):
        events.append(("start", (kw["host"], kw["port"])))
        return _Connector(kw["host"], kw["port"])

    passes = 0
    stop_event = asyncio.Event()

    async def fake_sleep(_):
        nonlocal passes
        passes += 1
        if passes == 1:
            set_participation(node.db, Participation.DECLINED)   # pass 2 must stop it
        elif passes == 2:
            set_participation(node.db, Participation.ACCEPTED)   # pass 3 restarts it
        elif passes == 3:
            stop_event.set()
        await asyncio.sleep(0)

    async def scenario():
        await run_reliable_anchor_connectors(
            node_identity=node.identity, link_node=node.link_node, lane=node.lane, registry=node.registry,
            on_frame=node.bridge.on_frame, track_session=node.bridge.track_session,
            participation_accepted=lambda db: set_participation and __import__("netbbs.link.onboarding", fromlist=["participation_accepted"]).participation_accepted(db),
            start_connector=start_connector, interval_seconds=0, sleep=fake_sleep, stop_event=stop_event,
        )

    asyncio.run(scenario())
    assert events == [
        ("start", ("127.0.0.1", 9001)), ("stop", ("127.0.0.1", 9001)),
        ("start", ("127.0.0.1", 9001)), ("stop", ("127.0.0.1", 9001)),  # final stop: task exit owns them
    ]
    node.lane.close(); node.db.close(); relay.lane.close(); relay.db.close()


def test_client_refuses_a_ready_whose_attach_address_the_relay_does_not_advertise():
    """Codex review (PR #269): correlation proves which relay spoke; the
    pin proves the address is that relay's own -- a malicious relay must
    not be able to point this node at an arbitrary service."""
    async def scenario():
        calls: list = []

        async def _establish(**kw):
            calls.append(kw)
            raise RuntimeError("unreachable in this test")

        client = RealtimeRelayClient(
            own_fingerprint="me", registry=LinkRealtimeSessionRegistry(own_fingerprint="me"),
            establish_session=_establish, decide_peer_allowed=lambda fp: asyncio.sleep(0, result=True),
            rendezvous_timeout_seconds=1.0, allowed_attach_addresses=lambda fp: [("127.0.0.1", 7863)],
        )
        relay = _RecordingSession("relay")
        task = asyncio.create_task(client.request_bridge(relay, "peer"))
        await asyncio.sleep(0)
        bad = build_relay_ready_frame(
            bridge_id="x", peer_fingerprint="peer", role="initiator", attach_token="0" * 32,
            attach_address="10.0.0.5", attach_port=22,
        )
        await client.handle_frame(relay, bad)
        with pytest.raises(RelayRendezvousError) as excinfo:
            await task
        assert excinfo.value.reason == "attach_failed"
        assert calls == []
        # The advertised address is accepted (and then fails only at establish).
        task = asyncio.create_task(client.request_bridge(relay, "peer"))
        await asyncio.sleep(0)
        good = build_relay_ready_frame(
            bridge_id="x", peer_fingerprint="peer", role="initiator", attach_token="0" * 32,
            attach_address="127.0.0.1", attach_port=7863,
        )
        await client.handle_frame(relay, good)
        with pytest.raises(RelayRendezvousError):
            await task
        assert len(calls) == 1

    asyncio.run(scenario())


def test_ensure_session_rechecks_trust_before_reusing_a_session(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path)
        try:
            session = await a.direct.ensure_session(b.identity.fingerprint)
            assert a.registry.get(b.identity.fingerprint) is session
            set_trust_override(
                a.db, TrustSubject.node(b.identity.fingerprint), TrustDimension.RESOURCE_BEHAVIOR,
                TrustState.BLOCKED, reason="sysop lockout", now_iso="2026-09-03T12:00:00+00:00",
            )
            with pytest.raises(DirectChatUnreachable) as excinfo:
                await a.direct.ensure_session(b.identity.fingerprint)
            assert excinfo.value.reason == "policy"
            with pytest.raises(DirectChatUnreachable):
                await a.direct.send_direct_message(
                    b.identity.fingerprint, to_user_id="bob", from_user_id="alice", from_display_label="Alice",
                    body="must not go out", created_at=utc_now_iso(),
                )
            assert b.delivered == []
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_reliable_node_fingerprints_accepts_https_descriptors(tmp_path):
    node = _Node(tmp_path, "https-matcher")
    relay = _Node(tmp_path, "https-relay")
    node.link_node.handle_hello(relay.link_node.build_hello(
        addresses=[{"protocol": "https", "address": "relink.example", "port": 443}],
        outgoing_only=False, created_at=utc_now_iso(),
    ))
    assert reliable_node_fingerprints(node.link_node, [ReliableNode(name="R", url="https://relink.example")]) == [relay.identity.fingerprint]
    node.lane.close(); node.db.close(); relay.lane.close(); relay.db.close()


def test_direct_message_frame_rejects_a_malformed_timestamp():
    with pytest.raises(LinkProtocolError):
        build_direct_message_frame(
            to_user_id="bob", from_user_id="alice", from_display_label="Alice", body="hi", created_at="x",
        )


# -- Codex round 3 (PR #269) --------------------------------------------------------


def test_a_node_dials_a_reliable_relay_on_demand_when_it_holds_no_session(tmp_path):
    """A full peer runs no standing anchor unless it opts in; it must still
    reach an outgoing-only peer through the shared relay."""
    async def scenario():
        relay = _Node(tmp_path, "relay-od")
        a = _Node(tmp_path, "party-a-od")
        b = _Node(tmp_path, "party-b-od")
        await relay.start_server(serving=True)
        a.wire_party(); b.wire_party()
        relay.trust(a, b); a.trust(relay, b); b.trust(relay, a)
        a.know_relay(relay); b.know_relay(relay)
        from netbbs.link.reliable_nodes import set_cached_reliable_nodes
        set_cached_reliable_nodes(a.db, ROSTER); set_cached_reliable_nodes(b.db, ROSTER)
        await b.connect_to(relay)  # only the target stands by; A has no session yet
        try:
            assert a.registry.get(relay.identity.fingerprint) is None
            session = await a.direct.ensure_session(b.identity.fingerprint)
            assert session.remote_fingerprint == b.identity.fingerprint
            assert a.registry.get(relay.identity.fingerprint) is not None  # dialed on demand
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_relay_rechecks_the_requesters_policy_on_every_request(tmp_path):
    async def scenario():
        relay, a, b = await _three_nodes(tmp_path)
        try:
            set_trust_override(
                relay.db, TrustSubject.node(a.identity.fingerprint), TrustDimension.RESOURCE_BEHAVIOR,
                TrustState.BLOCKED, reason="sysop lockout", now_iso="2026-09-03T12:00:00+00:00",
            )
            with pytest.raises(DirectChatUnreachable) as excinfo:
                await a.direct.ensure_session(b.identity.fingerprint)
            assert excinfo.value.reason == "policy_refused"
            assert relay.relay.pending_rendezvous == 0
        finally:
            await a.teardown(); await b.teardown(); await relay.teardown()

    asyncio.run(scenario())


def test_track_session_creates_exactly_one_close_watcher_per_session(tmp_path):
    async def scenario():
        node = _Node(tmp_path, "watchers")

        class _Sess:
            remote_fingerprint = "peer"
            closed = asyncio.Event()

            async def send(self, frame):
                pass

        s = _Sess()
        for _ in range(5):
            await node.bridge.track_session(s)
        assert len(node.bridge._watchers) == 1
        s.closed.set()
        await asyncio.sleep(0.05)
        assert node.bridge._watchers == set() and node.bridge._watched == {}
        node.lane.close(); node.db.close()

    asyncio.run(scenario())


def test_attach_closes_its_socket_when_cancelled_mid_handshake():
    async def scenario():
        seen_eof = asyncio.Event()

        async def silent(reader, writer):
            # Never answers; just drains until the client goes away.
            while await reader.read(4096):
                pass
            seen_eof.set()
            writer.close()

        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        identity = bootstrap_node_identity("cancelled-attach")
        registry = LinkRealtimeSessionRegistry(own_fingerprint=identity.fingerprint)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(attach_relayed_session(
                    "127.0.0.1", port, identity, attach_token="0" * 32, role="initiator",
                    expected_fingerprint="peer", on_frame=lambda s, f: asyncio.sleep(0), registry=registry,
                ), timeout=0.3)
            assert await _wait_until(seen_eof.is_set)
        finally:
            server.close()

    asyncio.run(scenario())


def test_version_mismatch_on_direct_dial_surfaces_as_an_upgrade_notice(tmp_path, monkeypatch):
    import netbbs.link.realtime_direct as direct_module
    from netbbs.link.protocol import RealtimeProtocolVersionError

    node = _Node(tmp_path, "vmismatch")
    peer = _Node(tmp_path, "vmismatch-peer")
    node.wire_party()
    node.trust(peer)
    node.link_node.handle_hello(peer.link_node.build_hello(
        addresses=[{"protocol": LINK_REALTIME_PROTOCOL_TAG, "address": "127.0.0.1", "port": 9}],
        outgoing_only=False, created_at=utc_now_iso(),
    ))

    async def old_peer(*args, **kwargs):
        raise RealtimeProtocolVersionError("unsupported real-time protocol version")

    monkeypatch.setattr(direct_module, "dial_realtime_session", old_peer)
    with pytest.raises(RealtimeProtocolVersionError):
        asyncio.run(node.direct.ensure_session(peer.identity.fingerprint))
    node.lane.close(); node.db.close(); peer.lane.close(); peer.db.close()


def test_connector_cycles_through_every_advertised_address(monkeypatch):
    import netbbs.link.transport as transport_module
    from netbbs.link.transport import LinkRealtimeConnector

    dialed: list = []

    async def failing_dial(host, port, *args, **kwargs):
        dialed.append((host, port))
        raise RuntimeError("nope")

    monkeypatch.setattr(transport_module, "dial_realtime_session", failing_dial)

    async def scenario():
        connector = LinkRealtimeConnector(
            host="a", port=1, identity=None, on_frame=None, registry=None,
            addresses=[("a", 1), ("b", 2), ("c", 3)], min_backoff_seconds=0.0, max_backoff_seconds=0.0,
        )
        connector.start()
        await asyncio.sleep(0.05)
        await connector.stop()

    asyncio.run(scenario())
    assert dialed[:3] == [("a", 1), ("b", 2), ("c", 3)] and len(dialed) >= 3


def test_client_ignores_a_late_answer_to_an_earlier_attempt():
    async def scenario():
        client = _client([], timeout=0.2)
        relay = _RecordingSession("relay")
        with pytest.raises(RelayRendezvousError):
            await client.request_bridge(relay, "peer")  # times out
        stale_id = relay.sent[-1].message_id
        task = asyncio.create_task(client.request_bridge(relay, "peer"))  # the retry
        await asyncio.sleep(0)
        late = build_relay_reject_frame(target_fingerprint="peer", reason="declined", origin="relay", request_id=stale_id)
        assert client.owns_frame(relay, late) is False
        fresh = build_relay_reject_frame(
            target_fingerprint="peer", reason="declined", origin="relay", request_id=relay.sent[-1].message_id,
        )
        assert client.owns_frame(relay, fresh) is True
        await client.handle_frame(relay, fresh)
        with pytest.raises(RelayRendezvousError) as excinfo:
            await task
        assert excinfo.value.reason == "declined"

    asyncio.run(scenario())


def test_malformed_roster_urls_are_skipped_not_fatal(tmp_path):
    from netbbs.link.realtime_direct import _url_host_port
    from netbbs.link.reliable_nodes import parse_reliable_nodes
    import json

    assert _url_host_port("http://[::1") == ("", None)
    assert _url_host_port("http://host:notaport") == ("", None)
    assert _url_host_port("http://host:99999") == ("", None)
    node = _Node(tmp_path, "badurl")
    assert reliable_node_fingerprints(node.link_node, [ReliableNode(name="x", url="http://[::1")]) == []
    parsed = parse_reliable_nodes(json.dumps({"version": 1, "nodes": [
        {"name": "bad", "url": "http://host:notaport"}, {"name": "ok", "url": "http://host:7862"},
    ]}).encode())
    assert [n.name for n in parsed] == ["ok"]
    node.lane.close(); node.db.close()
