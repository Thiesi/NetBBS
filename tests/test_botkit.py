"""The bot developer kit (issue #1374): botkit, Baud and Tally on it, and Try."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

import netbbs.bots
from netbbs.bots import BOT_API_VERSION, botkit
from netbbs.bots.baud import Baud
from netbbs.bots.registry import save_bot
from netbbs.bots.runtime import BotRuntime
from netbbs.bots.state import state_root
from netbbs.bots.tally import Tally
from netbbs.bots.trial import STDERR_LIMIT, Trial, TrialSpec
from netbbs.chat.bots import BotPrivateAnswer
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.net import bot_flow
from netbbs.net.admin_ui import _load_chrome
from tests.test_bot_runtime import (  # noqa: F401
    db, lane, hub, sysop, alice, channel, bot_state, eventually, install, next_line,
)
from tests.test_bot_settings_ui import Screen
from tests.bounded import run_bounded

BOTS = Path(netbbs.bots.__file__).parent


def message(text, request="r", name="alice", channel="general"):
    return {"type": "message", "request": request, "caller": {"id": 7, "name": name}, "channel": channel, "text": text}


def test_helper_speaks_the_packages_one_api_version():
    assert botkit.API_VERSION == BOT_API_VERSION


@pytest.mark.parametrize("version, ready", [(BOT_API_VERSION, True), (BOT_API_VERSION + 1, False)])
def test_handshake_answers_only_its_own_version(version, ready, tmp_path):
    # By filename and without PYTHONPATH, as the host starts Baud.
    hello = json.dumps({"type": "hello", "version": version, "name": "Baud.bot", "state": {}}) + "\n"
    done = subprocess.run([sys.executable, "-u", str(BOTS / "baud.py")], input=hello, capture_output=True,
                          text=True, timeout=30, cwd=tmp_path, env={"PATH": "", "SYSTEMROOT": "C:\\Windows"})
    if ready:
        assert json.loads(done.stdout) == {"type": "ready", "version": BOT_API_VERSION}
    else:
        assert done.stdout == "" and done.returncode == 1
        assert f"botkit speaks BOT API version {BOT_API_VERSION}" in done.stderr
        assert f"offered version {version}" in done.stderr


def test_results_reach_their_own_callbacks_in_any_order():
    sent, answers = [], []

    class Asker(botkit.Bot):
        def on_message(self, msg):
            msg.query("stats", then=lambda result: msg.reply(result.text))

    bot = Asker({"queries": ["stats"]}, sent.append)
    bot.handle(message("one", request="r1", name="alice"))
    bot.handle(message("two", request="r2", name="bob"))
    first, second = sent
    assert first["id"] != second["id"]
    bot.handle({"type": "result", "id": second["id"], "ok": True, "value": {"text": "for bob"}})
    bot.handle({"type": "result", "id": first["id"], "ok": False, "error": "refused"})
    answers = [(a["request"], a["text"]) for a in sent[2:]]
    assert answers == [("r2", "bob: for bob"), ("r1", "alice: refused")]
    # A result nobody waits for, and an unknown event type, are ignored.
    bot.handle({"type": "result", "id": "999", "ok": True})
    bot.handle({"type": "door", "text": "a future event type"})
    assert len(sent) == 4


def test_reply_names_the_caller_and_keeps_to_the_line_limit():
    sent = []

    class Long(botkit.Bot):
        def on_message(self, msg):
            msg.reply("x" * 900)

    Long({}, sent.append).handle(message("hi"))
    assert sent[0]["text"].startswith("alice: x") and len(sent[0]["text"]) == botkit.LINE_LIMIT


def test_capabilities_come_from_the_grant_lists_not_scope():
    bot = botkit.Bot({"scope": "addressed", "reading_channels": ["lobby"], "queries": ["seen"],
                      "event_channels": ["lobby"], "model_conversation": True}, lambda value: None)
    assert bot.reading_channels == ["lobby"] and bot.queries == {"seen"}
    assert bot.event_channels == ["lobby"] and bot.model_conversation


def test_state_cache_changes_wait_for_saves_in_order():
    sent, seen = [], []
    bot = botkit.Bot({"state": {"n": 0}}, sent.append)
    bot.save({"n": 1})
    assert bot.saving and bot.state == {"n": 0}
    assert bot.change(lambda: seen.append(dict(bot.state)))
    assert not seen
    for _ in range(botkit.MAX_CHANGES - 1):
        bot.change(lambda: None)
    assert not bot.change(lambda: seen.append("refused"))
    bot.handle({"type": "result", "id": sent[0]["id"], "ok": True})
    assert seen == [{"n": 1}] and not bot.saving
    # A refused save leaves the cache as it was.
    bot.save({"n": 2})
    bot.handle({"type": "result", "id": sent[-1]["id"], "ok": False, "error": "16 KiB"})
    assert bot.state == {"n": 1}


def test_baud_reminders_sent_during_a_save_both_survive():
    # The helper's ordered writes replace Baud's own waiting slot: a second
    # remind while the first is saving builds on the saved first one.
    sent = []
    baud = Baud({}, sent.append)
    baud.handle(message("remind 1h first"))
    first = sent[-1]
    baud.handle(message("remind 1h second"))
    assert sent[-1] is first
    baud.handle({"type": "result", "id": first["id"], "ok": True})
    assert sent[-2]["text"].startswith("alice: Saved reminder #1 for ")
    assert sent[-1]["action"] == "state.set"
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True})
    assert [r["text"] for r in baud.state["reminders"]] == ["first", "second"]


def test_baud_answers_queries_while_another_is_outstanding():
    sent = []
    baud = Baud({"queries": ["stats"]}, sent.append)
    baud.handle(message("stats", request="a"))
    baud.handle(message("help", request="b"))
    assert sent[-1]["action"] == "reply" and sent[-1]["request"] == "b"
    baud.handle({"type": "result", "id": sent[0]["id"], "ok": True, "value": {"text": "3 online"}})
    assert sent[-1] == {"id": sent[-1]["id"], "action": "reply", "request": "a", "text": "alice: 3 online"}


def channel_line(text, name="alice", kind="message", channel="general"):
    return {"type": "channel", "kind": kind, "channel": channel, "caller": {"id": 7, "name": name}, "text": text}


def test_tally_counts_whole_channel_lines_and_says_them():
    sent = []
    tally = Tally({"reading_channels": ["general"]}, sent.append)
    tally.handle(channel_line("thanks bob++ and carol++ for the fix; alice++ c++ x--"))
    assert sent[-1]["action"] == "state.set"
    assert sent[-1]["value"]["karma"] == {"bob": ["bob", 1], "carol": ["carol", 1]}
    tally.handle({"type": "result", "id": sent[-1]["id"], "ok": True})
    # An observation has no request token: Tally says, it does not reply.
    assert sent[-1] == {"id": sent[-1]["id"], "action": "say", "channel": "general",
                        "text": "bob now has 1 karma; carol now has 1 karma."}
    tally.handle(channel_line("bob-- oops", name="dave"))
    tally.handle({"type": "result", "id": sent[-1]["id"], "ok": True})
    tally.handle(message("!karma Bob", name="carol"))
    assert sent[-1]["text"] == "carol: bob has 0 karma (top: carol 1, bob 0)."
    tally.handle(message("karma", name="erin"))
    assert sent[-1]["text"] == "erin: erin has 0 karma (top: carol 1, bob 0)."


def test_tally_says_nothing_when_the_save_is_refused(capsys):
    sent = []
    tally = Tally({"reading_channels": ["general"]}, sent.append)
    tally.handle(channel_line("bob++"))
    tally.handle({"type": "result", "id": sent[-1]["id"], "ok": False, "error": "Bot state exceeds 16 KiB."})
    assert sent[-1]["action"] == "state.set" and tally.state == {"karma": {}}
    assert "could not save karma: Bot state exceeds 16 KiB." in capsys.readouterr().err


def test_real_tally_reads_the_channel_and_answers_karma(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel, name="Tally", command=[sys.executable, "-u", str(BOTS / "tally.py")],
                  reading_channels=[channel.name])

    async def run():
        pid = ParticipantId(alice.username, 1)
        queue = hub.join(channel.name, pid)
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            line = record_message(db, channel, kind="message", author_label=alice.username, body="thanks bob++")
            await runtime.observe(channel, line, caller={"id": alice.id, "name": alice.username})
            await next_line(queue, "bob now has 1 karma.")
            assert await bot_state(lane, bot.storage_name) == {"karma": {"bob": ["bob", 1]}}
            ask = record_message(db, channel, kind="message", author_label=alice.username, body="!karma bob")
            assert await runtime.addressed(channel, alice, ask, pid) is None
            await next_line(queue, "alice: bob has 1 karma (top: bob 1).")
        finally:
            await runtime.close()
            hub.leave(channel.name, pid)

    asyncio.run(run())


def test_live_runtime_keeps_no_trace_and_no_stderr(db, lane, hub, sysop, channel):
    code = ("import json, sys; sys.stdin.readline(); sys.stderr.write(('SECRET' + '-STDERR ') * 50); sys.stderr.flush(); "
            "print(json.dumps({'type': 'ready', 'version': 1}), flush=True); sys.stdin.read()")
    bot = install(db, sysop, channel, command=[sys.executable, "-u", "-c", code])

    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            await asyncio.sleep(.3)
            assert runtime.trial is None
            worker = runtime.workers[bot.id]
            assert "SECRET-STDERR" not in repr(vars(worker)) and "SECRET-STDERR" not in repr(vars(runtime))
        finally:
            await runtime.close()

    asyncio.run(run())


def test_trial_runs_the_real_runtime_on_a_scratch_node(db, sysop, channel):
    # A live node with its own Baud: the trial must leave all of it alone.
    install(db, sysop, channel)
    root = state_root(db.path)

    async def run():
        trial = Trial(TrialSpec(), caller="sysop")
        await trial.start()
        try:
            directory = Path(trial.directory.name)
            await trial.wait_ready()
            await trial.line("!help")
            # Help answers the trying SysOp privately (issue #1372).
            await eventually(lambda: any(e.kind == "chat" and isinstance(e.value, BotPrivateAnswer)
                                         and "No language model" in e.value.text for e in trial.log.entries))
            await trial.line("!remind 1h water the plants")
            await eventually(lambda: any(e.kind == "chat" and "Saved reminder #1" in getattr(e.value, "body", "")
                                         for e in trial.log.entries))
            assert any(e.kind == "bot" and e.value.get("action") == "state.set" for e in trial.log.entries)
            kinds = {(e.kind, e.value.get("type") or e.value.get("action")) for e in trial.log.entries
                     if e.kind in {"host", "bot"}}
            assert {("host", "hello"), ("bot", "ready"), ("host", "message"), ("bot", "reply"),
                    ("host", "result")} <= kinds
            # Addressed exactly as in a channel: plain chat reaches nobody.
            before = trial.log.count
            await trial.line("just chatting")
            await asyncio.sleep(.3)
            assert not [e for e in trial.log.since(before) if e.kind in {"host", "bot"}]
        finally:
            await trial.close()
        return directory

    directory = run_bounded(run())
    assert not directory.exists()
    assert get_scrollback(db, channel) == []
    assert not (root / "Baud" / "state.json").exists()


def test_trial_rehearses_model_actions_and_keeps_a_bounded_stderr():
    code = ("import json, sys\n"
            "sys.stdin.readline()\n"
            "sys.stderr.write('A' * 9000 + 'TAIL')\n"
            "sys.stderr.flush()\n"
            "print(json.dumps({'type': 'ready', 'version': 1}), flush=True)\n"
            "for line in sys.stdin:\n"
            "    event = json.loads(line)\n"
            "    if event['type'] == 'message':\n"
            "        print(json.dumps({'id': 'm', 'action': 'model', 'request': event['request']}), flush=True)\n")
    spec = TrialSpec(name="Asker", command=[sys.executable, "-u", "-c", code], model_conversation=True)

    async def run():
        trial = Trial(spec, caller="sysop", model_enabled=True)
        await trial.start()
        try:
            await trial.wait_ready()
            await trial.line("Asker.bot: what is a modem?")
            await eventually(lambda: any(e.kind == "host" and e.value.get("type") == "result"
                                         for e in trial.log.entries))
            result = next(e.value for e in trial.log.entries if e.kind == "host" and e.value.get("type") == "result")
            assert result["ok"] and "would ask the model" in result["value"]["rehearsal"]
            await eventually(lambda: any(e.kind == "chat" and "Rehearsal" in str(e.value) for e in trial.log.entries))
            excerpt = trial.log.stderr_since(0)
            assert len(excerpt) == STDERR_LIMIT and excerpt.endswith("TAIL")
        finally:
            await trial.close()

    run_bounded(run())


def test_try_selected_and_the_editors_try_run_without_a_question(db, lane, sysop, channel, monkeypatch):
    save_bot(db, actor=sysop, name="Baud", channels=[channel.name], defaults=[channel.name])
    real_settle = Trial.settle

    async def settle(self, **kwargs):
        # Try shows what came within a short quiet spell, and a person
        # presses Enter for anything later. This scripted SysOp types on at
        # once, so it first waits for the answer it asked for: on a loaded
        # suite the answer can come after the quiet spell (issue #1454).
        await eventually(lambda: not any(worker.pending for worker in self.runtime.workers.values()))
        await real_settle(self, **kwargs)

    monkeypatch.setattr(Trial, "settle", settle)
    session = Screen(["t", "b"], lines=["01", "!help", "/quit"])
    run_bounded(bot_flow.bot_menu(session, lane, sysop))
    plain = session.plain
    assert "Try Baud.bot" in plain and "scratch #lobby" in plain
    assert "bot   > reply" in plain and "No language model is enabled." in plain
    assert "Try of Baud.bot ended. Nothing it did was kept." in plain
    assert get_scrollback(db, channel) == []

    # The editor's Try field is last, so the fields before it keep their numbers.
    async def edit():
        chrome = await _load_chrome(lane, sysop)
        screen = Screen(["3", "0", "b", "y"], lines=["/join bob", "/quit"])
        await bot_flow.bot_editor(screen, lane, sysop, chrome)
        return screen.plain
    plain = run_bounded(edit())
    assert "30 Try" in plain or "Try:" in plain
    assert "Try Baud.bot" in plain and "presence join bob" not in plain


def test_baud_reminder_saved_during_a_delivery_is_not_lost():
    # #1419 review: removing a delivered reminder waits for a save in flight,
    # or it builds on the state before that save and drops the new reminder.
    sent = []
    baud = Baud({"state": {"next": 2, "reminders": [{"id": 1, "user": 7, "name": "alice", "channel": "general",
                                                    "due": 0, "text": "due now"}]}}, sent.append)
    baud.handle({"type": "tick", "time": 1})
    say = sent[-1]
    assert say["action"] == "say.for"
    baud.handle(message("remind 1h keep me"))
    remind_save = sent[-1]
    assert remind_save["action"] == "state.set"
    baud.handle({"type": "result", "id": say["id"], "ok": True})
    # The removal waits behind the remind's save, and no tick re-says it.
    assert sent[-1] is remind_save
    baud.handle({"type": "tick", "time": 2})
    assert sent[-1] is remind_save
    baud.handle({"type": "result", "id": remind_save["id"], "ok": True})
    removal = sent[-1]
    assert removal["action"] == "state.set"
    assert [r["text"] for r in removal["value"]["reminders"]] == ["keep me"]
    baud.handle({"type": "result", "id": removal["id"], "ok": True})
    assert [r["text"] for r in baud.state["reminders"]] == ["keep me"]


def test_try_descriptions_sanitize_every_field():
    from netbbs.net.bot_try_flow import describe_bot, describe_host
    evil = "x\x1b[2J\x1b]0;spoof\x07"
    lines = [
        describe_bot({"action": "say", "channel": evil, "text": "hi"}),
        describe_bot({"action": "query", "query": evil}),
        describe_bot({"type": "ready", "version": evil}),
        describe_host({"type": "result", "id": evil, "ok": True}),
        describe_host({"type": "presence", "kind": evil, "caller": {"name": evil}, "channel": evil}),
        describe_host({"type": "hello", "version": evil, "channels": [evil]}),
    ]
    assert all("\x1b" not in line and "\x07" not in line for line in lines)


def test_try_flush_keeps_entries_added_while_it_renders():
    # #1419 review: the watermark is the snapshot's, so a line logged during
    # the render's awaits is shown on the next flush, never skipped.
    from netbbs.bots.trial import TrialLog
    from netbbs.net import bot_try_flow

    class Session:
        def __init__(self):
            self.written = []

        async def write_line(self, text):
            self.written.append(text)
            if len(self.written) == 1:
                log.add("status", ("restarting", "late arrival"))
                log.stderr(b"late stderr")

    class FakeTrial:
        pass

    log = TrialLog(model_enabled=False)
    trial = FakeTrial()
    trial.log = log
    log.add("status", ("running", ""))
    session = Session()

    async def run():
        shown, err = await bot_try_flow._flush(session, trial, 0, 0)
        await bot_try_flow._flush(session, trial, shown, err)
    asyncio.run(run())
    text = "".join(session.written)
    assert "late arrival" in text and "late stderr" in text


def test_trial_start_failure_leaves_nothing_behind(monkeypatch):
    # #1419 review: a failure after the scratch node is built, a
    # cancellation included, closes the lane, the listener and the directory.
    import netbbs.bots.runtime as runtime_module

    def refuse(self):
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime_module.BotRuntime, "start", refuse)

    seen = {}

    class Watched(Trial):
        async def close(self):
            if self.directory is not None:
                seen["directory"] = Path(self.directory.name)
                seen["lane"] = self.lane
                seen["listen"] = self._listen
            await super().close()

    async def run():
        trial = Watched(TrialSpec(), caller="sysop")
        with pytest.raises(asyncio.CancelledError):
            await trial.start()
        return trial

    trial = asyncio.run(run())
    assert trial.directory is None and trial.lane is None and trial._listen is None
    assert seen["lane"] is not None and seen["listen"].done()
    assert not seen["directory"].exists()


def _late_exits(monkeypatch, seconds):
    """The bot process's exit reaches the event loop `seconds` late once
    armed, as it can on a loaded machine (a full suite beside other suites)."""
    import asyncio.base_subprocess as base_subprocess
    real = base_subprocess.BaseSubprocessTransport._process_exited
    state = {"armed": False, "pending": 0}

    def late(transport, returncode):
        if not state["armed"]:
            return real(transport, returncode)
        state["pending"] += 1

        def arrive():
            state["pending"] -= 1
            real(transport, returncode)
        transport._loop.call_later(seconds, arrive)

    monkeypatch.setattr(base_subprocess.BaseSubprocessTransport, "_process_exited", late)
    return state


def test_try_quit_ends_although_the_bot_exit_is_seen_late(monkeypatch):
    # The Try test once hung a loaded suite for over 10 minutes in /quit:
    # the exit came after _reap's two 2-second waits, _reap raised
    # TimeoutError in place of the stop's CancelledError, and the worker
    # restarted the bot instead of stopping, so close() waited forever.
    late = _late_exits(monkeypatch, 5)
    # Baud, deaf to SIGTERM: on POSIX it is still alive for the SIGKILL, as
    # a bot that is slow to stop would be, so both of _reap's waits run out
    # there too. (On Windows terminate() kills it outright either way.)
    baud = str(BOTS / "baud.py")
    deaf = ("import runpy, signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"sys.path.insert(0, {str(BOTS)!r}); runpy.run_path({baud!r}, run_name='__main__')")

    async def run():
        trial = Trial(TrialSpec(command=[sys.executable, "-u", "-c", deaf]), caller="sysop")
        await trial.start()
        runtime = trial.runtime
        await trial.wait_ready()
        assert trial.bot.id in runtime.running_ids
        workers = list(runtime.workers.values())
        late["armed"] = True
        await trial.close()
        assert workers and all(w.task.done() for w in workers) and not runtime.workers
        # Let the late exits arrive before the loop closes.
        await eventually(lambda: late["pending"] == 0)

    run_bounded(run(), timeout=60)


def test_a_failing_session_cleanup_does_not_turn_a_stop_into_a_restart(monkeypatch, caplog):
    # The class behind the hang above: whatever cleanup raises on the way
    # out of a cancelled session, the stop still ends the worker.
    import netbbs.bots.runtime as runtime_module
    real_reap = runtime_module._Worker._reap

    async def failing_reap(self):
        await real_reap(self)
        if asyncio.current_task().cancelling():
            raise RuntimeError("cleanup failed")

    monkeypatch.setattr(runtime_module._Worker, "_reap", failing_reap)

    async def run():
        trial = Trial(TrialSpec(), caller="sysop")
        await trial.start()
        runtime = trial.runtime
        await trial.wait_ready()
        assert trial.bot.id in runtime.running_ids
        workers = list(runtime.workers.values())
        await trial.close()
        assert workers and all(w.task.cancelled() for w in workers)

    run_bounded(run(), timeout=60)
    # Stopped, and the failure is still told, not dropped.
    assert "cleanup failed while stopping (RuntimeError)" in caplog.text


# -- private answers (issue #1372)

def private_message(text, request="r", private=True, asked=False, channel="general"):
    return {**message(text, request=request, channel=channel), "private": private, "asked_privately": asked}


def test_private_reply_answers_the_caller_alone_without_their_name():
    sent = []

    class Whisper(botkit.Bot):
        def on_message(self, msg):
            seen.append((msg.private, msg.asked_privately))
            if msg.text == "whisper":
                msg.reply("x" * 900, private=True)
            elif msg.text == "talk":
                msg.model(private=True)
            else:
                msg.reply("as asked")

    seen = []
    bot = Whisper({}, sent.append)
    bot.handle(private_message("whisper"))
    assert sent[-1]["private"] is True and sent[-1]["text"] == "x" * botkit.LINE_LIMIT
    # Left out, the flag follows how the request was asked: a privately
    # asked one is answered privately, so no name goes in front.
    bot.handle(private_message("hi", asked=True))
    assert sent[-1]["text"] == "as asked" and "private" not in sent[-1]
    bot.handle(private_message("hi", asked=False))
    assert sent[-1]["text"] == "alice: as asked" and "private" not in sent[-1]
    bot.handle(private_message("talk"))
    assert sent[-1] == {"id": sent[-1]["id"], "action": "model", "request": "r", "private": True}
    # A host that sends neither flag reads as false.
    bot.handle(message("hi"))
    assert seen == [(True, False), (True, True), (True, False), (True, False), (False, False)]


def test_baud_help_is_private_when_it_can_reach_the_caller():
    sent = []
    baud = Baud({"name": "Baud.bot"}, sent.append)
    baud.handle(private_message("help"))
    assert sent[-1]["private"] is True and not sent[-1]["text"].startswith("alice:")
    assert sent[-1]["text"].endswith("Ask privately: /msg Baud.bot <text>.")
    assert "Replies are public" not in sent[-1]["text"]
    # A Link caller addresses the bot by its node.
    baud.handle({**private_message("help"), "caller": {"id": "link:abc:bob", "name": "bob@Home"}})
    assert sent[-1]["text"].endswith("/msg Baud.bot@Node <text>.")
    # A caller whose node cannot take a private answer is told the truth.
    baud.handle(private_message("help", private=False))
    assert "private" not in sent[-1] and sent[-1]["text"].startswith("alice: ")
    assert sent[-1]["text"].endswith("Replies are public.")


def test_baud_private_reminders_list_every_channel_public_ones_only_this_one():
    sent = []
    state = {"next": 3, "reminders": [
        {"id": 1, "user": 7, "name": "alice", "channel": "general", "due": 9e9, "text": "check the oven"},
        {"id": 2, "user": 7, "name": "alice", "channel": "staff", "due": 9e9, "text": "privateword"},
        {"id": 9, "user": 8, "name": "bob", "channel": "general", "due": 9e9, "text": "bobs own"}]}
    baud = Baud({"state": state}, sent.append)
    baud.handle(private_message("reminders"))
    assert sent[-1]["private"] is True
    assert sent[-1]["text"] == "#1 general: check the oven; #2 staff: privateword"
    baud.handle(private_message("reminders", private=False))
    assert "private" not in sent[-1] and sent[-1]["text"] == "alice: #1: check the oven"
    baud.handle(private_message("reminders", private=False, channel="lobby"))
    assert sent[-1]["text"] == "alice: No reminders here."
    baud = Baud({"state": {"next": 1, "reminders": []}}, sent.append)
    baud.handle(private_message("reminders", asked=True))
    assert sent[-1]["text"] == "You have no reminders." and sent[-1]["private"] is True


def test_baud_sysop_hint_does_not_call_a_private_line_public():
    sent = []
    baud = Baud({}, sent.append)
    baud.handle(private_message("sysop help", asked=True))
    assert sent[-1]["text"] == "I can't page anyone. Type /sysop <reason>: only the SysOp and staff see it."
    baud.handle(private_message("sysop help"))
    assert sent[-1]["text"].startswith("alice: I can't page anyone, and this line was public.")


def test_baud_answers_reminders_privately_through_the_real_runtime(db, lane, hub, sysop, alice, channel):
    # A public "!reminders" gets the private, every-channel answer: it goes
    # to the requesting session alone and never into the channel.
    from netbbs.bots.state import write_state
    bot = install(db, sysop, channel)
    write_state(db, bot.storage_name, {"next": 3, "reminders": [
        {"id": 1, "user": alice.id, "name": alice.username, "channel": channel.name, "due": 9e9, "text": "oven"},
        {"id": 2, "user": alice.id, "name": alice.username, "channel": "staff", "due": 9e9, "text": "rota"}]})

    async def run():
        pid, other = ParticipantId(alice.username, 1), ParticipantId(sysop.username, 2)
        queue, other_queue = hub.join(channel.name, pid), hub.join(channel.name, other)
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            message = record_message(db, channel, kind="message", author_label=alice.username, body="!reminders")
            assert await runtime.addressed(channel, alice, message, pid) is None
            answer = await next_line(queue, "rota")
            assert isinstance(answer, BotPrivateAnswer)
            assert answer.text == f"#1 {channel.name}: oven; #2 staff: rota"
            assert [m.body for m in get_scrollback(db, channel)] == ["!reminders"]
            assert other_queue.empty()
        finally:
            await runtime.close()
            hub.leave(channel.name, pid)
            hub.leave(channel.name, other)

    asyncio.run(run())


def test_baud_answers_every_privately_asked_command_privately(db, lane, hub, sysop, alice, channel):
    # /msg Baud.bot <command>: each command gets its answer, to the caller
    # alone, and nothing reaches the channel.
    from netbbs.chat.bots import BotNotice
    bot = install(db, sysop, channel)
    commands = ["help", "remind 1h water the plants", "reminders", "cancel 1", "cancel 99", "sysop help",
                "stats", "xyzzy", "help remind", "help tell", f"tell {sysop.username} hello", "tells",
                "cancel tell 99"]

    async def run():
        pid, other = ParticipantId(alice.username, 1), ParticipantId(sysop.username, 2)
        queue, other_queue = hub.join(channel.name, pid), hub.join(channel.name, other)
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        runtime.start()
        answers = []
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            for text in commands:
                assert await runtime.ask_privately(channel, alice, "Baud.bot", text, pid) is None
                answer = await asyncio.wait_for(queue.get(), 30)
                assert isinstance(answer, (BotPrivateAnswer, BotNotice)), (text, answer)
                answers.append(answer.text if isinstance(answer, BotPrivateAnswer) else str(answer))
            assert not [m for m in get_scrollback(db, channel) if m.author_kind == "bot"]
            assert other_queue.empty()
        finally:
            await runtime.close()
            hub.leave(channel.name, pid)
            hub.leave(channel.name, other)
        return answers

    answers = asyncio.run(run())
    assert answers[1].startswith("Saved reminder #1") and answers[2].startswith("#1 ")
    assert answers[3] == "Cancelled reminder #1." and not any(a.startswith("alice:") for a in answers)


def test_baud_private_cancel_reaches_every_id_the_private_listing_shows():
    # Review of #1422: an id the private listing shows from another channel
    # can be cancelled from here; a public cancel keeps to this channel.
    sent = []
    state = {"next": 3, "reminders": [
        {"id": 2, "user": 7, "name": "alice", "channel": "staff", "due": 9e9, "text": "rota"}]}
    baud = Baud({"state": state}, sent.append)
    baud.handle(private_message("cancel 2", private=False))
    assert sent[-1]["text"] == "alice: No reminder with that id belongs to you in this channel."
    baud.handle(private_message("cancel 2"))
    assert sent[-1]["action"] == "state.set" and sent[-1]["value"]["reminders"] == []
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True})
    assert sent[-1]["text"] == "alice: Cancelled reminder #2."
    baud.handle(private_message("cancel 5", asked=True))
    assert sent[-1]["text"] == "No reminder with that id is yours."
