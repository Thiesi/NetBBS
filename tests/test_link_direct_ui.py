"""
Tests for `netbbs.net.link_direct` (issue #168): the `/msg user@node`
send flow's caller-facing outcomes (Decision 3's reason-free refusal
included) and the receiving-side deliverer, against a real database and
the real hub/mailbox/presence objects -- only the network layer
(`LiveDirectChat`) is stubbed, since `tests/test_link_realtime_relay.py`
already proves that end to end over real sockets.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from netbbs.auth.users import create_user
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.link.boards import LinkContext
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.link.realtime_channels import LiveChannelBridge
from netbbs.link.realtime_direct import DirectChatUnreachable, IncomingDirectMessage
from netbbs.link.transport import LINK_REALTIME_PROTOCOL_TAG, LinkRealtimeSessionRegistry
from netbbs.messaging_preferences import set_accepts_direct_messages
from netbbs.net.link_direct import (
    UNREACHABLE_NOTE,
    build_direct_message_deliverer,
    parse_remote_address,
    resolve_node_fingerprint,
    send_live_direct_message,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso
from tests.test_admin_flow import FakeSession


@dataclass
class _StubDirectChat:
    unreachable: bool = False
    fail_send: bool = False
    sent: list = None

    def __post_init__(self):
        self.sent = []

    async def ensure_session(self, fingerprint: str):
        if self.unreachable:
            raise DirectChatUnreachable(fingerprint, "no_relay")
        return object()

    async def send_direct_message(self, fingerprint, **kw):
        if self.fail_send:
            raise DirectChatUnreachable(fingerprint, "dropped")
        self.sent.append((fingerprint, kw))


class _Rig:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path / "node.db")
        self.lane = DatabaseLane(self.db.path)
        self.user = create_user(self.db, "alice", password="pw", user_level=10)
        self.identity = bootstrap_node_identity("me")
        self.link_node = LinkNode(identity=self.identity)
        self.hub = ChatHub()
        self.presence = PresenceRegistry()
        self.registry = LinkRealtimeSessionRegistry(own_fingerprint=self.identity.fingerprint)
        self.bridge = LiveChannelBridge(hub=self.hub, lane=self.lane, presence=self.presence, registry=self.registry)
        self.peer_identity = bootstrap_node_identity("peer")
        peer_node = LinkNode(identity=self.peer_identity)
        self.link_node.handle_hello(peer_node.build_hello(
            addresses=[{"protocol": LINK_REALTIME_PROTOCOL_TAG, "address": "127.0.0.1", "port": 1}],
            outgoing_only=False, created_at=utc_now_iso(),
        ))
        self.direct = _StubDirectChat()

    def context(self, *, direct=True) -> LinkContext:
        return LinkContext(
            node_identity=self.identity, link_node=self.link_node, realtime_registry=self.registry,
            realtime_bridge=self.bridge, direct_chat=self.direct if direct else None,
        )

    def close(self):
        self.lane.close(); self.db.close()


def _send(rig: _Rig, address: str, body: str, *, context=None) -> tuple[bool, str]:
    session = FakeSession([])

    async def scenario():
        return await send_live_direct_message(
            session, rig.lane, rig.user, address, body, link_context=rig.context() if context is None else context,
        )

    ok = asyncio.run(scenario())
    return ok, "".join(session.written)


def test_parse_remote_address():
    assert parse_remote_address("bob@abc") == ("bob", "abc")
    assert parse_remote_address("bob") is None
    assert parse_remote_address("@abc") is None
    assert parse_remote_address("bob@") is None


def test_resolve_node_fingerprint_by_unique_prefix_case_insensitively(tmp_path):
    rig = _Rig(tmp_path)
    fp = rig.peer_identity.fingerprint
    assert resolve_node_fingerprint(rig.context(), fp[:10].upper()) == fp
    assert resolve_node_fingerprint(rig.context(), "zzzz-not-a-peer") == []
    rig.close()


def test_send_delivers_when_the_peer_is_reachable(tmp_path):
    rig = _Rig(tmp_path)
    fp = rig.peer_identity.fingerprint
    rig.bridge._remote_node_presence[fp] = {"Bob": "Bob"}  # case-insensitive match
    ok, text = _send(rig, f"bob@{fp[:12]}", "hi there")
    assert ok
    assert rig.direct.sent[0][0] == fp
    assert rig.direct.sent[0][1]["to_user_id"] == "bob"
    assert rig.direct.sent[0][1]["from_user_id"] == "alice"
    assert "(sent to bob@" in text
    rig.close()


def test_send_refuses_reason_free_when_unreachable(tmp_path):
    """Decision 3: one explicit refusal, no reason, Link mail pointer."""
    rig = _Rig(tmp_path)
    rig.direct.unreachable = True
    ok, text = _send(rig, f"bob@{rig.peer_identity.fingerprint[:12]}", "hi")
    assert not ok
    assert UNREACHABLE_NOTE in text
    assert "no_relay" not in text
    assert "Link mail" in text
    rig.close()


def test_send_never_claims_delivery_without_presence_from_that_node(tmp_path):
    """Code review (PR #269): with no presence snapshot from the peer, the
    guard must refuse rather than send blind and print '(sent ...)'."""
    rig = _Rig(tmp_path)
    ok, text = _send(rig, f"bob@{rig.peer_identity.fingerprint[:12]}", "hi")
    assert not ok
    assert "Couldn't confirm who is online" in text
    assert rig.direct.sent == []
    rig.close()


def test_send_refuses_an_overlong_body_or_user_with_a_plain_notice(tmp_path):
    rig = _Rig(tmp_path)
    fp = rig.peer_identity.fingerprint
    rig.bridge._remote_node_presence[fp] = {"bob": "bob"}
    ok, text = _send(rig, f"bob@{fp[:12]}", "x" * 4001)
    assert not ok and "Message too long" in text
    ok, text = _send(rig, f"{'u' * 129}@{fp[:12]}", "hi")
    assert not ok and "too long to be a NetBBS account" in text
    assert rig.direct.sent == []
    rig.close()


def test_send_refuses_when_presence_says_the_user_is_not_online_there(tmp_path):
    rig = _Rig(tmp_path)
    fp = rig.peer_identity.fingerprint
    rig.bridge._remote_node_presence[fp] = {"carol": "carol"}
    ok, text = _send(rig, f"bob@{fp[:12]}", "hi")
    assert not ok
    assert "not currently online on that node" in text
    assert rig.direct.sent == []
    rig.close()


def test_send_explains_unknown_and_ambiguous_nodes_and_off_link_nodes(tmp_path):
    rig = _Rig(tmp_path)
    ok, text = _send(rig, "bob@nope", "hi")
    assert not ok and "No linked node this board knows starts with" in text
    ok, text = _send(rig, "bob", "hi")
    assert not ok and "user@node-fingerprint" in text
    ok, text = _send(rig, "bob@abc", "hi", context=LinkContext(node_identity=rig.identity, link_node=rig.link_node))
    assert not ok and "isn't on NetBBS Link" in text
    rig.close()


def test_deliverer_queues_for_an_online_recipient_and_drops_otherwise(tmp_path):
    rig = _Rig(tmp_path)
    bob = create_user(rig.db, "bob", password="pw", user_level=10)
    mailbox = MessageMailbox()
    bob_session = object()

    class _Registry:
        def sessions_for_username(self, username):
            return [bob_session] if username == "bob" else []

    deliver = build_direct_message_deliverer(
        lane=rig.lane, hub=rig.hub, mailbox=mailbox, session_registry=_Registry(), presence=rig.presence,
    )
    message = IncomingDirectMessage(
        from_node_fingerprint="f" * 64, from_user_id="alice", from_display_label="Alice", to_user_id="bob",
        body="psst <esc>\x1b[31m", created_at=utc_now_iso(),
    )
    # Offline: dropped.
    assert asyncio.run(deliver(message)) is False
    assert mailbox.flush(bob_session) == []
    # Online: queued, sanitized, attributed to user@node.
    rig.presence.enter("bob")
    assert asyncio.run(deliver(message)) is True
    queued = mailbox.flush(bob_session)
    assert len(queued) == 1
    assert "Private message from Alice@ffffffffffff" in queued[0][0]
    assert "\x1b[31m" not in queued[0][0].split("Private message from")[1]
    # Opted out: dropped even while online.
    set_accepts_direct_messages(rig.db, bob, False)
    assert asyncio.run(deliver(message)) is False
    # Unknown user: dropped.
    assert asyncio.run(deliver(IncomingDirectMessage(
        from_node_fingerprint="f" * 64, from_user_id="alice", from_display_label="Alice", to_user_id="nobody",
        body="x", created_at=utc_now_iso(),
    ))) is False
    rig.close()
