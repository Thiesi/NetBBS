"""Tests for /me (design doc round 32/40) — a typed action event,
distinct from tests/test_chat_dispatch.py's dispatcher-level coverage."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
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
def mailbox():
    return MessageMailbox()


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


def test_me_shows_action_to_the_actor(db, hub, presence, mailbox, alice, channel):
    async def scenario():
        session = FakeSession(["/me waves", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, mailbox, channel, alice), timeout=2)
        return session

    session = asyncio.run(scenario())
    assert "* alice waves" in _written_text(session)


def test_me_is_broadcast_to_others(db, hub, presence, mailbox, alice, bob, channel):
    async def scenario():
        bystander = FakeSession()
        bystander_task = asyncio.create_task(chat_flow._chat_loop(bystander, db, hub, presence, mailbox, channel, bob))
        await asyncio.sleep(0)

        actor = FakeSession(["/me waves", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(actor, db, hub, presence, mailbox, channel, alice), timeout=2)

        bystander_task.cancel()
        await asyncio.gather(bystander_task, return_exceptions=True)
        return bystander

    bystander = asyncio.run(scenario())
    assert "* alice waves" in _written_text(bystander)


def test_me_is_recorded_as_an_action_in_scrollback(db, hub, presence, mailbox, alice, channel):
    async def scenario():
        session = FakeSession(["/me waves", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, mailbox, channel, alice), timeout=2)

    asyncio.run(scenario())
    scrollback = get_scrollback(db, channel)
    actions = [m for m in scrollback if m.kind == "action"]
    assert len(actions) == 1
    assert actions[0].author_label == "alice"
    assert actions[0].body == "waves"


def test_me_with_no_action_text_shows_usage(db, hub, presence, mailbox, alice, channel):
    async def scenario():
        session = FakeSession(["/me", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, mailbox, channel, alice), timeout=2)
        return session

    session = asyncio.run(scenario())
    assert "Usage: /me" in _written_text(session)


def test_me_replays_correctly_from_scrollback(db, hub, presence, mailbox, alice, channel):
    async def first_session():
        session = FakeSession(["/me waves", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, mailbox, channel, alice), timeout=2)

    async def second_session():
        session = FakeSession(["/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, mailbox, channel, alice), timeout=2)
        return session

    asyncio.run(first_session())
    session = asyncio.run(second_session())
    assert "* alice waves" in _written_text(session)
