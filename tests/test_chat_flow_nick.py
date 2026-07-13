"""
Integration tests for the `/nick` command wiring in netbbs.net.chat_flow
(design doc round 32/41) — the command itself, plus that every live
chat rendering path (join/leave/message/action) shows the alias
alongside the canonical username once set. Library-level nick
validation is covered separately in tests/test_chat_nick.py.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.nick import set_nick
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
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, alice):
    return create_channel(db, "general", creator=alice)


def _written_text(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(db, hub, presence, channel, user, lines):
    session = FakeSession(lines)
    await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, channel, user), timeout=2)
    return session


# -- /nick command --------------------------------------------------------


def test_nick_sets_alias_and_announces_it(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/nick DeepParse", "/quit"]))
    assert "is now known as DeepParse|alice" in _written_text(session)


def test_nick_off_clears_alias_and_announces_it(db, hub, presence, alice, channel):
    set_nick(db, alice, "DeepParse")
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/nick off", "/quit"]))
    assert "is no longer using an alias" in _written_text(session)


def test_nick_with_no_args_shows_current_alias(db, hub, presence, alice, channel):
    set_nick(db, alice, "DeepParse")
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/nick", "/quit"]))
    assert "Your current alias: DeepParse" in _written_text(session)


def test_nick_with_no_args_and_none_set_shows_usage(db, hub, presence, alice, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/nick", "/quit"]))
    assert "Usage: /nick" in _written_text(session)


def test_nick_rejects_invalid_alias(db, hub, presence, alice, bob, channel):
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/nick bob", "/quit"]))
    assert "Could not set alias" in _written_text(session)


def test_nick_change_recorded_in_scrollback(db, hub, presence, alice, channel):
    asyncio.run(_run(db, hub, presence, channel, alice, ["/nick DeepParse", "/quit"]))
    scrollback = get_scrollback(db, channel)
    assert any(m.kind == "nick" for m in scrollback)


# -- alias shows up across rendering paths ---------------------------------


def test_alias_shown_on_join(db, hub, presence, alice, bob, channel):
    # A join broadcast excludes the joiner themselves (they get their
    # own "Joined #channel" line instead) -- needs a bystander already
    # present to observe it, same shape as the leave test below.
    set_nick(db, alice, "DeepParse")

    async def scenario():
        watcher = FakeSession()
        watcher_task = asyncio.create_task(chat_flow._chat_loop(watcher, db, hub, presence, channel, bob))
        await asyncio.sleep(0)

        joiner = FakeSession(["/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(joiner, db, hub, presence, channel, alice), timeout=2)

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return watcher

    watcher = asyncio.run(scenario())
    assert "DeepParse|alice has joined" in _written_text(watcher)


def test_alias_shown_on_leave(db, hub, presence, alice, bob, channel):
    set_nick(db, bob, "Bobby")

    async def scenario():
        watcher = FakeSession()
        watcher_task = asyncio.create_task(chat_flow._chat_loop(watcher, db, hub, presence, channel, alice))
        await asyncio.sleep(0)

        leaver = FakeSession(["/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(leaver, db, hub, presence, channel, bob), timeout=2)

        watcher_task.cancel()
        await asyncio.gather(watcher_task, return_exceptions=True)
        return watcher

    watcher = asyncio.run(scenario())
    assert "Bobby|bob has left" in _written_text(watcher)


def test_alias_shown_in_regular_message(db, hub, presence, alice, channel):
    set_nick(db, alice, "DeepParse")
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["hello", "/quit"]))
    assert "<DeepParse|alice>" in _written_text(session)


def test_alias_shown_in_me_action(db, hub, presence, alice, channel):
    set_nick(db, alice, "DeepParse")
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/me waves", "/quit"]))
    assert "* DeepParse|alice waves" in _written_text(session)


def test_alias_shown_on_scrollback_replay(db, hub, presence, alice, channel):
    set_nick(db, alice, "DeepParse")
    asyncio.run(_run(db, hub, presence, channel, alice, ["hello", "/quit"]))
    # A second session replays scrollback -- current alias should show,
    # not whatever was canonical-only at storage time.
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/quit"]))
    assert "<DeepParse|alice>" in _written_text(session)


def test_moderation_notices_stay_canonical_only(db, hub, presence, alice, bob, channel):
    from netbbs.moderation import ChannelPermission, grant_permissions

    set_nick(db, alice, "DeepParse")
    grant_permissions(
        db, alice, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MODERATE, granted_by=alice,
    )
    session = asyncio.run(_run(db, hub, presence, channel, alice, ["/kick bob testing", "/quit"]))
    # The moderator's own alias must not appear in the kick notice --
    # moderation/auditing always shows canonical identity only (design
    # doc round 32, point 7).
    assert "by DeepParse|alice" not in _written_text(session)
    assert "by alice" in _written_text(session)
