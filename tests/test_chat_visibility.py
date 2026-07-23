"""
Tests for hidden-channel visibility (design doc §8):
`netbbs.net.chat_flow._visible_channels_for`, the shared filter behind
the picker, `/list`, and `/whois`'s channel-membership display, plus
`/list` and `/whois` themselves through the real dispatcher for
end-to-end confirmation.

`hidden` and `members_only` are independent axes -- "hidden + open is
obscurity, not access control": a `members_only`-but-not-`hidden`
channel still appears in listings, only `hidden` controls listing
visibility itself. Exercised as distinct combinations below, not
assumed.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.membership import add_member, create_invitation
from netbbs.chat.presence import PresenceRegistry
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.net import chat_flow
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_chat_flow_moderation import FakeSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def hub():
    return ChatHub()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def mailbox():
    return MessageMailbox()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


def _grant_manage_members(db, user, channel):
    grant_permissions(
        db, user, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=user,
    )


def _written(session: FakeSession) -> str:
    return "\n".join(session.written)


# -- _visible_channels_for: independent hidden/members_only axes -----------


def test_open_channel_is_visible_to_everyone(db, alice, bob):
    channel = create_channel(db, "lobby", creator=alice)
    assert channel in chat_flow._visible_channels_for(db, bob)


def test_members_only_but_not_hidden_channel_still_appears_in_listings(db, alice, bob):
    # "hidden + open is obscurity, not access control" -- members_only
    # alone doesn't hide anything, it only gates /join.
    channel = create_channel(db, "vip-lounge", creator=alice, members_only=True)
    assert channel in chat_flow._visible_channels_for(db, bob)


def test_hidden_channel_is_excluded_for_an_unrelated_user(db, alice, bob):
    channel = create_channel(db, "secret", creator=alice, hidden=True)
    assert channel not in chat_flow._visible_channels_for(db, bob)


def test_hidden_channel_is_visible_to_a_direct_member(db, alice, bob):
    channel = create_channel(db, "secret", creator=alice, hidden=True, members_only=True)
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    assert channel in chat_flow._visible_channels_for(db, bob)


def test_hidden_channel_is_visible_to_someone_with_a_pending_invitation(db, alice, bob):
    channel = create_channel(db, "secret", creator=alice, hidden=True, members_only=True)
    _grant_manage_members(db, alice, channel)
    create_invitation(db, channel, bob, invited_by=alice)
    assert channel in chat_flow._visible_channels_for(db, bob)


def test_hidden_channel_is_visible_to_any_moderator_grant_holder(db, alice, bob):
    # Any ChannelPermission grant qualifies, not just MANAGE_MEMBERS --
    # a channel's own moderator (MODERATE) or topic-editor (EDIT)
    # shouldn't have a hidden channel invisible to them either.
    channel = create_channel(db, "secret", creator=alice, hidden=True)
    grant_permissions(
        db, bob, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MODERATE, granted_by=alice,
    )
    assert channel in chat_flow._visible_channels_for(db, bob)


def test_a_grant_on_a_different_channel_does_not_reveal_this_hidden_one(db, alice, bob):
    channel = create_channel(db, "secret", creator=alice, hidden=True)
    other = create_channel(db, "public", creator=alice)
    grant_permissions(
        db, bob, object_type="channel", object_id=other.id,
        permissions=ChannelPermission.MODERATE, granted_by=alice,
    )
    assert channel not in chat_flow._visible_channels_for(db, bob)


def test_hidden_channel_still_respects_min_level(db, alice, bob):
    # Being a member doesn't bypass the level gate -- both conditions
    # must hold, not either/or.
    channel = create_channel(db, "secret", creator=alice, hidden=True, members_only=True, min_level=50)
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)
    assert channel not in chat_flow._visible_channels_for(db, bob)


# -- /list through the real dispatcher --------------------------------


def test_list_excludes_a_hidden_channel_for_a_non_member(db, lane, hub, presence, mailbox, alice, bob):
    create_channel(db, "secret", creator=alice, hidden=True)
    visible_channel = create_channel(db, "lobby", creator=alice)

    session = asyncio.run(
        _run_list(lane, hub, presence, mailbox, visible_channel, bob)
    )
    output = _written(session)
    assert "secret" not in output
    assert "lobby" in output


def test_list_includes_a_hidden_channel_for_a_member(db, lane, hub, presence, mailbox, alice, bob):
    channel = create_channel(db, "secret", creator=alice, hidden=True, members_only=True)
    _grant_manage_members(db, alice, channel)
    add_member(db, channel, bob, granted_by=alice)

    session = asyncio.run(_run_list(lane, hub, presence, mailbox, channel, bob))
    assert "secret" in _written(session)


def test_list_includes_a_members_only_but_not_hidden_channel_for_a_non_member(db, lane, hub, presence, mailbox, alice, bob):
    channel = create_channel(db, "vip-lounge", creator=alice, members_only=True)
    other = create_channel(db, "lobby", creator=alice)

    session = asyncio.run(_run_list(lane, hub, presence, mailbox, other, bob))
    assert "vip-lounge" in _written(session)


async def _run_list(lane, hub, presence, mailbox, channel, user):
    from netbbs.net.char_input import InputHistory

    session = FakeSession(["/list", "/quit"])
    await asyncio.wait_for(
        chat_flow._chat_loop(session, lane, hub, presence, mailbox, InputHistory(), channel, user), timeout=2
    )
    return session


# -- /whois's channel-membership display --------------------------------


def test_whois_hides_a_hidden_channel_the_requester_cannot_see(db, lane, hub, presence, mailbox, alice, bob):
    # A separate admin grants bob membership -- alice (the requester
    # below) must hold no grant of her own on "secret" at all, or she'd
    # legitimately gain visibility through it and the test would prove
    # nothing.
    presence.enter("bob")
    admin = create_user(db, "admin", password="hunter2", user_level=10)
    secret = create_channel(db, "secret", creator=alice, hidden=True, members_only=True)
    _grant_manage_members(db, admin, secret)
    add_member(db, secret, bob, granted_by=admin)
    lobby = create_channel(db, "lobby", creator=alice)

    async def scenario():
        target_session = FakeSession()
        target_task = asyncio.create_task(
            _run_whois_target(lane, hub, presence, mailbox, secret, bob)
        )
        while hub.participant_count(secret.name) < 1:
            await asyncio.sleep(0)

        asker_session = FakeSession(["/whois bob", "/quit"])
        from netbbs.net.char_input import InputHistory

        await asyncio.wait_for(
            chat_flow._chat_loop(asker_session, lane, hub, presence, mailbox, InputHistory(), lobby, alice),
            timeout=2,
        )
        target_task.cancel()
        await asyncio.gather(target_task, return_exceptions=True)
        return asker_session

    asker = asyncio.run(scenario())
    output = _written(asker)
    assert "secret" not in output


async def _run_whois_target(lane, hub, presence, mailbox, channel, user):
    from netbbs.net.char_input import InputHistory

    session = FakeSession()
    await chat_flow._chat_loop(session, lane, hub, presence, mailbox, InputHistory(), channel, user)
