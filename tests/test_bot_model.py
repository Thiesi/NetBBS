"""Model privacy, budgets and public replies at real SQLite/HTTP/process boundaries."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import time

from aiohttp import web
import pytest

from netbbs.auth.users import create_user
from netbbs.bots import model
from netbbs.bots.registry import BotError, save_bot, stop_bots, mute_bot
from netbbs.bots.runtime import BotRuntime, _Worker
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.config import get_config
from netbbs.guest import set_guest_user
from netbbs.link.protocol import LinkProtocolError, build_bot_frame
from tests.test_bot_runtime import db, lane, hub, sysop, alice, channel, install, eventually, next_line  # noqa: F401


@asynccontextmanager
async def provider():
    calls = []
    control = {"status": 200, "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}
    async def complete(request):
        payload = await request.json()
        calls.append((payload, request.headers.get("Authorization")))
        if "gate" in control:
            await control["gate"].wait()
        return web.json_response({"choices": [{"message": {"content": control.get("text", "A short model answer.")}}],
                                  "usage": control["usage"]}, status=control["status"])
    app = web.Application()
    app.router.add_post("/v1/chat/completions", complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1", calls, control
    finally:
        if "gate" in control:
            control["gate"].set()
        await runner.cleanup()


def configure(db, sysop, endpoint, **kwargs):
    return model.save_settings(db, model.ModelSettings(enabled=True, endpoint=endpoint, model="local-test", cooldown=0,
                                                      channel_per_minute=60, **kwargs), actor=sysop, api_key="private-key")


async def exchange(service, bot, channel, caller, prompt, **kwargs):
    text, accept = await service.respond(bot.id, channel.name, caller, prompt, **kwargs)
    accept()
    return text


def test_default_off_secret_validation_and_authority(db, sysop, alice, channel):
    bot = install(db, sysop, channel)
    assert not bot.model_conversation and not model.settings(db).enabled
    with pytest.raises(BotError, match="not enabled"):
        model.authorize(db, bot.id, channel.name, {"id": alice.id, "name": alice.username})
    with pytest.raises(BotError, match="SysOp"):
        configure(db, alice, "http://localhost/v1")
    for changes in ({"endpoint": "https://secret:password@example.com/v1"}, {"month_tokens": 0},
                    {"context_lines": 21}, {"day_money": 100}, {"model": ""}):
        with pytest.raises(BotError):
            model.validate_settings(replace(model.ModelSettings(enabled=True, model="local-test"), **changes))
    assert len(model.disclosure(replace(model.ModelSettings(), endpoint="https://" + "x" * 172))) <= 500


def test_real_baud_disclosure_no_first_prompt_help_reset_and_restart(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel, model_conversation=True)
    async def run():
        async with provider() as (endpoint, calls, control):
            configure(db, sysop, endpoint, token_parameter="max_completion_tokens")
            runtime = BotRuntime(lane, hub, poll_seconds=.03)
            pid = ParticipantId(alice.username, 1)
            queue = hub.join(channel.name, pid)
            async def send(text):
                line = record_message(db, channel, kind="message", author_label=alice.username, body="Baud.bot: " + text)
                assert await runtime.addressed(channel, alice, line, pid) is None
            runtime.start()
            try:
                await eventually(lambda: bot.id in runtime.running_ids)
                await send("first secret never sent")
                await next_line(queue, "Submit again")
                assert not calls and not db.connection.execute("SELECT 1 FROM bot_model_usage").fetchone()
                await send("second input")
                await next_line(queue, "A short model answer")
                assert calls[0][1] == "Bearer private-key"
                payload = calls[0][0]
                assert [m["content"] for m in payload["messages"][1:]] == ["second input"]
                assert not payload["store"] and not payload["stream"]
                assert payload["max_completion_tokens"] == 256 and "max_tokens" not in payload
                await send("help")
                await next_line(queue, "reset clears")
                assert len(calls) == 1
                await send("reset")
                await next_line(queue, "provider-held records are not deleted")
                assert not runtime.model.contexts
                await send("after reset")
                await next_line(queue, "Submit again")
                await runtime.close()
                assert not runtime.model.contexts
                runtime.start()
                await eventually(lambda: bot.id in runtime.running_ids)
                await send("after restart")
                await next_line(queue, "Submit again")
                assert len(calls) == 1
                row = db.connection.execute("SELECT * FROM bot_model_usage").fetchone()
                assert row["tokens"] == 20 and row["prompt"] is None and row["state"] == "reported"
                assert "private-key" not in str(get_scrollback(db, channel))
            finally:
                await runtime.close()
    asyncio.run(run())


def test_context_isolated_by_caller_bot_channel_expiry_and_changed_disclosure(db, lane, sysop, alice, channel):
    bot = install(db, sysop, channel, model_conversation=True)
    other_channel = create_channel(db, "elsewhere", creator=sysop)
    bot = save_bot(db, actor=sysop, bot_id=bot.id, name=bot.name, channels=[channel.name, other_channel.name],
                   defaults=[], enabled=True, model_conversation=True)
    other_bot = install(db, sysop, channel, name="Other", defaults=[], model_conversation=True)
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    a, b = {"id": alice.id, "name": alice.username}, {"id": bob.id, "name": bob.username}
    async def run():
        async with provider() as (endpoint, calls, control):
            config = configure(db, sysop, endpoint, context_lines=2)
            service = model.ModelService(lane)
            for current_bot, current_channel, caller in ((bot, channel, a), (bot, channel, b),
                                                         (bot, other_channel, a), (other_bot, channel, a)):
                assert "Submit again" in await exchange(service, current_bot, current_channel, caller, "discard")
                await exchange(service, current_bot, current_channel, caller, "first for this scope")
                assert len(calls[-1][0]["messages"]) == 2
                system_prompt = calls[-1][0]["messages"][0]["content"]
                assert f"Your chat identity is {current_bot.label}." in system_prompt
                if current_bot.name != "Baud":
                    assert "Baud" not in system_prompt
            await exchange(service, bot, channel, a, "follow up")
            assert [m["role"] for m in calls[-1][0]["messages"]] == ["system", "user", "assistant", "user"]
            assert len(service.contexts[(bot.id, str(alice.id), channel.id)].lines) == 2
            service.contexts[(bot.id, str(alice.id), channel.id)].touched -= 1801
            await service.upkeep()
            assert "Submit again" in await exchange(service, bot, channel, a, "expired")
            model.save_settings(db, replace(config, context_lines=0, log_prompts=True), actor=sysop, api_key="new-key")
            assert "logging ON" in await exchange(service, bot, channel, a, "changed settings")
            await exchange(service, bot, channel, a, "logged current input")
            assert len(calls[-1][0]["messages"]) == 2 and calls[-1][1] == "Bearer new-key"
            assert db.connection.execute("SELECT prompt FROM bot_model_usage ORDER BY id DESC").fetchone()[0] == "logged current input"
            assert not service.contexts[(bot.id, str(alice.id), channel.id)].lines
    asyncio.run(run())


@pytest.mark.parametrize("change,match", [({"day_tokens": 1}, "daily token"), ({"month_tokens": 1}, "monthly token"),
    ({"day_money": 1, "input_price": 1000000}, "daily money"), ({"month_money": 1, "output_price": 1000000}, "monthly money"),
    ({"user_daily": 1}, "daily model quota"), ({"cooldown": 30}, "cooldown"), ({"channel_per_minute": 1}, "channel model rate")])
def test_persistent_host_limits_refuse_before_provider_and_count_hits(db, sysop, alice, channel, change, match):
    bot = install(db, sysop, channel, model_conversation=True)
    config = model.save_settings(db, replace(model.ModelSettings(enabled=True, model="local", cooldown=0, channel_per_minute=60), **change), actor=sysop, api_key="")
    caller = {"id": alice.id, "name": alice.username}
    messages = [{"role": "user", "content": "request"}]
    if match in {"daily model quota", "cooldown", "channel model rate"}:
        ticket = model.reserve(db, bot.id, channel.name, caller, config, messages, "request", now=1791720000)
        model.settle(db, ticket, config, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    with pytest.raises(BotError, match=match):
        model.reserve(db, bot.id, channel.name, caller, config, messages, "refused", now=1791720001)
    assert not db.connection.in_transaction
    assert model.usage_summary(db, now=1791720001)["limits"][0]["hits"] == 1


def test_guest_disabled_level_and_publication_rechecks(db, lane, sysop, alice, channel):
    bot = install(db, sysop, channel, model_conversation=True)
    caller = {"id": alice.id, "name": alice.username}
    async def run():
        async with provider() as (endpoint, calls, control):
            config = configure(db, sysop, endpoint, min_level=11)
            with pytest.raises(BotError, match="level"):
                model.authorize(db, bot.id, channel.name, caller)
            model.save_settings(db, replace(config, min_level=0), actor=sysop, api_key="")
            set_guest_user(db, alice)
            with pytest.raises(BotError, match="Guest"):
                model.authorize(db, bot.id, channel.name, caller)
            set_guest_user(db, None)
            service = model.ModelService(lane)
            await exchange(service, bot, channel, caller, "disclosure")
            control["gate"] = asyncio.Event()
            pending = asyncio.create_task(exchange(service, bot, channel, caller, "in flight"))
            await eventually(lambda: calls)
            mute_bot(db, channel, bot, actor=sysop, mute=True)
            control["gate"].set()
            with pytest.raises(BotError, match="muted"):
                await pending
            assert not service.contexts[(bot.id, str(alice.id), channel.id)].lines
            assert model.usage_summary(db)["day"]["tokens"] == 20
    asyncio.run(run())


def test_stop_cancels_owned_http_keeps_reservation_and_help_remains_responsive(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel, model_conversation=True)
    async def run():
        async with provider() as (endpoint, calls, control):
            configure(db, sysop, endpoint)
            control["gate"] = asyncio.Event()
            runtime = BotRuntime(lane, hub, poll_seconds=.03)
            queue = hub.join(channel.name, ParticipantId("alice", 1))
            caller = {"id": alice.id, "name": alice.username}
            runtime.start()
            try:
                await eventually(lambda: bot.id in runtime.running_ids)
                worker = runtime.workers[bot.id]
                await runtime.enqueue(bot, channel, caller, "notice", ParticipantId("alice", 1))
                await next_line(queue, "Submit again")
                await runtime.enqueue(bot, channel, caller, "slow call", ParticipantId("alice", 1))
                await eventually(lambda: calls)
                await runtime.enqueue(bot, channel, caller, "help", ParticipantId("alice", 1))
                await next_line(queue, "reset clears")
                await runtime.close()
                assert not worker.model_tasks and not runtime.model.busy and not runtime.model.contexts
                row = db.connection.execute("SELECT * FROM bot_model_usage").fetchone()
                assert row["state"] == "reserved" and row["tokens"] > 256 and row["prompt"] is None
            finally:
                await runtime.close()
    asyncio.run(run())


@pytest.mark.parametrize("status,usage,body,match", [(401, None, "secret error body", "HTTP 401"),
    (200, None, "x" * 70000, "64 KiB"), (200, None, "  \n", "no usable text")], ids=["http-error", "oversized", "empty"])
def test_provider_failure_safe_and_accounted(db, lane, sysop, alice, channel, status, usage, body, match):
    bot = install(db, sysop, channel, model_conversation=True)
    caller = {"id": alice.id, "name": alice.username}
    async def run():
        async with provider() as (endpoint, calls, control):
            configure(db, sysop, endpoint)
            control.update(status=status, usage=usage, text=body)
            service = model.ModelService(lane)
            await exchange(service, bot, channel, caller, "notice")
            with pytest.raises(BotError, match=match):
                await exchange(service, bot, channel, caller, "actual")
            assert model.usage_summary(db)["day"]["estimated"] == 1
            assert not service.busy and not service.contexts[(bot.id, str(alice.id), channel.id)].lines
    asyncio.run(run())


def test_model_action_uses_host_original_and_rejects_forged_token(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel, model_conversation=True)
    async def run():
        async with provider() as (endpoint, calls, control):
            configure(db, sysop, endpoint)
            runtime = BotRuntime(lane, hub)
            worker = _Worker(runtime, bot)
            queue = hub.join(channel.name, ParticipantId("alice", 1))
            with pytest.raises(BotError, match="live addressed"):
                await worker._action({"action": "model", "request": "forged"})
            for prompt, needle in (("notice not sent", "Submit again"), ("original input", "A short model answer")):
                worker.pending[prompt] = (channel.name, ParticipantId("alice", 1), time.monotonic() + 60, None,
                                                {"id": alice.id, "name": alice.username}, prompt)
                await worker._action({"action": "model", "request": prompt, "text": "substituted secret", "caller": {"id": 999}})
                await next_line(queue, needle)
                await asyncio.gather(*list(worker.model_tasks.values()))
            assert calls[0][0]["messages"][-1]["content"] == "original input"
    asyncio.run(run())


def test_editor_masked_secret_consent_denial_and_atomic_save(db, lane, sysop, monkeypatch):
    from netbbs.net import bot_model_flow
    from netbbs.net.admin_flow import _load_chrome
    from tests.test_chat_flow_moderation import FakeSession
    prompts = []
    answers = iter((False, True))
    async def confirm(session, prompt, **kwargs):
        prompts.append(prompt)
        assert kwargs["default"] is False and not model.settings(db).enabled
        return next(answers)
    async def editor(session, lane, **kwargs):
        draft = kwargs["draft"]
        assert draft["endpoint"] == "http://127.0.0.1:11434/v1" and not draft["enabled"]
        draft.update(enabled=True, model="local", api_key="do-not-show", input_price="1.25", day_money="2.5")
        secret = next(f for f in kwargs["fields"] if f.key == "api_key")
        assert "do-not-show" not in secret.render(draft)
        with pytest.raises(BotError, match="draft has been kept"):
            await kwargs["save"](draft)
        assert get_config(db, model.SECRET_KEY) is None
        return await kwargs["save"](draft)
    monkeypatch.setattr(bot_model_flow, "edit_resource_draft", editor)
    monkeypatch.setattr(bot_model_flow, "prompt_yes_no", confirm)
    async def run():
        result = await bot_model_flow.model_editor(FakeSession(), lane, sysop, await _load_chrome(lane, sysop))
        assert result.enabled and result.input_price == 1250000 and result.day_money == 2500000
        assert len(prompts) == 2 and "Prompt logging off" in prompts[0]
    asyncio.run(run())


@pytest.mark.parametrize("access", [{"level": True, "authenticated": True}, {"level": 256, "authenticated": True},
                                      {"level": 1, "authenticated": 1}, {"level": 1}])
def test_link_access_fields_are_bounded(access):
    with pytest.raises(LinkProtocolError):
        build_bot_frame("bot_request", {"channel_id": "c", "bot": "Baud.bot", "caller": "alice", "text": "hello", "access": access})


def test_busy_reset_and_settings_change_discard_inflight_context(db, lane, sysop, alice, channel, monkeypatch):
    from types import SimpleNamespace
    clock = {"now": 1000.0}
    monkeypatch.setattr(model, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock["now"]))
    bot = install(db, sysop, channel, model_conversation=True)
    callers = [{"id": alice.id, "name": alice.username}]
    for name in ("bob", "carol"):
        user = create_user(db, name, password="hunter2", user_level=10)
        callers.append({"id": user.id, "name": name})
    async def run():
        async with provider() as (endpoint, calls, control):
            config = configure(db, sysop, endpoint)
            service = model.ModelService(lane)
            for caller in callers:
                await exchange(service, bot, channel, caller, "notice")
            control["gate"] = asyncio.Event()
            active_key = (bot.id, str(alice.id), channel.id)
            service.contexts[active_key].touched -= 1799
            jobs = [asyncio.create_task(exchange(service, bot, channel, c, "waiting")) for c in callers[:2]]
            await eventually(lambda: len(calls) == 2)
            clock["now"] += 2
            await service.upkeep()
            assert active_key in service.contexts  # accepted input is activity, even before the reply
            with pytest.raises(BotError, match="busy"):
                await exchange(service, bot, channel, callers[2], "third")
            monkeypatch.setattr(model, "MAX_CONTEXTS", 3)
            late = create_user(db, "late", password="hunter2", user_level=10)
            await exchange(service, bot, channel, {"id": late.id, "name": late.username}, "notice")
            assert active_key in service.contexts  # capacity eviction also respects recent input
            await exchange(service, bot, channel, callers[0], "reset", reset=True)
            model.save_settings(db, replace(config, persona="Changed persona"), actor=sysop, api_key="different-key")
            control["gate"].set()
            results = await asyncio.gather(*jobs, return_exceptions=True)
            assert all(isinstance(r, BotError) and "discarded" in str(r) for r in results)
            assert model.usage_summary(db)["day"]["tokens"] == 40
            assert not service.busy and all(not c.lines for c in service.contexts.values())
            assert "Submit again" in await exchange(service, bot, channel, callers[2], "new settings")
    asyncio.run(run())


def test_baud_availability_tracks_node_switch_without_losing_instance_grant(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel, model_conversation=True)
    async def run():
        async with provider() as (endpoint, calls, _control):
            config = configure(db, sysop, endpoint)
            model.save_settings(db, replace(config, enabled=False), actor=sysop, api_key="")
            runtime = BotRuntime(lane, hub, poll_seconds=.03)
            pid = ParticipantId(alice.username, 1)
            queue = hub.join(channel.name, pid)
            caller = {"id": alice.id, "name": alice.username}
            runtime.start()
            try:
                await eventually(lambda: bot.id in runtime.running_ids)
                await runtime.enqueue(bot, channel, caller, "help", pid)
                await next_line(queue, "No language model is enabled")
                await runtime.enqueue(bot, channel, caller, "unknown command", pid)
                await next_line(queue, "Conversation and other abilities are not enabled")
                for enabled in (True, False):
                    old = runtime.workers[bot.id]
                    model.save_settings(db, replace(config, enabled=enabled), actor=sysop, api_key="")
                    await eventually(lambda: bot.id in runtime.running_ids and runtime.workers[bot.id] is not old)
                    await runtime.enqueue(bot, channel, caller, "help", pid)
                    await next_line(queue, "Conversation is enabled" if enabled else "No language model is enabled")
                assert runtime.workers[bot.id].bot.model_conversation and not calls
            finally:
                await runtime.close()
    asyncio.run(run())


def test_reported_cost_and_reservation_survive_database_reopen(db, sysop, alice, channel):
    from netbbs.storage.database import Database
    bot = install(db, sysop, channel, model_conversation=True)
    config = configure(db, sysop, "http://localhost/v1", input_price=1000000, output_price=2000000, user_daily=2)
    caller = {"id": alice.id, "name": alice.username}
    messages = [{"role": "user", "content": "sample"}]
    first = model.reserve(db, bot.id, channel.name, caller, config, messages, "sample")
    model.settle(db, first, config, {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})
    model.reserve(db, bot.id, channel.name, caller, config, messages, "uncertain")
    with Database(db.path) as reopened:
        with pytest.raises(BotError, match="daily model quota"):
            model.reserve(reopened, bot.id, channel.name, caller, config, messages, "third")
        summary = model.usage_summary(reopened)
        assert summary["day"]["calls"] == 2 and summary["day"]["estimated"] == 1
        assert summary["users"][0]["caller"] == alice.username
        row = reopened.connection.execute("SELECT * FROM bot_model_usage WHERE id=?", (first,)).fetchone()
        assert row["cost_micros"] == 28 and row["tokens"] == 20


def test_unmetered_valid_response_is_capped_sanitized_and_reserved(db, lane, sysop, alice, channel):
    bot = install(db, sysop, channel, model_conversation=True)
    caller = {"id": alice.id, "name": alice.username}
    async def run():
        async with provider() as (endpoint, calls, control):
            configure(db, sysop, endpoint, reply_chars=80, token_parameter="max_tokens")
            control.update(usage=None, text="\x1b[31m" + "multiline\n" * 50)
            service = model.ModelService(lane)
            await exchange(service, bot, channel, caller, "notice")
            text = await exchange(service, bot, channel, caller, "actual")
            assert len(text) == 80 and "\x1b" not in text and "\n" not in text
            assert calls[0][0]["max_tokens"] == 256 and "max_completion_tokens" not in calls[0][0]
            assert model.usage_summary(db)["day"]["estimated"] == 1
    asyncio.run(run())


def test_real_link_model_disclosure_caller_claim_and_old_peer_fallback(tmp_path):
    from tests.test_bot_link import Node, connect, _setup_linked_channel
    from netbbs.link.events import CHAT_BOT_CALLERS_CAPABILITY, LINK_CAPABILITIES, build_endpoint_descriptor
    from netbbs.timeutil import utc_now_iso
    async def run():
        host, caller = Node(tmp_path, "host"), Node(tmp_path, "caller")
        hc, cc = _setup_linked_channel(host, caller)
        bot = save_bot(host.db, actor=host.sysop, name="Baud", channels=[hc.name], defaults=[],
                       linked_channels=[hc.name], enabled=True, model_conversation=True)
        queue = caller.hub.join(cc.name, ParticipantId("alice", 1))
        address = f"Baud.bot@{host.identity.fingerprint}: "
        try:
            async with provider() as (endpoint, calls, control):
                configure(host.db, host.sysop, endpoint, min_level=10)
                await connect((host, caller))
                await eventually(lambda: bot.id in host.runtime.running_ids)
                assert await caller.address(cc, address + "first remote input") is None
                await next_line(queue, "Submit again")
                assert not calls
                assert await caller.address(cc, address + "accepted remote input") is None
                await next_line(queue, "A short model answer")
                assert calls[0][0]["messages"][-1]["content"] == "accepted remote input"
                assert host.db.connection.execute("SELECT caller FROM bot_model_usage").fetchone()[0] == f"link:{caller.identity.fingerprint}:alice"
                set_guest_user(caller.db, caller.alice)
                assert await caller.address(cc, address + "guest input") is None
                await next_line(queue, "Guest accounts")
                set_guest_user(caller.db, None)
                # A new caller node talking to a host that advertised only the old
                # bot API omits the new fields; ordinary commands still work.
                caller.link_node.peers[host.identity.fingerprint].descriptor = build_endpoint_descriptor(
                    signing_identity=host.identity.signing_key, subject_fingerprint=host.identity.fingerprint,
                    addresses=[], outgoing_only=True, created_at=utc_now_iso(),
                    capabilities=tuple(c for c in LINK_CAPABILITIES if c != CHAT_BOT_CALLERS_CAPABILITY))
                assert await caller.address(cc, address + "legacy input") is None
                await next_line(queue, "caller-access support")
                assert await caller.address(cc, address + "help") is None
                await next_line(queue, "reset clears")
                assert len(calls) == 1
        finally:
            await caller.close()
            await host.close()
    asyncio.run(run())
