"""Real SQLite, Baud subprocesses and authenticated loopback Link sessions."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from netbbs.auth.users import create_user
from netbbs.bots.link import BotLink
from netbbs.bots.registry import BotError, allowed_channel, save_bot, stop_bots
from netbbs.bots.runtime import BotRuntime
from netbbs.bots.state import read_state
from netbbs.chat.bots import bot_message_visible, mute_remote_bot, set_channel_bot_policy
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.link.channels import materialize_carried_channel, materialize_carried_channel_message
from netbbs.link.events import ChannelGenesis, ChannelMessage, CHAT_BOTS_CAPABILITY, LINK_CAPABILITIES
from netbbs.link.protocol import LinkProtocolError, build_bot_frame, build_channel_message_frame, build_subscribe_frame
from netbbs.link.realtime_direct import LiveDirectChat
from netbbs.link.transport import LINK_REALTIME_PROTOCOL_TAG, LinkRealtimeServer
from netbbs.net.chat_flow import _address_bot
from netbbs.timeutil import utc_now_iso
from tests.test_bot_runtime import eventually, next_line
from tests.test_link_realtime_channels import _Node, _establish_trust, _setup_linked_channel


class Node(_Node):
    def __init__(self, path, name):
        super().__init__(path, name)
        self.sysop = create_user(self.db, "sysop", password="hunter2", user_level=255)
        self.alice = create_user(self.db, "alice", password="hunter2", user_level=10)
        self.runtime = BotRuntime(self.lane, self.hub, poll_seconds=.03)
        self.direct = LiveDirectChat(link_node=self.link_node, lane=self.lane, registry=self.registry,
                                     on_frame=self.bridge.on_frame, track_session=self.bridge.track_session,
                                     relay_client=None, deliver=None)
        self.bots = BotLink(self.runtime, self.link_node, self.bridge, self.direct, self.registry)
        self.bridge._peer_capable = lambda fp: True
        self.server = LinkRealtimeServer(host="127.0.0.1", port=0, identity=self.identity, registry=self.registry,
                                         on_frame=self.bridge.on_frame, lane=self.lane, enforce_trust_policy=True)

    async def close(self):
        await self.runtime.close()
        await self.server.stop()
        await self.teardown()

    def install(self, channel, *, approved=True):
        return save_bot(self.db, actor=self.sysop, name="Baud", channels=[channel.name], defaults=[channel.name],
                        linked_channels=[channel.name] if approved else [], enabled=True)

    async def address(self, channel, text):
        line = record_message(self.db, channel, kind="message", author_label="alice", body=text)
        ctx = SimpleNamespace(link_node=self.link_node, realtime_registry=self.registry)
        return await _address_bot(self.runtime, channel, self.alice, line, ParticipantId("alice", 1), ctx)


async def connect(nodes):
    for node in nodes:
        await node.server.start()
    for node in nodes:
        for peer in nodes:
            if peer is node:
                continue
            _establish_trust(node.db, peer.identity.fingerprint)
            node.link_node.handle_hello(peer.link_node.build_hello(
                addresses=[{"protocol": LINK_REALTIME_PROTOCOL_TAG, "address": "127.0.0.1", "port": peer.server.port}],
                outgoing_only=False, created_at=utc_now_iso()))
        node.runtime.start()


def test_three_nodes_local_shorthand_remote_address_presence_mute_replay_and_stop(tmp_path):
    async def run():
        origin, first, second = [Node(tmp_path, n) for n in ("origin", "first", "second")]
        oc, fc = _setup_linked_channel(origin, first)
        genesis = origin.db.connection.execute("SELECT link_genesis_json FROM channels WHERE id=?", (oc.id,)).fetchone()[0]
        sc = materialize_carried_channel(second.db, ChannelGenesis.from_dict(json.loads(genesis)))
        ob, fb = origin.install(oc), first.install(fc)
        queues = [n.hub.join(c.name, ParticipantId("alice", 1)) for n, c in ((origin, oc), (first, fc), (second, sc))]
        oq, fq, sq = queues
        try:
            await connect((origin, first, second))
            session = await second.direct.ensure_session(origin.identity.fingerprint)
            await session.send(build_subscribe_frame(sc.channel_id))
            await eventually(lambda: len(second.bots.visible_roster(second.db, sc)) == 2)
            assert first.hub.participant_count(fc.name) == 1
            assert "Baud.bot" not in first.presence.online_usernames()
            # A bot on a subscriber advertises to the origin and the other subscriber.
            assert ("Baud.bot", first.identity.fingerprint) in second.bots.visible_roster(second.db, sc)
            assert await first.address(fc, "!help") is None
            local_reply = await next_line(fq, "No language model")
            await next_line(oq, "No language model")
            remote_reply = await next_line(sq, "No language model")
            assert remote_reply.author_fingerprint == first.identity.fingerprint
            assert get_scrollback(origin.db, oc) == []  # origin's same-named default was not dispatched
            assert len([m for m in get_scrollback(first.db, fc) if m.author_kind == "bot"]) == 1
            assert local_reply.author_kind == "bot"

            # Explicitly address one remote bot, using the actual chat input seam.
            address = f"Baud.bot@{first.identity.fingerprint}: "
            assert await second.address(sc, address + "remind 1d remote-owned") is None
            await next_line(sq, "Saved reminder #1")
            await eventually(lambda: not second.bots.requests)
            saved = read_state(first.db, "Baud")["reminders"][0]
            assert saved["user"] == f"link:{second.identity.fingerprint}:alice"
            assert await first.address(fc, "!cancel 1") is None
            await next_line(fq, "No reminder with that id")
            assert len(read_state(first.db, "Baud")["reminders"]) == 1
            assert await second.address(sc, address + "cancel 1") is None
            await next_line(sq, "Cancelled reminder #1")
            assert not read_state(first.db, "Baud")["reminders"]

            # Duplicated request ids cannot create duplicate reminders; bot
            # callers are refused even though their transport is authentic.
            direct = second.registry.get(first.identity.fingerprint)
            request = build_bot_frame("bot_request", {"channel_id": sc.channel_id, "bot": "Baud.bot",
                                                       "caller": "alice", "text": "remind 1d one-copy"})
            await direct.send(request)
            await next_line(sq, "Saved reminder #2")
            await direct.send(request)
            await direct.send(build_bot_frame("bot_request", {"channel_id": sc.channel_id, "bot": "Baud.bot",
                                                               "caller": "Other.bot", "text": "remind 1d forbidden"}))
            await asyncio.sleep(.1)
            assert [r["text"] for r in read_state(first.db, "Baud")["reminders"]] == ["one-copy"]

            # Live input frames and later signed materialization never become commands.
            count = len(get_scrollback(first.db, fc))
            upstream = second.registry.get(first.identity.fingerprint)
            await upstream.send(build_channel_message_frame(sc.channel_id, "alice", "alice", "!help", utc_now_iso()))
            await next_line(fq, "!help")
            row = first.db.connection.execute("SELECT link_event_json FROM channel_messages WHERE author_kind='bot' LIMIT 1").fetchone()
            signed = ChannelMessage.from_dict(json.loads(row[0]))
            materialized = materialize_carried_channel_message(second.db, signed, sender_fingerprint=first.identity.fingerprint)
            await second.bridge.on_carried_message_materialized(signed.content_id, signed.payload, materialized)
            await asyncio.sleep(.1)
            assert len(get_scrollback(first.db, fc)) == count

            # Receiving mute works for live, durable history and roster without erasing signed events.
            mute_remote_bot(second.db, sc, "Baud.bot", first.identity.fingerprint, actor=second.sysop, mute=True)
            assert not bot_message_visible(second.db, sc, remote_reply)
            assert not bot_message_visible(second.db, sc, get_scrollback(second.db, sc)[-1])
            assert ("Baud.bot", first.identity.fingerprint) not in second.bots.visible_roster(second.db, sc)
            assert await second.address(sc, address + "help") is None
            await next_line(sq, "muted or hidden")
            mute_remote_bot(second.db, sc, "Baud.bot", first.identity.fingerprint, actor=second.sysop, mute=False)
            assert bot_message_visible(second.db, sc, remote_reply)

            set_channel_bot_policy(second.db, sc, "local", changed_by=second.sysop)
            assert second.bots.visible_roster(second.db, sc) == []
            set_channel_bot_policy(second.db, sc, "all", changed_by=second.sysop)
            stop_bots(first.db, actor=first.sysop)
            await eventually(lambda: ("Baud.bot", first.identity.fingerprint) not in second.bots.visible_roster(second.db, sc))
            assert ("Baud.bot", origin.identity.fingerprint) in second.bots.visible_roster(second.db, sc)
            # A session disappearing clears its presence, then a fresh
            # subscription recovers the still-running origin's bot.
            await second.registry.close_all(reason="reconnect test")
            await eventually(lambda: not second.bots.visible_roster(second.db, sc))
            session = await second.direct.ensure_session(origin.identity.fingerprint)
            await session.send(build_subscribe_frame(sc.channel_id))
            await eventually(lambda: ("Baud.bot", origin.identity.fingerprint) in second.bots.visible_roster(second.db, sc))
        finally:
            for node in (second, first, origin):
                await node.close()

    asyncio.run(run())


def test_link_approval_is_per_channel_and_older_peer_gets_no_new_frames(tmp_path):
    async def run():
        origin, peer = Node(tmp_path, "approval-origin"), Node(tmp_path, "older")
        oc, pc = _setup_linked_channel(origin, peer)
        try:
            with pytest.raises(BotError, match="approval"):
                origin.install(oc, approved=False)
            bot = origin.install(oc)
            assert allowed_channel(origin.db, bot.id, oc.name)
            await connect((origin, peer))
            await eventually(lambda: bot.id in origin.runtime.running_ids)
            # Advertise an older endpoint descriptor with no bot capability.
            from netbbs.link.events import build_endpoint_descriptor
            descriptor = build_endpoint_descriptor(
                signing_identity=peer.identity.signing_key, subject_fingerprint=peer.identity.fingerprint,
                addresses=[], outgoing_only=True, created_at=utc_now_iso(),
                capabilities=tuple(c for c in LINK_CAPABILITIES if c != CHAT_BOTS_CAPABILITY),
            )
            origin.link_node.peers[peer.identity.fingerprint].descriptor = descriptor
            assert "does not advertise" in origin.bots.request(oc, origin.alice, ParticipantId("alice", 1),
                                                               peer.identity.fingerprint, "Baud.bot", "help")
            session = await peer.direct.ensure_session(origin.identity.fingerprint)
            await session.send(build_subscribe_frame(pc.channel_id))
            await asyncio.sleep(.1)
            assert peer.bots.remote == {}
            assert not session.closed.is_set()
        finally:
            await peer.close()
            await origin.close()

    asyncio.run(run())


def test_remote_reply_reaches_caller_when_channel_origin_is_offline(tmp_path):
    async def run():
        origin, host, caller = [Node(tmp_path, n) for n in ("offline", "host", "caller")]
        oc, hc = _setup_linked_channel(origin, host)
        genesis = origin.db.connection.execute("SELECT link_genesis_json FROM channels WHERE id=?", (oc.id,)).fetchone()[0]
        cc = materialize_carried_channel(caller.db, ChannelGenesis.from_dict(json.loads(genesis)))
        bot = host.install(hc)
        queue = caller.hub.join(cc.name, ParticipantId("alice", 1))
        try:
            await connect((host, caller))
            await eventually(lambda: bot.id in host.runtime.running_ids)
            assert await caller.address(cc, f"Baud.bot@{host.identity.fingerprint}: help") is None
            assert "No language model" in (await next_line(queue, "No language model")).body
            await eventually(lambda: not caller.bots.requests)
        finally:
            for node in (caller, host, origin):
                await node.close()

    asyncio.run(run())


def test_editor_requires_explicit_link_approval_before_persisting(tmp_path, monkeypatch):
    from netbbs.net import bot_flow
    from netbbs.net.admin_flow import _load_chrome
    from tests.test_bot_runtime import FakeSession
    from netbbs.bots.registry import list_bots

    async def run():
        origin, peer = Node(tmp_path, "editor"), Node(tmp_path, "editor-peer")
        channel, _ = _setup_linked_channel(origin, peer)
        answers = iter((False, True))
        prompts = []

        async def confirm(session, prompt, **kwargs):
            prompts.append(prompt)
            assert list_bots(origin.db) == []
            return next(answers)

        async def editor(session, lane, **kwargs):
            draft = {**kwargs["draft"], "channels": channel.name, "linked": channel.name,
                     "reading": channel.name, "enabled": True}
            with pytest.raises(BotError, match="draft has been kept"):
                await kwargs["save"](draft)
            assert list_bots(origin.db) == []
            return await kwargs["save"](draft)

        monkeypatch.setattr(bot_flow, "prompt_yes_no", confirm)
        monkeypatch.setattr(bot_flow, "edit_resource_draft", editor)
        try:
            bot = await bot_flow.bot_editor(FakeSession(), origin.lane, origin.sysop,
                                            await _load_chrome(origin.lane, origin.sysop))
            assert bot.linked_channels == (channel.name,)
            assert len(prompts) == 2 and "other nodes" in prompts[0]
            assert "every live human message" in prompts[0]  # one combined final prompt, not a chain
        finally:
            await peer.close()
            await origin.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["bot_presence", "bot_readers"])
def test_presence_refuses_fabricated_authors_before_retention_and_capacity_is_not_a_strike(tmp_path, monkeypatch, kind):
    from netbbs.bots import link as bot_link_module

    async def run():
        origin, receiver, other = [Node(tmp_path, n) for n in ("roster-origin", "roster-receiver", "roster-other")]
        oc, rc = _setup_linked_channel(origin, receiver)
        try:
            await connect((origin, receiver, other))
            source = SimpleNamespace(remote_fingerprint=origin.identity.fingerprint, closed=asyncio.Event())
            other_source = SimpleNamespace(remote_fingerprint=other.identity.fingerprint, closed=asyncio.Event())

            def frame(author):
                return build_bot_frame(kind, {"channel_id": rc.channel_id,
                    "author_node_fingerprint": author, "labels": ["Baud.bot"]})

            for index in range(20):
                await receiver.bots.handle_frame(source, frame(f"fabricated-{index}"))
            roster = receiver.bots.remote_readers if kind == "bot_readers" else receiver.bots.remote
            assert roster == {}
            assert receiver.db.connection.execute(
                "SELECT COUNT(*) FROM link_trust_subjects WHERE node_fingerprint LIKE 'fabricated-%'"
            ).fetchone()[0] == 0
            monkeypatch.setattr(bot_link_module, "MAX_REMOTE_ROSTERS", 1)
            await receiver.bots.handle_frame(source, frame(origin.identity.fingerprint))
            assert len(roster) == 1
            # An unrelated honest peer must not receive a strike because
            # this node's global roster is already full.
            await receiver.bots.handle_frame(other_source, frame(other.identity.fingerprint))
            assert len(roster) == 1
        finally:
            for node in (other, receiver, origin):
                await node.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind,payload", [
    ("bot_request", {"channel_id": "c", "bot": "alice", "caller": "bob", "text": "help"}),
    ("bot_request", {"channel_id": "c", "bot": "Baud.bot", "caller": "bob", "text": "x" * 4001}),
    ("bot_presence", {"channel_id": "c", "author_node_fingerprint": "n", "labels": ["Baud.bot"] * 17}),
    ("bot_presence", {"channel_id": "c", "author_node_fingerprint": "n", "labels": ["Human"]}),
    ("bot_result", {"request_id": "r", "error": "x" * 501}),
])
def test_bot_wire_bounds(kind, payload):
    with pytest.raises(LinkProtocolError):
        build_bot_frame(kind, payload)
