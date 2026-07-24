"""
Tests for wiring linked-channel messages into the live interactive chat
send path (design doc, issue #91) -- `netbbs.net.chat_flow._chat_loop`
queuing a `channel_message` Link event for a self-authored message sent
in a Linked channel, mirroring `netbbs.net.login_flow._compose_new_post`'s
own `queue_board_post_if_linked` call exactly.

Driven through the real `_chat_loop` dispatcher, same harness
`test_chat_flow_join.py` already established (`FakeSession` borrowed from
test_chat_flow_moderation.py).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.scrollback import get_scrollback
from netbbs.link.boards import LinkContext
from netbbs.link.channels import link_channel
from netbbs.link.events import ChannelMessage
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
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
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=100)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, sysop):
    return create_channel(db, "lobby", creator=sysop)


@pytest.fixture
def node_identity():
    return bootstrap_node_identity("thisnode")


def _link_context_for(node_identity) -> LinkContext:
    return LinkContext(node_identity=node_identity, link_node=LinkNode(identity=node_identity))


async def _run(lane, hub, presence, channel, user, lines, *, link_context=None):
    session = FakeSession(lines)
    mailbox = MessageMailbox()
    history = InputHistory()
    action = await asyncio.wait_for(
        chat_flow._chat_loop(
            session, lane, hub, presence, mailbox, history, channel, user, link_context=link_context
        ),
        timeout=2,
    )
    return session, action


def test_message_sent_in_a_linked_channel_queues_a_channel_message(db, lane, hub, presence, channel, alice, node_identity):
    link_channel(db, channel, node_identity=node_identity)
    link_context = _link_context_for(node_identity)

    asyncio.run(_run(lane, hub, presence, channel, alice, ["hello there", "/quit"], link_context=link_context))

    row = db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE channel_id = ? AND kind = 'message' ORDER BY id DESC LIMIT 1", (channel.id,)
    ).fetchone()
    assert row["link_event_json"] is not None
    event = ChannelMessage.from_dict(json.loads(row["link_event_json"]))
    assert event.payload["body"] == "hello there"
    assert event.payload["channel_id"] == channel.channel_id


def test_message_sent_in_an_unlinked_channel_behaves_exactly_as_before(db, lane, hub, presence, channel, alice, node_identity):
    link_context = _link_context_for(node_identity)  # a real link_context, but the channel itself isn't Linked

    asyncio.run(_run(lane, hub, presence, channel, alice, ["hello there", "/quit"], link_context=link_context))

    scrollback = get_scrollback(db, channel)
    assert [m.body for m in scrollback if m.kind == "message"] == ["hello there"]
    row = db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE channel_id = ? AND kind = 'message' ORDER BY id DESC LIMIT 1", (channel.id,)
    ).fetchone()
    assert row["link_event_json"] is None


def test_link_disabled_node_sends_exactly_as_before(db, lane, hub, presence, channel, alice, node_identity):
    """link_context=None (Link disabled on this node, or a caller that
    bypasses handle_session's real Link wiring) -- local chat stays fully
    usable and Link-unaware, same as before this issue."""
    link_channel(db, channel, node_identity=node_identity)  # Linked, but this session has no link_context at all

    session, action = asyncio.run(_run(lane, hub, presence, channel, alice, ["hello there", "/quit"]))

    scrollback = get_scrollback(db, channel)
    assert [m.body for m in scrollback if m.kind == "message"] == ["hello there"]
    row = db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE channel_id = ? AND kind = 'message' ORDER BY id DESC LIMIT 1", (channel.id,)
    ).fetchone()
    assert row["link_event_json"] is None


def test_repeated_send_in_a_linked_channel_is_idempotent_per_message(
    db, lane, hub, presence, channel, alice, node_identity
):
    """Each sent message gets its own queued event, keyed on that
    message's own row -- sending a second, different message doesn't
    disturb the first's already-queued event."""
    link_channel(db, channel, node_identity=node_identity)
    link_context = _link_context_for(node_identity)

    asyncio.run(
        _run(lane, hub, presence, channel, alice, ["first message", "second message", "/quit"], link_context=link_context)
    )

    rows = db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE channel_id = ? AND kind = 'message' ORDER BY id ASC", (channel.id,)
    ).fetchall()
    assert len(rows) == 2
    assert all(row["link_event_json"] is not None for row in rows)
    bodies = [ChannelMessage.from_dict(json.loads(row["link_event_json"])).payload["body"] for row in rows]
    assert bodies == ["first message", "second message"]
