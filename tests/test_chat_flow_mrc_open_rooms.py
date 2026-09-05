"""
Issue #300 from the caller's side: the picker's "[Multi Relay Chat]"
section, opening a room by name or from the observed list, `/join`
resolution inside and outside MRC rooms, the refusals, and the one-
identity rule -- the real `browse_channels`/`pick_item`/`_chat_loop`
driven by the mixed read_key/read_line `FakeSession` of
`tests/test_chat_flow_picker_authorization.py`, with a real bridge on
the loopback fake hub.
"""

from __future__ import annotations

import asyncio
import random

from netbbs.chat.channels import create_channel, get_channel_by_name
from netbbs.chat.hub import ParticipantId
from netbbs.chat.mailbox import MessageMailbox
from netbbs.mrc.bridge import MrcBridge, MrcState
from netbbs.mrc.settings import MrcSettings, OpenRoomSettings, save_mrc_settings, save_open_room_settings, set_mrc_room
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from tests.mrc_fake_hub import FakeMrcHub
from tests.test_chat_flow_picker_authorization import (  # noqa: F401 -- fixtures
    FakeSession,
    _visible_text,
    alice,
    bob,
    db,
    hub,
    lane,
    presence,
)


async def _bridge_on(db, lane, hub, *, open_rooms: bool = True, **open_overrides):
    fake = FakeMrcHub()
    await fake.start()
    save_mrc_settings(db, MrcSettings(enabled=True, host="127.0.0.1", port=fake.port, tls=False, site_name="My Board"))
    save_open_room_settings(db, OpenRoomSettings(enabled=open_rooms, **open_overrides))
    bridge = MrcBridge(
        hub=hub, lane=lane, version="5.8.0", rng=random.Random(1),
        min_backoff_seconds=0.05, max_backoff_seconds=0.2, stable_after_seconds=0.0,
    )
    await bridge.start()
    deadline = asyncio.get_running_loop().time() + 2
    while bridge.state is not MrcState.CONNECTED:
        assert asyncio.get_running_loop().time() < deadline, bridge.status()
        await asyncio.sleep(0.01)
    return fake, bridge


async def _browse(lane, hub, presence, user, inputs, *, mrc_bridge):
    session = FakeSession(inputs)
    await asyncio.wait_for(
        chat_flow.browse_channels(
            session, lane, hub, presence, MessageMailbox(), InputHistory(), user, mrc_bridge=mrc_bridge,
        ),
        timeout=4,
    )
    return session


def test_picker_offers_the_mrc_section_only_when_open_rooms_are_on(db, lane, hub, presence, alice):
    create_channel(db, "general", creator=alice)

    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub, open_rooms=False)
        try:
            session = await _browse(lane, hub, presence, alice, ["b"], mrc_bridge=bridge)
            assert "[Multi Relay Chat]" not in _visible_text(session)
            save_open_room_settings(db, OpenRoomSettings(enabled=True))
            await bridge.refresh_channel_mappings()
            session = await _browse(lane, hub, presence, alice, ["b"], mrc_bridge=bridge)
            text = _visible_text(session)
            assert "[Multi Relay Chat]" in text and "rooms on the MRC network" in text
            # No bridge at all (standalone/test callers): no section either.
            session = await _browse(lane, hub, presence, alice, ["b"], mrc_bridge=None)
            assert "[Multi Relay Chat]" not in _visible_text(session)
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_opening_a_room_by_name_enters_it_and_announces_the_caller(db, lane, hub, presence, alice):
    create_channel(db, "general", creator=alice)

    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub, blocklist=("secret",))
        try:
            # 0,1: the MRC section (listed first); 0,1: "[Open a room by
            # name]"; a blocked room is refused and the section stays;
            # then "Lobby" opens and enters mrc:Lobby; /quit leaves.
            session = await _browse(
                lane, hub, presence, alice,
                ["0", "1", "0", "1", "secret", "0", "1", "Lobby", "/quit"], mrc_bridge=bridge,
            )
            text = _visible_text(session)
            assert "The SysOp has blocked MRC room #secret" in text
            assert "Joined" in text and "mrc:Lobby" in text
            assert "bridged to MRC room #Lobby" in text
            newroom = await fake.wait_for(lambda p: p.body == "NEWROOM::Lobby")
            assert newroom.from_user == "alice"
            await fake.wait_for(lambda p: p.body == "LOGOFF")
            channel = get_channel_by_name(db, "mrc:Lobby")
            assert channel.description == "MRC room #Lobby on the Multi Relay Chat network"
            # Now open here: the section lists it with occupancy, and the
            # ordinary channel list does not.
            session = await _browse(lane, hub, presence, alice, ["0", "1", "b", "b"], mrc_bridge=bridge)
            text = _visible_text(session)
            assert "Lobby" in text and "0 here, 0 on MRC" in text
            assert "[Open a room by name]" in text
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_join_resolves_mrc_rooms_first_inside_a_room_and_explicitly_anywhere(db, lane, hub, presence, alice):
    general = create_channel(db, "general", creator=alice)

    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub)
        try:
            # From a local channel: a bare name is local, mrc:<room> opens
            # the MRC room; inside it, a bare unknown name opens another
            # MRC room, and a bare local name switches to the local channel.
            session = await _browse(
                lane, hub, presence, alice,
                ["0", "2", "/join mrc:Alpha", "/join beta", "/join general", "/quit"], mrc_bridge=bridge,
            )
            text = _visible_text(session)
            assert "Joined" in text and "mrc:Alpha" in text and "mrc:beta" in text
            assert get_channel_by_name(db, "mrc:beta").name == "mrc:beta"
            await fake.wait_for(lambda p: p.body == "NEWROOM::Alpha")
            await fake.wait_for(lambda p: p.body == "NEWROOM::beta")
            assert text.count("Joined") == 4  # general, Alpha, beta, general
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_join_refuses_when_open_rooms_are_off_and_at_the_cap(db, lane, hub, presence, alice):
    create_channel(db, "general", creator=alice)

    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub, open_rooms=False)
        try:
            session = await _browse(lane, hub, presence, alice, ["0", "1", "/join mrc:lobby", "/quit"], mrc_bridge=bridge)
            assert "Opening MRC rooms is switched off on this node." in _visible_text(session)
            save_open_room_settings(db, OpenRoomSettings(enabled=True, cap=1))
            await bridge.refresh_channel_mappings()
            # With the section now present, "0", "2" is #general.
            session = await _browse(
                lane, hub, presence, alice, ["0", "2", "/join mrc:one", "/join two", "/quit"], mrc_bridge=bridge,
            )
            text = _visible_text(session)
            assert "mrc:one" in text
            assert "already has 1 MRC rooms open" in text
            assert "mrc:two" not in text
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_a_second_session_cannot_take_the_identity_into_another_room(db, lane, hub, presence, alice):
    general = create_channel(db, "general", creator=alice)
    set_mrc_room(db, general, "general")

    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub)
        try:
            other = (await bridge.open_room("other", "alice")).channel
            # alice's first session sits in #general (announced there).
            hub.join(general.name, ParticipantId("alice", 1))
            await bridge.local_join(general, "alice")
            await fake.wait_for(lambda p: p.body == "NEWROOM::general")
            # Her second session: the MRC section lists "other"; picking it
            # is refused naming #general, and so is /join from a local room.
            create_channel(db, "plain", creator=alice)
            # Section (0,1), pick "other" (0,2): refused by the outer loop,
            # which lands back at the top level; 0,3 enters #plain; from
            # there an explicit /join is refused the same way.
            session = await _browse(
                lane, hub, presence, alice,
                ["0", "1", "0", "2", "0", "3", "/join mrc:other", "/quit"], mrc_bridge=bridge,
            )
            text = _visible_text(session)
            assert text.count("Your MRC identity is already in #general") == 2
            assert "Joined" in text and "plain" in text
            assert hub.participant_count(other.name) == 0
            assert not [p for p in fake.received if p.body == "NEWROOM::other"]
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_listing_the_section_accepts_no_invitation(db, lane, hub, presence, alice, bob):
    """Review of #300: building the section's list must be free of side
    effects -- a pending invitation to a members-only open room is not
    accepted by merely looking at the list."""
    from netbbs.chat.channels import update_channel
    from netbbs.chat.membership import create_invitation, has_pending_invitation, is_member
    from netbbs.moderation import ChannelPermission, grant_permissions

    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub)
        try:
            room = (await bridge.open_room("club", "bob")).channel
            update_channel(
                db, room, name=room.name, description=room.description, min_level=0, category_id=None,
                pinned=False, hidden=False, members_only=True, allow_member_invites=False, min_age=None,
                name_requirement=None, community_id=None, changed_by=bob,
            )
            grant_permissions(
                db, bob, object_type="channel", object_id=room.id,
                permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=bob,
            )
            create_invitation(db, room, alice, invited_by=bob)
            assert has_pending_invitation(db, room, alice)
            session = await _browse(lane, hub, presence, alice, ["0", "1", "b", "b"], mrc_bridge=bridge)
            assert "club" not in _visible_text(session).split("Multi Relay Chat", 1)[-1].split("Available chat channels")[0]
            assert has_pending_invitation(db, room, alice) and not is_member(db, room, alice)
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_gates_are_checked_before_a_room_is_materialized(db, lane, hub, presence, alice):
    """Review of #300: an account the open-room gates turn away must not
    be able to fill the cap with rooms it can never enter."""
    from netbbs.mrc.settings import count_open_rooms

    create_channel(db, "general", creator=alice)

    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub, min_level=50)
        try:
            session = await _browse(
                lane, hub, presence, alice,
                ["0", "1", "0", "1", "viaPicker", "b", "0", "2", "/join mrc:viaJoin", "/quit"], mrc_bridge=bridge,
            )
            text = _visible_text(session)
            assert text.count("You are not authorized to open MRC rooms on this node.") == 2
            assert count_open_rooms(db) == 0
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_a_room_blocked_after_opening_admits_nobody(db, lane, hub, presence, alice):
    """Review of #300: the blocklist applies to every way into an
    existing open room, not only to opening a new one."""
    from netbbs.mrc.settings import get_mrc_mapping

    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub)
        try:
            temp = (await bridge.open_room("temp", "alice")).channel
            other = (await bridge.open_room("other", "alice")).channel
            save_open_room_settings(db, OpenRoomSettings(enabled=True, blocklist=("temp",)))
            await bridge.refresh_channel_mappings()
            assert get_mrc_mapping(db, temp) is not None  # still there until the sweeper retires it
            # Section: temp is not listed; from #other, both /join forms refuse.
            session = await _browse(
                lane, hub, presence, alice,
                ["0", "1", "0", "2", "/join temp", "/join mrc:temp", "/quit"], mrc_bridge=bridge,
            )
            text = _visible_text(session)
            section = text.split("Multi Relay Chat", 1)[-1]
            assert "other" in section and " temp" not in section.split("Joined")[0]
            assert "Joined" in text and other.name in text
            assert text.count("The SysOp has blocked MRC room #temp on this node.") == 2
            assert hub.participant_count(temp.name) == 0
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())
