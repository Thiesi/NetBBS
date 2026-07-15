"""
Tests for Phase 2 Track 5d (design doc §8, sign-off round 44):
`/join`, `/leave`'s new "back to the channel picker" meaning (distinct
from `/quit`), and `/topic`, driven through the real `_chat_loop`
dispatcher -- plus `browse_channels`'s outer-loop dispatch on the
resulting `ChatAction`, tested in isolation via monkeypatched
`_pick_channel`/`_chat_loop` since `pick_item` itself needs a
`read_key()`-capable session `FakeSession` (borrowed from
test_chat_flow_moderation.py) doesn't implement.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel, get_channel_by_name
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import get_scrollback
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.storage.database import Database
from tests.test_chat_flow_moderation import FakeSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def hub():
    return ChatHub()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=100)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, sysop):
    return create_channel(db, "lobby", creator=sysop)


@pytest.fixture
def other_channel(db, sysop):
    return create_channel(db, "offtopic", creator=sysop)


@pytest.fixture
def high_level_channel(db, sysop):
    return create_channel(db, "staff", creator=sysop, min_level=50)


def _written(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(db, hub, presence, channel, user, lines):
    session = FakeSession(lines)
    mailbox = MessageMailbox()
    history = InputHistory()
    action = await asyncio.wait_for(
        chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, user), timeout=2
    )
    return session, action


# -- /quit vs /leave: distinct ChatAction outcomes ---------------------------


def test_quit_returns_quit_action(db, hub, presence, alice, channel):
    _, action = asyncio.run(_run(db, hub, presence, channel, alice, ["/quit"]))
    assert isinstance(action, chat_flow._Quit)


def test_leave_returns_to_picker_action(db, hub, presence, alice, channel):
    # A real behavior change from round 39 (where /leave aliased /quit) --
    # confirms the two are no longer the same handler under the hood.
    _, action = asyncio.run(_run(db, hub, presence, channel, alice, ["/leave"]))
    assert isinstance(action, chat_flow._ToPicker)


def test_kick_still_resolves_to_quit_action(db, hub, presence, sysop, alice, channel):
    # A kick/ban forcing a session out has no ChatAction of its own --
    # confirms it still resolves to _Quit(), not left as None/crashing.
    grant_permissions(
        db,
        sysop,
        object_type="channel",
        object_id=channel.id,
        permissions=ChannelPermission.MODERATE,
        granted_by=sysop,
    )

    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        target_session = FakeSession()  # never types anything
        target_task = asyncio.create_task(
            chat_flow._chat_loop(target_session, db, hub, presence, mailbox, history, channel, alice)
        )
        await asyncio.sleep(0)  # let target actually join before the kick is issued

        kicker_session = FakeSession([f"/kick {alice.username}", "/quit"])
        kicker_action = await asyncio.wait_for(
            chat_flow._chat_loop(kicker_session, db, hub, presence, mailbox, history, channel, sysop), timeout=2
        )
        target_action = await asyncio.wait_for(target_task, timeout=2)
        return kicker_action, target_action

    kicker_action, target_action = asyncio.run(scenario())
    assert isinstance(kicker_action, chat_flow._Quit)
    assert isinstance(target_action, chat_flow._Quit)


# -- /join --------------------------------------------------------------


def test_join_known_authorized_channel_returns_switch_to_action(
    db, hub, presence, alice, channel, other_channel
):
    _, action = asyncio.run(
        _run(db, hub, presence, channel, alice, [f"/join {other_channel.name}"])
    )
    assert isinstance(action, chat_flow._SwitchTo)
    assert action.channel.id == other_channel.id


def test_join_unknown_channel_shows_error_and_does_not_switch(db, hub, presence, alice, channel):
    session, action = asyncio.run(
        _run(db, hub, presence, channel, alice, ["/join nosuchchannel", "/quit"])
    )
    assert "No such channel" in _written(session)
    assert isinstance(action, chat_flow._Quit)  # only exited via the trailing /quit


def test_join_unauthorized_channel_shows_error_and_does_not_switch(
    db, hub, presence, alice, channel, high_level_channel
):
    session, action = asyncio.run(
        _run(db, hub, presence, channel, alice, [f"/join {high_level_channel.name}", "/quit"])
    )
    assert "not authorized" in _written(session)
    assert isinstance(action, chat_flow._Quit)


def test_join_current_channel_shows_message_and_does_not_switch(db, hub, presence, alice, channel):
    session, action = asyncio.run(
        _run(db, hub, presence, channel, alice, [f"/join {channel.name}", "/quit"])
    )
    assert "already in" in _written(session)
    assert isinstance(action, chat_flow._Quit)


def test_join_with_no_argument_shows_usage(db, hub, presence, alice, channel):
    session, action = asyncio.run(_run(db, hub, presence, channel, alice, ["/join", "/quit"]))
    assert "Usage: /join" in _written(session)


# -- /topic ---------------------------------------------------------------


def test_topic_shows_no_topic_set_by_default(db, hub, presence, alice, channel):
    session, _ = asyncio.run(_run(db, hub, presence, channel, alice, ["/topic", "/quit"]))
    assert "No topic set." in _written(session)


def test_topic_change_without_permission_is_rejected(db, hub, presence, alice, channel):
    session, _ = asyncio.run(
        _run(db, hub, presence, channel, alice, ["/topic new topic here", "/quit"])
    )
    assert "do not have permission" in _written(session)
    assert get_channel_by_name(db, channel.name).topic is None


def test_topic_change_with_permission_sets_and_announces(db, hub, presence, sysop, channel):
    grant_permissions(
        db,
        sysop,
        object_type="channel",
        object_id=channel.id,
        permissions=ChannelPermission.EDIT,
        granted_by=sysop,
    )

    session, _ = asyncio.run(_run(db, hub, presence, channel, sysop, ["/topic Retro chat", "/quit"]))

    assert "Topic changed by sysop: Retro chat" in _written(session)
    assert get_channel_by_name(db, channel.name).topic == "Retro chat"


def test_topic_shows_current_topic_once_set(db, hub, presence, sysop, channel):
    grant_permissions(
        db,
        sysop,
        object_type="channel",
        object_id=channel.id,
        permissions=ChannelPermission.EDIT,
        granted_by=sysop,
    )

    session, _ = asyncio.run(
        _run(db, hub, presence, channel, sysop, ["/topic Retro chat", "/topic", "/quit"])
    )

    assert "Topic: Retro chat" in _written(session)


def test_topic_change_is_broadcast_to_other_participants(db, hub, presence, sysop, channel):
    grant_permissions(
        db,
        sysop,
        object_type="channel",
        object_id=channel.id,
        permissions=ChannelPermission.EDIT,
        granted_by=sysop,
    )
    queue = hub.join(channel.name, "bob:1")
    try:
        asyncio.run(_run(db, hub, presence, channel, sysop, ["/topic Retro chat", "/quit"]))
        received = []
        while not queue.empty():
            item = queue.get_nowait()
            received.append(item.text if isinstance(item, chat_flow._TimestampedNotice) else item)
        assert any("Topic changed by sysop: Retro chat" in msg for msg in received)
    finally:
        hub.leave(channel.name, "bob:1")


def test_topic_change_is_not_persisted_to_scrollback(db, hub, presence, sysop, channel):
    grant_permissions(
        db,
        sysop,
        object_type="channel",
        object_id=channel.id,
        permissions=ChannelPermission.EDIT,
        granted_by=sysop,
    )

    asyncio.run(_run(db, hub, presence, channel, sysop, ["/topic Retro chat", "/quit"]))

    scrollback = get_scrollback(db, channel)
    assert all(m.kind in ("join", "leave") for m in scrollback)


# -- browse_channels' outer-loop dispatch on ChatAction (isolated) ----------


def test_browse_channels_switch_to_skips_the_picker(monkeypatch, db, hub, presence, alice, channel, other_channel):
    pick_calls = []

    async def fake_pick_channel(session, db, hub, user, *, category_id):
        pick_calls.append(category_id)
        return channel

    chat_loop_calls = []

    async def fake_chat_loop(session, db, hub, presence, mailbox, history, ch, user, **kwargs):
        chat_loop_calls.append(ch)
        if len(chat_loop_calls) == 1:
            return chat_flow._SwitchTo(other_channel)
        return chat_flow._Quit()

    monkeypatch.setattr(chat_flow, "_pick_channel", fake_pick_channel)
    monkeypatch.setattr(chat_flow, "_chat_loop", fake_chat_loop)

    asyncio.run(chat_flow.browse_channels(FakeSession([]), db, hub, presence, MessageMailbox(), InputHistory(), alice))

    assert pick_calls == [None]  # picker only consulted once, for the initial entry
    assert chat_loop_calls == [channel, other_channel]  # second call jumped straight there


def test_browse_channels_to_picker_reconsults_the_picker(monkeypatch, db, hub, presence, alice, channel):
    pick_calls = []

    async def fake_pick_channel(session, db, hub, user, *, category_id):
        pick_calls.append(category_id)
        return channel

    chat_loop_calls = 0

    async def fake_chat_loop(session, db, hub, presence, mailbox, history, ch, user, **kwargs):
        nonlocal chat_loop_calls
        chat_loop_calls += 1
        if chat_loop_calls < 2:
            return chat_flow._ToPicker()
        return chat_flow._Quit()

    monkeypatch.setattr(chat_flow, "_pick_channel", fake_pick_channel)
    monkeypatch.setattr(chat_flow, "_chat_loop", fake_chat_loop)

    asyncio.run(chat_flow.browse_channels(FakeSession([]), db, hub, presence, MessageMailbox(), InputHistory(), alice))

    assert pick_calls == [None, None]  # re-consulted after /leave, always the top level
    assert chat_loop_calls == 2


def test_browse_channels_quit_exits_without_repicking(monkeypatch, db, hub, presence, alice, channel):
    pick_calls = []

    async def fake_pick_channel(session, db, hub, user, *, category_id):
        pick_calls.append(category_id)
        return channel

    async def fake_chat_loop(session, db, hub, presence, mailbox, history, ch, user, **kwargs):
        return chat_flow._Quit()

    monkeypatch.setattr(chat_flow, "_pick_channel", fake_pick_channel)
    monkeypatch.setattr(chat_flow, "_chat_loop", fake_chat_loop)

    asyncio.run(chat_flow.browse_channels(FakeSession([]), db, hub, presence, MessageMailbox(), InputHistory(), alice))

    assert pick_calls == [None]  # picked once, then exited -- no second pick


def test_browse_channels_returns_immediately_if_nothing_picked(monkeypatch, db, hub, presence, alice):
    async def fake_pick_channel(session, db, hub, user, *, category_id):
        return None

    chat_loop_called = False

    async def fake_chat_loop(session, db, hub, presence, mailbox, history, ch, user, **kwargs):
        nonlocal chat_loop_called
        chat_loop_called = True
        return chat_flow._Quit()

    monkeypatch.setattr(chat_flow, "_pick_channel", fake_pick_channel)
    monkeypatch.setattr(chat_flow, "_chat_loop", fake_chat_loop)

    asyncio.run(chat_flow.browse_channels(FakeSession([]), db, hub, presence, MessageMailbox(), InputHistory(), alice))

    assert chat_loop_called is False
