"""Explicit live grants, real program delivery, and caller disclosure."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from netbbs.bots.registry import BotError, save_bot, mute_bot, observation_targets, channel_readers
from netbbs.bots.runtime import BotRuntime
from netbbs.bots.state import read_state
from netbbs.chat.bots import mute_remote_bot, set_channel_bot_policy
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import record_message, get_scrollback
from netbbs.net import chat_flow
from netbbs.timeutil import utc_now_iso
from tests.test_bot_runtime import (  # noqa: F401
    db, lane, hub, presence, mailbox, history, sysop, alice, channel,
    install, eventually, next_line, FakeSession,
)


def observer_command():
    return [sys.executable, "-u", str(Path(__file__).parent / "fixtures" / "bot_observer_peer.py")]


def test_source_grants_default_off_and_greetings_require_presence_events(db, sysop, channel):
    bot = install(db, sysop, channel)
    assert bot.reading_channels == bot.event_channels == () and not bot.greetings
    assert observation_targets(db, channel.name, "message") == []
    other = create_channel(db, "elsewhere", creator=sysop)
    with pytest.raises(BotError, match="must also be allowed"):
        install(db, sysop, channel, bot_id=bot.id, reading_channels=[other.name])
    with pytest.raises(BotError, match="Greetings require"):
        install(db, sysop, channel, bot_id=bot.id, greetings=True)
    bot = install(db, sysop, channel, bot_id=bot.id, event_channels=[channel.name], greetings=True)
    assert observation_targets(db, channel.name, "join") == [bot.id]
    assert observation_targets(db, channel.name, "message") == []
    mute_bot(db, channel, bot, actor=sysop, mute=True)
    assert observation_targets(db, channel.name, "join") == []


def test_real_external_bot_observes_only_granted_live_humans_and_stops_on_revocation(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel, reading_channels=[channel.name], command=observer_command())
    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            human = record_message(db, channel, kind="message", author_label=alice.username, body="ordinary text")
            caller = {"id": alice.id, "name": alice.username}
            await runtime.observe(channel, human, caller=caller)
            await eventually(lambda: len((read_state(db, "Baud") or {}).get("events", [])) == 1)
            assert read_state(db, "Baud")["events"][0]["text"] == "ordinary text"
            assert not runtime.workers[bot.id].pending
            await runtime.observe(channel, replace(human, author_kind="bot"), caller=caller)
            await runtime.observe(channel, replace(human, external_source="MRC"), caller=caller)
            await runtime.observe(channel, replace(human, kind="join"), caller=caller)
            await runtime.observe(channel, replace(human, author_label="Other.bot@Node"), caller={"id": "link:fp:Other.bot", "name": "Other.bot@Node"})
            # Revocation is effective before the manager's next worker restart.
            install(db, sysop, channel, bot_id=bot.id, command=observer_command())
            await runtime.observe(channel, human, caller=caller)
            await asyncio.sleep(.1)
            assert len(read_state(db, "Baud")["events"]) == 1
        finally:
            await runtime.close()
    asyncio.run(run())


def test_real_chat_heading_who_actions_join_leave_and_no_replay(db, lane, hub, presence, mailbox, history, sysop, alice, channel):
    bot = install(db, sysop, channel, reading_channels=[channel.name], event_channels=[channel.name], command=observer_command())
    class Interactive(FakeSession):
        def __init__(self):
            super().__init__()
            self.input = asyncio.Queue()
        async def read_line(self, **kwargs):
            return await self.input.get()

    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        runtime.start()
        session = Interactive()
        task = None
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            record_message(db, channel, kind="message", author_label="alice", body="old history")
            task = asyncio.create_task(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, sysop))
            await eventually(lambda: "Joined" in "".join(session.written))
            for line in ("/who", "ordinary text", "/me waves"):
                session.input.put_nowait(line)
            await eventually(lambda: len((read_state(db, "Baud") or {}).get("events", [])) == 3)
            assert "".join(session.written).count("Baud.bot reads this channel") >= 2
            session.input.put_nowait("/quit")
            await asyncio.wait_for(task, 6)
            await eventually(lambda: len(read_state(db, "Baud")["events"]) == 4)
            events = read_state(db, "Baud")["events"]
            assert [e["kind"] for e in events] == ["join", "message", "action", "leave"]
            assert "old history" not in str(events)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runtime.close()
    asyncio.run(run())


def test_real_baud_greets_without_reading_conversation_and_obeys_mute(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel, event_channels=[channel.name], greetings=True)
    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        queue = hub.join(channel.name, ParticipantId("alice", 1))
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            join = record_message(db, channel, kind="join", author_label="alice")
            await runtime.observe(channel, join, caller={"id": alice.id, "name": "alice"})
            await next_line(queue, "Welcome, alice!")
            assert channel_readers(db, channel) == []
            mute_bot(db, channel, bot, actor=sysop, mute=True)
            await runtime.observe(channel, join, caller={"id": alice.id, "name": "alice"})
            await asyncio.sleep(.1)
            assert len([m for m in get_scrollback(db, channel) if m.author_kind == "bot"]) == 1
        finally:
            await runtime.close()
    asyncio.run(run())


def test_real_link_observation_disclosure_and_receiving_mutes_do_not_hide_readers(tmp_path):
    from tests.test_bot_link import Node, connect, _setup_linked_channel
    from netbbs.link.protocol import build_channel_message_frame, build_presence_delta_frame
    async def run():
        host, caller = Node(tmp_path, "scope-host"), Node(tmp_path, "scope-caller")
        hc, cc = _setup_linked_channel(host, caller)
        bot = save_bot(host.db, actor=host.sysop, name="Observer", channels=[hc.name], defaults=[],
                       linked_channels=[hc.name], enabled=True, reading_channels=[hc.name],
                       event_channels=[hc.name], command=observer_command())
        try:
            await connect((host, caller))
            await eventually(lambda: bot.id in host.runtime.running_ids)
            session = await caller.direct.ensure_session(host.identity.fingerprint)
            from netbbs.link.protocol import build_subscribe_frame
            await session.send(build_subscribe_frame(cc.channel_id))
            await eventually(lambda: caller.bots.visible_roster(caller.db, cc, readers=True))
            mute_remote_bot(caller.db, cc, "Observer.bot", host.identity.fingerprint, actor=caller.sysop, mute=True)
            set_channel_bot_policy(caller.db, cc, "none", changed_by=caller.sysop)
            assert caller.bots.visible_roster(caller.db, cc) == []
            assert "Observer.bot@" in str(chat_flow._bot_readers(caller.db, caller.hub, cc))
            from netbbs.link.trust import TrustSubject, TrustDimension, TrustState, register_subject, set_trust_override
            subject = TrustSubject.user(host.identity.fingerprint, "Observer.bot")
            register_subject(caller.db, subject, first_accepted_at=utc_now_iso(), now_iso=utc_now_iso())
            set_trust_override(caller.db, subject, TrustDimension.IDENTITY_INTEGRITY, TrustState.BLOCKED,
                               reason="Hide this bot's output", now_iso=utc_now_iso())
            assert caller.bots.visible_roster(caller.db, cc, readers=True) == [("Observer.bot", host.identity.fingerprint)]
            # Fresh advertisements must retain the disclosure too.
            await caller.bots.on_disconnect(caller.registry.get(host.identity.fingerprint))
            await session.send(build_subscribe_frame(cc.channel_id))
            await eventually(lambda: caller.bots.visible_roster(caller.db, cc, readers=True))
            frame = build_channel_message_frame(cc.channel_id, "alice", "alice", "live text", utc_now_iso())
            await session.send(frame)
            await session.send(frame)  # normal live dedup applies before observation
            await session.send(build_presence_delta_frame(cc.channel_id, "join", "alice", "alice"))
            await eventually(lambda: len((read_state(host.db, "Observer") or {}).get("events", [])) == 2)
            events = read_state(host.db, "Observer")["events"]
            assert [e["kind"] for e in events] == ["message", "join"]
            assert events[0]["caller"]["id"] == f"link:{caller.identity.fingerprint}:alice"
            assert not host.runtime.workers[bot.id].pending
        finally:
            await caller.close()
            await host.close()
    asyncio.run(run())


def test_reader_roster_relay_withdrawal_and_old_peer_compatibility(tmp_path):
    from tests.test_bot_link import Node, connect, _setup_linked_channel
    from netbbs.bots.registry import stop_bots
    from netbbs.link.channels import materialize_carried_channel
    from netbbs.link.events import ChannelGenesis, LINK_CAPABILITIES, CHAT_BOT_READERS_CAPABILITY, build_endpoint_descriptor
    from netbbs.link.protocol import build_subscribe_frame
    async def run():
        origin, host, caller = [Node(tmp_path, n) for n in ("scope-origin", "scope-host", "scope-caller")]
        oc, hc = _setup_linked_channel(origin, host)
        genesis = origin.db.connection.execute("SELECT link_genesis_json FROM channels WHERE id=?", (oc.id,)).fetchone()[0]
        cc = materialize_carried_channel(caller.db, ChannelGenesis.from_dict(json.loads(genesis)))
        bot = save_bot(host.db, actor=host.sysop, name="Observer", channels=[hc.name], defaults=[],
                       linked_channels=[hc.name], enabled=True, reading_channels=[hc.name])
        try:
            await connect((origin, host, caller))
            await eventually(lambda: bot.id in host.runtime.running_ids)
            await eventually(lambda: origin.bots.visible_roster(origin.db, oc, readers=True))
            session = await caller.direct.ensure_session(origin.identity.fingerprint)
            # Existing bot support without the newer reader capability must
            # still receive ordinary bot presence, never an unknown frame.
            origin.link_node.peers[caller.identity.fingerprint].descriptor = build_endpoint_descriptor(
                signing_identity=caller.identity.signing_key, subject_fingerprint=caller.identity.fingerprint,
                addresses=[], outgoing_only=True, created_at=utc_now_iso(),
                capabilities=tuple(c for c in LINK_CAPABILITIES if c != CHAT_BOT_READERS_CAPABILITY),
            )
            await session.send(build_subscribe_frame(cc.channel_id))
            await eventually(lambda: caller.bots.visible_roster(caller.db, cc))
            assert not caller.bots.remote_readers and not session.closed.is_set()
            origin.link_node.peers[caller.identity.fingerprint].descriptor = build_endpoint_descriptor(
                signing_identity=caller.identity.signing_key, subject_fingerprint=caller.identity.fingerprint,
                addresses=[], outgoing_only=True, created_at=utc_now_iso(), capabilities=LINK_CAPABILITIES,
            )
            await session.send(build_subscribe_frame(cc.channel_id))
            await eventually(lambda: caller.bots.visible_roster(caller.db, cc, readers=True))
            assert caller.bots.visible_roster(caller.db, cc, readers=True) == [("Observer.bot", host.identity.fingerprint)]
            stop_bots(host.db, actor=host.sysop)
            await eventually(lambda: not caller.bots.visible_roster(caller.db, cc, readers=True))
        finally:
            for node in (caller, host, origin):
                await node.close()
    asyncio.run(run())


def test_editor_warns_and_persists_explicit_scopes(db, lane, sysop, channel, monkeypatch):
    from netbbs.net import bot_flow
    from netbbs.net.admin_flow import _load_chrome
    from netbbs.bots.registry import list_bots
    answers = iter((False, True))
    prompts = []
    async def confirm(session, prompt, **kwargs):
        prompts.append(prompt)
        assert not list_bots(db) and kwargs["default"] is False
        return next(answers)
    async def editor(session, lane, **kwargs):
        draft = kwargs["draft"]
        assert draft["reading"] == draft["events"] == "" and not draft["greetings"]
        assert "other callers' conversation" in kwargs["preamble"]
        draft.update(channels=channel.name, reading=channel.name, events=channel.name, greetings=True)
        with pytest.raises(BotError, match="draft has been kept"):
            await kwargs["save"](draft)
        assert not list_bots(db)
        return await kwargs["save"](draft)
    monkeypatch.setattr(bot_flow, "edit_resource_draft", editor)
    monkeypatch.setattr(bot_flow, "prompt_yes_no", confirm)
    async def run():
        bot = await bot_flow.bot_editor(FakeSession(), lane, sysop, await _load_chrome(lane, sysop))
        assert bot.reading_channels == bot.event_channels == (channel.name,) and bot.greetings
        assert len(prompts) == 2 and "every live human message" in prompts[0]
    asyncio.run(run())
