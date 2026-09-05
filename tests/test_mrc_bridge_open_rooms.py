"""
Issue #300 at the bridge: opening a room makes it an active mapping the
caller is announced into, the one-identity rule, the observed-room
table, the activity stamp, and the sweeper on the keepalive tick --
against the loopback fake hub, reusing `tests/test_mrc_bridge.py`'s
fixtures and helpers.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.activity import follow
from netbbs.chat.channels import create_channel, get_channel_by_name
from netbbs.chat.hub import ChatHub, ParticipantId
from netbbs.chat.scrollback import record_message
from netbbs.mrc.bridge import MrcState
from netbbs.mrc.settings import (
    MrcSettingsError,
    OpenRoomSettings,
    count_open_rooms,
    get_mrc_mapping,
    save_open_room_settings,
    set_mrc_room,
    touch_open_room,
)
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


def test_open_room_materializes_and_announces_the_caller_on_entry(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        hub = ChatHub()
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            assert not bridge.open_rooms_enabled
            with pytest.raises(MrcSettingsError, match="switched off"):
                await bridge.open_room("lobby", "alice")
            save_open_room_settings(db, OpenRoomSettings(enabled=True, cap=2, blocklist=("secret",)))
            await bridge.refresh_channel_mappings()
            assert bridge.open_rooms_enabled

            mapping = await bridge.open_room("#Lobby", "alice")
            assert mapping.channel.name == "mrc:Lobby" and mapping.is_open_room
            assert bridge.is_bridged(mapping.channel)
            assert bridge.status().open_rooms == 1 and bridge.status().open_room_cap == 2
            assert "Lobby" in bridge.observed_rooms()

            # Entering it goes through the ordinary announce path.
            hub.join(mapping.channel.name, ParticipantId("alice", 1))
            await bridge.local_join(mapping.channel, "alice")
            newroom = await fake.wait_for(lambda p: p.body == "NEWROOM::Lobby")
            assert newroom.from_user == "alice"

            with pytest.raises(MrcSettingsError, match="blocked"):
                await bridge.open_room("secret", "alice")
            await bridge.open_room("two", "alice")
            with pytest.raises(MrcSettingsError, match="already has 2"):
                await bridge.open_room("three", "alice")
            assert count_open_rooms(db) == 2
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_one_identity_per_account_across_rooms(db, lane, lobby, alice, sysop):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        save_open_room_settings(db, OpenRoomSettings(enabled=True))
        set_mrc_room(db, lobby, "general")
        hub = ChatHub()
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            other = (await bridge.open_room("other", "alice")).channel
            assert bridge.identity_room_elsewhere(other, "alice") is None
            hub.join(lobby.name, ParticipantId("alice", 1))
            await bridge.local_join(lobby, "alice")
            await fake.wait_for(lambda p: p.body == "NEWROOM::general")
            # Announced in #general: a different room is refused, the
            # same room (a second window) is not.
            assert bridge.identity_room_elsewhere(other, "alice") == "general"
            assert bridge.identity_room_elsewhere(lobby, "alice") is None
            assert bridge.identity_room_elsewhere(other, "bob") is None
            hub.leave(lobby.name, ParticipantId("alice", 1))
            await bridge.local_leave(lobby, "alice")
            assert bridge.identity_room_elsewhere(other, "alice") is None
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_observed_rooms_come_from_moves_chatter_and_openings_and_stay_bounded(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        save_open_room_settings(db, OpenRoomSettings(enabled=True))
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake, keepalive_interval_seconds=30.0)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            await fake.send_line("SERVER~~~~~lobby~*** Joining garden: carol@Third~")
            await fake.send_line("bob~Other~lobby~NOTME~~lobby~*** Leaving attic: bob@Other~")
            await fake.send_line("SERVER~~~alice~~~USERROOM:cellar~")
            await fake.wait_for(lambda p: p.body == "NEWROOM:cellar:lobby")
            await asyncio.sleep(0.05)
            assert set(bridge.observed_rooms()) >= {"lobby", "garden", "attic", "cellar"}
            # The table is remotely named, so bounded: 250 sightings keep
            # the 200 most recent (fed directly -- the inbound bucket would
            # otherwise be the thing under test).
            for i in range(250):
                bridge._observe_room(f"room{i:03d}")
            observed = bridge.observed_rooms()
            assert len(observed) == 200
            assert "room249" in observed and "room000" not in observed and "lobby" not in observed
            # Chat text that merely mentions joining is not a room sighting.
            await fake.send_line("bob~Other~lobby~~~lobby~<bob> I am joining kitchen: later~")
            await asyncio.sleep(0.05)
            assert "kitchen:" not in bridge.observed_rooms() and "kitchen" not in bridge.observed_rooms()
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_activity_is_stamped_and_the_sweeper_retires_idle_rooms_on_the_tick(db, lane, lobby, alice, sysop):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        save_open_room_settings(db, OpenRoomSettings(enabled=True, retention_days=7))
        hub = ChatHub()
        bridge = await _connected_bridge(db, lane, hub, fake, keepalive_interval_seconds=0.2)
        try:
            idle = (await bridge.open_room("idle", "alice")).channel
            busy = (await bridge.open_room("busy", "alice")).channel
            followed = (await bridge.open_room("followed", "alice")).channel
            fresh = (await bridge.open_room("fresh", "alice")).channel
            long_ago = "2020-01-01T00:00:00.000000Z"
            for channel in (idle, busy, followed, fresh):
                touch_open_room(db, channel, now=long_ago)
            follow(db, alice, "channel", followed.id)
            hub.join(busy.name, ParticipantId("alice", 1))
            # A line in `fresh` stamps it (one lane write), so it is not idle.
            recorded = record_message(db, fresh, kind="message", author_label="alice", author_fingerprint=None, body="hi")
            assert await bridge.local_message(fresh, recorded) == (True, False)
            await _wait_until(lambda: get_mrc_mapping(db, fresh).last_active_at != long_ago)

            await _wait_until(lambda: bridge.status().retired_rooms == 1, timeout=3.0)
            assert get_mrc_mapping(db, busy) is not None
            assert get_mrc_mapping(db, followed) is not None
            assert get_mrc_mapping(db, fresh) is not None
            with pytest.raises(Exception):
                get_channel_by_name(db, "mrc:idle")
            assert not bridge.is_bridged(idle)
            assert bridge.status().open_rooms == 3
            # Rooms opened earlier still age out after the switch is turned off.
            save_open_room_settings(db, OpenRoomSettings(enabled=False, retention_days=7))
            hub.leave(busy.name, ParticipantId("alice", 1))
            await _wait_until(lambda: bridge.status().retired_rooms == 2, timeout=3.0)
            assert get_mrc_mapping(db, busy) is None
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_identity_is_decided_from_local_occupancy_not_from_announcements(db, lane, lobby, alice, sysop):
    """Review of #300: the announced set is empty while the link is
    down, so the rule must read the ChatHub, or two sessions could
    settle in two rooms during backoff and fight over one nick later."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        save_open_room_settings(db, OpenRoomSettings(enabled=True))
        set_mrc_room(db, lobby, "general")
        hub = ChatHub()
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            other = (await bridge.open_room("other", "alice")).channel
            await fake.drop_clients()
            await _wait_until(lambda: bridge.state is not MrcState.CONNECTED)
            # In #general per the hub's own roster, announced to nobody.
            hub.join(lobby.name, ParticipantId("alice", 1))
            assert bridge.identity_room_elsewhere(other, "alice") == "general"
            assert bridge.identity_room_elsewhere(other, "alice", leaving=lobby) is None
            hub.join(lobby.name, ParticipantId("alice", 2))
            assert bridge.identity_room_elsewhere(other, "alice", leaving=lobby) == "general"
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_retirement_drops_the_rooms_caches_and_runs_without_a_hub(db, lane, lobby, alice):
    """Review of #300: a retired room must leave no roster behind, and
    the sweeper must run while the hub is unreachable -- otherwise a
    long outage strands the whole cap."""
    async def scenario():
        settings = save_open_room_settings(db, OpenRoomSettings(enabled=True, retention_days=7))
        # Port 1 is never listening: every connection attempt fails.
        _enable(db, 1)
        hub = ChatHub()
        from tests.test_mrc_bridge import _bridge
        bridge = _bridge(hub, lane, keepalive_interval_seconds=0.2)
        await bridge.start()
        try:
            from netbbs.mrc.settings import materialize_open_room
            stale = materialize_open_room(db, "stale", open_settings=settings).channel
            touch_open_room(db, stale, now="2020-01-01T00:00:00.000000Z")
            await bridge.refresh_channel_mappings()
            bridge._rosters["stale"] = ("ghost@Other",)
            bridge._last_userlist_request["stale"] = 0.0
            assert bridge.state is not MrcState.CONNECTED
            await _wait_until(lambda: bridge.status().retired_rooms == 1, timeout=3.0)
            assert get_mrc_mapping(db, stale) is None
            assert "stale" not in bridge._rosters and "stale" not in bridge._last_userlist_request
            assert bridge.state is not MrcState.CONNECTED
        finally:
            await bridge.close()
    asyncio.run(scenario())


def test_activity_is_stamped_while_the_hub_is_unreachable(db, lane, lobby, alice):
    """Second review of #300: a room in use during an outage is not idle;
    the stamp must not sit behind the connectivity check."""
    async def scenario():
        settings = save_open_room_settings(db, OpenRoomSettings(enabled=True))
        _enable(db, 1)  # never listening
        hub = ChatHub()
        from tests.test_mrc_bridge import _bridge
        bridge = _bridge(hub, lane, keepalive_interval_seconds=30.0)
        await bridge.start()
        try:
            from netbbs.mrc.settings import materialize_open_room
            room = materialize_open_room(db, "outage", open_settings=settings).channel
            long_ago = "2020-01-01T00:00:00.000000Z"
            touch_open_room(db, room, now=long_ago)
            await bridge.refresh_channel_mappings()
            assert bridge.state is not MrcState.CONNECTED
            hub.join(room.name, ParticipantId("alice", 1))
            await bridge.local_join(room, "alice")
            assert get_mrc_mapping(db, room).last_active_at != long_ago
            touch_open_room(db, room, now=long_ago)
            bridge._last_touch.clear()
            recorded = record_message(db, room, kind="message", author_label="alice", author_fingerprint=None, body="hi")
            assert await bridge.local_message(room, recorded) == (False, False)  # not relayed, but noted
            assert get_mrc_mapping(db, room).last_active_at != long_ago
        finally:
            await bridge.close()
    asyncio.run(scenario())


def test_one_identity_holds_when_the_sysop_maps_two_occupied_channels(db, lane, lobby, alice, sysop):
    """Third review of #300: no hub.join happens when the SysOp maps or
    unpauses a channel a caller already sits in, so the wire announce
    itself must keep one nick in one room."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        second = create_channel(db, "second", creator=sysop)
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        second_queue = hub.join(second.name, ParticipantId("alice", 2))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            set_mrc_room(db, lobby, "lobby")
            set_mrc_room(db, second, "elsewhere")
            await bridge.refresh_channel_mappings()
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            await asyncio.sleep(0.1)
            assert not [p for p in fake.received if p.body == "NEWROOM::elsewhere"]
            notice = await asyncio.wait_for(second_queue.get(), timeout=2)
            assert "Your MRC identity is already in #lobby" in notice
            # What she says in the second channel is not relayed under her nick.
            recorded = record_message(db, second, kind="message", author_label="alice", author_fingerprint=None, body="hi")
            assert await bridge.local_message(second, recorded) == (False, False)
            assert bridge.status().participants == 1
            # The notice is said once per conflict, not once per refresh.
            await bridge.refresh_channel_mappings()
            await bridge.refresh_channel_mappings()
            await asyncio.sleep(0.05)
            assert second_queue.empty()
            # When the holder leaves its room, the waiting channel is
            # announced at once -- not on the next keepalive tick.
            hub.leave(lobby.name, ParticipantId("alice", 1))
            await bridge.local_leave(lobby, "alice")
            await fake.wait_for(lambda p: p.body == "LOGOFF")
            await fake.wait_for(lambda p: p.body == "NEWROOM::elsewhere")
            notice = await asyncio.wait_for(second_queue.get(), timeout=2)
            assert "now bridged to MRC room #elsewhere" in notice
            assert await bridge.local_message(second, recorded) == (True, False)
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_room_names_with_colons_and_entry_reservations(db, lane, lobby, alice):
    """Second review of #300: a colon is legal inside a room name (the
    separator is the last ': '), and a room a session is about to join
    counts as occupied for the sweeper."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        settings = save_open_room_settings(db, OpenRoomSettings(enabled=True, retention_days=7))
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake, keepalive_interval_seconds=30.0)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            await fake.send_line("SERVER~~~~~lobby~*** Joining foo:bar: carol@Third~")
            await _wait_until(lambda: "foo:bar" in bridge.observed_rooms())
            assert "foo" not in bridge.observed_rooms()

            entering = (await bridge.open_room("entering", "alice")).channel
            touch_open_room(db, entering, now="2020-01-01T00:00:00.000000Z")
            bridge.note_entry(entering)
            await bridge._sweep_open_rooms()
            assert get_mrc_mapping(db, entering) is not None
            # The identity rule has a target-agnostic form for the moment
            # before a room exists.
            assert bridge.identity_room_held("alice") == "lobby"
            assert bridge.identity_room_held("alice", leaving=lobby) is None
            assert bridge.identity_room_held("bob") is None
            del settings
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())
