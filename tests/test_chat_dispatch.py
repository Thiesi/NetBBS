"""
Tests for the chat slash-command dispatch mechanism in
netbbs.net.chat_flow (design doc §13, sign-off round 39) — the
`_dispatch_command`/`_COMMANDS` registry itself, distinct from the
individual command behaviors already covered in
tests/test_chat_flow_moderation.py.

Reuses that file's FakeSession (a real Session subclass, needed since
`_chat_loop` runs two genuinely concurrent tasks).
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import get_scrollback
from netbbs.net import chat_flow
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
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "general", creator=alice)


def _written_text(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(db, hub, presence, channel, user, lines):
    session = FakeSession(lines)
    await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, channel, user), timeout=2)
    return session


# -- unknown commands are never broadcast as chat text ---------------------


def test_unknown_command_shows_a_message(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/bogus", "/quit"]))
    assert "Unknown command: /bogus" in _written_text(session)


def test_unknown_command_is_not_recorded_as_a_chat_message(db, hub, presence, alice, channel):
    asyncio.run(_run(db, hub, presence, channel, alice, ["/bogus", "/quit"]))
    scrollback = get_scrollback(db, channel)
    assert not any(m.kind == "message" for m in scrollback)


def test_unknown_command_preserves_leading_slash_text_verbatim(db, hub, presence, alice, channel):
    # A typo'd command must never silently become public chat text --
    # this was a real gap in the old ad hoc `if` chain (design doc
    # sign-off round 39).
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/mtue bob", "/quit"]))
    assert "Unknown command: /mtue" in _written_text(session)
    scrollback = get_scrollback(db, channel)
    assert not any(m.body == "/mtue bob" for m in scrollback)


# -- ordinary text without a leading slash is unaffected -------------------


def test_plain_message_is_still_sent_normally(db, hub, presence, alice, channel):
    asyncio.run(_run(db, hub, presence, channel, alice, ["hello everyone", "/quit"]))
    scrollback = get_scrollback(db, channel)
    assert any(m.kind == "message" and m.body == "hello everyone" for m in scrollback)


# -- /quit and /leave -------------------------------------------------------


def test_quit_exits_the_loop(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/quit"]))
    assert hub.participant_count(channel.name) == 0


def test_leave_also_exits_the_loop(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/leave"]))
    assert hub.participant_count(channel.name) == 0


# -- /help --------------------------------------------------------------


def test_help_lists_known_commands(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/help", "/quit"]))
    output = _written_text(session)
    assert "/mute" in output
    assert "/kick" in output
    assert "/finger" in output
    assert "/quit" in output


def test_help_does_not_error_for_a_regular_user(db, hub, presence, alice, channel):
    # /help itself needs no special permission -- confirms the
    # registry entry works for a non-moderator, unlike /mute etc.
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/help", "/quit"]))
    assert "Unknown command" not in _written_text(session)
