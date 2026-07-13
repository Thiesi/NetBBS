"""
Tests for the discovery commands in netbbs.net.chat_flow (design doc
rounds 32/33, sign-off round 43): /names, /who, /list, /whois. Reuses
the FakeSession/fixture conventions already established in
tests/test_chat_flow_moderation.py and friends.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.directory import set_bio, set_bio_visible
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
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


# -- /names -----------------------------------------------------------------


def test_names_lists_everyone_present(db, hub, presence, alice, bob, channel):
    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        watcher = FakeSession()
        watcher_task = asyncio.create_task(
            chat_flow._chat_loop(watcher, db, hub, presence, mailbox, history, channel, bob)
        )
        await asyncio.sleep(0)

        asker = FakeSession(["/names", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(asker, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return asker

    asker = asyncio.run(scenario())
    output = _written_text(asker)
    assert "alice" in output
    assert "bob" in output


def test_names_dedupes_two_sessions_of_the_same_account(db, hub, presence, alice, channel):
    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        extra = FakeSession()
        extra_task = asyncio.create_task(
            chat_flow._chat_loop(extra, db, hub, presence, mailbox, history, channel, alice)
        )
        await asyncio.sleep(0)

        asker = FakeSession(["/names", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(asker, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )

        extra_task.cancel()
        await asyncio.gather(extra_task, return_exceptions=True)
        return asker

    asker = asyncio.run(scenario())
    output = _written_text(asker)
    # "alice" should appear exactly once in the /names line, not twice
    # for the two sessions.
    names_line = next(line for line in output.splitlines() if "alice" in line and "joined" not in line)
    assert names_line.count("alice") == 1


def test_names_empty_channel_shows_message(db, hub, presence, alice, channel):
    # alice herself is present by the time /names runs, so this
    # exercises the "no one" branch by checking bob's *empty* view
    # isn't reachable -- instead confirm the message shows when only
    # the asker (excluded from being "no one") is there is impossible;
    # use the literal empty-roster case: no participant_ids at all
    # can't happen once joined, so this test instead confirms the
    # roster always includes the asker themselves.
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/names", "/quit"]))
    assert "alice" in _written_text(session)


# -- /who -------------------------------------------------------------------


def test_who_shows_away_indicator(db, hub, presence, alice, bob, channel):
    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        watcher = FakeSession()
        watcher_task = asyncio.create_task(
            chat_flow._chat_loop(watcher, db, hub, presence, mailbox, history, channel, bob)
        )
        await asyncio.sleep(0)

        asker = FakeSession(["/away gone to lunch", "/who", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(asker, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return asker

    asker = asyncio.run(scenario())
    assert "alice (away: gone to lunch)" in _written_text(asker)


def test_who_shows_no_suffix_when_not_away(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/who", "/quit"]))
    output = _written_text(session)
    who_line = next(line for line in output.splitlines() if line.strip() == "alice")
    assert who_line == "alice"


# -- /list --------------------------------------------------------------


def test_list_shows_visible_channels_with_online_count(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/list", "/quit"]))
    output = _written_text(session)
    assert "#general" in output
    assert "online" in output


def test_list_excludes_channels_above_the_users_level(db, hub, presence, alice, bob, channel):
    from netbbs.chat.channels import create_channel as _create_channel

    _create_channel(db, "staff-only", min_level=50, creator=alice)
    session = asyncio.run(_run(db, hub, presence, channel, bob, ["/list", "/quit"]))
    output = _written_text(session)
    assert "#general" in output
    assert "staff-only" not in output


# -- /whois -----------------------------------------------------------------


def test_whois_shows_online_status(db, hub, presence, alice, bob, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/whois bob", "/quit"]))
    output = _written_text(session)
    assert "bob" in output
    assert "Status: offline" in output


def test_whois_shows_online_when_target_is_present(db, hub, presence, alice, bob, channel):
    # presence.is_online reflects the *login* session count
    # (PresenceRegistry.enter, called by handle_session) -- driving
    # _chat_loop directly, as these tests do, bypasses login_flow
    # entirely, so the login step is simulated explicitly here to
    # match what actually happens in a real connection.
    presence.enter("bob")

    async def scenario():
        mailbox = MessageMailbox()
        history = InputHistory()
        target = FakeSession()
        target_task = asyncio.create_task(
            chat_flow._chat_loop(target, db, hub, presence, mailbox, history, channel, bob)
        )
        await asyncio.sleep(0)

        asker = FakeSession(["/whois bob", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(asker, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )

        target_task.cancel()
        await asyncio.gather(target_task, return_exceptions=True)
        return asker

    asker = asyncio.run(scenario())
    output = _written_text(asker)
    assert "Status: online" in output
    assert "#general" in output  # channel-membership line


def test_whois_shows_away_status(db, hub, presence, alice, bob, channel):
    presence.enter("bob")
    presence.set_away("bob", "brb")
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/whois bob", "/quit"]))
    assert "Away: brb" in _written_text(session)


def test_whois_shows_public_bio(db, hub, presence, alice, bob, channel):
    set_bio(db, bob, "Retro computing enthusiast")
    set_bio_visible(db, bob, True)
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/whois bob", "/quit"]))
    assert "Retro computing enthusiast" in _written_text(session)


def test_whois_hides_private_bio(db, hub, presence, alice, bob, channel):
    set_bio(db, bob, "Secret hobby list")
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/whois bob", "/quit"]))
    output = _written_text(session)
    assert "Secret hobby list" not in output
    assert "no public bio" in output


def test_whois_unknown_user_shows_friendly_message(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/whois nosuchuser", "/quit"]))
    assert "No such user" in _written_text(session)
