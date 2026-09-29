"""One block list for mail and live messages (issue #925).

A block made for mail (issue #817) also refuses the blocked person's live
messages -- `/msg`, `/private`, `/dm`, Who's online, and inbound Link direct
messages -- and `/msg` respects the direct-message opt-out as `/dm` and
Who's online always did. A blocked local sender is told, as a blocked
letter's sender is; an inbound Link direct message is dropped, since its
frame has no answer on the wire. This BBS's SysOp cannot be blocked.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat import ChatHub, DirectChatInvites, MessageMailbox, PresenceRegistry
from netbbs.chat.channels import create_channel
from netbbs.link.realtime_direct import IncomingDirectMessage
from netbbs.mail import block_link_sender, block_local_sender, blocks_link_sender, blocks_local_sender
from netbbs.messaging_preferences import (
    live_message_refusal,
    set_accepts_direct_messages,
)
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.link_direct import build_direct_message_deliverer
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.rendering.ansi import strip_ansi
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso
from tests.test_chat_flow_moderation import FakeSession as ChatSession
from tests.test_mail_from_meeting_places import FakeSession as KeySession, _run_who, _screens, _visible
from tests.test_who_online import (
    FakeSession as WhoSession,
    _FakeBridge,
    _FakeDirectChat,
    _FakeLinkContext,
    _node_controls,
    _run_main_menu,
    _written_text,
)

REMOTE = "abcdefghijklmnopqrstuvwxyz234567"


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
def alice(db):
    return create_user(db, "alice", password="hunter2pw", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2pw", user_level=10)


# -- the rule ---------------------------------------------------------------------


def test_nothing_refuses_a_live_message_by_default(db, alice, bob):
    assert live_message_refusal(db, bob, sender=alice) is None
    assert live_message_refusal(db, bob, sender_address=f"dave@{REMOTE}") is None


def test_a_mail_block_refuses_live_messages_too_and_says_so(db, alice, bob):
    block_local_sender(db, bob, alice)
    assert live_message_refusal(db, bob, sender=alice) == "bob does not accept messages from you."
    # One direction only: bob still reaches alice.
    assert live_message_refusal(db, alice, sender=bob) is None


def test_the_opt_out_is_answered_before_a_block(db, alice, bob):
    block_local_sender(db, bob, alice)
    set_accepts_direct_messages(db, bob, False)
    assert live_message_refusal(db, bob, sender=alice) == "bob has opted out of direct messages."


def test_the_sysop_cannot_be_blocked_from_live_messages(db, alice, bob):
    block_local_sender(db, bob, alice)
    db.connection.execute("UPDATE users SET user_level = ? WHERE id = ?", (SYSOP_LEVEL, alice.id))
    db.connection.commit()
    from netbbs.auth.users import get_user_by_id
    assert live_message_refusal(db, bob, sender=get_user_by_id(db, alice.id)) is None


def test_a_link_sender_is_blocked_by_address_whatever_the_user_part_case(db, bob):
    block_link_sender(db, bob, f"dave@{REMOTE}")
    assert live_message_refusal(db, bob, sender_address=f"Dave@{REMOTE}") == "bob does not accept messages from you."
    assert live_message_refusal(db, bob, sender_address=f"erin@{REMOTE}") is None


# -- /msg, /private, /dm ------------------------------------------------------------


async def _chat(lane, presence, channel, user, lines, *, registry=None, hub=None, mailbox=None):
    session = ChatSession(lines)
    await asyncio.wait_for(
        chat_flow._chat_loop(
            session, lane, hub or ChatHub(), presence, mailbox or MessageMailbox(), InputHistory(), channel, user,
            session_registry=registry,
        ),
        timeout=5,
    )
    return "\n".join(session.written)


def _online_bob(presence):
    presence.enter("bob")
    registry = ActiveSessionRegistry()
    bob_session = ChatSession([])
    return registry, bob_session


def test_msg_respects_the_direct_message_opt_out(db, lane, alice, bob):
    """#817 found `/msg` was the one live path that ignored the opt-out."""
    set_accepts_direct_messages(db, bob, False)
    channel = create_channel(db, "lobby", creator=alice)
    presence = PresenceRegistry()
    registry, bob_session = _online_bob(presence)
    mailbox = MessageMailbox()

    async def scenario():
        registry.enter(bob_session)
        registry.mark_authenticated(bob_session, "bob")
        return await _chat(lane, presence, channel, alice, ["/msg bob hello", "/quit"], registry=registry, mailbox=mailbox)

    text = asyncio.run(scenario())
    assert "bob has opted out of direct messages." in text
    assert "(sent to bob)" not in text
    assert mailbox.flush(bob_session) == []


def test_msg_to_someone_who_blocks_you_is_refused_and_told(db, lane, alice, bob):
    block_local_sender(db, bob, alice)
    channel = create_channel(db, "lobby", creator=alice)
    presence = PresenceRegistry()
    registry, bob_session = _online_bob(presence)
    mailbox = MessageMailbox()

    async def scenario():
        registry.enter(bob_session)
        registry.mark_authenticated(bob_session, "bob")
        return await _chat(
            lane, presence, channel, alice, ["/msg bob hello", "/private bob", "/quit"],
            registry=registry, mailbox=mailbox,
        )

    text = asyncio.run(scenario())
    assert text.count("bob does not accept messages from you.") == 2
    assert "Entering private conversation" not in text
    assert mailbox.flush(bob_session) == []


def test_a_block_made_during_a_private_conversation_ends_it(db, lane, alice, bob):
    channel = create_channel(db, "lobby", creator=alice)
    presence = PresenceRegistry()
    registry, bob_session = _online_bob(presence)
    mailbox = MessageMailbox()

    async def scenario():
        registry.enter(bob_session)
        registry.mark_authenticated(bob_session, "bob")
        session = ChatSession(["/private bob", "first", "second", "/quit"])
        # bob blocks alice after her first private line.
        original = chat_flow._deliver_private_message

        async def deliver_then_block(ctx, target, body):
            await original(ctx, target, body)
            await lane.run(block_local_sender, bob, alice)

        chat_flow._deliver_private_message = deliver_then_block
        try:
            await asyncio.wait_for(
                chat_flow._chat_loop(
                    session, lane, ChatHub(), presence, mailbox, InputHistory(), channel, alice,
                    session_registry=registry,
                ),
                timeout=5,
            )
        finally:
            chat_flow._deliver_private_message = original
        return "\n".join(session.written)

    text = asyncio.run(scenario())
    assert "bob does not accept messages from you." in text
    assert "Returned to #lobby." in text
    delivered = mailbox.flush(bob_session)
    assert len(delivered) == 1 and "first" in delivered[0][0]


def test_a_chat_invite_to_someone_who_blocks_you_is_refused(db, lane, alice, bob):
    block_local_sender(db, bob, alice)
    presence = PresenceRegistry()
    presence.enter("bob")
    session = ChatSession([])

    async def scenario():
        return await chat_flow.run_direct_chat_invite_flow(
            session, lane, ChatHub(), presence, DirectChatInvites(), ActiveSessionRegistry(), alice, bob,
        )

    assert asyncio.run(scenario()) is False
    assert "bob does not accept messages from you." in "\n".join(session.written)


# -- Who's online -----------------------------------------------------------------


def test_who_is_online_refuses_messages_to_someone_who_blocks_you(db, lane, alice, bob):
    block_local_sender(db, bob, alice)
    session = KeySession(keys=["0", "1", "b", "b"])

    _run_who(db, lane, alice, session)

    text = _visible(session)
    assert "bob does not accept messages from you." in text
    assert "[M]essage" not in text and "[I]nvite" not in text


def test_who_is_online_does_not_promise_mail_to_someone_who_opted_out_and_blocks_you(db, lane, alice, bob):
    set_accepts_direct_messages(db, bob, False)
    block_local_sender(db, bob, alice)
    session = KeySession(keys=["0", "1", "b", "b"])

    _run_who(db, lane, alice, session)

    text = " ".join(_visible(session).split())
    assert "e-mail still reaches them" not in text
    assert "bob does not accept messages from you." in text


def test_who_is_online_blocks_and_unblocks_a_local_caller(db, lane, alice, bob):
    session = KeySession(keys=["0", "1", "k", "0", "1", "k", "b"])

    _run_who(db, lane, alice, session)

    screens = _screens(session)
    assert "Bloc[k]" in _visible(session)
    assert "Unbloc[k]" in _visible(session)
    assert any("Blocked bob: their mail and live messages are refused" in " ".join(s.split()) for s in screens)
    assert "Unblocked bob" in screens[-1]
    assert not blocks_local_sender(db, alice, bob)


def test_who_is_online_offers_no_block_for_the_sysop(db, lane, alice):
    create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    session = KeySession(keys=["0", "1", "b", "b"])

    _run_who(db, lane, alice, session, online=("sysop",))

    assert "Bloc[k]" not in _visible(session)


def test_who_is_online_blocks_a_linked_caller_by_address(tmp_path):
    database = Database(tmp_path / "node.db")
    alice = create_user(database, "alice", password="hunter2", user_level=10)

    async def scenario():
        node_controls = _node_controls()
        link_context = _FakeLinkContext(
            _FakeBridge({REMOTE: {"erin": "erin"}}), direct_chat=_FakeDirectChat(), known_fingerprints=(REMOTE,),
        )
        lane = DatabaseLane(database.path)
        session = WhoSession(["w", "0", "1", "k", "b", "l", "y"])
        node_controls.session_registry.enter(session)
        node_controls.session_registry.mark_authenticated(session, "alice")
        try:
            await _run_main_menu(session, database, alice, node_controls, lane=lane, link_context=link_context)
        finally:
            node_controls.session_registry.leave(session)
            lane.close()
        return _written_text(session)

    text = asyncio.run(scenario())
    assert "Bloc[k]" in strip_ansi(text)
    assert "Blocked erin@" in " ".join(strip_ansi(text).split())
    assert blocks_link_sender(database, alice, f"erin@{REMOTE}")
    database.close()


# -- inbound Link direct messages -------------------------------------------------


def test_an_inbound_link_message_from_a_blocked_sender_is_dropped(db, lane, bob):
    block_link_sender(db, bob, f"dave@{REMOTE}")
    presence = PresenceRegistry()
    presence.enter("bob")
    mailbox = MessageMailbox()
    bob_session = object()

    class _Registry:
        def sessions_for_username(self, username):
            return [bob_session] if username == "bob" else []

    deliver = build_direct_message_deliverer(
        lane=lane, hub=ChatHub(), mailbox=mailbox, session_registry=_Registry(), presence=presence,
    )

    def message(sender: str) -> IncomingDirectMessage:
        return IncomingDirectMessage(
            from_node_fingerprint=REMOTE, from_user_id=sender, from_display_label=sender, to_user_id="bob",
            body="hi", created_at=utc_now_iso(),
        )

    assert asyncio.run(deliver(message("dave"))) is False
    assert mailbox.flush(bob_session) == []
    # Someone else on the same node still gets through.
    assert asyncio.run(deliver(message("erin"))) is True
    assert len(mailbox.flush(bob_session)) == 1
