"""
Tests for Phase 2 Track 5e (design doc round 32/33, sign-off round 46):
`/msg`, `/private`, `/close`, driven through the real `_chat_loop`
dispatcher via the shared `FakeSession` (`test_chat_flow_moderation.py`).
`/query` was removed in round 54 -- see `test_query_is_no_longer_a_command`.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import get_scrollback
from netbbs.moderation.log import list_actions_for_object
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.session_registry import ActiveSessionRegistry
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
def mailbox():
    return MessageMailbox()


@pytest.fixture
def history():
    return InputHistory()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "lobby", creator=alice)


@pytest.fixture
def other_channel(db, alice):
    return create_channel(db, "offtopic", creator=alice)


def _written(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(db, hub, presence, mailbox, channel, user, lines, *, session_registry=None):
    session = FakeSession(lines)
    history = InputHistory()
    await asyncio.wait_for(
        chat_flow._chat_loop(
            session, db, hub, presence, mailbox, history, channel, user, session_registry=session_registry
        ),
        timeout=2,
    )
    return session


# -- /msg: online check ------------------------------------------------------


def test_msg_to_offline_user_is_refused(db, hub, presence, mailbox, alice, bob, channel):
    session = asyncio.run(
        _run(db, hub, presence, mailbox, channel, alice, ["/msg bob hello", "/quit"])
    )
    assert "not currently online" in _written(session)
    assert mailbox.flush("bob") == []


def test_msg_to_unknown_user_shows_friendly_message(db, hub, presence, mailbox, alice, channel):
    session = asyncio.run(
        _run(db, hub, presence, mailbox, channel, alice, ["/msg nosuchuser hello", "/quit"])
    )
    assert "No such user" in _written(session)


def test_msg_with_no_text_shows_usage(db, hub, presence, mailbox, alice, channel):
    session = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/msg bob", "/quit"]))
    assert "Usage: /msg" in _written(session)


# -- /msg: delivery -----------------------------------------------------------


def test_msg_delivers_live_to_a_recipient_in_a_different_channel(
    db, hub, presence, mailbox, alice, bob, channel, other_channel
):
    presence.enter("bob")

    async def scenario():
        history = InputHistory()
        target_session = FakeSession()  # sits in other_channel, never types
        target_task = asyncio.create_task(
            chat_flow._chat_loop(target_session, db, hub, presence, mailbox, history, other_channel, bob)
        )
        await asyncio.sleep(0)  # let bob actually join before the /msg is sent

        sender_session = FakeSession(["/msg bob hello there", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(sender_session, db, hub, presence, mailbox, history, channel, alice),
            timeout=2,
        )

        target_task.cancel()
        await asyncio.gather(target_task, return_exceptions=True)
        return sender_session, target_session

    sender_session, target_session = asyncio.run(scenario())
    assert "(sent to bob)" in _written(sender_session)
    assert "Private message from alice: hello there" in _written(target_session)
    # Delivered live -- never queued in the mailbox.
    assert mailbox.flush("bob") == []


def test_msg_queues_in_mailbox_when_recipient_is_online_but_not_in_any_channel(
    db, hub, presence, mailbox, alice, bob, channel
):
    # Online (e.g. browsing boards) but not currently in a chat channel --
    # exactly the gap the mailbox exists for (design doc round 32/46).
    # A registered (but not chat-live) session is what makes mailbox
    # delivery possible at all under the session-addressed redesign
    # (GitHub issue #27) -- without a registry entry there's no Session
    # object to key the delivery by.
    presence.enter("bob")
    registry = ActiveSessionRegistry()
    bob_session = FakeSession()

    async def scenario():
        registry.enter(bob_session)  # requires a running event loop
        registry.mark_authenticated(bob_session, "bob")
        return await _run(
            db, hub, presence, mailbox, channel, alice, ["/msg bob hello there", "/quit"],
            session_registry=registry,
        )

    session = asyncio.run(scenario())

    assert "(sent to bob)" in _written(session)
    pending = mailbox.flush(bob_session)
    assert len(pending) == 1
    assert "Private message from alice: hello there" in pending[0][0]


# -- GitHub issue #27: session-addressed delivery ---------------------


def test_msg_reaches_every_one_of_the_recipients_sessions(
    db, hub, presence, mailbox, alice, bob, channel, other_channel
):
    """Regression test for the core bug: a recipient with two
    simultaneous non-chat-live sessions used to share one account-wide
    mailbox slot -- both must now get their own independent copy."""
    presence.enter("bob")
    registry = ActiveSessionRegistry()
    bob_session_one, bob_session_two = FakeSession(), FakeSession()

    async def scenario():
        registry.enter(bob_session_one)  # requires a running event loop
        registry.mark_authenticated(bob_session_one, "bob")
        registry.enter(bob_session_two)
        registry.mark_authenticated(bob_session_two, "bob")
        return await _run(
            db, hub, presence, mailbox, channel, alice, ["/msg bob hello there", "/quit"],
            session_registry=registry,
        )

    asyncio.run(scenario())

    pending_one = mailbox.flush(bob_session_one)
    pending_two = mailbox.flush(bob_session_two)
    assert len(pending_one) == 1
    assert len(pending_two) == 1
    assert "Private message from alice: hello there" in pending_one[0][0]
    assert "Private message from alice: hello there" in pending_two[0][0]


def test_msg_reaches_both_a_live_chat_session_and_a_non_chat_session(
    db, hub, presence, mailbox, alice, bob, channel, other_channel
):
    """One chat session plus one main-menu (non-chat) session both
    receive the same /msg -- the live one instantly via ChatHub, the
    other queued in its own mailbox slot."""
    presence.enter("bob")
    registry = ActiveSessionRegistry()
    non_chat_session = FakeSession()

    async def scenario():
        registry.enter(non_chat_session)  # requires a running event loop
        registry.mark_authenticated(non_chat_session, "bob")
        history = InputHistory()
        live_session = FakeSession()  # sits in other_channel, never types
        registry.enter(live_session)
        registry.mark_authenticated(live_session, "bob")
        live_task = asyncio.create_task(
            chat_flow._chat_loop(live_session, db, hub, presence, mailbox, history, other_channel, bob)
        )
        await asyncio.sleep(0)  # let bob's live session actually join first

        sender_session = FakeSession(["/msg bob hello there", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(
                sender_session, db, hub, presence, mailbox, history, channel, alice,
                session_registry=registry,
            ),
            timeout=2,
        )
        live_task.cancel()
        await asyncio.gather(live_task, return_exceptions=True)
        return live_session

    live_session = asyncio.run(scenario())
    assert "Private message from alice: hello there" in _written(live_session)
    pending = mailbox.flush(non_chat_session)
    assert len(pending) == 1
    assert "Private message from alice: hello there" in pending[0][0]


def test_msg_is_never_written_to_scrollback_or_moderation_log(
    db, hub, presence, mailbox, alice, bob, channel
):
    # The `channel` fixture itself now logs a `create_channel` audit
    # entry (design doc -- channel management round), so the baseline
    # here is that one entry, not an empty log -- this test's actual
    # claim is that /msg adds nothing further, not that the log is
    # globally empty.
    before = list_actions_for_object(db, "channel", channel.id)

    presence.enter("bob")
    asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/msg bob hello", "/quit"]))

    scrollback = get_scrollback(db, channel)
    assert all(m.kind in ("join", "leave") for m in scrollback)
    assert list_actions_for_object(db, "channel", channel.id) == before


# -- /private, /close --------------------------------------------------


def test_private_to_offline_user_is_refused(db, hub, presence, mailbox, alice, bob, channel):
    session = asyncio.run(
        _run(db, hub, presence, mailbox, channel, alice, ["/private bob", "/quit"])
    )
    assert "not currently online" in _written(session)


def test_private_enters_conversation_mode_and_routes_plain_lines(
    db, hub, presence, mailbox, alice, bob, channel
):
    presence.enter("bob")
    registry = ActiveSessionRegistry()
    bob_session = FakeSession()

    async def scenario():
        registry.enter(bob_session)  # requires a running event loop
        registry.mark_authenticated(bob_session, "bob")
        return await _run(
            db,
            hub,
            presence,
            mailbox,
            channel,
            alice,
            ["/private bob", "hello there", "/quit"],
            session_registry=registry,
        )

    session = asyncio.run(scenario())

    assert "Entering private conversation with bob" in _written(session)
    pending = mailbox.flush(bob_session)
    assert len(pending) == 1
    assert "Private message from alice: hello there" in pending[0][0]
    # The plain line went to bob privately, not posted to the channel.
    scrollback = get_scrollback(db, channel)
    assert all(m.kind in ("join", "leave") for m in scrollback)


def test_commands_still_dispatch_normally_while_in_private_mode(
    db, hub, presence, mailbox, alice, bob, channel
):
    presence.enter("bob")

    session = asyncio.run(
        _run(
            db,
            hub,
            presence,
            mailbox,
            channel,
            alice,
            ["/private bob", "/whois bob", "/quit"],
        )
    )

    output = _written(session)
    assert "Entering private conversation with bob" in output
    # /whois still ran as a command, not sent to bob as a private line.
    assert "Status: online" in output
    assert mailbox.flush("bob") == []


def test_close_returns_to_channel_input(db, hub, presence, mailbox, alice, bob, channel):
    presence.enter("bob")

    session = asyncio.run(
        _run(
            db,
            hub,
            presence,
            mailbox,
            channel,
            alice,
            ["/private bob", "/close", "hello everyone", "/quit"],
        )
    )

    assert "Returned to #lobby." in _written(session)
    # After /close, the plain line posted to the channel again, not to bob.
    scrollback = get_scrollback(db, channel)
    assert any(m.kind == "message" and m.body == "hello everyone" for m in scrollback)
    assert mailbox.flush("bob") == []


def test_close_without_being_in_private_mode_shows_message(
    db, hub, presence, mailbox, alice, channel
):
    session = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/close", "/quit"]))
    assert "You are not in a private conversation." in _written(session)


def test_query_is_no_longer_a_command(db, hub, presence, mailbox, alice, bob, channel):
    # design doc round 54: removed as a bare, value-free alias for
    # /private -- the only command that ever had two names.
    presence.enter("bob")

    session = asyncio.run(
        _run(db, hub, presence, mailbox, channel, alice, ["/query bob", "/quit"])
    )

    assert "Unknown command: /query" in _written(session)


class _SessionThatDropsBobAfterEnteringPrivateMode(FakeSession):
    """Simulates bob logging off *between* `/private bob` succeeding and
    the next line being sent -- `presence.leave("bob")` fires right
    before the *following* scripted line is handed back (i.e. after
    `/private bob` has already been dispatched and succeeded), so
    `send_loop` still has `private_target` set to bob but he's no
    longer online by the time the next plain line is processed."""

    def __init__(self, lines: list[str], presence: PresenceRegistry) -> None:
        super().__init__(lines)
        self._presence = presence
        self._dropped = False

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        if self._lines and self._lines[0] == "hello" and not self._dropped:
            self._dropped = True
            self._presence.leave("bob")
        return await super().read_line(echo=echo, history=history, completer=completer)


def test_private_target_going_offline_mid_conversation_is_handled(
    db, hub, presence, mailbox, alice, bob, channel
):
    presence.enter("bob")
    session = _SessionThatDropsBobAfterEnteringPrivateMode(
        ["/private bob", "hello", "/quit"], presence
    )

    asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, db, hub, presence, mailbox, InputHistory(), channel, alice), timeout=2
        )
    )

    output = _written(session)
    assert "Entering private conversation with bob" in output
    assert "bob is no longer online." in output
    # The "hello" line was never delivered anywhere -- neither live nor mailbox.
    assert mailbox.flush("bob") == []
