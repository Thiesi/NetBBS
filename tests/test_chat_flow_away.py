"""
Integration tests for the `/away` command wiring in
netbbs.net.chat_flow (design doc round 32, sign-off round 42) — the
command itself, plus that sending a message while away reminds rather
than silently clearing. Library-level PresenceRegistry behavior is
covered separately in tests/test_chat_presence.py; the
session-lifecycle enter()/leave() hook in handle_session is covered in
tests/test_login_presence.py.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
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
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "general", creator=alice)


def _written_text(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(lane, hub, presence, channel, user, lines):
    session = FakeSession(lines)
    mailbox = MessageMailbox()
    history = InputHistory()
    await asyncio.wait_for(
        chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, user), timeout=2
    )
    return session


def test_away_with_message_sets_status(db, lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/away gone to lunch", "/quit"]))
    assert "You are now marked away: gone to lunch" in _written_text(session)
    assert presence.is_away("alice") is True
    assert presence.get_away_message("alice") == "gone to lunch"


def test_away_with_no_args_clears_existing_status(db, lane, hub, presence, alice, channel):
    presence.set_away("alice", "brb")
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/away", "/quit"]))
    assert "You are no longer marked away." in _written_text(session)
    assert presence.is_away("alice") is False


def test_away_with_no_args_and_not_away_shows_message(db, lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/away", "/quit"]))
    assert "You are not currently marked away." in _written_text(session)


def test_away_not_written_to_scrollback(db, lane, hub, presence, alice, channel):
    from netbbs.chat.scrollback import get_scrollback

    asyncio.run(_run(lane, hub, presence, channel, alice, ["/away gone to lunch", "/quit"]))
    # join/leave events are always recorded regardless -- /away itself
    # must not add anything beyond those (design doc round 32: not
    # written to channel scrollback or broadcast as a channel event).
    scrollback = get_scrollback(db, channel)
    assert {m.kind for m in scrollback} == {"join", "leave"}


def test_away_not_broadcast_to_others(db, lane, hub, presence, alice, channel):
    bob = create_user(db, "bob", password="hunter2", user_level=10)

    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        watcher = FakeSession()
        watcher_task = asyncio.create_task(
            chat_flow._chat_loop(watcher, lane, hub, presence, mailbox, history, channel, bob)
        )
        while hub.participant_count(channel.name) < 1:
            await asyncio.sleep(0)

        actor = FakeSession(["/away gone to lunch", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(actor, lane, hub, presence, mailbox, history, channel, alice), timeout=2
        )

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return watcher

    watcher = asyncio.run(scenario())
    # Anchored to the away *reason* text specifically (design doc round
    # 77), not a blanket "away" never appears anywhere -- the chat
    # status line's own "N online(M away)" label (round 77) legitimately
    # contains that word as static chrome regardless of whether anyone
    # is actually away, so a bare substring check would now always fail
    # for a reason unrelated to what this test actually protects: that
    # alice's /away reason never reaches bob's chat stream.
    assert "gone to lunch" not in _written_text(watcher)


# -- sending while away (design doc round 32, point 6) ----------------------


def test_sending_a_message_while_away_reminds_but_does_not_clear(db, lane, hub, presence, alice, channel):
    session = asyncio.run(
        _run(lane, hub, presence, channel, alice, ["/away gone to lunch", "hello", "/quit"])
    )
    assert "(You are still marked away.)" in _written_text(session)
    assert presence.is_away("alice") is True


def test_sending_a_message_while_away_still_sends_it(db, lane, hub, presence, alice, channel):
    from netbbs.chat.scrollback import get_scrollback

    asyncio.run(_run(lane, hub, presence, channel, alice, ["/away gone to lunch", "hello", "/quit"]))
    scrollback = get_scrollback(db, channel)
    assert any(m.kind == "message" and m.body == "hello" for m in scrollback)


def test_no_reminder_when_not_away(db, lane, hub, presence, alice, channel):
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["hello", "/quit"]))
    assert "still marked away" not in _written_text(session)
