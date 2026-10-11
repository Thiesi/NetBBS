"""Query grants and public visibility through the same API for every bot."""

import asyncio
from pathlib import Path
import sys
import time

import pytest

from netbbs.auth.users import set_user_disabled
from netbbs.bots.queries import run_query
from netbbs.bots.registry import BotError, mute_bot
from netbbs.bots.runtime import BotRuntime, _Worker
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import record_message
from netbbs.session_history import record_session_start, set_session_history_name_visible
from tests.test_bot_runtime import (  # noqa: F401
    db, lane, hub, sysop, alice, channel, presence, install, eventually, next_line,
)


def test_query_grants_default_off_and_are_rechecked(db, sysop, alice, channel):
    bot = install(db, sysop, channel)
    caller = {"id": alice.id, "name": alice.username}
    assert bot.queries == ()
    with pytest.raises(BotError, match="not granted"):
        run_query(db, bot.id, channel.name, "stats", None, caller=caller, online=set(), uptime_seconds=0)
    install(db, sysop, channel, bot_id=bot.id, queries=["stats"])
    result = run_query(db, bot.id, channel.name, "stats", None, caller=caller, online={"alice"}, uptime_seconds=3661)
    assert result["online_humans"] == 1 and "1h 1m" in result["text"]
    with pytest.raises(BotError, match="not granted"):
        run_query(db, bot.id, channel.name, "seen", "sysop", caller=caller, online=set(), uptime_seconds=0)
    set_user_disabled(db, alice, True, changed_by=sysop)
    with pytest.raises(BotError, match="caller is no longer"):
        run_query(db, bot.id, channel.name, "stats", None, caller=caller, online=set(), uptime_seconds=0)


def test_seen_respects_history_privacy_even_for_sysop_and_never_names_private_activity(db, sysop, alice, channel):
    bot = install(db, sysop, channel, queries=["seen"])
    create_channel(db, "secret-project", creator=sysop, members_only=True)
    history = record_session_start(db, alice)
    db.connection.execute("UPDATE session_history SET connected_at=?, disconnected_at=? WHERE id=?",
                          ("2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00", history))
    db.connection.commit()
    caller = {"id": sysop.id, "name": sysop.username}

    def seen(target="alice", online=frozenset()):
        return run_query(db, bot.id, channel.name, "seen", target, caller=caller, online=online, uptime_seconds=0)

    result = seen()
    assert result["basis"] == "logout" and "11:00:00" in result["at"]
    set_session_history_name_visible(db, alice, False)
    assert seen() == seen("unknown-person")
    assert seen()["available"] is False
    assert seen(online={"alice"})["online"] is True  # node-wide presence is already public
    assert "secret-project" not in str(seen(online={"alice"}))
    with pytest.raises(BotError, match="local account"):
        seen("alice@AnotherNode")


def test_interrupted_visit_is_a_login_not_a_fabricated_logout(db, sysop, alice, channel):
    bot = install(db, sysop, channel, queries=["seen"])
    history = record_session_start(db, alice)
    db.connection.execute("UPDATE session_history SET connected_at=?, interrupted_at=? WHERE id=?",
                          ("2026-01-01T10:00:00+00:00", "2026-01-03T11:00:00+00:00", history))
    db.connection.commit()
    result = run_query(db, bot.id, channel.name, "seen", "alice", caller={"id": sysop.id, "name": sysop.username},
                       online=set(), uptime_seconds=0)
    assert result["basis"] == "login" and result["at"] == "2026-01-01T10:00:00+00:00"


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("granted", [False, True])
def test_real_baud_and_external_program_use_the_same_query_grants(db, lane, hub, presence, sysop, alice, channel, external, granted):
    command = [sys.executable, "-u", str(Path(__file__).parent / "fixtures" / "bot_query_peer.py")] if external else []
    bot = install(db, sysop, channel, queries=["stats", "seen"] if granted else [], command=command)

    async def run():
        presence.enter(alice.username)
        runtime = BotRuntime(lane, hub, presence=presence, poll_seconds=.03)
        pid = ParticipantId(alice.username, 1)
        queue = hub.join(channel.name, pid)
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            message = record_message(db, channel, kind="message", author_label=alice.username, body="!stats")
            assert await runtime.addressed(channel, alice, message, pid) is None
            await next_line(queue, "human accounts online" if granted else ("not granted" if external else "not enabled"))
            if granted and not external:
                message = record_message(db, channel, kind="message", author_label=alice.username, body="!seen alice")
                assert await runtime.addressed(channel, alice, message, pid) is None
                await next_line(queue, "alice is online now")
        finally:
            await runtime.close()

    asyncio.run(run())


def test_host_binds_queries_to_instance_request_and_rechecks_revocation(db, lane, hub, presence, sysop, alice, channel):
    bot = install(db, sysop, channel, queries=["stats"])

    async def run():
        runtime = BotRuntime(lane, hub, presence=presence)
        worker = _Worker(runtime, bot)
        caller = {"id": alice.id, "name": alice.username}
        worker.pending["owned"] = (channel.name, None, time.monotonic() + 60, None, caller)
        with pytest.raises(BotError, match="live request"):
            await worker._action({"action": "query", "query": "stats", "request": "forged"})
        result = await worker._action({"action": "query", "query": "stats", "request": "owned"})
        assert result["online_humans"] == 0
        mute_bot(db, channel, bot, actor=sysop, mute=True)
        with pytest.raises(BotError, match="muted"):
            await worker._action({"action": "query", "query": "stats", "request": "owned"})
        mute_bot(db, channel, bot, actor=sysop, mute=False)
        install(db, sysop, channel, bot_id=bot.id, queries=[])
        with pytest.raises(BotError, match="not granted"):
            await worker._action({"action": "query", "query": "stats", "request": "owned"})

    asyncio.run(run())


def test_remote_seen_uses_bot_hosts_public_data_over_real_link(tmp_path):
    from tests.test_bot_link import Node, connect, _setup_linked_channel
    from netbbs.bots.registry import save_bot

    async def run():
        host, caller = Node(tmp_path, "query-host"), Node(tmp_path, "query-caller")
        hc, cc = _setup_linked_channel(host, caller)
        bot = save_bot(host.db, actor=host.sysop, name="Baud", channels=[hc.name], defaults=[],
                       linked_channels=[hc.name], enabled=True, queries=["seen"])
        host.runtime.presence = host.presence
        host.presence.enter("alice")
        queue = caller.hub.join(cc.name, ParticipantId("alice", 1))
        try:
            await connect((host, caller))
            await eventually(lambda: bot.id in host.runtime.running_ids)
            assert await caller.address(cc, f"Baud.bot@{host.identity.fingerprint}: seen alice") is None
            await next_line(queue, "alice is online now")
            await eventually(lambda: not caller.bots.requests)
        finally:
            await caller.close()
            await host.close()

    asyncio.run(run())
