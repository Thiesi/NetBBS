"""
Issue #304 at the bridge: away state mirrored to the hub (and repeated
on reconnect), the periodic STATS reading, the hub's banner, an open
room's topic stored from ROOMTOPIC and asked for with NEWTOPIC, and the
caller's own nick colour in the house-style body -- against the
loopback fake hub, reusing `tests/test_mrc_bridge.py`'s fixtures.
"""

from __future__ import annotations

import asyncio

from netbbs.chat.channels import get_channel_by_name
from netbbs.chat.hub import ChatHub, ParticipantId
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import record_message
from netbbs.mrc.bridge import MrcNotice, MrcState, MrcStatus
from netbbs.mrc.settings import OpenRoomSettings, save_open_room_settings, set_mrc_room
from netbbs.net.directory_flow import _mrc_network_masthead
from netbbs.rendering.ansi import strip_ansi
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


def test_away_state_is_mirrored_and_repeated_on_reconnect(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        presence = PresenceRegistry()
        presence.enter("alice")
        bridge = await _connected_bridge(db, lane, hub, fake, presence=presence)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            # Not announced: nothing goes out for bob.
            assert await bridge.local_away("bob", "away") is False
            presence.set_away("alice", "gone fishing")
            assert await bridge.local_away("alice", "gone fishing") is False
            afk = await fake.wait_for(lambda p: p.body == "AFK gone fishing")
            assert (afk.from_user, afk.to_user, afk.to_room) == ("alice", "SERVER", "lobby")
            await _wait_until(lambda: fake.afk.get(("my_board", "alice")) == "gone fishing")
            assert ("my_board", "bob") not in fake.afk
            # A reconnect re-announces and repeats the away state -- read
            # from the presence registry, the one place it lives.
            await fake.drop_clients()
            await _wait_until(lambda: bridge.state is not MrcState.CONNECTED)
            await _wait_until(lambda: bridge.state is MrcState.CONNECTED, timeout=3.0)
            await _wait_until(lambda: len(fake.packets(body_prefix="AFK gone fishing")) >= 2, timeout=3.0)
            # Back: the bare form.
            presence.clear_away("alice")
            await bridge.local_away("alice", None)
            await fake.wait_for(lambda p: p.body == "AFK")
            await _wait_until(lambda: fake.afk.get(("my_board", "alice")) is None)
            # Too long or decorated: bounded and sanitized, and the caller
            # is told it was cut.
            long_message = "|12busy " + "x" * 200
            presence.set_away("alice", long_message)
            assert await bridge.local_away("alice", long_message) is True
            sent = await fake.wait_for(lambda p: p.body.startswith("AFK busy"))
            assert len(sent.body) <= 140 and "|12" not in sent.body
            # The account's final session leaving clears the registry's
            # away state; a later announcement sends no stale AFK.
            presence.leave("alice")
            assert not presence.is_away("alice")
            hub.leave(lobby.name, ParticipantId("alice", 1))
            await bridge.local_leave(lobby, "alice")
            await fake.wait_for(lambda p: p.body == "LOGOFF")
            count_before = len(fake.packets(body_prefix="AFK"))
            presence.enter("alice")
            hub.join(lobby.name, ParticipantId("alice", 2))
            await bridge.local_join(lobby, "alice")
            await _wait_until(lambda: len(fake.packets(body_prefix="NEWROOM::lobby")) >= 3, timeout=3.0)
            await asyncio.sleep(0.2)
            assert len(fake.packets(body_prefix="AFK")) == count_before
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_stats_are_read_periodically_and_shown_only_when_asked(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        fake.users[("other", "bob")] = "garden"
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body == "STATS")
            await _wait_until(lambda: bridge.status().network_users == 2)
            status = bridge.status()
            assert (status.network_bbses, status.network_rooms, status.network_users) == (2, 2, 2)
            assert status.network_summary == "2 users on 2 boards"
            assert status.network_stats_age_seconds is not None and status.network_stats_age_seconds < 5
            # The bridge's own ask is parsed, not shown; the caller's is shown.
            await asyncio.sleep(0.05)
            while not queue.empty():
                item = queue.get_nowait()
                assert not (isinstance(item, MrcNotice) and item.text.startswith("STATS:")), item
            assert bridge.send_hub_command(lobby, "alice", "STATS") is None
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            while not (isinstance(notice, MrcNotice) and notice.kind == "reply"):
                notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert notice.text == "STATS:2 2 2"
            # The banner the hub pushed on connect is remembered.
            assert bridge.banner_lines() == ["|14Welcome to the fake hub"]
            # An unparseable STATS keeps the raw line for the status screen.
            await fake.send_line("SERVER~~~alice~~~STATS:lots of people~")
            await _wait_until(lambda: bridge.status().network_users is None)
            assert bridge.status().network_stats_raw == "lots of people"
            assert bridge.status().network_summary is None
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_open_room_topics_come_from_the_hub_and_go_to_it(db, lane, lobby, alice, sysop):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        save_open_room_settings(db, OpenRoomSettings(enabled=True))
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            garden = (await bridge.open_room("garden", "alice")).channel
            hub.join(garden.name, ParticipantId("carol", 2))
            await bridge.local_join(garden, "carol")
            await fake.wait_for(lambda p: p.body == "NEWROOM::garden")
            # Inbound: stored on the open room, not on the mapped channel.
            await fake.send_line("SERVER~~~CLIENT~~~ROOMTOPIC:garden:|14be |15excellent~")
            await _wait_until(lambda: get_channel_by_name(db, "mrc:garden").topic == "be excellent")
            await fake.send_line("SERVER~~~CLIENT~~~ROOMTOPIC:lobby:not yours~")
            await asyncio.sleep(0.1)
            assert get_channel_by_name(db, lobby.name).topic is None
            # Outbound: NEWTOPIC as the caller, the hub decides.
            assert bridge.send_topic(garden, "carol", "hello there") is None
            sent = await fake.wait_for(lambda p: p.body == "NEWTOPIC:garden:hello there")
            assert sent.from_user == "carol"
            await _wait_until(lambda: get_channel_by_name(db, "mrc:garden").topic == "hello there")
            assert bridge.send_topic(lobby, "alice", "x" * 200) == "that topic is longer than MRC allows (140 characters with the room name)"
            assert bridge.send_topic(garden, "zed", "hi") == "you aren't announced to the hub yet"
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_the_callers_nick_colour_is_read_once_and_worn_on_the_wire(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        asked: list[str] = []

        def _colour(db_, username):
            asked.append(username)
            return 12 if username == "alice" else 14

        bridge = await _connected_bridge(db, lane, hub, fake, load_nick_color=_colour)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            recorded = record_message(db, lobby, kind="message", author_label="alice", author_fingerprint=None, body="hi")
            assert await bridge.local_message(lobby, recorded) == (True, False)
            chat = await fake.wait_for(lambda p: p.to_user == "" and p.body.endswith(" hi"))
            assert chat.body == "|08<|12alice|08>|16|07 hi"
            assert asked == ["alice"]
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_nick_colour_is_reread_after_the_caller_leaves(db, lane, lobby, alice):
    """Review of #306: "applies the next time you enter" means the next
    announcement after the account's last MRC room, not a bridge restart."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        colours = {"alice": 12}

        def _colour(db_, username):
            return colours.get(username, 14)

        bridge = await _connected_bridge(db, lane, hub, fake, load_nick_color=_colour)
        try:
            hub.join(lobby.name, ParticipantId("alice", 1))
            await bridge.local_join(lobby, "alice")
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            colours["alice"] = 9  # changed on the Profile while inside
            recorded = record_message(db, lobby, kind="message", author_label="alice", author_fingerprint=None, body="one")
            await bridge.local_message(lobby, recorded)
            first = await fake.wait_for(lambda p: p.body.endswith(" one"))
            assert first.body.startswith("|08<|12alice")  # still the colour read at entry
            hub.leave(lobby.name, ParticipantId("alice", 1))
            await bridge.local_leave(lobby, "alice")
            await fake.wait_for(lambda p: p.body == "LOGOFF")
            hub.join(lobby.name, ParticipantId("alice", 2))
            await bridge.local_join(lobby, "alice")
            recorded = record_message(db, lobby, kind="message", author_label="alice", author_fingerprint=None, body="two")
            await bridge.local_message(lobby, recorded)
            second = await fake.wait_for(lambda p: p.body.endswith(" two"))
            assert second.body.startswith("|08<|09alice")
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_banner_belongs_to_a_connection_and_stats_to_a_hub(db, lane, lobby, alice):
    """Review of #306: a reconnect must not stack banner lines, and a
    settings reload must not carry the previous hub's population."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await _wait_until(lambda: bridge.banner_lines() == ["|14Welcome to the fake hub"])
            await _wait_until(lambda: bridge.status().network_users is not None)
            await fake.drop_clients()
            await _wait_until(lambda: bridge.state is not MrcState.CONNECTED)
            await _wait_until(lambda: bridge.state is MrcState.CONNECTED, timeout=3.0)
            await _wait_until(lambda: len(fake.packets(body_prefix="NEWROOM::lobby")) >= 2, timeout=3.0)
            await asyncio.sleep(0.1)
            assert bridge.banner_lines() == ["|14Welcome to the fake hub"]
            assert bridge.status().network_users is not None  # same hub: kept across the reconnect
            await bridge.reload_settings()
            assert bridge.status().network_users is None and bridge.status().network_summary is None
            await _wait_until(lambda: bridge.state is MrcState.CONNECTED, timeout=3.0)
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_who_online_masthead_reads_the_network_size():
    class _Bridge:
        def __init__(self, status):
            self._status = status

        def status(self):
            return self._status

    class _Controls:
        def __init__(self, bridge):
            self.mrc_bridge = bridge

    base = dict(
        state=MrcState.CONNECTED, enabled=True, host="h", port=1, tls=False, site_name="s",
        connected_since=None, last_error=None, attempts=1, bridged_channels=0, participants=0,
        dropped_outbound=0, dropped_inbound=0,
    )
    known = MrcStatus(**base, network_bbses=12, network_rooms=5, network_users=41, network_stats_age_seconds=200.0)
    assert strip_ansi(_mrc_network_masthead(_Controls(_Bridge(known)))) == "MRC: 41 users on 12 boards (as of 3 min ago)"
    fresh = MrcStatus(**base, network_bbses=1, network_rooms=1, network_users=1, network_stats_age_seconds=10.0)
    assert strip_ansi(_mrc_network_masthead(_Controls(_Bridge(fresh)))) == "MRC: 1 user on 1 board (just now)"
    assert _mrc_network_masthead(_Controls(_Bridge(MrcStatus(**base)))) == ""
    assert _mrc_network_masthead(_Controls(_Bridge(MrcStatus(**{**base, "enabled": False}, network_users=3, network_bbses=1)))) == ""
    assert _mrc_network_masthead(_Controls(None)) == ""
