"""
Integration tests for the `/timestamps` command and per-user chat
timestamp preference wiring (design doc round 32 point 3, round 42
point 6, sign-off round 62). Library-level `netbbs.chat.timestamps`
behavior (`timestamps_enabled`/`set_timestamps_enabled`/
`format_with_preference`) is exercised indirectly here, through the
real command and chat loop -- mirroring tests/test_chat_flow_away.py's
structure for the sibling `/away` preference command.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.timestamps import set_timestamps_enabled, timestamps_enabled
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.storage.database import Database
from tests.test_chat_flow_moderation import FakeSession

_TIMESTAMP_PATTERN = re.compile(r"\[\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}\]")


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
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "general", creator=alice)


def _written_text(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(db, hub, presence, channel, user, lines):
    session = FakeSession(lines)
    mailbox = MessageMailbox()
    history = InputHistory()
    await asyncio.wait_for(
        chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, user), timeout=2
    )
    return session


# -- /timestamps command itself ---------------------------------------------


def test_timestamps_defaults_to_off(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/timestamps", "/quit"]))
    assert "Chat timestamps are off." in _written_text(session)


def test_timestamps_on_enables_the_preference(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/timestamps on", "/quit"]))
    assert "Chat timestamps are now on." in _written_text(session)
    assert timestamps_enabled(db, alice) is True


def test_timestamps_off_disables_the_preference(db, hub, presence, alice, channel):
    set_timestamps_enabled(db, alice, True)
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/timestamps off", "/quit"]))
    assert "Chat timestamps are now off." in _written_text(session)
    assert timestamps_enabled(db, alice) is False


def test_timestamps_toggle_flips_the_state(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/timestamps toggle", "/quit"]))
    assert "Chat timestamps are now on." in _written_text(session)
    assert timestamps_enabled(db, alice) is True

    session2 = asyncio.run(_run(db, hub, presence, channel, alice, ["/timestamps toggle", "/quit"]))
    assert "Chat timestamps are now off." in _written_text(session2)
    assert timestamps_enabled(db, alice) is False


def test_timestamps_invalid_argument_shows_usage(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/timestamps bogus", "/quit"]))
    assert "Usage: /timestamps" in _written_text(session)
    assert timestamps_enabled(db, alice) is False


# -- rendering: own live message ---------------------------------------------


def test_own_message_is_prefixed_when_enabled(db, hub, presence, alice, channel):
    set_timestamps_enabled(db, alice, True)
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["hello there", "/quit"]))
    text = _written_text(session)
    assert _TIMESTAMP_PATTERN.search(text) is not None
    assert "hello there" in text


def test_own_message_is_not_prefixed_by_default(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["hello there", "/quit"]))
    text = _written_text(session)
    assert _TIMESTAMP_PATTERN.search(text) is None
    assert "hello there" in text


# -- rendering: per-recipient, not per-sender --------------------------------


def test_timestamp_preference_is_per_recipient_for_broadcast_messages(db, hub, presence, alice, bob, channel):
    """The real payoff of the per-recipient envelope design: the same
    live message is shown timestamped to a recipient who opted in and
    unstamped to the sender who didn't -- proving each session applies
    its own preference, not a single shared rendering decision baked in
    at broadcast time."""
    set_timestamps_enabled(db, bob, True)  # recipient opts in
    # alice (sender) leaves the default (off)

    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        watcher = FakeSession()
        watcher_task = asyncio.create_task(
            chat_flow._chat_loop(watcher, db, hub, presence, mailbox, history, channel, bob)
        )
        await asyncio.sleep(0)

        sender = FakeSession(["hello there", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(sender, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return sender, watcher

    sender_session, watcher_session = asyncio.run(scenario())
    assert _TIMESTAMP_PATTERN.search(_written_text(sender_session)) is None
    assert _TIMESTAMP_PATTERN.search(_written_text(watcher_session)) is not None


def test_timestamp_preference_applies_to_join_and_leave_notices_for_an_opted_in_recipient(
    db, hub, presence, alice, bob, channel
):
    set_timestamps_enabled(db, bob, True)

    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        watcher = FakeSession()
        watcher_task = asyncio.create_task(
            chat_flow._chat_loop(watcher, db, hub, presence, mailbox, history, channel, bob)
        )
        await asyncio.sleep(0)

        actor = FakeSession(["/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(actor, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )
        await asyncio.sleep(0)

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return watcher

    watcher_session = asyncio.run(scenario())
    text = _written_text(watcher_session)
    assert "has joined the channel" in text
    assert "has left the channel" in text
    assert _TIMESTAMP_PATTERN.search(text) is not None


def test_timestamp_preference_applies_to_me_action_for_an_opted_in_recipient(
    db, hub, presence, alice, bob, channel
):
    set_timestamps_enabled(db, bob, True)

    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        watcher = FakeSession()
        watcher_task = asyncio.create_task(
            chat_flow._chat_loop(watcher, db, hub, presence, mailbox, history, channel, bob)
        )
        await asyncio.sleep(0)

        actor = FakeSession(["/me waves", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(actor, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return actor, watcher

    actor_session, watcher_session = asyncio.run(scenario())
    assert _TIMESTAMP_PATTERN.search(_written_text(actor_session)) is None
    watcher_text = _written_text(watcher_session)
    assert "alice waves" in watcher_text
    assert _TIMESTAMP_PATTERN.search(watcher_text) is not None


# -- rendering: scrollback replay ---------------------------------------------


def test_scrollback_replay_is_prefixed_for_a_recipient_with_the_preference_on(db, hub, presence, alice, channel):
    asyncio.run(_run(db, hub, presence, channel, alice, ["hello there", "/quit"]))

    set_timestamps_enabled(db, alice, True)
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/quit"]))
    text = _written_text(session)
    assert "hello there" in text
    assert _TIMESTAMP_PATTERN.search(text) is not None


def test_scrollback_replay_is_not_prefixed_by_default(db, hub, presence, alice, channel):
    asyncio.run(_run(db, hub, presence, channel, alice, ["hello there", "/quit"]))

    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/quit"]))
    text = _written_text(session)
    assert "hello there" in text
    assert _TIMESTAMP_PATTERN.search(text) is None


# -- rendering: private messages ---------------------------------------------


def test_online_private_message_is_prefixed_for_an_opted_in_recipient(db, hub, presence, alice, bob, channel):
    set_timestamps_enabled(db, bob, True)
    presence.enter("bob")

    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        watcher = FakeSession()
        watcher_task = asyncio.create_task(
            chat_flow._chat_loop(watcher, db, hub, presence, mailbox, history, channel, bob)
        )
        await asyncio.sleep(0)

        actor = FakeSession(["/msg bob hi there", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(actor, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return watcher

    watcher_session = asyncio.run(scenario())
    text = _written_text(watcher_session)
    assert "Private message from alice: hi there" in text
    assert _TIMESTAMP_PATTERN.search(text) is not None


def test_mailbox_queued_private_message_carries_a_timestamp(db, hub, presence, alice, bob, channel):
    """Offline delivery doesn't render anything itself (that happens at
    netbbs.net.login_flow._draw_main_menu's next flush, covered in
    tests/test_login_mailbox_flush.py) -- this just confirms the queued
    tuple actually carries a real timestamp through `/msg`, not a
    placeholder."""
    presence.enter("bob")  # online (e.g. browsing boards), not in a channel
    mailbox = MessageMailbox()
    history = InputHistory()
    session = FakeSession(["/msg bob hi there", "/quit"])
    asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )
    )
    pending = mailbox.flush("bob")
    assert len(pending) == 1
    text, created_at = pending[0]
    assert "Private message from alice: hi there" in text
    assert created_at  # a real ISO timestamp, not blank/None
