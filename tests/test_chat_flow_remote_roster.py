"""
Tests for issue #195: `/who` and `/names` annotating a linked channel's
roster with remote participants known through a live Link subscription
(`LiveChannelBridge.remote_channel_presence`), not just local ones.

Populates the bridge's tracked roster directly rather than driving a
full two-node live-subscribe round trip -- `test_link_realtime_channels.
py`'s own `test_remote_channel_presence_is_populated_from_the_origins_
initial_snapshot` already proves the bridge fills that state in
correctly from the wire; this file is scoped to proving `/who`/`/names`
render whatever the bridge exposes, the same split already established
between that file and this one for the node-wide (issue #164) case.

Driven through the real `_chat_loop` dispatcher, same harness
`test_chat_flow_link.py` established for Link-context-aware chat tests.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.link.boards import LinkContext
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode, LinkProtocolError
from netbbs.link.realtime_channels import LiveChannelBridge
from netbbs.link.transport import LinkRealtimeSessionRegistry
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
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def node_identity():
    return bootstrap_node_identity("thisnode")


@pytest.fixture
def channel(db, alice):
    """Every channel gets a real `channel_id` at creation regardless of
    Link status (`Channel.channel_id` is non-nullable) -- `chat_flow`'s
    own merge logic keys on that alone and never re-checks `is_channel_
    linked` itself (that authorization already happened, on the wire,
    before the bridge's tracked roster was ever populated), so a plain
    local channel is enough to test the rendering/merge behavior."""
    return create_channel(db, "lobby", creator=alice)


def _bridge_with_roster(hub, lane, presence, *, channel_id: str, roster: dict[str, str], origin_fingerprint: str):
    registry = LinkRealtimeSessionRegistry(own_fingerprint="this-node-fingerprint")
    bridge = LiveChannelBridge(hub=hub, lane=lane, presence=presence, registry=registry)
    if roster:
        bridge._remote_channel_presence[channel_id] = dict(roster)
        bridge._remote_channel_presence_source[channel_id] = origin_fingerprint
    return bridge


async def _run(lane, hub, presence, channel, user, lines, *, link_context=None):
    session = FakeSession(lines)
    mailbox = MessageMailbox()
    history = InputHistory()
    await asyncio.wait_for(
        chat_flow._chat_loop(
            session, lane, hub, presence, mailbox, history, channel, user, link_context=link_context
        ),
        timeout=2,
    )
    return session


def _written_text(session: FakeSession) -> str:
    return "\n".join(session.written)


def test_who_annotates_a_remote_participant_with_its_linked_node(lane, hub, presence, alice, channel, node_identity):
    origin_fingerprint = "abcdef0123456789abcdef0123456789"
    bridge = _bridge_with_roster(
        hub, lane, presence, channel_id=channel.channel_id,
        roster={"remoteuser": "Remote User"}, origin_fingerprint=origin_fingerprint,
    )
    link_context = LinkContext(node_identity=node_identity, link_node=LinkNode(identity=node_identity), realtime_bridge=bridge)

    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/who", "/quit"], link_context=link_context))
    text = _written_text(session)
    assert "Remote User" in text
    # No persisted peer record for the origin: its technical identity is
    # the only one it has, and is shown so two such nodes stay distinct.
    assert f"on linked node {origin_fingerprint}" in text


def test_remote_node_hello_with_an_unsafe_friendly_name_is_refused():
    """A friendly name carrying terminal-control or bidi-override
    characters never reaches `/who` at all: the hello is refused as a
    non-canonical profile claim at admission, so there is nothing left
    for render-time sanitization to catch."""
    remote_identity = bootstrap_node_identity("remote")
    remote_node = LinkNode(identity=remote_identity)
    with pytest.raises(LinkProtocolError, match="invalid profile claims"):
        remote_node.handle_hello(remote_node.build_hello(
            addresses=None, outgoing_only=True, created_at="2026-09-03T12:00:00+00:00",
            friendly_name="Safe\x9b31m\u202eevil", canonical_dns_name="safe.example.org",
        ))


def test_names_includes_a_remote_participant_unannotated(lane, hub, presence, alice, channel, node_identity):
    """`/names` stays a compact, unannotated list (design doc) -- the
    remote label appears, but without the "(on linked node ...)" detail
    `/who` shows."""
    bridge = _bridge_with_roster(
        hub, lane, presence, channel_id=channel.channel_id,
        roster={"remoteuser": "Remote User"}, origin_fingerprint="abcdef0123456789abcdef0123456789",
    )
    link_context = LinkContext(node_identity=node_identity, link_node=LinkNode(identity=node_identity), realtime_bridge=bridge)

    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/names", "/quit"], link_context=link_context))
    text = _written_text(session)
    assert "Remote User" in text
    assert "linked node" not in text


def test_who_with_no_remote_roster_shows_only_local_participants(lane, hub, presence, alice, channel, node_identity):
    """A channel this node doesn't currently hold a live subscription
    for (empty roster) shows exactly what it always did -- no phantom
    "no one is here" regression, no stray annotation."""
    bridge = _bridge_with_roster(
        hub, lane, presence, channel_id=channel.channel_id, roster={}, origin_fingerprint="",
    )
    link_context = LinkContext(node_identity=node_identity, link_node=LinkNode(identity=node_identity), realtime_bridge=bridge)

    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/who", "/quit"], link_context=link_context))
    text = _written_text(session)
    assert "alice" in text
    assert "linked node" not in text


def test_who_with_no_link_context_is_unaffected(lane, hub, presence, alice, channel):
    """`link_context=None` (Link disabled, or a caller with no Link
    machinery running at all) -- `_remote_roster_entries` must degrade
    to an empty list, never raise."""
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/who", "/quit"], link_context=None))
    text = _written_text(session)
    assert "alice" in text
    assert "linked node" not in text
