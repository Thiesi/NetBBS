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
from netbbs.chat.channels import get_channel_by_name
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
