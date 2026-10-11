"""Real bot processes and SQLite boundaries, including live chat wiring."""

import asyncio
from pathlib import Path
import sys

import pytest

from netbbs.bots.registry import (
    BotError, active_labels, allowed_channel, get_bot, list_bots, mute_bot, publish,
    runtime_status, save_bot, select_request, stop_bots,
)
from netbbs.bots.runtime import BotRuntime
from netbbs.bots.state import read_state, write_state
from netbbs.chat.bots import set_channel_bot_policy
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.link.channels import link_channel
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.net import chat_flow, bot_flow
from tests.test_chat_bots import sysop, alice, channel  # noqa: F401
from tests.test_chat_flow_moderation import FakeSession, db, lane, hub, presence, mailbox, history  # noqa: F401


def install(db, sysop, channel, **kwargs):
    return save_bot(db, actor=sysop, name=kwargs.pop("name", "Baud"), channels=[channel.name],
                    defaults=kwargs.pop("defaults", [channel.name]), enabled=kwargs.pop("enabled", True), **kwargs)


async def eventually(predicate, timeout=6):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


async def next_line(queue, needle):
    async with asyncio.timeout(6):
        while True:
            message = await queue.get()
            if needle in getattr(message, "body", str(message)):
                return message


def test_off_by_default_and_configuration_authority(db, sysop, alice, channel):
    assert list_bots(db) == []
    bot = save_bot(db, actor=sysop, name="Baud", channels=[], defaults=[])
    assert not bot.enabled
    with pytest.raises(BotError, match="SysOp"):
        install(db, alice, channel, name="Other")
    with pytest.raises(BotError, match="at least one"):
        save_bot(db, actor=sysop, name="Bad", channels=[], defaults=[], enabled=True)
    with pytest.raises(BotError, match="default"):
        install(db, sysop, channel, name="Other")
        install(db, sysop, channel, name="Third")


def test_channel_gates_recheck_mrc_link_policy_and_mute(db, sysop, channel, tmp_path):
    bot = install(db, sysop, channel)
    mute_bot(db, channel, bot, actor=sysop, mute=True)
    with pytest.raises(BotError, match="muted"):
        publish(db, bot.id, channel.name, "not allowed")
    # Editing does not silently lift a moderator's mute.
    install(db, sysop, channel, bot_id=bot.id)
    with pytest.raises(BotError, match="muted"):
        allowed_channel(db, bot.id, channel.name)
    mute_bot(db, channel, bot, actor=sysop, mute=False)
    set_channel_bot_policy(db, channel, "none", changed_by=sysop)
    with pytest.raises(BotError, match="policy"):
        publish(db, bot.id, channel.name, "not allowed")
    set_channel_bot_policy(db, channel, "all", changed_by=sysop)
    db.connection.execute("UPDATE channels SET mrc_room='lobby', mrc_paused=1 WHERE id=?", (channel.id,))
    db.connection.commit()
    with pytest.raises(BotError, match="MRC"):
        publish(db, bot.id, channel.name, "not allowed")
    db.connection.execute("UPDATE channels SET mrc_room=NULL WHERE id=?", (channel.id,))
    db.connection.commit()
    identity = bootstrap_node_identity(tmp_path / "identity")
    link_channel(db, channel, node_identity=identity)
    with pytest.raises(BotError, match="Linked"):
        publish(db, bot.id, channel.name, "not allowed")
    assert get_scrollback(db, channel) == []


def test_rate_persists_and_is_per_channel(db, sysop, channel):
    other = create_channel(db, "other", creator=sysop)
    bot = save_bot(db, actor=sysop, name="Baud", channels=[channel.name, other.name], defaults=[], enabled=True,
                   lines_per_hour=1)
    assert publish(db, bot.id, channel.name, "first")[1].author_kind == "bot"
    with pytest.raises(BotError, match="limit"):
        publish(db, bot.id, channel.name, "second")
    assert publish(db, bot.id, other.name, "first there")
    assert not db.connection.in_transaction
    assert len(get_scrollback(db, channel)) == 1


def test_addressing_selects_one_and_never_ordinary_chat(db, sysop, channel):
    first = install(db, sysop, channel)
    second = install(db, sysop, channel, name="Second", defaults=[])
    assert select_request(db, channel, "!help")[0] == first
    assert select_request(db, channel, "Second.bot: help")[0] == second
    assert select_request(db, channel, "hello everyone") is None
    assert select_request(db, channel, "talking about a.bot: something") is None
    with pytest.raises(BotError, match="Remote"):
        select_request(db, channel, "Baud.bot@Node: help")


def test_state_bound_and_old_value_survives_refused_write(db):
    write_state(db, "Baud", {"reminders": ["one"]})
    with pytest.raises(BotError, match="16 KiB"):
        write_state(db, "Baud", {"large": "x" * 20000})
    assert read_state(db, "Baud") == {"reminders": ["one"]}


def test_real_baud_help_persistent_reminder_restart_and_stop(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel)

    async def run():
        pid = ParticipantId(alice.username, 1)
        queue = hub.join(channel.name, pid)
        runtime = BotRuntime(lane, hub, poll_seconds=.03, restart_seconds=.03)
        runtime.start()

        async def send(text):
            message = record_message(db, channel, kind="message", author_label=alice.username, body=text)
            assert await runtime.addressed(channel, alice, message, pid) is None

        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            proc = runtime.workers[bot.id].proc
            assert active_labels(db, channel, runtime.running_ids) == ["Baud.bot"]
            assert hub.participant_count(channel.name) == 1
            await send("!help")
            assert "No language model" in (await next_line(queue, "No language model")).body
            # A real unexpected exit recovers without taking chat down.
            proc.kill()
            await eventually(lambda: bot.id in runtime.running_ids and runtime.workers[bot.id].proc is not proc)
            assert proc.returncode is not None
            proc = runtime.workers[bot.id].proc
            await send("Baud.bot: remind 1s restartproof")
            await next_line(queue, "Saved reminder")
            assert read_state(db, bot.storage_name)["reminders"][0]["text"] == "restartproof"
            await runtime.close()
            assert proc.returncode is not None
            assert hub.bot_runtime is None
            runtime.start()
            await eventually(lambda: bot.id in runtime.running_ids)
            await next_line(queue, "reminder #1: restartproof")
            await eventually(lambda: not read_state(db, bot.storage_name)["reminders"])
            stop_bots(db, actor=sysop)
            await eventually(lambda: not runtime.workers)
            assert not get_bot(db, bot.id).enabled
        finally:
            await runtime.close()
            hub.leave(channel.name, pid)

    asyncio.run(run())


def test_external_bot_same_api_grants_and_separate_state(db, lane, hub, sysop, alice, channel):
    create_channel(db, "private", creator=sysop, members_only=True)
    bot = install(db, sysop, channel, command=[sys.executable, "-u", str(Path(__file__).parent / "fixtures" / "bot_api_peer.py")])
    write_state(db, "Second", {"secret": "other bot state"})

    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        pid = ParticipantId(alice.username, 2)
        queue = hub.join(channel.name, pid)
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            message = record_message(db, channel, kind="message", author_label=alice.username, body="!help")
            assert await runtime.addressed(channel, alice, message, pid) is None
            await next_line(queue, "External reply: help")
            await eventually(lambda: read_state(db, "Baud").get("refused"))
            messages = get_scrollback(db, channel)
            assert [m.body for m in messages if m.author_kind == "bot"] == ["External reply: help"]
            assert read_state(db, "Second") == {"secret": "other bot state"}
        finally:
            await runtime.close()
    asyncio.run(run())


def test_baud_reminder_queries_do_not_leak_private_channel_text():
    from netbbs.bots.baud import Baud
    sent = []
    state = {"next": 2, "reminders": [{"id": 1, "user": 7, "channel": "private", "text": "privateword"}]}
    baud = Baud({"state": state}, sent.append)
    for text in ("reminders", "cancel 1"):
        baud.handle({"type": "message", "request": "a", "caller": {"id": 7, "name": "alice"},
                     "channel": "public", "text": text})
    assert all("privateword" not in action["text"] for action in sent)
    assert len(baud.state["reminders"]) == 1


def test_node_lifecycle_starts_and_reaps_configured_bot(tmp_path, monkeypatch):
    from netbbs.__main__ import run as run_node
    from netbbs.auth.users import get_user_by_username
    from netbbs.storage.database import Database
    from tests.test_main_lifecycle import _config
    import netbbs.bots.runtime as module

    config = _config(tmp_path)
    database = Database(config.db_path)
    try:
        actor = get_user_by_username(database, "sysop")
        room = create_channel(database, "lobby", creator=actor)
        bot = install(database, actor, room)
    finally:
        database.close()
    instances = []

    class ObservedRuntime(BotRuntime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

    monkeypatch.setattr(module, "BotRuntime", ObservedRuntime)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(run_node(config, shutdown_event=stop))
        try:
            await eventually(lambda: instances and bot.id in instances[0].running_ids)
            proc = instances[0].workers[bot.id].proc
            stop.set()
            await asyncio.wait_for(task, 10)
            assert proc.returncode is not None
            assert not instances[0].workers
            assert instances[0].hub.bot_runtime is None
        finally:
            stop.set()
            await asyncio.wait_for(task, 10)
    asyncio.run(run())


def test_real_chat_dispatch_roster_mute_and_no_replay_triggers(db, lane, hub, presence, mailbox, history,
                                                            sysop, alice, channel):
    bot = install(db, sysop, channel)

    class InteractiveSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.input = asyncio.Queue()

        async def read_line(self, **kwargs):
            return await self.input.get()

    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        runtime.start()
        session = InteractiveSession()
        task = None
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            # History is rendered, but must never be handed to the bot.
            record_message(db, channel, kind="message", author_label=alice.username, body="!help")
            task = asyncio.create_task(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, sysop))
            await eventually(lambda: hub.participant_count(channel.name) == 1)
            for text in ("ordinary chatter", "/names", "/who", "!help"):
                session.input.put_nowait(text)
            await eventually(lambda: "No language model" in "".join(session.written))
            assert "[BOT] Baud.bot" in "".join(session.written)
            assert "addressed messages only" in "".join(session.written)
            assert len([m for m in get_scrollback(db, channel) if m.author_kind == "bot"]) == 1
            session.input.put_nowait("/mute Baud.bot")
            await eventually(lambda: "was muted" in "".join(session.written))
            session.input.put_nowait("!help")
            await eventually(lambda: "Baud.bot is muted here" in "".join(session.written))
            session.input.put_nowait("/unmute Baud.bot")
            await eventually(lambda: "was unmuted" in "".join(session.written))
            session.input.put_nowait("/quit")
            await asyncio.wait_for(task, 3)
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["crash", "oversized", "version", "hang"])
def test_process_failure_is_bounded_and_owned(db, lane, hub, sysop, channel, mode):
    code = {
        "crash": "import sys; sys.exit(2)",
        "oversized": "print('x'*40000, flush=True)",
        "version": "import json; print(json.dumps({'type':'ready','version':99}), flush=True)",
        "hang": "import time; time.sleep(300)",
    }[mode]
    bot = install(db, sysop, channel, command=[sys.executable, "-u", "-c", code])

    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03, restart_seconds=.1)
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.workers and runtime.workers[bot.id].proc is not None)
            proc = runtime.workers[bot.id].proc
            if mode != "hang":
                await eventually(lambda: runtime_status(db)[bot.id][0] == "restarting")
                assert runtime_status(db)[bot.id][1]
            await asyncio.wait_for(runtime.close(), 5)
            assert proc.returncode is not None
            assert not runtime.workers
        finally:
            await runtime.close()

    asyncio.run(run())


def test_console_back_and_draft_save_use_real_editor(db, lane, sysop, channel):
    from netbbs.net.admin_flow import _load_chrome

    class Screen(FakeSession):
        def __init__(self, keys):
            super().__init__()
            self.keys = iter(keys)

        async def read_key(self, **kwargs):
            return next(self.keys)

        async def read_editor_key(self, **kwargs):
            from netbbs.net.char_input import EditorKey, EditorKeyKind
            return EditorKey(EditorKeyKind.CHAR, next(self.keys))

    async def run():
        session = Screen(["b"])
        await bot_flow.bot_menu(session, lane, sysop)
        assert list_bots(db) == []
        assert "No bots installed" in "".join(session.written)
        session = Screen(["s"])
        result = await bot_flow.bot_editor(session, lane, sysop, await _load_chrome(lane, sysop))
        assert result.name == "Baud" and not result.enabled

    asyncio.run(run())
