"""
Issue #304 from the caller's side: `/away` mirrored to the hub, the
hub's welcome once per session, `/topic` inside an open room, the
masked identity commands, and `/mrc stats` -- on the rigs of
`tests/test_chat_flow_mrc.py` (a `_chat_loop` on one channel) and
`tests/test_chat_flow_mrc_open_rooms.py` (the real picker), with a real
bridge on the loopback fake hub.
"""

from __future__ import annotations

import asyncio

from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ParticipantId
from netbbs.mrc.settings import set_mrc_room
from tests.test_chat_flow_mrc import (  # noqa: F401 -- fixtures and helpers
    _rig,
    _run,
    _text,
    alice,
    channel,
    db,
    hub,
    lane,
    presence,
    sysop,
)
from tests.test_chat_flow_mrc_open_rooms import _bridge_on, _browse, _visible_text


def test_away_is_mirrored_to_the_hub(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            session, _ = await _run(
                lane, hub, presence, channel, alice, ["/away making tea", "/away", "/quit"], mrc_bridge=rig.bridge,
            )
            text = _text(session)
            assert "You are now marked away: making tea" in text and "You are no longer marked away." in text
            await rig.fake.wait_for(lambda p: p.body == "AFK making tea" and p.from_user == "alice")
            await rig.fake.wait_for(lambda p: p.body == "AFK" and p.from_user == "alice")
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_the_hubs_welcome_is_shown_once_per_session(db, lane, hub, presence, alice, sysop):
    first = create_channel(db, "first", creator=sysop)
    second = create_channel(db, "second", creator=sysop)
    set_mrc_room(db, first, "lobby")
    set_mrc_room(db, second, "elsewhere")

    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub, open_rooms=False)
        try:
            # 0,1 enters #first (the only entries are first and second);
            # /join second moves to a second MRC room in the same session.
            session = await _browse(
                lane, hub, presence, alice, ["0", "1", "/join second", "/quit"], mrc_bridge=bridge,
            )
            text = _visible_text(session)
            assert text.count("Joined") == 2
            assert text.count("[MRC] Welcome to the fake hub") == 1
            assert text.count("[MRC] MOTD reply line 1") == 1
            assert len(fake.packets(body_prefix="MOTD")) == 1
            # A new session is welcomed again. The node-wide send bucket
            # paces the outbound queue, so wait for the packet to land.
            session = await _browse(lane, hub, presence, alice, ["0", "1", "/quit"], mrc_bridge=bridge)
            assert "[MRC] Welcome to the fake hub" in _visible_text(session)
            deadline = asyncio.get_running_loop().time() + 4
            while len(fake.packets(body_prefix="MOTD")) < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            assert len(fake.packets(body_prefix="MOTD")) == 2, [p.body for p in fake.received if p.from_user == "alice"]
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_topic_inside_an_open_room_belongs_to_the_hub(db, lane, hub, presence, alice):
    async def scenario():
        fake, bridge = await _bridge_on(db, lane, hub)
        try:
            garden = (await bridge.open_room("garden", "alice")).channel
            session, _ = await _run(
                lane, hub, presence, garden, alice, ["/topic", "/topic tulips today", "/quit"], mrc_bridge=bridge,
            )
            text = _text(session)
            assert "#garden has no topic set." in text
            assert "(topic change sent to the MRC hub; it decides, and its answer follows)" in text
            sent = await fake.wait_for(lambda p: p.body == "NEWTOPIC:garden:tulips today")
            assert sent.from_user == "alice"
            assert "Topic changed by" not in text
            # And a mapped channel keeps the local meaning (with the usual
            # edit permission the local /topic requires).
            from netbbs.moderation import ChannelPermission, grant_permissions

            local = create_channel(db, "local", creator=alice)
            grant_permissions(
                db, alice, object_type="channel", object_id=local.id,
                permissions=ChannelPermission.EDIT, granted_by=alice,
            )
            set_mrc_room(db, local, "localroom")
            await bridge.refresh_channel_mappings()
            session, _ = await _run(lane, hub, presence, local, alice, ["/topic mine", "/quit"], mrc_bridge=bridge)
            assert "Topic changed by alice: mine" in _text(session)
            assert not [p for p in fake.received if p.body.startswith("NEWTOPIC:localroom")]
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_identity_commands_take_the_secret_masked_and_never_show_it(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            session, _ = await _run(
                lane, hub, presence, channel, alice,
                [
                    "/mrc identify", "s3cret|12word", "/mrc register", "", "/mrc identify extra",
                    "/mrc roompass", "has a space", "/quit",
                ],
                mrc_bridge=rig.bridge,
            )
            text = _text(session)
            assert "Password (not shown; blank = cancel):" in text
            assert "(sent to the hub; its answer follows)" in text
            assert "(cancelled)" in text
            assert "Type the command alone" in text
            assert "printable ASCII without spaces or tildes" in text
            assert "s3cret" not in "\n".join(session.written)
            # Sent verbatim: a pipe-code-shaped substring is part of the
            # credential, never sanitized away as if it were chat.
            sent = await rig.fake.wait_for(lambda p: p.body.startswith("IDENTIFY "))
            assert (sent.from_user, sent.body) == ("alice", "IDENTIFY s3cret|12word")
            assert not [p for p in rig.fake.received if p.body.startswith(("REGISTER", "ROOMPASS"))]
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_mrc_stats_shows_the_reply_and_the_section_carries_the_size(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            rig.fake.users[("other", "bob")] = "garden"
            hub.join(channel.name, ParticipantId("carol", 5))
            await rig.bridge.local_join(channel, "carol")
            await rig.fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            await asyncio.sleep(0.2)
            session, _ = await _run(lane, hub, presence, channel, alice, ["/mrc stats", "/quit"], mrc_bridge=rig.bridge)
            await asyncio.sleep(0.2)
            assert rig.bridge.status().network_summary is not None
            from netbbs.net.chat_flow import _mrc_section_description
            assert rig.bridge.status().network_summary in _mrc_section_description(rig.bridge.status())
        finally:
            await rig.close()
    asyncio.run(scenario())
