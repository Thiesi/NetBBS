"""
The MRC room directory a caller finds already filled in: the hub's last
complete listing kept across restarts, rooms the hub stopped listing
dropped, and the one-line "there are other rooms" note on a session's
first entry.

The hub lists rooms only to a caller who is already in one, so without
these the Multi Relay Chat section starts every node run with `lobby`
and nothing else.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import random

from netbbs.chat.hub import ParticipantId
from netbbs.config import get_config, set_config
from netbbs.mrc.bridge import MrcBridge, MrcState
from netbbs.mrc.settings import (
    DIRECTORY_SNAPSHOT_KEY,
    DIRECTORY_TIMESTAMP_FORMAT,
    DirectoryEntry,
    hub_identity,
    load_directory_snapshot,
    load_mrc_settings,
    save_directory_snapshot,
)
from netbbs.net import chat_flow
from tests.test_chat_flow_mrc import _wait_for
from tests.test_chat_flow_mrc_open_rooms import (  # noqa: F401 -- fixtures and helpers
    FakeSession,
    _bridge_on,
    _visible_text,
    alice,
    db,
    hub,
    lane,
    presence,
)
from tests.test_chat_flow_mrc_presence import _browse_until

_LISTING = [
    "*. __Rooms___________________Usr__Topic_______________________",
    "*.:  #chess                    1  Chess - open games",
    "*.:  #lobby                   12  Welcome to the lobby",
    "*.:  #quiet                    0  ",
    "*.:__                             # = Normal  # = Locked",
]


def _listing_hub(fake, lines):
    original = fake.reply_lines
    fake.reply_lines = lambda command, params: list(lines) if command == "LIST" else original(command, params)


def _flat(session) -> str:
    """The session's text with wrapping undone: a note may wrap, and must still read whole."""
    return " ".join(_visible_text(session).split())


def _stamp(*, hours_ago: float = 0.0) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return moment.strftime(DIRECTORY_TIMESTAMP_FORMAT)


async def _enter(bridge, hub, room, *, session_id):
    mapping = await bridge.open_room(room, "alice")
    participant = ParticipantId("alice", session_id)
    hub.join(mapping.channel.name, participant)
    await bridge.local_join(mapping.channel, "alice")
    return mapping, participant


async def _second_run(lane, hub):
    """A fresh bridge over the same database and the same hub settings --
    what a node restart is, as far as the directory is concerned."""
    bridge = MrcBridge(
        hub=hub, lane=lane, version="5.8.0", rng=random.Random(2),
        min_backoff_seconds=0.05, max_backoff_seconds=0.2, stable_after_seconds=0.0, per_user_interval_seconds=0.0,
    )
    await bridge.start()
    deadline = asyncio.get_running_loop().time() + 2
    while bridge.state is not MrcState.CONNECTED:
        assert asyncio.get_running_loop().time() < deadline, bridge.status()
        await asyncio.sleep(0.01)
    return bridge


def test_the_last_listing_fills_the_section_after_a_restart(db, lane, hub, presence, alice):
    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub)
        _listing_hub(fake, _LISTING)
        second = None
        try:
            mapping, participant = await _enter(bridge, hub, "lobby", session_id=801)
            assert bridge.refresh_directory(mapping.channel, "alice")
            identity = hub_identity(load_mrc_settings(db))
            await _wait_for(lambda: load_directory_snapshot(db, identity), what="the listing to be kept")
            assert {entry.room for entry in load_directory_snapshot(db, identity)} == {"chess", "lobby", "quiet"}
            hub.leave(mapping.channel.name, participant)
            await bridge.local_leave(mapping.channel, "alice")
            await bridge.close()

            # Make the kept reading three hours old, then start again.
            stored = json.loads(get_config(db, DIRECTORY_SNAPSHOT_KEY))
            stored["rooms"] = [[room, users, topic, _stamp(hours_ago=3.2)] for room, users, topic, _at in stored["rooms"]]
            set_config(db, DIRECTORY_SNAPSHOT_KEY, json.dumps(stored))
            second = await _second_run(lane, hub)
            # Nobody has entered a room in this run, and no LIST has gone out.
            assert "chess" in second.observed_rooms()
            users, topic, fresh, age = second.directory_details("chess")
            assert (users, topic, fresh) == (1, "Chess - open games", False) and 3 * 3600 < age < 4 * 3600
            session = FakeSession(["b"])
            await chat_flow._pick_mrc_room(session, lane, hub, alice, second)
            visible = _visible_text(session)
            assert "chess" in visible and "1 on MRC (3 h ago) | Chess - open games" in visible
            assert "quiet" in visible
        finally:
            if second is not None:
                await second.close()
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_a_listing_kept_from_another_hub_or_too_long_ago_is_not_shown(db, lane, hub, alice):
    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub)
        await bridge.close()
        identity = hub_identity(load_mrc_settings(db))
        second = None
        try:
            save_directory_snapshot(db, "elsewhere.example:5001", [DirectoryEntry("chess", 4, "t", _stamp())])
            second = await _second_run(lane, hub)
            assert second.directory_details("chess") is None and "chess" not in second.observed_rooms()
            await second.close()
            save_directory_snapshot(db, identity, [
                DirectoryEntry("ancient", 4, "t", _stamp(hours_ago=24 * 8)),
                DirectoryEntry("recent", 2, "t", _stamp(hours_ago=24 * 2)),
            ])
            second = await _second_run(lane, hub)
            assert second.directory_details("ancient") is None
            assert second.directory_details("recent")[:3] == (2, "t", False)
        finally:
            if second is not None:
                await second.close()
            await fake.close()
    asyncio.run(scenario())


def test_a_kept_listing_is_checked_again_on_the_way_in(db):
    good = _stamp(hours_ago=1)
    set_config(db, DIRECTORY_SNAPSHOT_KEY, json.dumps({"hub": "h:1", "rooms": [
        ["fine", 3, "a |04topic", good],
        ["FINE", 9, "duplicate, differently cased", good],
        ["two words", 1, "", good],
        ["x" * 21, 1, "", good],
        ["tilde~room", 1, "", good],
        ["count", "7", "", good],
        ["flag", True, "", good],
        ["negative", -1, "", good],
        ["undated", 1, "", "yesterday"],
        ["tomorrow", 1, "", _stamp(hours_ago=-5)],
        ["short", 1],
        "not a row",
    ]}))
    entries = load_directory_snapshot(db, "h:1")
    assert [(entry.room, entry.users) for entry in entries] == [("fine", 3)]
    assert load_directory_snapshot(db, "h:2") == []
    for broken in ("", "{", "[]", json.dumps({"hub": "h:1", "rooms": "chess"})):
        set_config(db, DIRECTORY_SNAPSHOT_KEY, broken)
        assert load_directory_snapshot(db, "h:1") == []


def test_a_room_the_hub_no_longer_lists_is_dropped(db, lane, hub, alice):
    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub)
        _listing_hub(fake, _LISTING)
        try:
            mapping, _participant = await _enter(bridge, hub, "lobby", session_id=802)
            bridge.refresh_directory(mapping.channel, "alice")
            await _wait_for(lambda: bridge.directory_details("quiet") is not None, what="the first listing")
            identity = hub_identity(load_mrc_settings(db))
            await _wait_for(lambda: load_directory_snapshot(db, identity), what="the first listing to be kept")

            # #chess has emptied; the next complete listing no longer has it.
            await asyncio.sleep(0.05)
            _listing_hub(fake, [line for line in _LISTING if "#chess" not in line])
            assert bridge.send_hub_command(mapping.channel, "alice", "LIST") is None
            await _wait_for(lambda: bridge.directory_details("chess") is None, what="chess to be dropped")
            assert "chess" not in bridge.observed_rooms() and "quiet" in bridge.observed_rooms()
            await _wait_for(
                lambda: {entry.room for entry in load_directory_snapshot(db, identity)} == {"lobby", "quiet"},
                what="the kept listing to follow",
            )

            # A footer with no row before it proves nothing: nothing is dropped.
            await asyncio.sleep(0.05)
            _listing_hub(fake, [_LISTING[-1]])
            assert bridge.send_hub_command(mapping.channel, "alice", "LIST") is None
            await fake.wait_for(lambda p: p.body == "LIST" and len(fake.packets(body_prefix="LIST")) == 3)
            await asyncio.sleep(0.1)
            assert bridge.directory_details("quiet") is not None
            assert {entry.room for entry in load_directory_snapshot(db, identity)} == {"lobby", "quiet"}
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_the_first_room_of_a_session_says_there_are_others(db, lane, hub, presence, alice):
    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub)
        _listing_hub(fake, _LISTING)
        try:
            note = "2 more rooms on MRC: /rooms lists them, /join <room> moves."
            # 0,1: the MRC section; 0,2: `lobby`, listed after "[Open a room by name]".
            session = await _browse_until(
                lane, hub, presence, alice, ["0", "1", "0", "2"], ["/quit"], mrc_bridge=bridge,
                until=lambda s: note in _flat(s), what="the other-rooms note",
            )
            text = _flat(session)
            assert text.count(note) == 1
            assert "#chess" not in text  # one line, not the table
            # A second session within the refresh interval asks the hub
            # nothing and is told from the listing already in hand.
            asked = len(fake.packets(body_prefix="LIST"))
            session = await _browse_until(
                lane, hub, presence, alice, ["0", "1", "0", "2"], ["/quit"], mrc_bridge=bridge,
                until=lambda s: note in _flat(s), what="the note from the listing in hand",
            )
            assert _flat(session).count(note) == 1
            assert len(fake.packets(body_prefix="LIST")) == asked
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())
