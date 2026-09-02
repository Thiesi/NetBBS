"""
Integration tests for `netbbs.link.realtime_channels` (design doc
§8.10.2, issue #148's first vertical) -- real loopback-socket Noise
sessions between two nodes, exercising the full path from a subscribe
request through to a locally-connected `ChatHub` participant actually
receiving a rendered live message/presence event.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub, ParticipantId
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import record_message
from netbbs.link.channels import (
    link_channel,
    materialize_carried_channel,
    queue_channel_message_if_linked,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import (
    LinkNode,
    LinkProtocolError,
    RealtimeFrame,
    build_error_frame,
    build_scrollback_snapshot_frame,
    build_subscribe_frame,
)
from netbbs.link.realtime_channels import (
    LiveChannelBridge,
    _MAX_SCROLLBACK_SNAPSHOT_ENTRIES,
    ensure_live_subscription,
)
from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, set_trust_override
from netbbs.link.enforcement import ensure_node_subject
from netbbs.link.transport import (
    LINK_REALTIME_PROTOCOL_TAG,
    LinkRealtimeServer,
    LinkRealtimeSessionRegistry,
    dial_realtime_session,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso


class _Node:
    """One node's full test rig: db/lane, identity, chat hub, real-time
    session registry, and live-channel bridge -- everything `LiveChannel
    Bridge`/`ensure_live_subscription` need, bundled the way a running
    node would actually hold them."""

    def __init__(self, tmp_path, name: str) -> None:
        self.db = Database(tmp_path / f"{name}.db")
        self.lane = DatabaseLane(self.db.path)
        self.identity = bootstrap_node_identity(name)
        self.hub = ChatHub()
        self.presence = PresenceRegistry()
        self.registry = LinkRealtimeSessionRegistry(own_fingerprint=self.identity.fingerprint)
        self.bridge = LiveChannelBridge(hub=self.hub, lane=self.lane, presence=self.presence, registry=self.registry)
        self.link_node = LinkNode(identity=self.identity)

    async def teardown(self) -> None:
        await self.registry.close_all(reason="test_done")
        await self.bridge.close()
        self.lane.close()
        self.db.close()


async def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def _establish_trust(db: Database, fingerprint: str) -> None:
    """A freshly-seen node subject defaults to `PROBATIONARY` (design
    doc §14/§4), which `LinkPolicyAction.REALTIME` never allows -- tests
    exercising the authorized happy path pre-establish trust the same
    way an operator's own vouching/reputation accrual would, mirroring
    `test_link_transport.py`'s existing quarantine-test setup exactly."""
    ensure_node_subject(db, fingerprint)
    subject = TrustSubject.node(fingerprint)
    for dimension in (TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR):
        set_trust_override(
            db, subject, dimension, TrustState.ESTABLISHED,
            reason="pre-established for test", now_iso="2026-08-14T12:00:00+00:00",
        )


def _setup_linked_channel(origin: _Node, subscriber: _Node, *, name: str = "lobby"):
    """Create+Link a channel on `origin`, materialize the identical
    carried copy on `subscriber` -- the same relationship async catch-up
    already establishes, standing in for it here without a real
    HTTP hello/events round trip. Returns `(origin_channel,
    subscriber_channel)` -- same `channel_id`, independent local rows."""
    creator = create_user(origin.db, f"{name}-creator", password="hunter2", user_level=10)
    origin_channel = create_channel(origin.db, name, creator=creator)
    genesis = link_channel(origin.db, origin_channel, node_identity=origin.identity)
    subscriber_channel = materialize_carried_channel(subscriber.db, genesis)
    return origin_channel, subscriber_channel


def test_ensure_live_subscription_dials_the_origin_and_registers_the_subscription(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-dial")
        subscriber = _Node(tmp_path, "subscriber-dial")
        origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="dial-room")

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            _establish_trust(subscriber.db, origin.identity.fingerprint)
            # In-memory hello (LinkNode.handle_hello is transport-agnostic,
            # no socket needed) giving the subscriber a verified peer
            # record for origin advertising the real-time port just opened.
            origin_hello = origin.link_node.build_hello(
                addresses=[
                    {"protocol": LINK_REALTIME_PROTOCOL_TAG, "address": "127.0.0.1", "port": server.port}
                ],
                outgoing_only=False, created_at=utc_now_iso(),
            )
            subscriber.link_node.handle_hello(origin_hello)

            result = await ensure_live_subscription(
                channel=subscriber_channel, node_identity=subscriber.identity, link_node=subscriber.link_node,
                lane=subscriber.lane, registry=subscriber.registry, bridge=subscriber.bridge,
            )

            assert result is not None
            session, _request_id = result
            assert subscriber.registry.get(origin.identity.fingerprint) is session
            assert await _wait_until(lambda: origin_channel.channel_id in origin.bridge._subscribers)
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_ensure_live_subscription_is_a_harmless_no_op_for_an_unlinked_channel(tmp_path):
    async def scenario():
        subscriber = _Node(tmp_path, "subscriber-unlinked")
        creator = create_user(subscriber.db, "unlinked-creator", password="hunter2", user_level=10)
        channel = create_channel(subscriber.db, "not-linked", creator=creator)
        try:
            session = await ensure_live_subscription(
                channel=channel, node_identity=subscriber.identity, link_node=subscriber.link_node,
                lane=subscriber.lane, registry=subscriber.registry, bridge=subscriber.bridge,
            )
            assert session is None
        finally:
            await subscriber.teardown()

    asyncio.run(scenario())


def test_live_channel_message_and_presence_reach_a_locally_connected_participant(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-live")
        subscriber = _Node(tmp_path, "subscriber-live")
        origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="townsquare")

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            # Subscriber's own `_handle_channel_message`/`_handle_presence_
            # delta` re-check authorization against *its own* db for the
            # sending peer (origin) on every delivered frame -- needs
            # origin trusted there too, not just origin trusting subscriber.
            _establish_trust(subscriber.db, origin.identity.fingerprint)
            session = await dial_realtime_session(
                "127.0.0.1", server.port, subscriber.identity, on_frame=subscriber.bridge.on_frame,
                registry=subscriber.registry,
            )
            await subscriber.bridge.track_session(session)
            await session.send(build_subscribe_frame(subscriber_channel.channel_id))
            assert await _wait_until(lambda: origin_channel.channel_id in origin.bridge._subscribers)

            # A local caller on the *subscriber* node is watching this
            # linked channel -- register it with the hub exactly like a
            # real chat session's join already does.
            participant = ParticipantId(username="localwatcher", session_key=1)
            queue = subscriber.hub.join(subscriber_channel.name, participant)

            # Origin's own local user posts a message -- pushed live to
            # subscribers, the same call a real chat send loop makes.
            origin_user = create_user(origin.db, "origin-speaker", password="hunter2", user_level=10)
            recorded = record_message(
                origin.db, origin_channel, kind="message", author_label=origin_user.username,
                author_fingerprint=origin_user.fingerprint, body="hello from origin",
            )
            await origin.bridge.broadcast_local_message_live(origin_channel, recorded)

            delivered = await asyncio.wait_for(queue.get(), timeout=2.0)
            assert delivered.kind == "message"
            assert delivered.body == "hello from origin"
            assert delivered.author_label == f"origin-speaker@{origin.identity.fingerprint}"
            assert delivered.author_fingerprint is None  # never treated as locally verified

            await origin.bridge.broadcast_local_presence_live(
                origin_channel, change="join", username="origin-speaker"
            )
            presence_event = await asyncio.wait_for(queue.get(), timeout=2.0)
            assert presence_event.kind == "join"
            assert presence_event.author_label == f"origin-speaker@{origin.identity.fingerprint}"

            # Never persisted -- live delivery stays purely in-memory.
            assert origin.db.connection.execute(
                "SELECT COUNT(*) FROM channel_messages WHERE body = 'hello from origin'"
            ).fetchone()[0] == 1  # origin's own record_message call above, not a second copy
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_subscribe_to_an_unlinked_channel_is_rejected_without_registering_a_subscription(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-reject")
        subscriber = _Node(tmp_path, "subscriber-reject")
        creator = create_user(origin.db, "reject-creator", password="hunter2", user_level=10)
        create_channel(origin.db, "private-room", creator=creator)  # exists locally, never Linked

        received_errors: list[RealtimeFrame] = []

        async def on_frame_subscriber(session, frame):
            if frame.type == "error":
                received_errors.append(frame)

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            session = await dial_realtime_session(
                "127.0.0.1", server.port, subscriber.identity, on_frame=on_frame_subscriber,
                registry=subscriber.registry,
            )
            await session.send(build_subscribe_frame("nonexistent-channel-id"))
            assert await _wait_until(lambda: len(received_errors) == 1)
            assert origin.bridge._subscribers == {}
            assert session.closed.is_set() is False  # one rejection is a strike, not a hard close
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_a_quarantined_subscriber_stops_receiving_further_live_messages(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-quarantine")
        subscriber = _Node(tmp_path, "subscriber-quarantine")
        origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="watched-room")

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            session = await dial_realtime_session(
                "127.0.0.1", server.port, subscriber.identity, on_frame=subscriber.bridge.on_frame,
                registry=subscriber.registry,
            )
            await session.send(build_subscribe_frame(subscriber_channel.channel_id))
            assert await _wait_until(lambda: origin_channel.channel_id in origin.bridge._subscribers)

            participant = ParticipantId(username="localwatcher", session_key=1)
            queue = subscriber.hub.join(subscriber_channel.name, participant)

            subject = TrustSubject.node(subscriber.identity.fingerprint)
            ensure_node_subject(origin.db, subscriber.identity.fingerprint)
            set_trust_override(
                origin.db, subject, TrustDimension.RESOURCE_BEHAVIOR, TrustState.QUARANTINED,
                reason="quarantined mid-session for test", now_iso="2026-08-14T12:01:00+00:00",
            )

            origin_user = create_user(origin.db, "origin-speaker-q", password="hunter2", user_level=10)
            recorded = record_message(
                origin.db, origin_channel, kind="message", author_label=origin_user.username,
                author_fingerprint=origin_user.fingerprint, body="should never arrive",
            )
            await origin.bridge.broadcast_local_message_live(origin_channel, recorded)

            assert queue.empty()
            assert origin_channel.channel_id not in origin.bridge._subscribers
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_disconnect_without_unsubscribe_is_purged_by_the_watcher(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-disconnect")
        subscriber = _Node(tmp_path, "subscriber-disconnect")
        origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="fickle-room")

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            session = await dial_realtime_session(
                "127.0.0.1", server.port, subscriber.identity, on_frame=subscriber.bridge.on_frame,
                registry=subscriber.registry,
            )
            await session.send(build_subscribe_frame(subscriber_channel.channel_id))
            assert await _wait_until(lambda: origin_channel.channel_id in origin.bridge._subscribers)

            await session.close(reason="test_forced_disconnect")

            assert await _wait_until(lambda: origin_channel.channel_id not in origin.bridge._subscribers)
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_local_interest_reference_counts_holders_per_channel():
    """Issue #159: `register_local_interest`/`release_local_interest`
    are the subscriber-side mirror of `_subscribers` above (which tracks
    the *origin*-side "who subscribes to my channels"). Plain in-memory
    bookkeeping, no I/O -- covered directly rather than only through the
    full `_chat_loop` integration test in `test_chat_flow_link.py`."""
    bridge = LiveChannelBridge(
        hub=ChatHub(), lane=None, presence=PresenceRegistry(),
        registry=LinkRealtimeSessionRegistry(own_fingerprint="test-node"),
    )
    alice_holder, bob_holder = 1, 2

    # First registration for a channel is reported as such; a second,
    # different holder for the *same* channel is not -- someone else
    # already established local interest.
    assert bridge.register_local_interest("channel-a", alice_holder) is True
    assert bridge.register_local_interest("channel-a", bob_holder) is False

    # Registering the same holder twice (e.g. a caller re-entering the
    # same channel view) is idempotent, not a second holder.
    assert bridge.register_local_interest("channel-a", alice_holder) is False

    # Releasing one of two holders is never "the last" -- the other is
    # still relying on the same feed.
    assert bridge.release_local_interest("channel-a", alice_holder) is False
    # Releasing an already-released (or never-registered) holder is a
    # safe no-op, not an error and not falsely "the last."
    assert bridge.release_local_interest("channel-a", alice_holder) is False
    assert bridge.release_local_interest("channel-a", 999) is False

    # The one remaining holder leaving is genuinely the last.
    assert bridge.release_local_interest("channel-a", bob_holder) is True
    # A channel with zero holders left releases its own bookkeeping
    # entirely, so the next registration is a fresh "first" again --
    # this and the two independent channels below both prove holder
    # state never leaks across different channel_ids.
    assert bridge.register_local_interest("channel-a", alice_holder) is True
    assert bridge.register_local_interest("channel-b", alice_holder) is True


# -- node-wide presence (issue #164) -----------------------------------------


def _capturing_on_frame(bridge: LiveChannelBridge):
    """Wraps `bridge.on_frame` to also capture the first (origin-side)
    session object it's ever invoked with. That object only becomes
    reachable once *some* frame flows through it -- a bare connection
    with no channel activity at all has no other way to hand the
    origin-side session back to test code, which needs it to call
    `track_session` directly the way `_handle_subscribe` normally
    would."""
    captured: dict[str, LinkRealtimeSession] = {}

    async def on_frame(session, frame):
        captured.setdefault("session", session)
        await bridge.on_frame(session, frame)

    return on_frame, captured


async def _connect_bare(origin: _Node, subscriber: _Node, server: LinkRealtimeServer, *, on_frame_subscriber):
    """Dial `origin` from `subscriber` with no channel subscription at
    all, then send one harmless `error` frame (silently ignored by
    `LiveChannelBridge.on_frame`'s dispatch -- design doc: nothing
    actionable locally from a peer-reported rejection) purely to trigger
    capture of the origin-side session object. Returns `(subscriber_
    session, origin_session)`."""
    capturing_on_frame, captured = _capturing_on_frame(origin.bridge)
    server._on_frame = capturing_on_frame  # reach into the private attribute _admit_inbound actually calls
    session = await dial_realtime_session(
        "127.0.0.1", server.port, subscriber.identity, on_frame=on_frame_subscriber, registry=subscriber.registry,
    )
    await session.send(build_error_frame("probe", "capture session"))
    assert await _wait_until(lambda: "session" in captured)
    return session, captured["session"]


def test_connecting_sends_the_local_online_roster_as_a_node_presence_snapshot(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-node-presence-snapshot")
        subscriber = _Node(tmp_path, "subscriber-node-presence-snapshot")
        origin.presence.enter("alice")
        origin.presence.enter("bob")

        received: list[RealtimeFrame] = []

        async def on_frame_subscriber(session, frame):
            received.append(frame)

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            _subscriber_session, origin_session = await _connect_bare(
                origin, subscriber, server, on_frame_subscriber=on_frame_subscriber
            )
            received.clear()  # discard the probe's own "error" round trip, if anything came back for it

            await origin.bridge.track_session(origin_session)  # mirrors what _handle_subscribe already does

            assert await _wait_until(lambda: len(received) == 1)
            assert received[0].type == "node_presence_snapshot"
            entries = {entry["user_id"]: entry["display_label"] for entry in received[0].payload["entries"]}
            assert entries == {"alice": "alice", "bob": "bob"}
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_track_session_sends_the_initial_snapshot_only_once(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-track-once")
        subscriber = _Node(tmp_path, "subscriber-track-once")
        origin.presence.enter("alice")

        received: list[RealtimeFrame] = []

        async def on_frame_subscriber(session, frame):
            received.append(frame)

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            _subscriber_session, origin_session = await _connect_bare(
                origin, subscriber, server, on_frame_subscriber=on_frame_subscriber
            )
            received.clear()

            await origin.bridge.track_session(origin_session)
            await origin.bridge.track_session(origin_session)  # same session again -- e.g. a second subscribe
            await asyncio.sleep(0.05)  # give a wrongly-duplicated send a chance to arrive
            assert len(received) == 1
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_broadcast_node_presence_live_reaches_a_peer_with_no_channel_subscription_at_all(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-node-broadcast")
        subscriber = _Node(tmp_path, "subscriber-node-broadcast")

        received: list[RealtimeFrame] = []

        async def on_frame_subscriber(session, frame):
            received.append(frame)

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            # registry.admit() happens at the handshake, before any
            # application frame -- a bare dial with zero channel activity
            # is already enough for broadcast_node_presence_live (which
            # reads from `registry`, not `_subscribers`) to reach it.
            await dial_realtime_session(
                "127.0.0.1", server.port, subscriber.identity, on_frame=on_frame_subscriber,
                registry=subscriber.registry,
            )
            # The server admits the origin-side session in its own
            # background accept task -- give it a moment to actually land
            # in origin.registry before broadcasting, or this races.
            assert await _wait_until(lambda: len(origin.registry.all_sessions()) == 1)

            await origin.bridge.broadcast_node_presence_live(change="join", username="carol")

            assert await _wait_until(lambda: len(received) == 1)
            assert received[0].type == "node_presence_delta"
            assert received[0].payload == {"change": "join", "user_id": "carol", "display_label": "carol"}
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_broadcast_node_presence_live_stops_reaching_a_peer_once_quarantined(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-node-quarantine-broadcast")
        subscriber = _Node(tmp_path, "subscriber-node-quarantine-broadcast")

        received: list[RealtimeFrame] = []

        async def on_frame_subscriber(session, frame):
            received.append(frame)

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            # Connects while ESTABLISHED (enforce_trust_policy=True on the
            # server would otherwise refuse the handshake outright) --
            # then degrades, proving broadcast re-checks fresh rather than
            # trusting the still-open connection (design doc §8.10.2:
            # "checked again at message delivery").
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            await dial_realtime_session(
                "127.0.0.1", server.port, subscriber.identity, on_frame=on_frame_subscriber,
                registry=subscriber.registry,
            )

            set_trust_override(
                origin.db, TrustSubject.node(subscriber.identity.fingerprint), TrustDimension.RESOURCE_BEHAVIOR,
                TrustState.QUARANTINED, reason="test quarantine", now_iso="2026-08-14T12:00:01Z",
            )

            await origin.bridge.broadcast_node_presence_live(change="join", username="carol")
            await asyncio.sleep(0.1)
            assert received == []
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_remote_node_presence_is_populated_from_the_origins_initial_snapshot(tmp_path):
    """Full real round trip: the subscriber's own bridge (not a bare
    frame-collecting stub) processes what the origin sends on
    `_handle_subscribe`, populating `remote_node_presence()` -- the
    receiving half `test_connecting_sends_the_local_online_roster_...`
    above only proves the *sending* half of."""
    async def scenario():
        origin = _Node(tmp_path, "origin-remote-presence")
        subscriber = _Node(tmp_path, "subscriber-remote-presence")
        origin.presence.enter("dave")
        origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="presence-room")

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            _establish_trust(subscriber.db, origin.identity.fingerprint)
            # In-memory hello giving the subscriber a verified peer record
            # for origin advertising the real-time port just opened --
            # ensure_live_subscription needs a dialable address on file,
            # same setup test_ensure_live_subscription_dials_the_origin_
            # and_registers_the_subscription above already establishes.
            origin_hello = origin.link_node.build_hello(
                addresses=[
                    {"protocol": LINK_REALTIME_PROTOCOL_TAG, "address": "127.0.0.1", "port": server.port}
                ],
                outgoing_only=False, created_at=utc_now_iso(),
            )
            subscriber.link_node.handle_hello(origin_hello)

            result = await ensure_live_subscription(
                channel=subscriber_channel, node_identity=subscriber.identity, link_node=subscriber.link_node,
                lane=subscriber.lane, registry=subscriber.registry, bridge=subscriber.bridge,
            )
            assert result is not None
            session, _request_id = result

            assert await _wait_until(
                lambda: origin.identity.fingerprint in subscriber.bridge.remote_node_presence()
            )
            assert subscriber.bridge.remote_node_presence()[origin.identity.fingerprint] == {"dave": "dave"}

            # A subsequent login on the origin (a delta, not a fresh
            # snapshot) updates the same entry incrementally.
            await origin.bridge.broadcast_node_presence_live(change="join", username="erin")
            assert await _wait_until(
                lambda: subscriber.bridge.remote_node_presence().get(origin.identity.fingerprint) ==
                {"dave": "dave", "erin": "erin"}
            )

            await origin.bridge.broadcast_node_presence_live(change="leave", username="dave")
            assert await _wait_until(
                lambda: subscriber.bridge.remote_node_presence().get(origin.identity.fingerprint) ==
                {"erin": "erin"}
            )

            # Closing the session clears that peer's remote presence --
            # stale data from a now-disconnected peer must not linger.
            await session.close(reason="test_done")
            assert await _wait_until(
                lambda: origin.identity.fingerprint not in subscriber.bridge.remote_node_presence()
            )
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_remote_channel_presence_is_populated_from_the_origins_initial_snapshot(tmp_path):
    """Issue #195's merged live roster -- the channel-scoped counterpart
    to `test_remote_node_presence_is_populated_from_the_origins_initial_
    snapshot` above. `_handle_subscribe`'s presence_snapshot already
    carries the origin's own local hub participants for this exact
    channel (unlike node-wide presence, which comes from `PresenceRegistry`
    instead) -- this proves the *subscriber's* bridge actually stores
    that snapshot, then keeps it in sync through subsequent deltas, then
    clears it on disconnect, exactly the same three-part shape already
    proven for node-wide presence."""
    async def scenario():
        origin = _Node(tmp_path, "origin-channel-presence")
        subscriber = _Node(tmp_path, "subscriber-channel-presence")
        origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="roster-room")
        origin.hub.join(origin_channel.name, ParticipantId(username="dave", session_key=1))

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            _establish_trust(subscriber.db, origin.identity.fingerprint)
            origin_hello = origin.link_node.build_hello(
                addresses=[
                    {"protocol": LINK_REALTIME_PROTOCOL_TAG, "address": "127.0.0.1", "port": server.port}
                ],
                outgoing_only=False, created_at=utc_now_iso(),
            )
            subscriber.link_node.handle_hello(origin_hello)

            result = await ensure_live_subscription(
                channel=subscriber_channel, node_identity=subscriber.identity, link_node=subscriber.link_node,
                lane=subscriber.lane, registry=subscriber.registry, bridge=subscriber.bridge,
            )
            assert result is not None
            session, _request_id = result

            assert await _wait_until(
                lambda: subscriber.bridge.remote_channel_presence(subscriber_channel.channel_id) == {"dave": "dave"}
            )
            assert (
                subscriber.bridge.remote_channel_origin_fingerprint(subscriber_channel.channel_id)
                == origin.identity.fingerprint
            )

            # A subsequent local join/leave on the origin (deltas, not a
            # fresh snapshot) keeps the same tracked roster in sync.
            await origin.bridge.broadcast_local_presence_live(origin_channel, change="join", username="erin")
            assert await _wait_until(
                lambda: subscriber.bridge.remote_channel_presence(subscriber_channel.channel_id) ==
                {"dave": "dave", "erin": "erin"}
            )

            await origin.bridge.broadcast_local_presence_live(origin_channel, change="leave", username="dave")
            assert await _wait_until(
                lambda: subscriber.bridge.remote_channel_presence(subscriber_channel.channel_id) == {"erin": "erin"}
            )

            # Closing the session clears that channel's remote roster --
            # stale data from a now-disconnected origin must not linger.
            await session.close(reason="test_done")
            assert await _wait_until(
                lambda: subscriber.bridge.remote_channel_presence(subscriber_channel.channel_id) == {}
            )
            assert subscriber.bridge.remote_channel_origin_fingerprint(subscriber_channel.channel_id) is None
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


# -- scrollback-on-join (issue #194) ------------------------------------


def _snapshot_entry(*, author_node: str, author_user: str = "alice", body: str = "hello") -> dict:
    return {
        "kind": "message",
        "author_label": f"{author_user}@{author_node}",
        "author_node_fingerprint": author_node,
        "author_user_id": author_user,
        "content_id": None,
        "body": body,
        "body_truncated": False,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


class _SnapshotSession:
    def __init__(self, remote_fingerprint: str) -> None:
        self.remote_fingerprint = remote_fingerprint


def test_scrollback_snapshot_rejects_a_trusted_non_origin_sender(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-only-source")
        subscriber = _Node(tmp_path, "subscriber-only-source")
        _origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="origin-bound")
        attacker = bootstrap_node_identity("trusted-non-origin")
        _establish_trust(subscriber.db, attacker.fingerprint)
        request_id = subscriber.bridge.begin_scrollback_request(
            subscriber_channel.channel_id, attacker.fingerprint
        )
        frame = build_scrollback_snapshot_frame(
            subscriber_channel.channel_id, request_id,
            [_snapshot_entry(author_node=attacker.fingerprint)],
        )
        try:
            with pytest.raises(LinkProtocolError, match="current origin"):
                await subscriber.bridge._handle_scrollback_snapshot(
                    _SnapshotSession(attacker.fingerprint), frame
                )
        finally:
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_scrollback_snapshot_applies_the_subscribers_author_trust_policy(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-subscriber-policy")
        subscriber = _Node(tmp_path, "subscriber-own-policy")
        _origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="trust-filtered")
        _establish_trust(subscriber.db, origin.identity.fingerprint)
        blocked_author = bootstrap_node_identity("blocked-author").fingerprint
        ensure_node_subject(subscriber.db, blocked_author)
        set_trust_override(
            subscriber.db, TrustSubject.node(blocked_author), TrustDimension.IDENTITY_INTEGRITY,
            TrustState.BLOCKED, reason="blocked for test", now_iso="2026-09-02T00:00:00Z",
        )
        request_id = subscriber.bridge.begin_scrollback_request(
            subscriber_channel.channel_id, origin.identity.fingerprint
        )
        frame = build_scrollback_snapshot_frame(
            subscriber_channel.channel_id, request_id,
            [_snapshot_entry(author_node=blocked_author)],
        )
        try:
            await subscriber.bridge._handle_scrollback_snapshot(
                _SnapshotSession(origin.identity.fingerprint), frame
            )
            assert subscriber.bridge.pop_channel_scrollback(
                subscriber_channel.channel_id, request_id
            ) == []
        finally:
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_late_scrollback_snapshot_cannot_fill_a_later_subscribe_attempt(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-late-snapshot")
        subscriber = _Node(tmp_path, "subscriber-late-snapshot")
        _origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="late-snapshot")
        _establish_trust(subscriber.db, origin.identity.fingerprint)
        old_request = subscriber.bridge.begin_scrollback_request(
            subscriber_channel.channel_id, origin.identity.fingerprint
        )
        subscriber.bridge.finish_scrollback_request(old_request)
        new_request = subscriber.bridge.begin_scrollback_request(
            subscriber_channel.channel_id, origin.identity.fingerprint
        )
        old_frame = build_scrollback_snapshot_frame(
            subscriber_channel.channel_id, old_request,
            [_snapshot_entry(author_node=origin.identity.fingerprint, body="stale")],
        )
        try:
            await subscriber.bridge._handle_scrollback_snapshot(
                _SnapshotSession(origin.identity.fingerprint), old_frame
            )
            assert subscriber.bridge.pop_channel_scrollback(
                subscriber_channel.channel_id, new_request
            ) == []
        finally:
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_scrollback_snapshot_is_delivered_once_from_the_origins_recent_scrollback(tmp_path):
    """Design doc §16, issue #194 Decision 1/2: a `scrollback_snapshot`
    arrives as a sibling of the presence_snapshot `test_remote_channel_
    presence_is_populated_from_the_origins_initial_snapshot` above already
    proves, sourced from the origin's own `get_scrollback`, with origin-
    local labels qualified by the authenticated origin and `author_
    fingerprint` still `None` on the receiving side (never treated as a
    locally verified account) -- and popped exactly once."""
    async def scenario():
        origin = _Node(tmp_path, "origin-scrollback")
        subscriber = _Node(tmp_path, "subscriber-scrollback")
        origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="history-room")

        first = record_message(
            origin.db, origin_channel, kind="message", author_label="alice", body="first message"
        )
        first_event = queue_channel_message_if_linked(
            origin.db, first, origin_channel, node_identity=origin.identity
        )
        record_message(origin.db, origin_channel, kind="join", author_label="bob")
        second = record_message(
            origin.db, origin_channel, kind="message", author_label="alice", body="second message"
        )
        second_event = queue_channel_message_if_linked(
            origin.db, second, origin_channel, node_identity=origin.identity
        )

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            _establish_trust(subscriber.db, origin.identity.fingerprint)
            origin_hello = origin.link_node.build_hello(
                addresses=[
                    {"protocol": LINK_REALTIME_PROTOCOL_TAG, "address": "127.0.0.1", "port": server.port}
                ],
                outgoing_only=False, created_at=utc_now_iso(),
            )
            subscriber.link_node.handle_hello(origin_hello)

            result = await ensure_live_subscription(
                channel=subscriber_channel, node_identity=subscriber.identity, link_node=subscriber.link_node,
                lane=subscriber.lane, registry=subscriber.registry, bridge=subscriber.bridge,
            )
            assert result is not None
            session, request_id = result

            assert await _wait_until(
                lambda: request_id in subscriber.bridge._remote_channel_scrollback
            )
            entries = subscriber.bridge.pop_channel_scrollback(subscriber_channel.channel_id, request_id)
            assert [(entry.kind, entry.author_label, entry.body) for entry in entries] == [
                ("message", f"alice@{origin.identity.fingerprint}", "first message"),
                ("join", f"bob@{origin.identity.fingerprint}", None),
                ("message", f"alice@{origin.identity.fingerprint}", "second message"),
            ]
            assert all(entry.id == -1 for entry in entries)
            assert all(entry.author_fingerprint is None for entry in entries)
            assert [entry.link_content_id for entry in entries] == [
                first_event.content_id, None, second_event.content_id,
            ]

            # One-time pickup -- a second pop for the same channel is empty,
            # not a repeat of the same snapshot.
            assert subscriber.bridge.pop_channel_scrollback(subscriber_channel.channel_id, request_id) == []
            subscriber.bridge.finish_scrollback_request(request_id)
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_scrollback_snapshot_caps_at_the_most_recent_entries(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-scrollback-cap")
        subscriber = _Node(tmp_path, "subscriber-scrollback-cap")
        origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="chatty-room")

        total = _MAX_SCROLLBACK_SNAPSHOT_ENTRIES + 5
        for index in range(total):
            record_message(origin.db, origin_channel, kind="message", author_label="alice", body=f"message {index}")

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            _establish_trust(subscriber.db, origin.identity.fingerprint)
            origin_hello = origin.link_node.build_hello(
                addresses=[
                    {"protocol": LINK_REALTIME_PROTOCOL_TAG, "address": "127.0.0.1", "port": server.port}
                ],
                outgoing_only=False, created_at=utc_now_iso(),
            )
            subscriber.link_node.handle_hello(origin_hello)

            result = await ensure_live_subscription(
                channel=subscriber_channel, node_identity=subscriber.identity, link_node=subscriber.link_node,
                lane=subscriber.lane, registry=subscriber.registry, bridge=subscriber.bridge,
            )
            assert result is not None
            session, request_id = result

            assert await _wait_until(
                lambda: request_id in subscriber.bridge._remote_channel_scrollback
            )
            entries = subscriber.bridge.pop_channel_scrollback(subscriber_channel.channel_id, request_id)
            assert len(entries) == _MAX_SCROLLBACK_SNAPSHOT_ENTRIES
            # Most-recent bias: the tail of what was sent matches the tail
            # of what was recorded, not the oldest entries.
            assert entries[-1].body == f"message {total - 1}"
            assert entries[0].body == f"message {total - _MAX_SCROLLBACK_SNAPSHOT_ENTRIES}"
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_no_scrollback_snapshot_is_sent_for_a_channel_with_empty_local_scrollback(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-scrollback-empty")
        subscriber = _Node(tmp_path, "subscriber-scrollback-empty")
        origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="quiet-room")
        assert origin_channel  # never posted to -- empty local scrollback

        received: list[RealtimeFrame] = []

        async def on_frame_subscriber(session, frame):
            received.append(frame)

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            session = await dial_realtime_session(
                "127.0.0.1", server.port, subscriber.identity, on_frame=on_frame_subscriber,
                registry=subscriber.registry,
            )
            await session.send(build_subscribe_frame(subscriber_channel.channel_id))
            assert await _wait_until(lambda: any(frame.type == "presence_snapshot" for frame in received))
            await asyncio.sleep(0.1)  # give a wrongly-sent snapshot a chance to arrive
            assert not any(frame.type == "scrollback_snapshot" for frame in received)
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())


def test_scrollback_snapshot_is_cleared_on_disconnect_before_pickup(tmp_path):
    async def scenario():
        origin = _Node(tmp_path, "origin-scrollback-disconnect")
        subscriber = _Node(tmp_path, "subscriber-scrollback-disconnect")
        origin_channel, subscriber_channel = _setup_linked_channel(origin, subscriber, name="fleeting-room")
        record_message(origin.db, origin_channel, kind="message", author_label="alice", body="hello")

        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=origin.identity, registry=origin.registry,
            on_frame=origin.bridge.on_frame, lane=origin.lane, enforce_trust_policy=True,
        )
        await server.start()
        try:
            _establish_trust(origin.db, subscriber.identity.fingerprint)
            # Subscriber's own _handle_scrollback_snapshot re-checks
            # authorization against *its own* db for the sending peer
            # (origin) -- same requirement _handle_channel_message/
            # _handle_presence_delta already have (see
            # test_live_channel_message_and_presence_reach_a_locally_
            # connected_participant above), needs origin trusted there
            # too, not just origin trusting subscriber.
            _establish_trust(subscriber.db, origin.identity.fingerprint)
            session = await dial_realtime_session(
                "127.0.0.1", server.port, subscriber.identity, on_frame=subscriber.bridge.on_frame,
                registry=subscriber.registry,
            )
            # ensure_live_subscription (the real production path) always
            # calls this itself right after dialing -- needed here too,
            # since this test dials directly, for subscriber.bridge's own
            # _untrack_on_close watcher to actually be watching this
            # session at all.
            await subscriber.bridge.track_session(session)
            request_id = subscriber.bridge.begin_scrollback_request(
                subscriber_channel.channel_id, origin.identity.fingerprint
            )
            await session.send(build_subscribe_frame(subscriber_channel.channel_id, message_id=request_id))
            assert await _wait_until(
                lambda: request_id in subscriber.bridge._remote_channel_scrollback
            )

            await session.close(reason="test_done_before_pickup")

            assert await _wait_until(
                lambda: request_id not in subscriber.bridge._remote_channel_scrollback
            )
            assert subscriber.bridge.pop_channel_scrollback(subscriber_channel.channel_id, request_id) == []
        finally:
            await server.stop()
            await origin.teardown()
            await subscriber.teardown()

    asyncio.run(scenario())
