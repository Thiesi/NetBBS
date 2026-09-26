"""
Issue #716: callers inside a chat channel the SysOp deletes, hides (a
carried Link channel, #683) or retires (an MRC room a caller opened) are
moved back to the channel list, with the reason carried into it.

`ChatHub.close_channel` is the push; `_chat_loop` turns it into
`_ToPicker` plus a pending notice. The same loop also catches a channel
that closed where no push could reach it -- between entry authorization
and `hub.join`, or from outside this process -- when it next records or
sends anything.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from types import SimpleNamespace

from netbbs.chat.channels import create_channel, delete_channel, get_channel_by_name, update_channel
from netbbs.chat.hub import ChannelClosed, ChatHub, ParticipantId
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import get_scrollback
from netbbs.link.carry import accept_genesis, hide_carried_resource, restore_excluded
from netbbs.link.events import build_channel_genesis
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.mrc.settings import OpenRoomSettings, materialize_open_room, save_open_room_settings
from netbbs.net import admin_flow, chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.notices import pending_notices
from netbbs.rendering import strip_ansi
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession as AdminSession
from tests.test_chat_flow_moderation import FakeSession

CHANNEL_ID = "c" * 64


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
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture(scope="module")
def remote():
    return bootstrap_node_identity("close-remote")


@pytest.fixture(scope="module")
def own():
    return bootstrap_node_identity("close-own")


def _notices(session) -> str:
    return strip_ansi("\n".join(pending_notices(session)))


async def _enter(lane, hub, channel, user, session):
    task = asyncio.create_task(
        chat_flow._chat_loop(
            session, lane, hub, PresenceRegistry(), MessageMailbox(), InputHistory(), channel, user,
        )
    )
    # Polled, not a fixed number of yields: entry runs through the lane.
    while not any(m.kind == "join" for m in await lane.run(get_scrollback, channel)):
        await asyncio.sleep(0.01)
    return task


def _carried_channel(db, own, remote, sysop):
    genesis = build_channel_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        channel_id=CHANNEL_ID, name="lobby", created_at="2026-01-01T00:00:00Z",
    )
    assert accept_genesis(
        db, kind="channels", envelope=genesis.to_dict(), sender_fingerprint=remote.fingerprint,
        content_id=genesis.content_id, own_fingerprint=own.fingerprint, cap=None,
    ) == "carried"
    return get_channel_by_name(db, "lobby")


# -- the hub -------------------------------------------------------------------


def test_close_channel_supersedes_a_stalled_participants_backlog():
    hub = ChatHub(queue_maxsize=2)
    stalled = hub.join("lobby", ParticipantId("alice", 1))
    idle = hub.join("lobby", ParticipantId("bob", 2))
    other = hub.join("elsewhere", ParticipantId("carol", 3))
    for n in range(5):
        asyncio.run(hub.broadcast("lobby", f"line {n}"))
    asyncio.run(hub.broadcast("elsewhere", "still here"))

    assert hub.close_channel("lobby") == 2

    # The close is the next thing each reader sees, not the end of a backlog
    # of a channel that is gone.
    for queue in (stalled, idle):
        assert queue.qsize() == 1
        assert isinstance(queue.get_nowait(), ChannelClosed)
    assert other.get_nowait() == "still here"
    # Participants leave by themselves as their loops unwind.
    assert hub.participant_count("lobby") == 2


def test_close_channel_on_an_empty_channel_tells_nobody():
    assert ChatHub().close_channel("lobby") == 0


# -- the chat loop ---------------------------------------------------------------


def test_a_deleted_channel_sends_its_callers_to_the_channel_list(db, lane, hub, sysop, alice):
    channel = create_channel(db, "lobby", creator=sysop)
    session = FakeSession([])

    async def scenario():
        task = await _enter(lane, hub, channel, alice, session)
        await lane.run(delete_channel, channel, deleted_by=sysop)
        assert hub.close_channel(channel.name) == 1
        return await asyncio.wait_for(task, timeout=5)

    action = asyncio.run(scenario())

    # Back to the list, not out of chat, and no "press any key" hold.
    assert isinstance(action, chat_flow._ToPicker)
    assert "#lobby was closed by the SysOp." in _notices(session)
    assert "Press any key" not in strip_ansi("".join(session.written))
    assert hub.participant_count("lobby") == 0
    # Leaving recorded nothing into the deleted channel (its foreign key
    # used to fail the insert on the way out).
    assert db.connection.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0] == 0


def test_a_hidden_carried_channel_sends_its_callers_out_and_keeps_its_scrollback(db, lane, hub, own, remote, sysop, alice):
    from netbbs.managed_dns.state import set_node_fingerprint

    set_node_fingerprint(db, own.fingerprint)
    channel = _carried_channel(db, own, remote, sysop)
    session = FakeSession([])

    async def scenario():
        task = await _enter(lane, hub, channel, alice, session)
        before = await lane.run(get_scrollback, channel)
        await lane.run(hide_carried_resource, "channels", CHANNEL_ID, actor=sysop)
        hub.close_channel(channel.name)
        return before, await asyncio.wait_for(task, timeout=5)

    before, action = asyncio.run(scenario())

    assert isinstance(action, chat_flow._ToPicker)
    assert "#lobby was closed by the SysOp." in _notices(session)
    # Kept exactly as it was for Restore: no leave line was added.
    assert [m.kind for m in get_scrollback(db, channel)] == [m.kind for m in before] == ["join"]


def test_a_channel_closed_before_the_caller_joined_the_hub_still_sends_them_back(db, lane, hub, sysop, alice):
    channel = create_channel(db, "lobby", creator=sysop)
    delete_channel(db, channel, deleted_by=sysop)
    session = FakeSession([])

    action = asyncio.run(asyncio.wait_for(
        chat_flow._chat_loop(
            session, lane, hub, PresenceRegistry(), MessageMailbox(), InputHistory(), channel, alice,
        ),
        timeout=5,
    ))

    assert isinstance(action, chat_flow._ToPicker)
    assert "#lobby was closed by the SysOp." in _notices(session)
    assert hub.participant_count("lobby") == 0


def test_sending_into_a_channel_closed_elsewhere_names_the_real_reason(db, sysop, alice):
    channel = create_channel(db, "lobby", creator=sysop)
    assert chat_flow._live_send_refusal(db, channel, alice) is None

    delete_channel(db, channel, deleted_by=sysop)
    assert chat_flow._live_send_refusal(db, channel, alice) == "#lobby was closed by the SysOp."

    # A new channel that took the name is not the one the caller was in,
    # even where SQLite hands it the freed row id.
    create_channel(db, "lobby", creator=alice)
    assert chat_flow._live_send_refusal(db, channel, alice) == "#lobby was closed by the SysOp."


def test_a_restore_before_the_caller_unwinds_still_gains_no_leave(db, lane, hub, own, remote, sysop, alice):
    from netbbs.managed_dns.state import set_node_fingerprint

    set_node_fingerprint(db, own.fingerprint)
    channel = _carried_channel(db, own, remote, sysop)
    session = FakeSession([])

    async def scenario():
        task = await _enter(lane, hub, channel, alice, session)
        await lane.run(hide_carried_resource, "channels", CHANNEL_ID, actor=sysop)
        hub.close_channel(channel.name)
        # Restored before the caller's loop has had a turn to unwind: the
        # database says "open" again by the time it records its leave.
        restore_excluded(db, "channels", CHANNEL_ID, actor=sysop)
        return await asyncio.wait_for(task, timeout=5)

    assert isinstance(asyncio.run(scenario()), chat_flow._ToPicker)
    assert [m.kind for m in get_scrollback(db, channel)] == ["join"]


def test_a_close_before_the_join_was_announced_tells_link_peers_nothing(db, lane, hub, sysop, alice):
    channel = create_channel(db, "lobby", creator=sysop)
    delete_channel(db, channel, deleted_by=sysop)
    told = []

    class _Bridge:
        async def broadcast_local_presence_live(self, channel, *, change, username):
            told.append(change)

    link_context = SimpleNamespace(realtime_bridge=_Bridge(), realtime_registry=None)

    action = asyncio.run(asyncio.wait_for(
        chat_flow._chat_loop(
            FakeSession([]), lane, hub, PresenceRegistry(), MessageMailbox(), InputHistory(), channel, alice,
            link_context=link_context,
        ),
        timeout=5,
    ))

    assert isinstance(action, chat_flow._ToPicker)
    assert told == []


def test_a_rename_is_not_a_close(db, lane, hub, sysop, alice):
    # The standalone admin CLI may rename a channel with callers inside.
    channel = create_channel(db, "lobby", creator=sysop)
    session = FakeSession([])

    async def scenario():
        task = await _enter(lane, hub, channel, alice, session)
        await lane.run(lambda d: update_channel(
            d, channel, name="parlour", description=channel.description, min_level=channel.min_level,
            category_id=channel.category_id, pinned=channel.pinned, hidden=channel.hidden,
            members_only=channel.members_only, allow_member_invites=channel.allow_member_invites,
            min_age=channel.min_age, name_requirement=channel.name_requirement,
            community_id=channel.community_id, changed_by=sysop,
        ))
        assert await lane.run(chat_flow._live_send_refusal, channel, alice) is None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    # Its leave is recorded like any other.
    assert [m.kind for m in get_scrollback(db, channel)] == ["join", "leave"]


# -- the SysOp console -----------------------------------------------------------


def test_deleting_a_channel_moves_its_callers_out(db, lane, hub, sysop):
    channel = create_channel(db, "lobby", creator=sysop)
    queue = hub.join("lobby", ParticipantId("alice", 1))
    session = AdminSession(["lobby"])

    assert asyncio.run(admin_flow._delete_channel_screen(session, lane, sysop, channel, chat_hub=hub)) is True

    assert isinstance(queue.get_nowait(), ChannelClosed)
    notices = strip_ansi("".join(admin_flow._take_notices(session)))
    assert "'lobby' deleted." in notices
    assert "1 caller session in it moved back to the channel list." in notices


def test_deleting_a_carried_channel_hides_it_and_moves_its_callers_out(db, lane, hub, own, remote, sysop):
    channel = _carried_channel(db, own, remote, sysop)
    queue = hub.join("lobby", ParticipantId("alice", 1))
    session = AdminSession(["lobby"])

    assert asyncio.run(admin_flow._delete_channel_screen(
        session, lane, sysop, channel, own_fingerprint=own.fingerprint, chat_hub=hub,
    )) is True

    assert isinstance(queue.get_nowait(), ChannelClosed)
    assert "moved back to the channel list" in strip_ansi("".join(admin_flow._take_notices(session)))


def test_a_cancelled_delete_moves_nobody(db, lane, hub, sysop):
    channel = create_channel(db, "lobby", creator=sysop)
    queue = hub.join("lobby", ParticipantId("alice", 1))

    assert asyncio.run(admin_flow._delete_channel_screen(
        AdminSession(["not it"]), lane, sysop, channel, chat_hub=hub,
    )) is False

    assert queue.empty()


def test_retiring_an_open_mrc_room_moves_its_callers_out(db, lane, hub, sysop):
    settings = save_open_room_settings(db, OpenRoomSettings(enabled=True))
    channel = materialize_open_room(db, "lobby", open_settings=settings).channel
    queue = hub.join(channel.name, ParticipantId("alice", 1))
    session = AdminSession(["y"])

    assert asyncio.run(admin_flow._retire_open_room_screen(
        session, lane, sysop, channel, mrc_bridge=None, chat_hub=hub,
    )) is True

    assert isinstance(queue.get_nowait(), ChannelClosed)
