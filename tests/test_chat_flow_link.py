"""
Tests for wiring linked-channel messages into the live interactive chat
send path (design doc, issue #91) -- `netbbs.net.chat_flow._chat_loop`
queuing a `channel_message` Link event for a self-authored message sent
in a Linked channel, mirroring `netbbs.net.board_flow._compose_new_post`'s
own `queue_board_post_if_linked` call exactly.

Driven through the real `_chat_loop` dispatcher, same harness
`test_chat_flow_join.py` already established (`FakeSession` borrowed from
test_chat_flow_moderation.py).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.scrollback import ChannelMessage as LocalChannelMessage
from netbbs.chat.scrollback import get_scrollback
from netbbs.link.boards import LinkContext
from netbbs.link.channels import link_channel, materialize_carried_channel
from netbbs.link.enforcement import ensure_node_subject
from netbbs.link.events import ChannelMessage
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode, RealtimeFrame
from netbbs.link.realtime_channels import LiveChannelBridge
from netbbs.link.transport import LinkRealtimeServer, LinkRealtimeSessionRegistry, dial_realtime_session
from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, set_trust_override
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_chat_flow_moderation import FakeSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def hub():
    return ChatHub()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=100)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, sysop):
    return create_channel(db, "lobby", creator=sysop)


@pytest.fixture
def node_identity():
    return bootstrap_node_identity("thisnode")


def _link_context_for(node_identity, *, registry=None, bridge=None) -> LinkContext:
    return LinkContext(
        node_identity=node_identity, link_node=LinkNode(identity=node_identity),
        realtime_registry=registry, realtime_bridge=bridge,
    )


def _fake_live_result(kwargs, session):
    request_id = kwargs["bridge"].begin_scrollback_request(
        kwargs["channel"].channel_id, "fake-origin"
    )
    return session, request_id


def _establish_trust(db: Database, fingerprint: str) -> None:
    ensure_node_subject(db, fingerprint)
    subject = TrustSubject.node(fingerprint)
    for dimension in (TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR):
        set_trust_override(
            db, subject, dimension, TrustState.ESTABLISHED,
            reason="pre-established for test", now_iso="2026-08-14T12:00:00+00:00",
        )


async def _run(lane, hub, presence, channel, user, lines, *, link_context=None):
    session = FakeSession(lines)
    mailbox = MessageMailbox()
    history = InputHistory()
    action = await asyncio.wait_for(
        chat_flow._chat_loop(
            session, lane, hub, presence, mailbox, history, channel, user, link_context=link_context
        ),
        timeout=2,
    )
    return session, action


def test_message_sent_in_a_linked_channel_queues_a_channel_message(db, lane, hub, presence, channel, alice, node_identity):
    link_channel(db, channel, node_identity=node_identity)
    link_context = _link_context_for(node_identity)

    asyncio.run(_run(lane, hub, presence, channel, alice, ["hello there", "/quit"], link_context=link_context))

    row = db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE channel_id = ? AND kind = 'message' ORDER BY id DESC LIMIT 1", (channel.id,)
    ).fetchone()
    assert row["link_event_json"] is not None
    event = ChannelMessage.from_dict(json.loads(row["link_event_json"]))
    assert event.payload["body"] == "hello there"
    assert event.payload["channel_id"] == channel.channel_id


def test_message_sent_in_an_unlinked_channel_behaves_exactly_as_before(db, lane, hub, presence, channel, alice, node_identity):
    link_context = _link_context_for(node_identity)  # a real link_context, but the channel itself isn't Linked

    asyncio.run(_run(lane, hub, presence, channel, alice, ["hello there", "/quit"], link_context=link_context))

    scrollback = get_scrollback(db, channel)
    assert [m.body for m in scrollback if m.kind == "message"] == ["hello there"]
    row = db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE channel_id = ? AND kind = 'message' ORDER BY id DESC LIMIT 1", (channel.id,)
    ).fetchone()
    assert row["link_event_json"] is None


def test_link_disabled_node_sends_exactly_as_before(db, lane, hub, presence, channel, alice, node_identity):
    """link_context=None (Link disabled on this node, or a caller that
    bypasses handle_session's real Link wiring) -- local chat stays fully
    usable and Link-unaware, same as before this issue."""
    link_channel(db, channel, node_identity=node_identity)  # Linked, but this session has no link_context at all

    session, action = asyncio.run(_run(lane, hub, presence, channel, alice, ["hello there", "/quit"]))

    scrollback = get_scrollback(db, channel)
    assert [m.body for m in scrollback if m.kind == "message"] == ["hello there"]
    row = db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE channel_id = ? AND kind = 'message' ORDER BY id DESC LIMIT 1", (channel.id,)
    ).fetchone()
    assert row["link_event_json"] is None


def test_repeated_send_in_a_linked_channel_is_idempotent_per_message(
    db, lane, hub, presence, channel, alice, node_identity
):
    """Each sent message gets its own queued event, keyed on that
    message's own row -- sending a second, different message doesn't
    disturb the first's already-queued event."""
    link_channel(db, channel, node_identity=node_identity)
    link_context = _link_context_for(node_identity)

    asyncio.run(
        _run(lane, hub, presence, channel, alice, ["first message", "second message", "/quit"], link_context=link_context)
    )

    rows = db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE channel_id = ? AND kind = 'message' ORDER BY id ASC", (channel.id,)
    ).fetchall()
    assert len(rows) == 2
    assert all(row["link_event_json"] is not None for row in rows)
    bodies = [ChannelMessage.from_dict(json.loads(row["link_event_json"])).payload["body"] for row in rows]
    assert bodies == ["first message", "second message"]


def test_message_join_and_leave_in_a_linked_channel_are_pushed_live_to_a_real_subscriber(
    db, lane, hub, presence, channel, alice, node_identity, tmp_path
):
    """Design doc §8.10.2, issue #148: the *outbound* half of the live
    wiring -- `_chat_loop`'s own join/send/leave call sites pushing to
    `LiveChannelBridge`, proven against a real dialed `LinkRealtimeSession`
    subscriber, not just the bridge's own unit tests."""
    async def scenario():
        link_channel(db, channel, node_identity=node_identity)
        origin_registry = LinkRealtimeSessionRegistry(own_fingerprint=node_identity.fingerprint)
        origin_bridge = LiveChannelBridge(hub=hub, lane=lane, presence=presence, registry=origin_registry)
        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=node_identity, registry=origin_registry,
            on_frame=origin_bridge.on_frame, lane=lane, enforce_trust_policy=True,
        )
        await server.start()
        subscriber_db = Database(tmp_path / "subscriber.db")
        subscriber_lane = DatabaseLane(subscriber_db.path)
        try:
            subscriber_identity = bootstrap_node_identity("live-subscriber")
            _establish_trust(db, subscriber_identity.fingerprint)
            _establish_trust(subscriber_db, node_identity.fingerprint)

            received: list[RealtimeFrame] = []

            async def on_frame(session, frame):
                received.append(frame)

            subscriber_registry = LinkRealtimeSessionRegistry(own_fingerprint=subscriber_identity.fingerprint)
            subscriber_session = await dial_realtime_session(
                "127.0.0.1", server.port, subscriber_identity, on_frame=on_frame, registry=subscriber_registry,
            )
            try:
                from netbbs.link.protocol import build_subscribe_frame

                await subscriber_session.send(build_subscribe_frame(channel.channel_id))

                loop = asyncio.get_running_loop()
                deadline = loop.time() + 2.0
                while loop.time() < deadline and channel.channel_id not in origin_bridge._subscribers:
                    await asyncio.sleep(0.02)
                assert channel.channel_id in origin_bridge._subscribers

                link_context = _link_context_for(node_identity, registry=origin_registry, bridge=origin_bridge)
                await _run(
                    lane, hub, presence, channel, alice, ["hello subscribers", "/quit"],
                    link_context=link_context,
                )

                deadline = loop.time() + 2.0
                while loop.time() < deadline and len(received) < 5:
                    await asyncio.sleep(0.02)
            finally:
                await subscriber_session.close(reason="test_done")
        finally:
            await origin_registry.close_all(reason="test_done")
            await origin_bridge.close()
            await server.stop()
            subscriber_lane.close()
            subscriber_db.close()
        return received

    received = asyncio.run(scenario())

    assert [frame.type for frame in received] == [
        # Issue #164: node_presence_snapshot is now sent proactively the
        # instant the origin's bridge starts tracking the subscriber's
        # inbound session (track_session), before the channel-scoped
        # presence_snapshot that _handle_subscribe sends right after.
        "node_presence_snapshot", "presence_snapshot", "presence_delta", "channel_message", "presence_delta",
    ]
    _node_snapshot, _snapshot, join_delta, message_frame, leave_delta = received
    assert join_delta.payload == {
        "channel_id": channel.channel_id, "change": "join", "user_id": "alice", "display_label": "alice",
    }
    assert message_frame.payload["channel_id"] == channel.channel_id
    assert message_frame.payload["user_id"] == "alice"
    assert message_frame.payload["body"] == "hello subscribers"
    assert leave_delta.payload == {
        "channel_id": channel.channel_id, "change": "leave", "user_id": "alice", "display_label": "alice",
    }


def test_chat_loop_subscribes_to_a_linked_channels_origin_and_unsubscribes_on_quit(
    db, lane, hub, presence, channel, alice, node_identity, monkeypatch
):
    """The *inbound*-subscribe half of the wiring: `_chat_loop` calls
    `ensure_live_subscription` on join and sends `unsubscribe` on the way
    out. Monkeypatched rather than dialed over a real socket -- the dial/
    subscribe mechanics themselves are already proven in `tests/test_
    link_realtime_channels.py`; this test's only job is confirming
    `_chat_loop` actually invokes that wiring with the right channel and
    cleans it up on exit, without a real-network race against a
    background task racing a scripted FakeSession's own near-instant
    `/quit`."""
    link_channel(db, channel, node_identity=node_identity)  # this node is the origin

    calls: list[dict] = []
    sent_frames: list = []

    class _FakeSession:
        def __init__(self):
            # A real Event, left unset -- `_subscribe_live` awaits
            # `closed.wait()` for as long as the session stays up,
            # exactly like a genuine `LinkRealtimeSession` would.
            self.closed = asyncio.Event()

        async def send(self, frame):
            sent_frames.append(frame)

    async def fake_ensure_live_subscription(**kwargs):
        calls.append(kwargs)
        return _fake_live_result(kwargs, _FakeSession())

    monkeypatch.setattr(
        "netbbs.link.realtime_channels.ensure_live_subscription", fake_ensure_live_subscription
    )

    registry = LinkRealtimeSessionRegistry(own_fingerprint=node_identity.fingerprint)
    bridge = LiveChannelBridge(hub=hub, lane=lane, presence=presence, registry=registry)
    link_context = _link_context_for(node_identity, registry=registry, bridge=bridge)

    asyncio.run(_run(lane, hub, presence, channel, alice, ["/quit"], link_context=link_context))

    assert len(calls) == 1
    assert calls[0]["channel"] is channel
    assert calls[0]["node_identity"] is node_identity
    assert calls[0]["registry"] is registry
    assert calls[0]["bridge"] is bridge

    from netbbs.link.protocol import RealtimeFrame as _RealtimeFrame

    assert len(sent_frames) == 1
    assert isinstance(sent_frames[0], _RealtimeFrame)
    assert sent_frames[0].type == "unsubscribe"
    assert sent_frames[0].payload == {"channel_id": channel.channel_id}


def test_a_second_local_caller_still_watching_keeps_the_origin_subscription_alive_when_the_first_leaves(
    db, lane, hub, presence, channel, alice, node_identity, monkeypatch
):
    """Issue #159: the live subscription to a linked channel's origin is
    a node-level resource (`LiveChannelBridge.register_local_interest`/
    `release_local_interest`), shared by every local caller currently
    interested in that channel -- not owned by whichever one view
    happens to leave first. Without this reference counting, the sibling
    test above's own single-caller `unsubscribe`-on-quit behavior would
    be wrong the moment a *second* local caller is also relying on the
    same feed: the first to `/quit` would send `unsubscribe`
    unconditionally and silently cut off live delivery for the other."""
    link_channel(db, channel, node_identity=node_identity)
    bob = create_user(db, "bob", password="hunter2", user_level=10)

    calls: list[dict] = []
    sent_frames: list = []

    class _FakeLiveSession:
        def __init__(self):
            self.closed = asyncio.Event()

        async def send(self, frame):
            sent_frames.append(frame)

    shared_live_session = _FakeLiveSession()

    async def fake_ensure_live_subscription(**kwargs):
        calls.append(kwargs)
        return _fake_live_result(kwargs, shared_live_session)

    monkeypatch.setattr(
        "netbbs.link.realtime_channels.ensure_live_subscription", fake_ensure_live_subscription
    )

    registry = LinkRealtimeSessionRegistry(own_fingerprint=node_identity.fingerprint)
    bridge = LiveChannelBridge(hub=hub, lane=lane, presence=presence, registry=registry)
    link_context = _link_context_for(node_identity, registry=registry, bridge=bridge)

    async def scenario():
        # Bob joins first and stays -- no scripted `/quit`, so his
        # FakeSession blocks forever once its (empty) line list runs
        # out, same "still genuinely connected" shape every other test
        # here relies on.
        bob_task = asyncio.create_task(
            _run(lane, hub, presence, channel, bob, [], link_context=link_context)
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while loop.time() < deadline and len(calls) < 1:
            await asyncio.sleep(0.02)
        assert len(calls) == 1  # bob is now live-subscribed and interest-registered

        # Alice joins and immediately leaves -- her own `/quit` must NOT
        # unsubscribe the shared origin session while bob is still here.
        await _run(lane, hub, presence, channel, alice, ["/quit"], link_context=link_context)
        assert sent_frames == []

        # Bob disconnects too, now the *last* local holder -- this must
        # unsubscribe.
        bob_task.cancel()
        await asyncio.gather(bob_task, return_exceptions=True)
        assert len(sent_frames) == 1
        assert sent_frames[0].type == "unsubscribe"
        assert sent_frames[0].payload == {"channel_id": channel.channel_id}

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_chat_loop_announces_the_real_time_link_coming_up_and_going_down(
    db, lane, hub, presence, channel, alice, node_identity, monkeypatch
):
    """Design doc §8.10.2: the caller sees connecting/live/offline
    state, honestly -- monkeypatched (see the sibling test above for
    why) with a fake session whose `closed` event the test controls
    directly, so both the "is up" and "was lost" announcements are
    deterministic rather than racing a real network dial."""
    link_channel(db, channel, node_identity=node_identity)

    class _FakeSession:
        def __init__(self):
            self.closed = asyncio.Event()

        async def send(self, frame):
            pass

    fake_session = _FakeSession()

    async def fake_ensure_live_subscription(**kwargs):
        return _fake_live_result(kwargs, fake_session)

    monkeypatch.setattr(
        "netbbs.link.realtime_channels.ensure_live_subscription", fake_ensure_live_subscription
    )

    registry = LinkRealtimeSessionRegistry(own_fingerprint=node_identity.fingerprint)
    bridge = LiveChannelBridge(hub=hub, lane=lane, presence=presence, registry=registry)
    link_context = _link_context_for(node_identity, registry=registry, bridge=bridge)

    session, _action = asyncio.run(
        _run(lane, hub, presence, channel, alice, ["hello", "/quit"], link_context=link_context)
    )

    written = "\n".join(session.written)
    assert "Connecting to this channel's real-time origin" in written
    assert "Real-time link to this channel's origin is up" in written


def test_chat_loop_announces_a_lost_real_time_link_while_still_in_the_channel(
    db, lane, hub, presence, channel, alice, node_identity, monkeypatch
):
    link_channel(db, channel, node_identity=node_identity)

    class _FakeSession:
        def __init__(self):
            self.closed = asyncio.Event()

        async def send(self, frame):
            pass

    fake_session = _FakeSession()

    async def fake_ensure_live_subscription(**kwargs):
        return _fake_live_result(kwargs, fake_session)

    monkeypatch.setattr(
        "netbbs.link.realtime_channels.ensure_live_subscription", fake_ensure_live_subscription
    )

    registry = LinkRealtimeSessionRegistry(own_fingerprint=node_identity.fingerprint)
    bridge = LiveChannelBridge(hub=hub, lane=lane, presence=presence, registry=registry)
    link_context = _link_context_for(node_identity, registry=registry, bridge=bridge)

    async def scenario():
        session = FakeSession([])  # blocks on read_line forever -- driven manually below
        mailbox = MessageMailbox()
        history = InputHistory()
        task = asyncio.create_task(
            chat_flow._chat_loop(
                session, lane, hub, presence, mailbox, history, channel, alice, link_context=link_context
            )
        )
        # Let the announcer task actually run and reach `closed.wait()`.
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline and not any(
            "is up" in line for line in session.written
        ):
            await asyncio.sleep(0.01)
        assert any("is up" in line for line in session.written)

        fake_session.closed.set()

        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline and not any(
            "was lost" in line for line in session.written
        ):
            await asyncio.sleep(0.01)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return session

    session = asyncio.run(scenario())

    written = "\n".join(session.written)
    assert "was lost" in written


def test_chat_loop_renders_a_pending_remote_scrollback_snapshot_once_live_comes_up(
    db, lane, hub, presence, channel, alice, node_identity, monkeypatch
):
    """Issue #194: `_subscribe_live` picks up whatever `LiveChannelBridge.
    pop_channel_scrollback` already has waiting for this channel right
    after the LIVE badge, renders it once, and pops it -- proven here at
    the `_chat_loop` level (the bridge's own send/receive mechanics are
    already covered end-to-end in tests/test_link_realtime_channels.py).
    Pre-populated directly rather than sent over a real/fake session,
    same "reach into the bridge's own state" convention `test_local_
    interest_reference_counts_holders_per_channel` already uses -- the
    poll's first attempt finds it immediately, so this doesn't race
    the scripted `/quit`'s own near-instant cleanup."""
    link_channel(db, channel, node_identity=node_identity)

    class _FakeSession:
        def __init__(self):
            self.closed = asyncio.Event()

        async def send(self, frame):
            pass

    async def fake_ensure_live_subscription(**kwargs):
        result = _fake_live_result(kwargs, _FakeSession())
        _session, request_id = result
        kwargs["bridge"]._remote_channel_scrollback[request_id] = [
            LocalChannelMessage(
                id=-1, channel_id=channel.id, kind="message", author_label="remote-alice@origin",
                author_fingerprint=None, body="catch me up", created_at="2026-01-01T00:00:00+00:00",
            ),
        ]
        return result

    monkeypatch.setattr(
        "netbbs.link.realtime_channels.ensure_live_subscription", fake_ensure_live_subscription
    )

    registry = LinkRealtimeSessionRegistry(own_fingerprint=node_identity.fingerprint)
    bridge = LiveChannelBridge(hub=hub, lane=lane, presence=presence, registry=registry)
    link_context = _link_context_for(node_identity, registry=registry, bridge=bridge)

    session, _action = asyncio.run(
        _run(lane, hub, presence, channel, alice, ["hello"] * 10 + ["/quit"], link_context=link_context)
    )

    written = "\n".join(session.written)
    assert "Real-time link to this channel's origin is up" in written
    assert "Recent activity from this channel's origin" in written
    assert "remote-alice@origin" in written
    assert "catch me up" in written
    # Popped, not merely read -- nothing left pending afterward.
    assert bridge._remote_channel_scrollback == {}


def test_chat_loop_shows_no_scrollback_catch_up_when_nothing_is_pending(
    db, lane, hub, presence, channel, alice, node_identity, monkeypatch
):
    """The ordinary case -- an empty local scrollback at the origin means
    `_handle_subscribe` never sends a `scrollback_snapshot` frame at all
    (tests/test_link_realtime_channels.py::test_no_scrollback_snapshot_
    is_sent_for_a_channel_with_empty_local_scrollback), so nothing is
    ever pending here to pop -- the catch-up section must not appear."""
    link_channel(db, channel, node_identity=node_identity)

    class _FakeSession:
        def __init__(self):
            self.closed = asyncio.Event()

        async def send(self, frame):
            pass

    async def fake_ensure_live_subscription(**kwargs):
        return _fake_live_result(kwargs, _FakeSession())

    monkeypatch.setattr(
        "netbbs.link.realtime_channels.ensure_live_subscription", fake_ensure_live_subscription
    )

    registry = LinkRealtimeSessionRegistry(own_fingerprint=node_identity.fingerprint)
    bridge = LiveChannelBridge(hub=hub, lane=lane, presence=presence, registry=registry)
    link_context = _link_context_for(node_identity, registry=registry, bridge=bridge)

    session, _action = asyncio.run(
        _run(lane, hub, presence, channel, alice, ["hello", "/quit"], link_context=link_context)
    )

    written = "\n".join(session.written)
    assert "Real-time link to this channel's origin is up" in written
    assert "Recent activity from this channel's origin" not in written


def test_remote_scrollback_suppresses_entries_already_rendered_from_local_history(
    lane, hub, presence, channel, alice, node_identity
):
    async def scenario():
        registry = LinkRealtimeSessionRegistry(own_fingerprint=node_identity.fingerprint)
        bridge = LiveChannelBridge(hub=hub, lane=lane, presence=presence, registry=registry)
        request_id = bridge.begin_scrollback_request(channel.channel_id, "origin")
        bridge._remote_channel_scrollback[request_id] = [
            LocalChannelMessage(
                id=-1, channel_id=channel.id, kind="message", author_label="alice@origin",
                author_fingerprint=None, body="already here", created_at="2026-01-01T00:00:00+00:00",
                link_content_id="content-1",
            )
        ]
        delivered = []

        async def deliver(line):
            delivered.append(line)

        await chat_flow._deliver_remote_scrollback_snapshot(
            deliver, lane, channel, alice, bridge, request_id, {"content-1"},
            unicode_style=False, truecolor=False, terminal_width=80,
        )
        assert delivered == []
        assert request_id not in bridge._pending_scrollback_requests

    asyncio.run(scenario())


def test_remote_scrollback_marks_truncated_bodies_visibly(
    lane, hub, presence, channel, alice, node_identity
):
    async def scenario():
        registry = LinkRealtimeSessionRegistry(own_fingerprint=node_identity.fingerprint)
        bridge = LiveChannelBridge(hub=hub, lane=lane, presence=presence, registry=registry)
        request_id = bridge.begin_scrollback_request(channel.channel_id, "origin")
        bridge._remote_channel_scrollback[request_id] = [
            LocalChannelMessage(
                id=-1, channel_id=channel.id, kind="action", author_label="alice@origin",
                author_fingerprint=None, body="partial sentence", created_at="2026-01-01T00:00:00+00:00",
                body_truncated=True,
            )
        ]
        delivered = []

        async def deliver(line):
            delivered.append(line)

        await chat_flow._deliver_remote_scrollback_snapshot(
            deliver, lane, channel, alice, bridge, request_id, set(),
            unicode_style=False, truecolor=False, terminal_width=80,
        )
        rendered = "\n".join(delivered)
        assert "truncated in join snapshot" in rendered
        assert "full history" not in rendered

    asyncio.run(scenario())
