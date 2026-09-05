"""
Issue #305 at the bridge: private MRC lines for a caller who opted in
(peeled, coloured, never recorded, bounded per remote sender), the
unchanged single notice for one who did not, the nick -> site map
learned from inbound traffic, and `send_private` -- against the loopback
fake hub, reusing `tests/test_mrc_bridge.py`'s fixtures.
"""

from __future__ import annotations

import asyncio

from netbbs.chat.hub import ChatHub, ParticipantId
from netbbs.chat.scrollback import get_scrollback
from netbbs.mrc.bridge import PRIVATE_BURST, MrcNotice, MrcState
from netbbs.mrc.settings import set_mrc_room
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


def _optin_for(*usernames):
    def _load(db_, username):
        return username in usernames
    return _load


async def _next_notice(queue, *, kind="private", timeout=2.0):
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=timeout)
        if isinstance(item, MrcNotice) and item.kind == kind:
            return item


def test_opted_in_caller_gets_private_lines_peeled_and_unrecorded(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake, load_private_optin=_optin_for("alice"))
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            assert bridge.private_messages_enabled("alice") is True
            assert bridge.private_messages_enabled("nobody") is None
            # Mystic style, ENiGMA style and a bare body all arrive as the
            # sender's text with its colours, the handle peeled once.
            await fake.send_line("bob~Other~garden~alice~My_Board~~|03<|11bob|03>|16|07 hello |12there~")
            notice = await _next_notice(queue)
            assert (notice.kind, notice.text) == ("private", "bob@Other: hello |12there")
            await fake.send_line("carol~Third~lobby~alice~My_Board~~carol |07plain~")
            assert (await _next_notice(queue)).text == "carol@Third: |07plain"
            # Learned from the traffic: where each nick was last seen, and
            # who to answer with `/mrc r`.
            assert bridge.site_for_nick("BOB") == "Other"
            assert bridge.site_for_nick("nobody") == ""
            assert bridge.reply_target("alice") == ("carol", "Third")
            # A line addressed to a nick nobody here wears goes nowhere.
            await fake.send_line("bob~Other~garden~zed~My_Board~~hi zed~")
            await asyncio.sleep(0.1)
            assert queue.empty()
            # Never recorded: private lines are not room traffic.
            assert [m for m in get_scrollback(db, lobby) if "hello" in m.body] == []
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_a_caller_who_did_not_opt_in_gets_the_one_notice_only(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            assert bridge.private_messages_enabled("alice") is False
            await fake.send_line("bob~Other~garden~alice~My_Board~~|03<|11bob|03>|16|07 secret~")
            await fake.send_line("bob~Other~garden~alice~My_Board~~again~")
            await asyncio.sleep(0.2)
            items = []
            while not queue.empty():
                items.append(queue.get_nowait())
            assert len(items) == 1 and "tried to message you privately" in str(items[0])
            assert "secret" not in str(items[0])
            assert bridge.reply_target("alice") is None
            # Sending is refused too: the opt-in is one switch for both ways.
            reason, truncated = await bridge.send_private(lobby, "alice", "bob", "hi")
            assert reason == "you have not opted in to private MRC messages (Profile)" and truncated is False
            assert not [p for p in fake.received if p.to_user == "bob"]
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_send_private_wears_the_house_style_and_the_last_seen_site(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(
            db, lane, hub, fake, load_private_optin=_optin_for("alice"), load_nick_color=lambda db_, u: 12,
        )
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            # Site unknown: the hub routes on the nick alone.
            assert await bridge.send_private(lobby, "alice", "bob", "hi |12there") == (None, False)
            sent = await fake.wait_for(lambda p: p.to_user == "bob")
            assert (sent.from_user, sent.from_site, sent.from_room) == ("alice", "My_Board", "lobby")
            assert (sent.to_user, sent.msg_ext, sent.to_room) == ("bob", "", "")
            assert sent.body == "|08<|12alice|08>|16|07 hi there"
            # Site learned from any packet bob sent since.
            await fake.send_line("bob~Other~garden~~~garden~|03<|11bob|03>|16|07 room chatter~")
            await _wait_until(lambda: bridge.site_for_nick("bob") == "Other")
            assert await bridge.send_private(lobby, "alice", "Bob", "again") == (None, False)
            sent = await fake.wait_for(lambda p: p.to_user == "Bob")
            assert sent.msg_ext == "Other"
            # Bounded like a room line: chunked under the caller's own
            # allowance (PER_USER_BURST tokens, one per chunk), and the
            # tail reported cut.
            reason, truncated = await bridge.send_private(lobby, "alice", "bob", "x" * 600)
            assert reason == "you're sending faster than MRC allows"
            await asyncio.sleep(2.2)
            reason, truncated = await bridge.send_private(lobby, "alice", "bob", "x" * 600)
            assert reason is None and truncated is True
            await _wait_until(lambda: len([p for p in fake.received if p.to_user == "bob" and "xxx" in p.body]) == 3, timeout=3.0)
            # Refusals.
            assert (await bridge.send_private(lobby, "alice", "SERVER", "hi"))[0] == "that is not an MRC user name"
            assert (await bridge.send_private(lobby, "alice", "bob", "|12"))[0] == "nothing to send"
            assert (await bridge.send_private(lobby, "zed", "bob", "hi"))[0] == "you aren't announced to the hub yet"
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_private_lines_are_bounded_per_remote_sender(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake, load_private_optin=_optin_for("alice"))
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            for i in range(PRIVATE_BURST + 3):
                await fake.send_line(f"bob~Other~garden~alice~My_Board~~line {i}~")
            await fake.send_line("carol~Third~garden~alice~My_Board~~hello from carol~")
            await asyncio.sleep(0.3)
            texts = []
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, MrcNotice) and item.kind == "private":
                    texts.append(item.text)
            # bob's flood is cut at his own allowance; carol's line is
            # untouched by it.
            assert texts == [f"bob@Other: line {i}" for i in range(PRIVATE_BURST)] + ["carol@Third: hello from carol"]
            assert bridge.status().dropped_inbound == 3
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_the_optin_is_reread_after_the_caller_leaves(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        optin = {"alice": False}
        bridge = await _connected_bridge(db, lane, hub, fake, load_private_optin=lambda db_, u: optin.get(u, False))
        try:
            hub.join(lobby.name, ParticipantId("alice", 1))
            await bridge.local_join(lobby, "alice")
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            optin["alice"] = True  # flipped on the Profile while inside
            assert bridge.private_messages_enabled("alice") is False
            hub.leave(lobby.name, ParticipantId("alice", 1))
            await bridge.local_leave(lobby, "alice")
            await fake.wait_for(lambda p: p.body == "LOGOFF")
            assert bridge.private_messages_enabled("alice") is None
            hub.join(lobby.name, ParticipantId("alice", 2))
            await bridge.local_join(lobby, "alice")
            await _wait_until(lambda: bridge.private_messages_enabled("alice") is True)
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_a_reply_keeps_the_site_it_came_from(db, lane, lobby, alice):
    """Review of #307: the same nick can exist on two boards; `/mrc r`
    answers the identity that wrote, not wherever that nick was last
    seen."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake, load_private_optin=_optin_for("alice"))
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            await fake.send_line("bob~SiteA~garden~alice~My_Board~~hello~")
            await _next_notice(queue)
            await fake.send_line("bob~SiteB~garden~~~garden~room chatter~")
            await _wait_until(lambda: bridge.site_for_nick("bob") == "SiteB")
            assert bridge.reply_target("alice") == ("bob", "SiteA")
            assert await bridge.send_private(lobby, "alice", "bob", "back", site="SiteA") == (None, False)
            sent = await fake.wait_for(lambda p: p.to_user == "bob" and p.body.endswith(" back"))
            assert sent.msg_ext == "SiteA"
            # And a fresh `/mrc msg bob` goes by the last sighting.
            assert await bridge.send_private(lobby, "alice", "bob", "new") == (None, False)
            sent = await fake.wait_for(lambda p: p.to_user == "bob" and p.body.endswith(" new"))
            assert sent.msg_ext == "SiteB"
            # A reconnect starts a new conversation state.
            await fake.drop_clients()
            await _wait_until(lambda: bridge.state is not MrcState.CONNECTED)
            await _wait_until(lambda: bridge.state is MrcState.CONNECTED, timeout=3.0)
            assert bridge.reply_target("alice") is None
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_the_sender_table_never_resets_an_allowance(db, lane, lobby, alice, monkeypatch):
    """Review of #307: identities are free to invent, so reaching the
    table's cap evicts an idle (full) bucket or admits nobody new --
    never clears everyone's allowance."""
    from netbbs.mrc import bridge as bridge_module
    monkeypatch.setattr(bridge_module, "MAX_TRACKED_PRIVATE_BUCKETS", 2)

    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake, load_private_optin=_optin_for("alice"))
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            for i in range(PRIVATE_BURST):
                await fake.send_line(f"bob~Other~garden~alice~My_Board~~bob {i}~")
            await fake.send_line("carol~Other~garden~alice~My_Board~~carol 0~")
            for _ in range(PRIVATE_BURST + 1):
                await _next_notice(queue)
            # Both tracked senders are mid-burst: a third identity gets nothing,
            # and bob's allowance is still spent.
            await fake.send_line("dave~Other~garden~alice~My_Board~~dave 0~")
            await fake.send_line("bob~Other~garden~alice~My_Board~~bob late~")
            await asyncio.sleep(0.2)
            assert queue.empty()
            assert bridge.status().dropped_inbound == 2
            # Once a tracked bucket is full again it is evicted for the newcomer.
            await asyncio.sleep(1.1)
            await fake.send_line("dave~Other~garden~alice~My_Board~~third try~")
            assert (await _next_notice(queue)).text == "dave@Other: third try"
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_private_overflow_is_reported_as_private_loss(db, lane, lobby, alice):
    """Review of #307: the per-caller allowance's overflow notice names
    what was dropped and is latched per kind."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake, load_private_optin=_optin_for("alice"), reply_burst=2)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            for i in range(4):
                await fake.send_line(f"bob~Other~garden~alice~My_Board~~line {i}~")
            await asyncio.sleep(0.3)
            notices = []
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, MrcNotice):
                    notices.append((item.kind, item.text))
            assert notices == [
                ("private", "bob@Other: line 0"),
                ("private", "bob@Other: line 1"),
                ("private", "(private MRC messages for you arrived faster than can be shown -- some were dropped)"),
            ]
            assert "hub's reply" not in str(notices)
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_caches_follow_the_announced_set_and_a_failed_read_is_not_cached(db, lane, lobby, alice):
    """Review of #307: pausing a mapping unannounces its callers and must
    drop their cached Profile choices; a read that fails is logged, not
    remembered as "off"."""
    from netbbs.mrc.settings import set_mrc_paused

    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        optin = {"alice": False}
        failing = {"on": False}

        def _load(db_, username):
            if failing["on"]:
                raise RuntimeError("database is away")
            return optin.get(username, False)

        bridge = await _connected_bridge(db, lane, hub, fake, load_private_optin=_load)
        try:
            hub.join(lobby.name, ParticipantId("alice", 1))
            await bridge.local_join(lobby, "alice")
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            assert bridge.private_messages_enabled("alice") is False
            set_mrc_paused(db, lobby, True)
            await bridge.refresh_channel_mappings()
            await fake.wait_for(lambda p: p.body == "LOGOFF")
            assert bridge.private_messages_enabled("alice") is None
            optin["alice"] = True  # switched on while the mapping was paused
            set_mrc_paused(db, lobby, False)
            await bridge.refresh_channel_mappings()
            await _wait_until(lambda: bridge.private_messages_enabled("alice") is True)
            # A failed read: not cached, so the next read tries again.
            hub.leave(lobby.name, ParticipantId("alice", 1))
            await bridge.local_leave(lobby, "alice")
            assert bridge.private_messages_enabled("alice") is None
            failing["on"] = True
            hub.join(lobby.name, ParticipantId("alice", 2))
            await bridge.local_join(lobby, "alice")
            assert bridge.private_messages_enabled("alice") is None
            assert (await bridge.send_private(lobby, "alice", "bob", "hi"))[0] == "you have not opted in to private MRC messages (Profile)"
            failing["on"] = False
            assert await bridge.send_private(lobby, "alice", "bob", "hi") == (None, False)
            assert bridge.private_messages_enabled("alice") is True
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())
