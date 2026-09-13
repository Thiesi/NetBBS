"""
Issue #298 at the socket level, against `tests.mrc_fake_hub.FakeMrcHub`
like `tests/test_mrc_bridge.py` (whose fixtures and helpers this reuses):
the body convention every reference client shares, replies addressed to
one caller, CTCP, and the hub moving, renaming or terminating a session.
"""

from __future__ import annotations

import asyncio
import time

from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub, ParticipantId
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.mrc.bridge import MrcNotice, MrcState
from netbbs.mrc.settings import set_mrc_paused, set_mrc_room
from tests.mrc_fake_hub import FakeMrcHub
from tests.test_mrc_bridge import (  # noqa: F401 -- fixtures
    _connected_bridge,
    _enable,
    _wait_until,
    alice,
    db,
    lane,
    lobby,
    sysop,
)


def test_inbound_bodies_lose_the_senders_own_embedded_handle(db, lane, lobby, alice):
    """Every reference client puts the sender's coloured handle inside
    the body; the caller must see one name, and an action must land
    as an action. A body that names someone else is recorded whole."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body.startswith("NEWROOM:"))
            lines = [
                "bob~Mystic~lobby~~~lobby~|03<|11bob|03>|16|07 from mystic~",
                "bob~Enigma~lobby~~~lobby~|00|10<|02bob|10>|00 |03from enigma~",
                "bob~Sync~lobby~~~lobby~bob |07from synchronet~",
                "bob~ANet~lobby~~~lobby~|07bob|07 from anetbbs~",
                "bob~Mystic~lobby~~~lobby~|15* |13bob waves~",
                "bob~Mystic~lobby~~~lobby~<carol> quoted someone else~",
            ]
            for line in lines:
                await fake.send_line(line)
            # The connect-time "this channel is bridged" notice (a plain
            # string) may land among the messages; it is not what this
            # test reads.
            delivered = []
            while len(delivered) < len(lines):
                item = await asyncio.wait_for(queue.get(), timeout=2)
                if not isinstance(item, str):
                    delivered.append(item)
            assert [(m.kind, m.author_label, m.body) for m in delivered] == [
                ("message", "bob@Mystic (MRC)", "from mystic"),
                ("message", "bob@Enigma (MRC)", "|03from enigma"),
                ("message", "bob@Sync (MRC)", "|07from synchronet"),
                ("message", "bob@ANet (MRC)", "from anetbbs"),
                ("action", "bob@Mystic (MRC)", "waves"),
                ("message", "bob@Mystic (MRC)", "<carol> quoted someone else"),
            ]
            assert all(m.external_source == "mrc" for m in delivered)
            # The search index gets the words, never the codes.
            hits = db.connection.execute(
                "SELECT body FROM channel_message_search WHERE channel_id = ? ORDER BY message_id", (lobby.id,)
            ).fetchall()
            assert [row["body"] for row in hits] == [
                "from mystic", "from enigma", "from synchronet", "from anetbbs", "waves", "<carol> quoted someone else",
            ]
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_outbound_chunks_each_carry_the_handle_within_the_hub_limit(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body.startswith("NEWROOM:"))
            long_line = record_message(
                db, lobby, kind="message", author_label="alice", author_fingerprint=None,
                body=" ".join(f"w{i:03d}" for i in range(60)),
            )
            assert await bridge.local_message(lobby, long_line) == (True, False)
            await _wait_until(lambda: len([p for p in fake.received if p.to_user == "" and "w0" in p.body]) == 3)
            chunks = [p.body for p in fake.received if p.to_user == "" and "w0" in p.body]
            assert all(chunk.startswith("|08<|14alice|08>|16|07 ") for chunk in chunks)
            assert all(len(chunk) <= 140 for chunk in chunks)
            words = " ".join(chunk.split("|16|07 ", 1)[1] for chunk in chunks).split()
            assert words == [f"w{i:03d}" for i in range(60)]
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_ctcp_requests_are_answered_from_the_targets_nick_and_bounded(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body.startswith("NEWROOM:"))
            ctcp = "bob~Other~ctcp_echo_channel~alice~~ctcp_echo_channel~"
            await fake.send_line(ctcp + "[CTCP] bob alice VERSION~")
            reply = await fake.wait_for(lambda p: p.body.startswith("[CTCP-REPLY] VERSION"))
            assert (reply.from_user, reply.from_site, reply.to_user, reply.to_room, reply.body) == (
                "alice", "My_Board", "bob", "ctcp_echo_channel", "[CTCP-REPLY] VERSION NetBBS 5.7.0",
            )
            await fake.send_line(ctcp + "[CTCP] bob alice PING 1234 5678~")
            await fake.wait_for(lambda p: p.body == "[CTCP-REPLY] PING 1234 5678")
            await fake.send_line(ctcp + "[CTCP] bob alice CLIENTINFO~")
            await fake.wait_for(lambda p: p.body == "[CTCP-REPLY] CLIENTINFO VERSION TIME PING CLIENTINFO")
            # Burst spent: the next requests from the same sender go
            # unanswered, and one for a nick this node never announced is
            # ignored outright.
            for _ in range(4):
                await fake.send_line(ctcp + "[CTCP] bob alice TIME~")
            await fake.send_line("bob~Other~ctcp_echo_channel~zed~~ctcp_echo_channel~[CTCP] bob zed VERSION~")
            await asyncio.sleep(0.2)
            assert len(fake.packets(body_prefix="[CTCP-REPLY]")) == 3
            assert bridge.status().dropped_inbound == 4
            # Nothing CTCP-shaped was ever recorded as chat.
            assert not [m for m in get_scrollback(db, lobby) if m.kind == "message"]
            # A reply to something alice asked is shown to her.
            await fake.send_line(ctcp + "[CTCP-REPLY] VERSION Mystic 1.12~")
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert isinstance(notice, MrcNotice) and notice.kind == "reply"
            assert notice.text == "CTCP VERSION reply from bob@Other: Mystic 1.12"
            # An underscored handle is shown with spaces here too (issue #378).
            await fake.send_line("Some_User~Other~ctcp_echo_channel~alice~~ctcp_echo_channel~[CTCP-REPLY] TIME noon~")
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert notice.text == "CTCP TIME reply from Some User@Other: noon"
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


class _FreezableClock:
    """`time.monotonic`, until a test freezes it (issue #536).

    `MrcBridge` already takes a `clock`, but handing it a fully fake one
    from construction would stall the connection-stability timing it
    also uses that clock for. This runs real until `freeze()`, which is
    all the reply-allowance assertion needs: a token bucket that cannot
    refill while the burst is being measured.

    Without it the test depended on the steps between spending two
    tokens and sending the burst taking under 0.2 s -- `REPLY_RATE_PER_
    SECOND` is 10.0, so those two were back by then on any host slower
    than the one it was written on, the burst got its full allowance,
    and the assertion about which line carried the notice failed. It
    got *worse* when other sleeps in the suite were lengthened, which is
    how it was identified.
    """

    def __init__(self) -> None:
        self._frozen: float | None = None

    def __call__(self) -> float:
        return time.monotonic() if self._frozen is None else self._frozen

    def freeze(self) -> None:
        self._frozen = time.monotonic()

    def thaw(self) -> None:
        self._frozen = None


def test_hub_replies_reach_only_the_asker_and_are_bounded_per_caller(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        alice_queue = hub.join(lobby.name, ParticipantId("alice", 1))
        carol_queue = hub.join(lobby.name, ParticipantId("carol", 7))
        clock = _FreezableClock()
        bridge = await _connected_bridge(db, lane, hub, fake, reply_burst=5, clock=clock)
        try:
            await _wait_until(lambda: len(fake.packets(body_prefix="NEWROOM:")) == 2)
            # Frozen before alice's reply bucket exists (issue #536,
            # Codex review). `_TokenBucket` refills lazily -- `now -
            # _last_refill` on the next check -- so freezing *after* the
            # LIST replies would still hand back whatever real time had
            # passed since the bucket was created, which is the very
            # refill this is trying to exclude. Frozen from before the
            # bucket is created, no time elapses for it at all.
            #
            # Safe across this stretch: the outbound bucket starts full,
            # and the only commands sent here are this LIST and two that
            # are refused before they reach the wire.
            clock.freeze()
            assert bridge.send_hub_command(lobby, "alice", "LIST") is None
            sent = await fake.wait_for(lambda p: p.body == "LIST")
            assert (sent.from_user, sent.to_user, sent.to_room) == ("alice", "SERVER", "lobby")
            first = await asyncio.wait_for(alice_queue.get(), timeout=2)
            second = await asyncio.wait_for(alice_queue.get(), timeout=2)
            assert [n.kind for n in (first, second)] == ["reply", "reply"]
            assert [n.text for n in (first, second)] == ["|10LIST reply line 1", "|07LIST reply line 2"]
            await asyncio.sleep(0.05)
            assert carol_queue.empty()
            # Not announced, or not bridged: a reason, never silence.
            assert bridge.send_hub_command(lobby, "zed", "LIST") == "you aren't announced to the hub yet"
            other = create_channel(db, "other", creator=alice)
            assert bridge.send_hub_command(other, "alice", "LIST") == "this channel isn't bridged to MRC"
            # A burst past the per-caller allowance is cut short, and said so once.
            for i in range(8):
                await fake.send_line(f"SERVER~~~alice~~~line {i}~")
            got = []
            deadline = asyncio.get_running_loop().time() + 2
            while len(got) < 4 and asyncio.get_running_loop().time() < deadline:
                got.append(await asyncio.wait_for(alice_queue.get(), timeout=2))
            texts = [n.text for n in got]
            assert texts[:3] == ["line 0", "line 1", "line 2"]
            assert "cut short" in texts[3]
            # Still frozen for the assertion the freeze exists to make
            # (Codex review). Receiving the notice proves the bridge
            # handled the *first* refused line, not the four after it --
            # `send_line` drains the sender without waiting for the
            # receiver. Thawing here jumped the bucket's clock to real
            # monotonic time, so those four could refill it and be
            # delivered during the sleep below, failing this assertion
            # on exactly the slow host the test is for.
            #
            # Frozen, no refill is possible at all, so "nothing more was
            # delivered" is a claim about the allowance rather than
            # about who won a race.
            await asyncio.sleep(0.1)
            assert alice_queue.empty()
            clock.thaw()
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_hub_moves_renames_and_termination_are_handled(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake, keepalive_interval_seconds=30.0)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            # Moved out of the mapped room: told, and announced there again
            # -- once per keepalive tick, however often the hub insists.
            await fake.send_line("SERVER~~~alice~~~USERROOM:secret~")
            rehome = await fake.wait_for(lambda p: p.body == "NEWROOM:secret:lobby")
            assert rehome.from_user == "alice"
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert "moved you to #secret" in notice.text
            await fake.send_line("SERVER~~~alice~~~USERROOM:secret~")
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert "keeps moving you" in notice.text
            assert len(fake.packets(body_prefix="NEWROOM:secret")) == 1
            # A move *into* the mapped room is not news.
            await fake.send_line("SERVER~~~alice~~~USERROOM:lobby~")
            await asyncio.sleep(0.05)
            assert queue.empty()
            # Renamed by the hub: tracked, so what alice says next goes out
            # under the name the hub knows, and she is told.
            await fake.send_line("SERVER~~~alice~~~USERNICK:alice2~")
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert "now knows you as 'alice2'" in notice.text
            recorded = record_message(db, lobby, kind="message", author_label="alice", author_fingerprint=None, body="hi")
            assert await bridge.local_message(lobby, recorded) == (True, False)
            chat = await fake.wait_for(lambda p: p.to_user == "" and p.body.endswith(" hi"))
            assert (chat.from_user, chat.body) == ("alice2", "|08<|14alice2|08>|16|07 hi")
            # Terminated by the hub: fatal until settings change, like OLDVERSION.
            await fake.send_line("SERVER~~~CLIENT~~~TERMINATE:|12banned for testing~")
            await _wait_until(lambda: bridge.state is MrcState.ERROR)
            assert "terminated the session: banned for testing" in bridge.status().last_error
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_network_broadcasts_reach_every_active_bridged_channel(db, lane, lobby, alice, sysop):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        other = create_channel(db, "other", creator=sysop)
        set_mrc_room(db, other, "elsewhere")
        paused = create_channel(db, "paused", creator=sysop)
        set_mrc_room(db, paused, "quiet")
        set_mrc_paused(db, paused, True)
        hub = ChatHub()
        # Three different accounts: one MRC identity sits in one room.
        lobby_queue = hub.join(lobby.name, ParticipantId("alice", 1))
        other_queue = hub.join(other.name, ParticipantId("bob", 2))
        paused_queue = hub.join(paused.name, ParticipantId("carol", 3))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body.startswith("NEWROOM:"))
            await fake.send_line("bob~Other~lobby~~~~|07<|11bob|07> |15hello every room~")
            for queue in (lobby_queue, other_queue):
                notice = await asyncio.wait_for(queue.get(), timeout=2)
                assert isinstance(notice, MrcNotice) and notice.kind == "broadcast"
                assert notice.text == "bob@Other: |15hello every room"
            await asyncio.sleep(0.05)
            assert paused_queue.empty()
            assert not [m for m in get_scrollback(db, lobby) if m.kind == "message"]
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_repeated_hub_moves_stop_producing_notices_within_a_tick(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake, keepalive_interval_seconds=30.0)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            for _ in range(6):
                await fake.send_line("SERVER~~~alice~~~USERROOM:secret~")
            await fake.wait_for(lambda p: p.body == "NEWROOM:secret:lobby")
            await asyncio.sleep(0.2)
            notices = []
            while not queue.empty():
                notices.append(queue.get_nowait())
            # One re-announce, two notices, then silence for the rest of the tick.
            assert len(fake.packets(body_prefix="NEWROOM:secret")) == 1
            assert [("moved you" in n.text, "keeps moving" in n.text) for n in notices] == [(True, False), (False, True)]
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_hub_command_longer_than_the_wire_limit_is_refused_with_a_reason(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            reason = bridge.send_hub_command(lobby, "alice", "HELP " + "x" * 200)
            assert reason == "that command is longer than MRC allows (140 characters)"
            assert bridge.status().dropped_outbound == 0
            assert bridge.send_hub_command(lobby, "alice", "HELP " + "x" * 100) is None
            await fake.wait_for(lambda p: p.body.startswith("HELP xxx"))
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_reply_truncation_notice_is_once_per_burst_even_when_the_hub_trickles(db, lane, lobby, alice):
    """A hub streaming just above the refill rate must not earn a fresh
    priority notice at every refilled token: the latch lifts only once
    the allowance has recovered to half the burst."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        now = [1000.0]
        bridge = await _connected_bridge(
            db, lane, hub, fake, reply_burst=4, clock=lambda: now[0], keepalive_interval_seconds=30.0,
        )
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")

            async def drain(count: int) -> list[str]:
                got = []
                for _ in range(count):
                    got.append((await asyncio.wait_for(queue.get(), timeout=2)).text)
                await asyncio.sleep(0.1)
                assert queue.empty(), queue.get_nowait()
                return got

            for i in range(6):
                await fake.send_line(f"SERVER~~~alice~~~line {i}~")
            first = await drain(5)
            assert first[:4] == ["line 0", "line 1", "line 2", "line 3"] and "cut short" in first[4]
            # One token refilled (10/s): one line admitted, the next dropped
            # -- and no second notice, the allowance is nowhere near back.
            now[0] += 0.1
            await fake.send_line("SERVER~~~alice~~~trickle a~")
            await fake.send_line("SERVER~~~alice~~~trickle b~")
            assert await drain(1) == ["trickle a"]
            # Fully recovered: the latch lifts, and a new burst earns one new notice.
            now[0] += 10.0
            for i in range(6):
                await fake.send_line(f"SERVER~~~alice~~~again {i}~")
            second = await drain(5)
            assert second[:4] == ["again 0", "again 1", "again 2", "again 3"] and "cut short" in second[4]
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())
