"""Baud's social commands (issue #1379): dice, the coin, per-channel quotes
and quick polls, and the host's caller facts and hello fields behind them."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from netbbs.bots import baud as baud_module
from netbbs.bots.baud import Baud, POLL_VOTERS, PRESETS
from netbbs.bots.registry import get_bot, local_caller, save_bot
from netbbs.bots.runtime import BotRuntime
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import record_message
from netbbs.guest import set_guest_user
from netbbs.moderation import ChannelPermission
from netbbs.moderation.roles import grant_permissions
from netbbs.net import bot_flow
from netbbs.net.admin_ui import _load_chrome
from tests.test_bot_runtime import (  # noqa: F401
    db, lane, hub, sysop, alice, channel, eventually, eventually_state, install, next_line,
)
from tests.test_bot_settings_ui import Screen


def message(text, *, request="r", caller_id=7, name="alice", channel="general", moderator=False, guest=False,
            private=True, asked=False):
    return {"type": "message", "request": request, "channel": channel, "text": text,
            "caller": {"id": caller_id, "name": name, "moderator": moderator, "guest": guest},
            "private": private, "asked_privately": asked}


class Harness:
    """Baud with every save answered at once: ok, or refused when `full`."""

    def __init__(self, **hello):
        self.sent = []
        self.full = False
        self.baud = Baud(hello, lambda value: self.emit(value))

    def emit(self, value):
        self.sent.append(value)
        if value.get("action") == "state.set":
            self.baud.handle({"type": "result", "id": value["id"], "ok": not self.full,
                              **({"error": "Bot state is limited to 16 KiB."} if self.full else {})})

    def __call__(self, text, **kwargs):
        self.baud.handle(message(text, **kwargs))
        return self.sent[-1]

    def tick(self, when):
        self.baud.handle({"type": "tick", "time": when})
        return self.sent[-1] if self.sent else None


# -- dice and the coin: always on


def test_roll_defaults_to_one_d6_and_lists_each_die_and_the_total(monkeypatch):
    rolls = iter([4, 3, 5, 6, 2])
    monkeypatch.setattr(baud_module._dice, "randint", lambda low, high: next(rolls))
    bot = Harness()
    assert bot("!roll")["text"] == "alice: rolled 1d6: 4."
    assert bot("roll 2D6+3")["text"] == "alice: rolled 2d6+3: 3 + 5 + 3 = 11."
    assert bot("roll d20-1")["text"] == "alice: rolled 1d20-1: 6 - 1 = 5."
    assert bot("roll 1d6+0")["text"] == "alice: rolled 1d6+0: 2 + 0 = 2."


@pytest.mark.parametrize("spec", ["0d6", "21d6", "1d1", "1d1001", "1d6+1001", "2x6", "1d6 extra", "99999d6"])
def test_roll_out_of_range_gets_usage(spec):
    assert Harness()("roll " + spec)["text"].startswith("alice: Use roll [NdS+M]: N 1-20 dice of S 2-1000 sides")


def test_roll_bounds_are_inclusive_and_fit_one_line():
    reply = Harness()("roll 20d1000+1000")["text"]
    assert reply.startswith("alice: rolled 20d1000+1000: ") and len(reply) <= 500


def test_roll_uses_system_randomness_and_coin_answers_heads_or_tails():
    import random
    assert isinstance(baud_module._dice, random.SystemRandom)
    assert Harness()("coin")["text"] in {"alice: flips a coin: heads.", "alice: flips a coin: tails."}


def test_help_lists_only_the_enabled_abilities():
    off = Harness()("help")["text"]
    assert "roll|coin; " in off and "quote" not in off and "poll" not in off
    on = Harness(quotes=True, polls=True)("help")["text"]
    assert "roll|coin; quote; poll; " in on
    assert Harness()("frobnicate")["text"].endswith("roll and coin. Conversation is not enabled.")


@pytest.mark.parametrize("private", [True, False])
def test_help_line_fits_whole_with_every_grant_switch_and_the_longest_names(private):
    # The one-line help for an older host, with the longest opening, the
    # longest bot name, a 32-character Link caller, model conversation, both
    # queries and both switches: whole, never cut. `top <door>; ` (issue
    # #1408) used the 12 characters of room this once left.
    from netbbs.bots import botkit
    sent = []
    baud = Baud({"name": "B" * 24 + ".bot", "queries": ["stats", "seen", "top"], "model_conversation": True,
                 "quotes": True, "polls": True, "phrases": {"help": "x" * 168 + "{name}"}}, sent.append)
    baud.handle(message("help", caller_id="link:" + "a" * 64 + ":nib", name="n" * 26 + "@Node1", private=private))
    text = sent[-1]["text"]
    assert len(text) <= botkit.LINE_LIMIT and "top <door>; " in text
    assert "roll|coin; quote; poll; " in text and "Conversation is on; reset clears your context." in text
    assert text.endswith(f"Ask privately: /msg {'B' * 24}.bot@Node <text>." if private else "Replies are public.")


def test_switched_off_commands_are_unknown():
    bot = Harness()
    assert "I know" in bot("quote add hi")["text"]
    assert "I know" in bot("poll Best? | a | b")["text"]
    assert "I know" in bot("vote 1")["text"]
    assert not bot.baud.state.get("quotes") and not bot.baud.state.get("polls")


# -- quotes


def test_quotes_are_saved_shown_and_numbered_per_channel():
    bot = Harness(quotes=True)
    assert bot("quote")["text"] == "alice: No quotes here yet. Add one with quote add <text>."
    assert bot("quote add <Quill> the modem is on fire again")["text"] == "alice: Saved quote #1 (added by alice)."
    assert bot("quote add elsewhere", channel="private")["text"] == "alice: Saved quote #2 (added by alice)."
    assert bot("quote 1")["text"] == 'alice: #1 "<Quill> the modem is on fire again" (added by alice)'
    assert bot("quote")["text"] == 'alice: #1 "<Quill> the modem is on fire again" (added by alice)'
    # Text from another, possibly private, channel never surfaces here.
    assert bot("quote 2")["text"] == "alice: No quote #2 here."
    assert bot("quote del 2")["text"] == "alice: No quote #2 here."
    assert bot("quote add " + "x" * 161)["text"] == "alice: Use quote add <text of 1-160 characters>."
    assert bot("quote sideways")["text"].startswith("alice: Use quote, quote <n>, quote add")


def test_guests_cannot_add_quotes():
    bot = Harness(quotes=True)
    assert bot("quote add hi", guest=True)["text"] == "alice: Guests cannot add quotes."
    assert not bot.baud.state.get("quotes")


def test_only_the_adder_or_a_moderator_deletes_and_a_moderator_is_said_publicly():
    bot = Harness(quotes=True)
    bot("quote add one")
    bot("quote add two")
    assert bot("quote del 1", caller_id=8, name="bob")["text"] == (
        "bob: Only the person who added quote #1 or a channel moderator can delete it.")
    assert bot("quote del 1")["text"] == "alice: Deleted quote #1."
    assert bot("quote del 2", caller_id=9, name="ann", moderator=True)["text"] == "ann: Quote #2 deleted by ann."
    assert bot.baud.state["quotes"] == []
    # Asked privately, the moderator's deletion still reaches the channel.
    bot("quote add three")
    bot("quote del 3", caller_id=9, name="ann", moderator=True, asked=True)
    say, reply = bot.sent[-2], bot.sent[-1]
    assert say["action"] == "say" and say["channel"] == "general" and say["text"] == "Quote #3 deleted by ann."
    assert reply["action"] == "reply" and "Deleted quote #3" in reply["text"]


def test_quote_caps_per_adder_and_per_bot():
    bot = Harness(quotes=True)
    for i in range(10):
        bot(f"quote add mine {i}")
    assert bot("quote add one more")["text"].startswith("alice: Quote limit reached (10 per caller, 30 per bot).")
    for i in range(20):
        bot(f"quote add {i}", caller_id=100 + i % 2, name="other")
    assert len(bot.baud.state["quotes"]) == 30
    assert "Quote limit reached" in bot("quote add last", caller_id=200, name="new")["text"]


def test_a_refused_save_says_memory_full_and_keeps_the_old_state():
    bot = Harness(quotes=True, polls=True)
    bot.full = True
    assert bot("quote add hello")["text"] == "alice: " + PRESETS["full"]["friendly"]
    assert bot("poll Best? | a | b")["text"] == "alice: Baud's memory is full; ask the SysOp."
    assert not bot.baud.state.get("quotes") and not bot.baud.state.get("polls")
    custom = Harness(quotes=True, phrases={"full": "Speicher voll, {name}."})
    custom.full = True
    assert custom("quote add hello")["text"] == "alice: Speicher voll, alice."


# -- quick polls


def test_poll_opens_for_ten_minutes_and_one_per_channel(monkeypatch):
    monkeypatch.setattr(baud_module.time, "time", lambda: 1000.0)
    bot = Harness(polls=True)
    opened = bot("poll Best door? | Voidrunner | War Dialer | Trivia")["text"]
    assert opened == ("alice: Poll #1 is open for 10 min: Best door? 1 Voidrunner, 2 War Dialer, 3 Trivia. "
                      "Vote with !vote <n>. Votes are public.")
    assert bot.baud.state["polls"][0]["due"] == 1600.0
    assert bot("poll 5m Again? | a | b")["text"].startswith("alice: Poll #1 is already open here; it closes at ")
    assert bot("poll 25h Long? | a | b", channel="other")["text"].startswith("alice: Use poll")
    assert bot("poll Only one? | a", channel="other")["text"].startswith("alice: Use poll")
    assert bot("poll Seven? | 1 | 2 | 3 | 4 | 5 | 6 | 7", channel="other")["text"].startswith("alice: Use poll")
    assert "open for 24 h" in bot("poll 24h Day? | a | b", channel="other")["text"]


def test_polls_per_bot_cap_and_guests_cannot_open():
    bot = Harness(polls=True)
    assert bot("poll Q? | a | b", guest=True)["text"] == "alice: Guests can vote but cannot open polls."
    for name in ("one", "two", "three", "four"):
        bot("poll Q? | a | b", channel=name)
    assert bot("poll Q? | a | b", channel="five")["text"] == (
        "alice: 4 polls are already open; try again when one closes.")


def test_linked_channel_poll_gives_the_explicit_address():
    bot = Harness(name="Baud.bot", polls=True, linked_channels=["general"], node="Roanoke")
    opened = bot("poll Best door? | Voidrunner | War Dialer")["text"]
    assert opened.endswith("Vote with !vote <n>, or from another BBS: Baud.bot@Roanoke: vote <n>. Votes are public.")


def test_votes_are_acknowledged_privately_changeable_and_results_come_at_close():
    bot = Harness(polls=True)
    bot("poll Best door? | Voidrunner | War Dialer | Trivia")
    ack = bot("vote 1", caller_id=8, name="ann")
    assert ack["private"] is True and ack["text"].startswith("Vote counted: 1 Voidrunner. Change it until ")
    bot("vote 2", caller_id=8, name="ann")  # changed, not added
    bot("vote 2", caller_id="link:" + "a" * 64 + ":quill", name="quill@Vertigo")
    bot("vote 1", guest=True, caller_id=11, name="guest")  # guests vote
    # A caller whose node cannot take a private answer is acknowledged publicly.
    public = bot("vote 1", caller_id="link:" + "b" * 64 + ":old", name="old@Older", private=False)
    assert "private" not in public and public["text"].startswith("old@Older: Vote counted: 1 Voidrunner.")
    assert bot("vote 9")["text"] == "alice: Vote with vote 1-3 on poll #1: Best door?"
    status = bot("poll", name="nib")["text"]
    assert status.startswith("nib: Poll #1: Best door? 4 voters so far, closes ") and status.endswith("Results then.")
    assert "Voidrunner 2" not in status
    assert bot("poll close")["text"] == "alice: Closing poll #1; the results follow in the channel."
    assert bot("vote 1")["text"] == "alice: Poll #1 has closed; the results follow."
    said = bot.tick(time.time() + 1)
    assert said["action"] == "say" and said["text"] == (
        "Poll #1 closed: Best door? Voidrunner 2 (50%), War Dialer 2 (50%), Trivia 0 (0%). 4 voters.")
    bot.baud.handle({"type": "result", "id": said["id"], "ok": True})
    assert bot.baud.state["polls"] == []
    assert bot("vote 1")["text"] == "alice: No poll is open here."


def test_early_close_by_opener_or_moderator_only():
    bot = Harness(polls=True)
    bot("poll Q? | a | b")
    assert bot("poll close", caller_id=8, name="bob")["text"] == (
        "bob: Only the person who opened poll #1 or a channel moderator can close it early.")
    assert bot("poll close", caller_id=9, name="ann", moderator=True)["text"] == (
        "ann: Closing poll #1; the results follow in the channel.")
    assert bot("poll close")["text"] == "alice: Poll #1 is closing now."
    # The results are said in the channel, with retries, even when the
    # first attempt is refused (review of #1425).
    said = bot.tick(time.time() + 1)
    assert said["action"] == "say" and said["text"] == "Poll #1 closed: Q? No votes."
    bot.baud.handle({"type": "result", "id": said["id"], "ok": False, "error": "Rate limited."})
    assert bot.baud.state["polls"]
    retry = bot.tick(time.time() + 61)
    assert retry is not said and retry["text"] == "Poll #1 closed: Q? No votes."
    bot.baud.handle({"type": "result", "id": retry["id"], "ok": True})
    assert bot.baud.state["polls"] == []


def test_voter_cap_counts_the_votes_not_counted():
    bot = Harness(polls=True)
    bot("poll Q? | a | b")
    for voter in range(POLL_VOTERS):
        bot("vote 1", caller_id=1000 + voter)
    assert bot("vote 2", caller_id=1)["text"] == "Poll #1 already has 100 voters; your vote was not counted."
    assert bot("vote 2", caller_id=1000)["text"].startswith("Vote counted: 2 b.")  # a counted voter may change
    bot("poll close")
    assert bot.tick(time.time() + 1)["text"].endswith("100 voters. 1 vote not counted (limit 100 voters).")
    # The state stays compact: a voter is a short digest of the caller id.
    assert len(json.dumps(bot.baud.state)) < 4000


def test_due_poll_closes_on_a_tick_with_retries_and_survives_a_restart(monkeypatch):
    monkeypatch.setattr(baud_module.time, "time", lambda: 1000.0)
    bot = Harness(polls=True)
    bot("poll 1m Q? | a | b")
    bot("vote 2", caller_id=8)
    # A restarted Baud has the poll from its saved state.
    restarted = Harness(polls=True, state=bot.baud.state)
    assert restarted.tick(1059) is None
    say = restarted.tick(1060)
    assert say["action"] == "say" and say["channel"] == "general"
    assert say["text"] == "Poll #1 closed: Q? a 0 (0%), b 1 (100%). 1 voter."
    restarted.baud.handle({"type": "result", "id": say["id"], "ok": False, "error": "Rate limited."})
    assert restarted.baud.state["polls"]  # kept for the retry
    assert restarted.tick(1059) is say  # backoff: not said again yet
    retry = restarted.tick(1060)
    assert retry is not say and retry["action"] == "say"
    restarted.baud.handle({"type": "result", "id": retry["id"], "ok": True})
    assert restarted.baud.state["polls"] == []
    assert restarted.tick(5000)["action"] == "state.set"  # nothing more is said


def test_a_reminder_keeps_the_quotes_and_polls():
    # Review of #1425: a remind saved a fresh state and dropped the rest.
    bot = Harness(quotes=True, polls=True)
    bot("quote add keep me")
    bot("poll Q? | a | b")
    bot("remind 1h water the plants")
    assert [q["t"] for q in bot.baud.state["quotes"]] == ["keep me"] and bot.baud.state["polls"]
    assert bot("quote add next")["text"] == "alice: Saved quote #2 (added by alice)."


def test_close_waits_for_a_vote_being_saved():
    sent = []
    baud = Baud({"polls": True, "state": {"next": 1, "reminders": [], "pnext": 2, "polls": [
        {"n": 1, "c": "general", "u": 7, "q": "Q?", "o": ["a", "b"], "due": 4e9, "v": {}, "x": 0}]}}, sent.append)
    baud.handle(message("vote 1", caller_id=8))
    vote_save = sent[-1]
    assert vote_save["action"] == "state.set"
    baud.handle({"type": "tick", "time": 4e9 + 10})
    assert sent[-1] is vote_save  # no results while the vote is unsaved
    baud.handle({"type": "result", "id": vote_save["id"], "ok": True})
    baud.handle({"type": "tick", "time": 4e9 + 11})
    assert sent[-1]["action"] == "say" and sent[-1]["text"].endswith("a 1 (100%), b 0 (0%). 1 voter.")


def test_remote_vote_to_this_nodes_baud_gets_a_pointer_even_with_polls_off():
    bot = Harness(name="Baud.bot", linked_channels=["general"], node="Vertigo")
    reply = bot("vote 1")["text"]
    assert reply.startswith("alice: No poll is open here. A poll opened on another BBS takes its own bot's address")
    assert "Baud.bot@<BBS>: vote <n>" in reply
    assert "I know" in bot("vote 1", channel="local")["text"]


# -- the host: switches, hello fields and caller facts


def test_switches_are_off_by_default_and_saved_per_instance(db, sysop, channel):
    bot = save_bot(db, actor=sysop, name="Baud", channels=[channel.name], defaults=[])
    assert (bot.quotes, bot.polls) == (False, False)
    assert db.connection.execute("SELECT quotes, polls FROM chat_bots WHERE id=?", (bot.id,)).fetchone()[:] == (0, 0)
    bot = save_bot(db, actor=sysop, bot_id=bot.id, name="Baud", channels=[channel.name], defaults=[],
                   quotes=True, polls=True)
    assert (get_bot(db, bot.id).quotes, get_bot(db, bot.id).polls) == (True, True)
    detail = db.connection.execute("SELECT detail FROM moderation_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "quotes/polls=True/True" in detail


def test_editor_switches_quotes_and_polls(db, lane, sysop, channel):
    bot = save_bot(db, actor=sysop, name="Baud", channels=[channel.name], defaults=[])
    session = Screen(["2", "5", "2", "6", "s"], height=60)

    async def run():
        return await bot_flow.bot_editor(session, lane, sysop, await _load_chrome(lane, sysop), bot=bot)

    saved = asyncio.run(run())
    assert "Baud quotes" in session.plain and "Baud quick polls" in session.plain
    assert (saved.quotes, saved.polls) == (True, True)


def test_local_caller_facts(db, sysop, alice, channel):
    from netbbs.auth.users import create_user
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    assert local_caller(db, channel, sysop) == {"id": sysop.id, "name": "sysop", "moderator": True, "guest": False}
    assert local_caller(db, channel, alice)["moderator"] is False
    grant_permissions(db, bob, object_type="channel", object_id=channel.id, permissions=ChannelPermission.MODERATE,
                      granted_by=sysop)
    assert local_caller(db, channel, bob)["moderator"] is True
    set_guest_user(db, alice)
    assert local_caller(db, channel, alice)["guest"] is True


def test_message_events_carry_caller_facts_and_hello_carries_linked_channels(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel, quotes=True,
                  command=[sys.executable, "-u", str(Path(__file__).parent / "fixtures" / "bot_caller_facts.py")])

    async def run():
        pid = ParticipantId(alice.username, 3)
        queue = hub.join(channel.name, pid)
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            for user in (alice, sysop):
                sent = record_message(db, channel, kind="message", author_label=user.username, body="!facts")
                assert await runtime.addressed(channel, user, sent, pid) is None
            first = json.loads((await next_line(queue, "facts ")).body.split("facts ", 1)[1])
            second = json.loads((await next_line(queue, "facts ")).body.split("facts ", 1)[1])
        finally:
            await runtime.close()
            hub.leave(channel.name, pid)
        return first, second

    first, second = asyncio.run(run())
    assert first == {"moderator": False, "guest": False, "linked": [], "node": first["node"],
                     "quotes": True, "polls": False}
    assert isinstance(first["node"], str) and first["node"]
    assert second["moderator"] is True


def test_linked_poll_remote_vote_by_address_and_pointer_from_the_callers_own_baud(tmp_path):
    from netbbs.chat.scrollback import get_scrollback
    from netbbs.config import get_node_display_name
    from tests.test_bot_link import Node, connect
    from tests.test_link_realtime_channels import _setup_linked_channel

    async def run():
        origin, first = Node(tmp_path, "origin"), Node(tmp_path, "first")
        oc, fc = _setup_linked_channel(origin, first)
        save_bot(origin.db, actor=origin.sysop, name="Baud", channels=[oc.name], defaults=[oc.name],
                 linked_channels=[oc.name], enabled=True, polls=True)
        first.install(fc)  # the caller's own Baud, polls off
        oq = origin.hub.join(oc.name, ParticipantId("alice", 1))
        fq = first.hub.join(fc.name, ParticipantId("alice", 1))
        try:
            await connect((origin, first))
            await eventually(lambda: origin.bots and first.bots.visible_roster(first.db, fc))
            assert await origin.address(oc, "!poll Best door? | Voidrunner | War Dialer") is None
            node = get_node_display_name(origin.db)
            opened = await next_line(oq, "Poll #1 is open")
            assert f"or from another BBS: Baud.bot@{node}: vote <n>." in opened.body
            # Shorthand stays on the caller's node: their Baud points the way.
            assert await first.address(fc, "!vote 1") is None
            await next_line(fq, "No poll is open here. A poll opened on another BBS")
            address = f"Baud.bot@{origin.identity.fingerprint}: "
            assert await first.address(fc, address + "vote 2") is None
            state = await eventually_state(origin.lane, "Baud", lambda state: state["polls"][0]["v"])
            assert list(state["polls"][0]["v"].values()) == [1]
            # A Link caller is never a moderator at the bot's host.
            assert await first.address(fc, address + "poll close") is None
            await next_line(fq, "Only the person who opened poll #1")
            await eventually(lambda: not first.bots.requests)
            assert not any("Vote counted" in m.body for m in get_scrollback(origin.db, oc))
        finally:
            for n in (first, origin):
                await n.close()

    asyncio.run(run())


def test_help_block_with_every_grant_and_switch_fits_twelve_rows():
    from netbbs.bots.registry import check_block
    sent = []
    baud = Baud({"blocks": True, "queries": ["stats", "seen"], "model_conversation": True, "quotes": True,
                 "polls": True, "phrases": {"help": "x" * 200}}, sent.append)
    baud.handle(message("help"))
    block = check_block(sent[-1]["block"])
    labels = [label for label, _ in block.rows]
    assert len(block.rows) == 12 and block.rows[-1][1].startswith("Conversation is enabled")
    assert {"stats, seen <name>", "roll [NdS+M], coin", "quote [<n>|add|del]", "poll, vote <n>",
            "/sysop <reason>"} <= set(labels)
    off = Baud({"blocks": True}, sent.append)
    off.handle(message("help"))
    labels = [label for label, _ in check_block(sent[-1]["block"]).rows]
    assert "roll [NdS+M], coin" in labels and not any(l.startswith(("quote", "poll")) for l in labels)
