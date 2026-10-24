"""Host-held bot lines and tells (issues #1375, #1376) at real SQLite, hub and runtime boundaries."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from types import SimpleNamespace

import pytest

from netbbs.auth.renames import rename_user
from netbbs.auth.users import create_user
from netbbs.bots import held
from netbbs.bots.registry import BotError, save_bot
from netbbs.bots.runtime import BotRuntime, _Worker
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import get_scrollback
from netbbs.mail import block_link_sender, block_local_sender, unblock_local_sender
from netbbs.messaging_preferences import set_accepts_direct_messages
from netbbs.timeutil import utc_iso
from tests.test_chat_bots import sysop, alice, channel  # noqa: F401
from tests.test_chat_flow_moderation import FakeSession, db, lane, hub, presence, mailbox, history  # noqa: F401
from tests.test_bot_runtime import CROSS_PROCESS_TIMEOUT, eventually_state

WEEK = 7 * 24 * 3600


def install(db, sysop, channel, **kwargs):
    return save_bot(db, actor=sysop, name="Baud", channels=[channel.name], defaults=[channel.name],
                    enabled=True, **kwargs)


@pytest.fixture
def nib(db):
    return create_user(db, "nib", password="hunter2", user_level=10)


def bot_lines(db, channel):
    return [m.body for m in get_scrollback(db, channel) if m.author_kind == "bot"]


def held_rows(db):
    return db.connection.execute("SELECT * FROM bot_held_lines ORDER BY id").fetchall()


def expire_all(db):
    past = utc_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))
    db.connection.execute("UPDATE bot_held_lines SET expires_at = ?", (past,))
    db.connection.commit()


class Harness:
    """A runtime with one ready worker and no process: the worker's events
    queue is what the bot would read."""

    def __init__(self, lane, hub, bot, link=None):
        self.runtime = BotRuntime(lane, hub, poll_seconds=.02)
        self.runtime.link = link
        self.worker = _Worker(self.runtime, bot)
        self.worker.ready = True
        self.runtime.workers[bot.id] = self.worker
        self.runtime.delivery_task = None

    async def __aenter__(self):
        self.runtime.delivery_task = asyncio.create_task(self.runtime._deliveries())
        return self

    async def __aexit__(self, *exc):
        self.runtime.delivery_task.cancel()
        await asyncio.gather(self.runtime.delivery_task, return_exceptions=True)

    async def act(self, **value):
        return await self.worker._action(value)

    def request(self, channel, caller):
        token = f"req-{len(self.worker.pending)}"
        self.worker.pending[token] = (channel.name, None, time.monotonic() + 60, None, dict(caller), "")
        return token

    async def event(self, kind="held", timeout=30):
        async with asyncio.timeout(timeout):
            while True:
                event = await self.worker.events.get()
                if event["type"] == kind:
                    return event

    async def settle(self):
        # Every queued join handled, not merely taken off the queue.
        async with asyncio.timeout(30):
            await self.runtime.joins.join()


def test_say_for_a_present_caller_publishes_at_once(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel)

    async def run():
        hub.join(channel.name, ParticipantId(alice.username, 1))
        async with Harness(lane, hub, bot) as h:
            result = await h.act(action="say.for", channel=channel.name, caller=alice.id,
                                 text="alice: reminder #1: tea", held_text="alice: held", expires=WEEK)
            assert result["delivered"] and result["message_id"]
        assert bot_lines(db, channel) == ["alice: reminder #1: tea"]
        assert held_rows(db) == []

    asyncio.run(run())


def test_held_line_waits_for_the_next_join_and_two_sessions_get_one(db, lane, hub, sysop, alice, channel):
    other = create_channel(db, "other", creator=sysop)
    bot = save_bot(db, actor=sysop, name="Baud", channels=[channel.name, other.name], defaults=[], enabled=True)

    async def run():
        async with Harness(lane, hub, bot) as h:
            result = await h.act(action="say.for", channel=channel.name, caller=alice.id,
                                 text="now", held_text="alice: reminder #1 (held): tea", expires=WEEK)
            assert result["held"] and isinstance(result["held_id"], int)
            assert (await h.act(action="held.list"))[0]["caller"] == alice.id
            # A join elsewhere is not this channel: text never crosses channels.
            h.runtime.joined(other, alice.id)
            await h.settle()
            assert bot_lines(db, channel) == [] and len(held_rows(db)) == 1
            # Two sessions of one account join at once: one delivery.
            hub.join(channel.name, ParticipantId(alice.username, 1))
            hub.join(channel.name, ParticipantId(alice.username, 2))
            h.runtime.joined(channel, alice.id)
            h.runtime.joined(channel, alice.id)
            event = await h.event()
            assert event == {"type": "held", "held_id": result["held_id"], "outcome": "delivered",
                             "message_id": event["message_id"]}
            await h.settle()
        assert bot_lines(db, channel) == ["alice: reminder #1 (held): tea"]
        assert held_rows(db) == []

    asyncio.run(run())


def test_held_lines_survive_a_runtime_restart(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel)

    async def run():
        async with Harness(lane, hub, bot) as h:
            await h.act(action="say.for", channel=channel.name, caller=alice.id, text="x",
                        held_text="alice: after restart", expires=WEEK)
        # A new runtime (a node restart) knows nothing but the table.
        async with Harness(lane, hub, bot) as h:
            hub.join(channel.name, ParticipantId(alice.username, 1))
            h.runtime.joined(channel, alice.id)
            assert (await h.event())["outcome"] == "delivered"
        assert bot_lines(db, channel) == ["alice: after restart"]

    asyncio.run(run())


def test_refused_publication_stays_held_and_expiry_drops_silently(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel, lines_per_hour=1)

    async def run():
        async with Harness(lane, hub, bot) as h:
            first = await h.act(action="say.for", channel=channel.name, caller=alice.id, text="x",
                                held_text="alice: one", expires=WEEK)
            await h.act(action="say", channel=channel.name, text="uses the hour")
            hub.join(channel.name, ParticipantId(alice.username, 1))
            h.runtime.joined(channel, alice.id)
            await h.settle()
            # Over the hourly limit: refused, so still held.
            assert [r["id"] for r in held_rows(db)] == [first["held_id"]]
            expire_all(db)
            dropped = await lane.run(held.prune_expired)
            assert dropped == [(bot.id, first["held_id"], "line")]
        assert bot_lines(db, channel) == ["uses the hour"]
        assert held_rows(db) == []

    asyncio.run(run())


def test_expired_line_on_join_is_dropped_and_the_bot_told(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel)

    async def run():
        async with Harness(lane, hub, bot) as h:
            first = await h.act(action="say.for", channel=channel.name, caller=alice.id, text="x",
                                held_text="alice: stale", expires=WEEK)
            # A row found on join after its expiry, before pruning reached
            # it, is dropped at delivery the same way.
            expire_all(db)
            await h.runtime._deliver(channel, first["held_id"], bot.id, "line")
            assert (await h.event())["outcome"] == "expired"
        assert bot_lines(db, channel) == [] and held_rows(db) == []

    asyncio.run(run())


def test_line_bounds_and_cancel(db, sysop, alice, channel):
    bot = install(db, sysop, channel)
    with pytest.raises(BotError, match="60 seconds to 7 days"):
        held.hold_line(db, bot.id, channel.name, alice.id, "x", WEEK + 1)
    with pytest.raises(BotError, match="caller id"):
        held.hold_line(db, bot.id, channel.name, "alice", "x", WEEK)
    with pytest.raises(BotError, match="plain text"):
        held.hold_line(db, bot.id, channel.name, alice.id, "a\x1b[2Jb", WEEK)
    ids = [held.hold_line(db, bot.id, channel.name, alice.id, f"n{i}", WEEK)["held_id"]
           for i in range(held.LINES_PER_CALLER)]
    with pytest.raises(BotError, match="for that caller"):
        held.hold_line(db, bot.id, channel.name, alice.id, "one more", WEEK)
    # A Link caller is a separate caller.
    held.hold_line(db, bot.id, channel.name, "link:abc123:bob", "for bob", WEEK)
    # An expired line frees its place but stays for the sweep, which tells
    # the bot it expired (review of #1421).
    db.connection.execute("UPDATE bot_held_lines SET expires_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
                          (ids[-1],))
    db.connection.commit()
    replacement = held.hold_line(db, bot.id, channel.name, alice.id, "replacement", WEEK)["held_id"]
    assert (bot.id, ids[-1], "line") in held.prune_expired(db)
    held.cancel_line(db, bot.id, replacement)
    assert held.cancel_line(db, bot.id, ids[0]) == {"cancelled": True}
    assert held.cancel_line(db, bot.id, ids[0]) == {"cancelled": False}
    # Alice: 8 less the expired one and the cancelled one; Bob: 1.
    assert len(held.list_lines(db, bot.id)) == held.LINES_PER_CALLER - 2 + 1


def test_linked_caller_line_waits_for_their_linked_join(db, lane, hub, sysop, channel):
    bot = install(db, sysop, channel)
    roster = []
    link = SimpleNamespace(node=SimpleNamespace(identity=None),
                           bridge=SimpleNamespace(remote_channel_roster=lambda cid: list(roster)))
    caller = "link:abc123:bob"

    async def broadcast_local_message_live(channel, message):
        pass

    link.bridge.broadcast_local_message_live = broadcast_local_message_live

    async def run():
        async with Harness(lane, hub, bot, link=link) as h:
            result = await h.act(action="say.for", channel=channel.name, caller=caller, text="x",
                                 held_text="bob@Elsewhere: held", expires=WEEK)
            assert result["held"]
            roster.append(("abc123", "bob", "bob@Elsewhere"))
            assert await h.runtime.present(channel, caller)
            h.runtime.joined(channel, caller)
            assert (await h.event())["outcome"] == "delivered"
        assert bot_lines(db, channel) == ["bob@Elsewhere: held"]

    asyncio.run(run())


def test_real_chat_join_delivers_the_held_line(db, lane, hub, presence, mailbox, history, sysop, alice, channel):
    from netbbs.net import chat_flow
    bot = install(db, sysop, channel)

    class InteractiveSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.input = asyncio.Queue()

        async def read_line(self, **kwargs):
            return await self.input.get()

    async def run():
        async with Harness(lane, hub, bot) as h:
            hub.bot_runtime = h.runtime
            await h.act(action="say.for", channel=channel.name, caller=alice.id, text="x",
                        held_text="alice: reminder #1 (due 18:00, held until you came back): tea", expires=WEEK)
            session = InteractiveSession()
            task = asyncio.create_task(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history,
                                                            channel, alice))
            try:
                # The line is wrapped at the terminal width.
                async with asyncio.timeout(30):
                    while "held until you came" not in "".join(session.written):
                        await asyncio.sleep(.02)
                session.input.put_nowait("/quit")
                await asyncio.wait_for(task, CROSS_PROCESS_TIMEOUT)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                hub.bot_runtime = None
        assert held_rows(db) == []

    asyncio.run(run())


# -- tells -------------------------------------------------------------------


def test_tell_is_bound_to_the_request_and_delivered_on_join(db, lane, hub, sysop, alice, nib, channel):
    bot = install(db, sysop, channel)

    async def run():
        async with Harness(lane, hub, bot) as h:
            token = h.request(channel, {"id": alice.id, "name": alice.username})
            # The bot cannot name a sender: only the token's caller counts.
            made = await h.act(action="tell", request=token, to="nib", text="check the Retro board",
                               sender={"id": sysop.id})
            assert made["held"] and made["name"] == "nib"
            row = held_rows(db)[0]
            assert (row["sender_user_id"], row["caller_user_id"], row["kind"]) == (alice.id, nib.id, "tell")
            listed = await h.act(action="tell.list", request=token)
            assert [(t["to"], t["text"]) for t in listed] == [("nib", "check the Retro board")]
            # Renamed between the tell and its delivery: keyed by account.
            rename_user(db, nib, "nibbles", renamed_by=sysop)
            hub.join(channel.name, ParticipantId("nibbles", 1))
            h.runtime.joined(channel, nib.id)
            await h.settle()
        assert bot_lines(db, channel) == ["nibbles: alice asked me to tell you (1m ago): check the Retro board"]
        assert held_rows(db) == []

    asyncio.run(run())


def test_tell_refusals_at_creation(db, lane, hub, sysop, alice, nib, channel):
    bot = install(db, sysop, channel)
    secret = create_channel(db, "secret", creator=sysop, members_only=True)
    save_bot(db, actor=sysop, name="Baud", channels=[channel.name, secret.name], defaults=[channel.name],
             enabled=True, bot_id=bot.id)
    alice_caller = {"id": alice.id, "name": alice.username}

    def tell(to, text="hello", where=channel, present=frozenset(), caller=alice_caller):
        return held.hold_tell(db, bot.id, where.name, caller, to, text, present)

    with pytest.raises(BotError, match="local accounts"):
        tell("bob@Elsewhere")
    with pytest.raises(BotError, match="no account named ghost"):
        tell("ghost")
    with pytest.raises(BotError, match="1-160"):
        tell("nib", "x" * 161)
    with pytest.raises(BotError, match="can't reach nib in this channel"):
        tell("nib", where=secret)
    assert tell("nib", present=frozenset({"nib"})) == {"held": False, "present": True, "name": "nib"}
    assert held_rows(db) == []
    block_local_sender(db, nib, alice)
    with pytest.raises(BotError, match="nib does not accept messages from you"):
        tell("nib")
    unblock_local_sender(db, nib, alice.id)
    set_accepts_direct_messages(db, nib, False)
    with pytest.raises(BotError, match="nib does not accept messages from you"):
        tell("nib")
    assert held_rows(db) == []


def test_tell_caps(db, sysop, alice, nib, channel):
    bot = install(db, sysop, channel)
    senders = [create_user(db, f"s{i}", password="hunter2", user_level=10) for i in range(6)]
    callers = [{"id": u.id, "name": u.username} for u in senders]
    for _ in range(held.TELLS_PER_SENDER):
        held.hold_tell(db, bot.id, channel.name, callers[0], "alice", "hi")
    with pytest.raises(BotError, match="You have 3 tells waiting"):
        held.hold_tell(db, bot.id, channel.name, callers[0], "alice", "hi")
    for caller in callers[1:6]:
        held.hold_tell(db, bot.id, channel.name, caller, "nib", "hi")
    with pytest.raises(BotError, match="nib has 5 tells waiting"):
        held.hold_tell(db, bot.id, channel.name, {"id": alice.id, "name": "alice"}, "nib", "hi")
    # 8 so far; fill the instance to 32 with fresh pairs.
    while len(held_rows(db)) < held.TELLS_PER_BOT:
        u = create_user(db, f"r{len(held_rows(db))}", password="hunter2", user_level=10)
        s = create_user(db, f"q{len(held_rows(db))}", password="hunter2", user_level=10)
        held.hold_tell(db, bot.id, channel.name, {"id": s.id, "name": s.username}, u.username, "hi")
    with pytest.raises(BotError, match="holding 32 tells"):
        held.hold_tell(db, bot.id, channel.name, {"id": alice.id, "name": "alice"}, "s1", "hi")
    # Expired tells free their places at once; the sweep deletes them.
    expire_all(db)
    held.hold_tell(db, bot.id, channel.name, callers[0], "alice", "fresh")
    assert len(held_rows(db)) == held.TELLS_PER_BOT + 1
    held.prune_expired(db)
    assert len(held_rows(db)) == 1


def test_tell_dropped_silently_when_blocked_after_it_was_made(db, lane, hub, sysop, alice, nib, channel):
    bot = install(db, sysop, channel)

    async def run():
        async with Harness(lane, hub, bot) as h:
            token = h.request(channel, {"id": alice.id, "name": alice.username})
            await h.act(action="tell", request=token, to="nib", text="psst")
            block_local_sender(db, nib, alice)
            hub.join(channel.name, ParticipantId("nib", 1))
            h.runtime.joined(channel, nib.id)
            await h.settle()
        assert bot_lines(db, channel) == [] and held_rows(db) == []

    asyncio.run(run())


def test_link_sender_tell_rechecked_at_delivery(db, lane, hub, sysop, nib, channel, monkeypatch):
    bot = install(db, sysop, channel)
    sender = {"id": "link:abc123:bob", "name": "bob@Elsewhere"}
    import netbbs.link.enforcement as enforcement
    allowed = SimpleNamespace(allowed=True)
    monkeypatch.setattr(enforcement, "decide_node_action", lambda *a: allowed)
    monkeypatch.setattr(enforcement, "decide_user_authorship", lambda *a: allowed)

    made = held.hold_tell(db, bot.id, channel.name, sender, "nib", "first")
    assert held_rows(db)[0]["sender_link"] == "link:abc123:bob"
    held.hold_tell(db, bot.id, channel.name, sender, "nib", "second")

    async def run():
        async with Harness(lane, hub, bot) as h:
            hub.join(channel.name, ParticipantId("nib", 1))
            await h.runtime._deliver(channel, made["tell_id"], bot.id, "tell")
            # The Link sender's node may no longer author here: dropped.
            monkeypatch.setattr(enforcement, "decide_user_authorship", lambda *a: SimpleNamespace(allowed=False))
            await h.runtime._deliver(channel, made["tell_id"] + 1, bot.id, "tell")
        assert bot_lines(db, channel) == ["nib: bob@Elsewhere asked me to tell you (1m ago): first"]
        assert held_rows(db) == []

    asyncio.run(run())
    # A blocked Link address is refused when the tell is made.
    block_link_sender(db, nib, "bob@abc123")
    with pytest.raises(BotError, match="does not accept messages from you"):
        held.hold_tell(db, bot.id, channel.name, sender, "nib", "third")


def test_cancel_own_tell_only(db, sysop, alice, nib, channel):
    bot = install(db, sysop, channel)
    made = held.hold_tell(db, bot.id, channel.name, {"id": alice.id, "name": "alice"}, "nib", "hi")
    assert held.cancel_tell(db, bot.id, channel.name, {"id": nib.id, "name": "nib"}, made["tell_id"]) == {
        "cancelled": False}
    assert held.list_tells(db, bot.id, channel.name, {"id": nib.id, "name": "nib"}) == []
    assert held.cancel_tell(db, bot.id, channel.name, {"id": alice.id, "name": "alice"}, made["tell_id"]) == {
        "cancelled": True}


HELLO_ECHO = (
    "import json, sys\n"
    "hello = json.loads(sys.stdin.readline())\n"
    "print(json.dumps({'type': 'ready', 'version': 1}), flush=True)\n"
    "print(json.dumps({'id': 's', 'action': 'state.set', 'value': {'tz': hello['timezone']}}), flush=True)\n"
    "for line in sys.stdin:\n"
    "    pass\n"
)


def test_hello_names_the_display_zone_and_a_zone_change_restarts(db, lane, hub, sysop, channel):
    import sys
    from netbbs.timeutil import set_display_timezone
    set_display_timezone(db, "Europe/Berlin")
    bot = install(db, sysop, channel, command=[sys.executable, "-u", "-c", HELLO_ECHO])

    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03, restart_seconds=.03)
        runtime.start()
        try:
            await eventually_state(lane, bot.storage_name, lambda state: state.get("tz") == "Europe/Berlin")
            set_display_timezone(db, "America/New_York")
            await eventually_state(lane, bot.storage_name, lambda state: state.get("tz") == "America/New_York")
        finally:
            await runtime.close()

    asyncio.run(run())


# -- Baud --------------------------------------------------------------------

BERLIN = "Europe/Berlin"


def _ts(text, tz=BERLIN):
    from zoneinfo import ZoneInfo
    return dt.datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(tz)).timestamp()


def _msg(text, request="r", caller=7, name="alice", channel="general"):
    return {"type": "message", "request": request, "caller": {"id": caller, "name": name},
            "channel": channel, "text": text}


@pytest.mark.parametrize("when, now, expected", [
    # Later today, and a time already past today meaning tomorrow.
    ("20:00", "2026-10-21T10:00", "2026-10-21T20:00"),
    ("08:00", "2026-10-21T10:00", "2026-10-22T08:00"),
    ("tomorrow 9:05", "2026-10-21T23:30", "2026-10-22T09:05"),
    # A weekday: the next such day, today while the time is still ahead.
    ("wed 20:00", "2026-10-21T10:00", "2026-10-21T20:00"),
    ("wed 08:00", "2026-10-21T10:00", "2026-10-28T08:00"),
    ("fri 20:00", "2026-10-21T10:00", "2026-10-23T20:00"),
    ("2026-11-01 20:00", "2026-10-21T10:00", "2026-11-01T20:00"),
])
def test_baud_absolute_times_in_the_node_clock(when, now, expected):
    from netbbs.bots.baud import parse_when, zone
    due, text = parse_when(f"{when} net meeting", _ts(now), zone(BERLIN))
    assert (due, text) == (_ts(expected), "net meeting")


def test_baud_absolute_times_across_both_dst_transitions():
    from netbbs.bots.baud import describe, parse_when, zone
    tz = zone(BERLIN)
    utc = dt.timezone.utc
    # Spring forward (29 March 2026, 02:00 -> 03:00): 02:30 does not exist and
    # is read at its pre-transition offset, as the backup schedule does.
    due, _ = parse_when("2026-03-29 02:30 gap", _ts("2026-03-28T12:00"), tz)
    assert dt.datetime.fromtimestamp(due, utc) == dt.datetime(2026, 3, 29, 1, 30, tzinfo=utc)
    assert describe(due, due - 3600, tz) == "Sun 03:30 CEST"
    # Fall back (25 October 2026, 03:00 -> 02:00): 02:30 happens twice; the
    # first occurrence counts.
    due, _ = parse_when("sun 02:30 repeat", _ts("2026-10-24T12:00"), tz)
    assert dt.datetime.fromtimestamp(due, utc) == dt.datetime(2026, 10, 25, 0, 30, tzinfo=utc)
    assert describe(due, due - 3600, tz) == "Sun 02:30 CEST"
    # A wall time after the change carries the new offset.
    due, _ = parse_when("sun 20:00 after", _ts("2026-10-24T12:00"), tz)
    assert describe(due, due - 3600, tz) == "Sun 20:00 CET"


@pytest.mark.parametrize("rest, reason", [
    ("01.11 20:00 x", "YYYY-MM-DD"),
    ("11/01 20:00 x", "YYYY-MM-DD"),
    ("2026-10-01 20:00 x", "already passed"),
    ("2026-12-31 20:00 x", "30 days"),
    ("31d x", "30 days"),
    ("8pm x", "Use remind"),
    ("2026-02-30 10:00 x", "no date"),
])
def test_baud_refuses_ambiguous_past_and_far_times(rest, reason):
    from netbbs.bots.baud import WhenError, parse_when, zone
    with pytest.raises(WhenError, match=reason):
        parse_when(rest, _ts("2026-10-21T10:00"), zone(BERLIN))


def test_baud_confirms_the_instant_with_the_zone(monkeypatch):
    import netbbs.bots.baud as baud_module
    monkeypatch.setattr(baud_module.time, "time", lambda: _ts("2026-10-21T10:00"))
    sent = []
    baud = baud_module.Baud({"timezone": BERLIN}, sent.append)
    baud.handle(_msg("remind fri 20:00 net meeting"))
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True})
    assert sent[-1]["text"] == ("alice: Saved reminder #1 for Fri 20:00 CEST. I'll say it here then, "
                                "or when you next join if you're away.")


def test_baud_holds_a_due_reminder_for_an_absent_owner_and_lists_it():
    from netbbs.bots.baud import HELD_SECONDS, Baud
    sent = []
    due = _ts("2026-10-21T18:00")
    reminder = {"id": 3, "user": 7, "name": "alice", "channel": "general", "due": due, "text": "net meeting 20:00"}
    baud = Baud({"timezone": BERLIN, "state": {"next": 4, "reminders": [reminder]}}, sent.append)
    baud.handle({"type": "tick", "time": due + 1})
    say = sent[-1]
    assert say == {"id": say["id"], "action": "say.for", "channel": "general", "caller": 7,
                   "text": "alice: reminder #3: net meeting 20:00",
                   "held_text": "alice: reminder #3 (due 18:00, held until you came back): net meeting 20:00",
                   "expires": HELD_SECONDS}
    baud.handle({"type": "result", "id": say["id"], "ok": True, "value": {"held": True, "held_id": 12}})
    assert sent[-1]["action"] == "state.set" and sent[-1]["value"]["reminders"][0]["held"] == 12
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True})
    # Held: never said again on later ticks, and listed as waiting.
    count = len(sent)
    baud.handle({"type": "tick", "time": due + 5})
    assert len(sent) == count
    baud.handle(_msg("reminders"))
    assert sent[-1]["text"] == "alice: #3: net meeting 20:00 (waiting since 18:00)"
    # Still counts toward the eight per caller.
    for n in range(7):
        baud.handle(_msg(f"remind 1h n{n}"))
        baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True})
    baud.handle(_msg("remind 1h one too many"))
    assert "Reminder limit reached" in sent[-1]["text"]
    # The host delivered it on the owner's return: Baud forgets it.
    baud.handle({"type": "held", "held_id": 12, "outcome": "delivered", "message_id": 5})
    assert sent[-1]["action"] == "state.set"
    assert 3 not in [r["id"] for r in sent[-1]["value"]["reminders"]]


def test_baud_cancelling_a_held_reminder_withdraws_the_held_line():
    from netbbs.bots.baud import Baud
    sent = []
    reminder = {"id": 3, "user": 7, "name": "alice", "channel": "general", "due": 0, "text": "x", "held": 12}
    baud = Baud({"state": {"next": 4, "reminders": [reminder]}}, sent.append)
    baud.handle(_msg("cancel 3"))
    assert sent[-2] == {"id": sent[-2]["id"], "action": "held.cancel", "held_id": 12}
    assert sent[-1]["action"] == "state.set" and sent[-1]["value"]["reminders"] == []


def test_baud_restart_reconciles_held_reminders_with_the_host():
    from netbbs.bots.baud import Baud
    sent = []
    reminders = [{"id": i, "user": 7, "name": "alice", "channel": "general", "due": 0, "text": "x", "held": 10 + i}
                 for i in (1, 2)]
    baud = Baud({"state": {"next": 3, "reminders": reminders}}, sent.append)
    baud.handle({"type": "tick", "time": 1})
    assert sent[-1]["action"] == "held.list"
    # Held line 11 was delivered while Baud was down; 12 still waits.
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True, "value": [{"held_id": 12}]})
    assert sent[-1]["action"] == "state.set"
    assert [r["id"] for r in sent[-1]["value"]["reminders"]] == [2]


def test_baud_tell_commands_use_the_request_token():
    from netbbs.bots.baud import Baud
    sent = []
    baud = Baud({}, sent.append)
    baud.handle(_msg("tell Nib: check the Retro board", request="t1"))
    assert sent[-1] == {"id": sent[-1]["id"], "action": "tell", "request": "t1", "to": "Nib",
                        "text": "check the Retro board"}
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True,
                 "value": {"held": True, "tell_id": 4, "name": "nib"}})
    assert sent[-1]["text"].startswith("alice: I'll tell nib here, publicly, when they next join")
    baud.handle(_msg("tell nib hi", request="t2"))
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True, "value": {"present": True, "name": "nib"}})
    assert sent[-1]["text"] == "alice: nib is here now."
    # A refusal was shown privately by the host: nothing public follows.
    baud.handle(_msg("tell nib hi", request="t3"))
    count = len(sent)
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": False,
                 "error": "nib does not accept messages from you."})
    assert len(sent) == count
    baud.handle(_msg("tells", request="t4"))
    assert sent[-1]["action"] == "tell.list"
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True,
                 "value": [{"tell_id": 4, "to": "nib", "text": "check the Retro board", "created_at": "x"}]})
    assert sent[-1]["text"] == "alice: tell #4 to nib: check the Retro board"
    baud.handle(_msg("cancel tell 4", request="t5"))
    assert sent[-1]["action"] == "tell.cancel" and sent[-1]["tell_id"] == 4
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True, "value": {"cancelled": True}})
    assert sent[-1]["text"] == "alice: Cancelled tell #4."


def test_real_baud_holds_a_reminder_until_its_owner_joins(db, lane, hub, sysop, alice, channel):
    bot = install(db, sysop, channel)

    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03, restart_seconds=.03)
        runtime.start()
        watcher = ParticipantId(sysop.username, 9)
        queue = hub.join(channel.name, watcher)
        try:
            async with asyncio.timeout(30):
                while bot.id not in runtime.running_ids:
                    await asyncio.sleep(.02)
                # Alice sets it and is gone when it falls due.
                await runtime.enqueue(bot, channel, {"id": alice.id, "name": alice.username}, "remind 1s tea")
                await eventually_state(lane, bot.storage_name,
                                       lambda state: (state.get("reminders") or [{}])[0].get("held"))
                assert len(held_rows(db)) == 1
                hub.join(channel.name, ParticipantId(alice.username, 1))
                runtime.joined(channel, alice.id)
                while True:
                    line = await queue.get()
                    if "held until you came back" in getattr(line, "body", ""):
                        break
                await eventually_state(lane, bot.storage_name, lambda state: not state["reminders"])
        finally:
            await runtime.close()

    asyncio.run(run())


def test_private_held_line_reaches_its_caller_alone(db, lane, hub, sysop, alice, channel):
    from netbbs.chat.bots import BotPrivateAnswer
    bot = install(db, sysop, channel)

    async def run():
        other = hub.join(channel.name, ParticipantId(sysop.username, 9))
        async with Harness(lane, hub, bot) as h:
            with pytest.raises(BotError, match="local callers only"):
                await h.act(action="say.for", channel=channel.name, caller="link:abc123:bob", text="x",
                            expires=WEEK, private=True)
            result = await h.act(action="say.for", channel=channel.name, caller=alice.id, text="x",
                                 held_text="alice: just for you", expires=WEEK, private=True)
            assert result["held"] and held_rows(db)[0]["private"] == 1
            mine = hub.join(channel.name, ParticipantId(alice.username, 1))
            h.runtime.joined(channel, alice.id)
            assert (await h.event()) == {"type": "held", "held_id": result["held_id"], "outcome": "delivered",
                                         "private": True}
            answer = mine.get_nowait()
            assert isinstance(answer, BotPrivateAnswer) and answer.text == "alice: just for you"
            # Present now: shown at once, to alice alone.
            now = await h.act(action="say.for", channel=channel.name, caller=alice.id, text="alice: now",
                              expires=WEEK, private=True)
            assert now == {"delivered": True, "private": True}
            assert mine.get_nowait().text == "alice: now"
        assert other.empty()
        assert bot_lines(db, channel) == [] and held_rows(db) == []
        # Both debited alice's private-answer limit, not the channel's.
        assert db.connection.execute("SELECT COUNT(*) FROM bot_private_history").fetchone()[0] == 2
        assert db.connection.execute("SELECT COUNT(*) FROM bot_output_history").fetchone()[0] == 0

    asyncio.run(run())
