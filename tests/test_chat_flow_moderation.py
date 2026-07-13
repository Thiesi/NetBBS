"""
Integration tests for the mute/ban/kick command wiring in
netbbs.net.chat_flow (design doc §13, sign-off round 37) — exercising
the actual `/mute`, `/ban`, `/kick` commands through `_chat_loop`
itself, not just the underlying netbbs.chat.moderation library
(covered separately in tests/test_chat_moderation.py).

No fake `Session` helper existed anywhere in this test suite before
this file (confirmed during round 37's design survey) — `FakeSession`
below is a minimal one, just enough to drive two genuinely concurrent
`_chat_loop` calls against a shared `ChatHub`/`Database`.
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
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.session import Session
from netbbs.storage.database import Database


class FakeSession(Session):
    """Scripted input, captured output. `read_line` blocks forever
    (never resolves) once its scripted lines run out -- the same shape
    a real session has while genuinely waiting for a user to type
    something that never comes, which is exactly what lets
    `_chat_loop`'s `asyncio.wait(..., FIRST_COMPLETED)` exit via
    `receive_loop` (a kick notice arriving) rather than `send_loop`."""

    def __init__(self, lines: list[str] | None = None):
        self._lines = list(lines or [])
        self.written: list[str] = []
        self.closed = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        # `history`/`completer` accepted only to satisfy the Session ABC
        # signature (design doc rounds 47/49, Tracks 5f/5g) -- this fake
        # works from a pre-scripted list of whole logical lines, not raw
        # terminal bytes/characters, so there's no escape-sequence
        # recognition here for Up/Down or Tab to hook into; real recall/
        # completion behavior is covered directly in
        # tests/test_char_input_history.py, tests/test_web_line_editing.py,
        # and tests/test_chat_completion.py instead.
        if self._lines:
            return self._lines.pop(0)
        await asyncio.Event().wait()  # blocks forever, like unread real input
        raise AssertionError("unreachable")

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        self.closed = True

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


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
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=100)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, sysop):
    ch = create_channel(db, "general", creator=sysop)
    grant_permissions(
        db, sysop, object_type="channel", object_id=ch.id,
        permissions=ChannelPermission.MODERATE, granted_by=sysop,
    )
    return ch


def _written_text(session: FakeSession) -> str:
    return "\n".join(session.written)


# -- kick, while target is present ----------------------------------------


def test_kick_forces_out_a_present_target(db, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        target_session = FakeSession()  # never types anything
        target_task = asyncio.create_task(chat_flow._chat_loop(target_session, db, hub, presence, mailbox, history, channel, bob))
        await asyncio.sleep(0)  # let target actually join before the kick is issued

        mod_session = FakeSession(["/kick bob disruptive", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, db, hub, presence, mailbox, history, channel, sysop), timeout=2)

        await asyncio.wait_for(target_task, timeout=2)
        return target_session, mod_session

    target_session, mod_session = asyncio.run(scenario())

    assert "kicked" in _written_text(target_session)
    assert hub.participant_count(channel.name) == 0

    scrollback = get_scrollback(db, channel)
    assert any(m.kind == "kick" for m in scrollback)


def test_kick_notice_is_broadcast_to_others(db, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        target_session = FakeSession()
        target_task = asyncio.create_task(chat_flow._chat_loop(target_session, db, hub, presence, mailbox, history, channel, bob))
        await asyncio.sleep(0)

        carol = create_user(db, "carol", password="hunter2", user_level=10)
        bystander_session = FakeSession()
        bystander_task = asyncio.create_task(
            chat_flow._chat_loop(bystander_session, db, hub, presence, mailbox, history, channel, carol)
        )
        await asyncio.sleep(0)

        mod_session = FakeSession(["/kick bob spamming", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, db, hub, presence, mailbox, history, channel, sysop), timeout=2)
        await asyncio.wait_for(target_task, timeout=2)

        bystander_task.cancel()
        await asyncio.gather(bystander_task, return_exceptions=True)
        return bystander_session

    bystander_session = asyncio.run(scenario())
    assert "kicked" in _written_text(bystander_session)


# -- ban, while target is present ------------------------------------------


def test_ban_forces_out_a_present_target(db, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        target_session = FakeSession()
        target_task = asyncio.create_task(chat_flow._chat_loop(target_session, db, hub, presence, mailbox, history, channel, bob))
        await asyncio.sleep(0)

        mod_session = FakeSession(["/ban bob 10m abuse", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, db, hub, presence, mailbox, history, channel, sysop), timeout=2)

        await asyncio.wait_for(target_task, timeout=2)
        return target_session

    target_session = asyncio.run(scenario())
    assert "banned" in _written_text(target_session)


def test_banned_user_cannot_rejoin(db, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        mod_session = FakeSession(["/ban bob abuse", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, db, hub, presence, mailbox, history, channel, sysop), timeout=2)

        rejoin_session = FakeSession(["/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(rejoin_session, db, hub, presence, mailbox, history, channel, bob), timeout=2)
        return rejoin_session

    rejoin_session = asyncio.run(scenario())
    assert "banned" in _written_text(rejoin_session)
    assert hub.participant_count(channel.name) == 0


# -- mute -------------------------------------------------------------------


def test_muted_user_message_is_not_broadcast(db, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        mod_session = FakeSession(["/mute bob spamming", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, db, hub, presence, mailbox, history, channel, sysop), timeout=2)

        target_session = FakeSession(["hello everyone", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(target_session, db, hub, presence, mailbox, history, channel, bob), timeout=2)
        return target_session

    target_session = asyncio.run(scenario())
    assert "muted" in _written_text(target_session)

    scrollback = get_scrollback(db, channel)
    assert not any(m.kind == "message" and m.body == "hello everyone" for m in scrollback)


def test_unmuted_user_can_send_again(db, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        mod_session = FakeSession(["/mute bob", "/unmute bob", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, db, hub, presence, mailbox, history, channel, sysop), timeout=2)

        target_session = FakeSession(["hello again", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(target_session, db, hub, presence, mailbox, history, channel, bob), timeout=2)

    asyncio.run(scenario())
    scrollback = get_scrollback(db, channel)
    assert any(m.kind == "message" and m.body == "hello again" for m in scrollback)


# -- permission denial via the real command path ---------------------------


def test_non_moderator_cannot_kick(db, hub, presence, mailbox, history, bob, channel):
    async def scenario():
        session = FakeSession(["/kick sysop nope", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, bob), timeout=2)
        return session

    session = asyncio.run(scenario())
    assert "do not have permission" in _written_text(session)


# -- /finger (design doc §13, sign-off round 38) ---------------------------


def test_finger_shows_public_bio(db, hub, presence, mailbox, history, sysop, bob, channel):
    from netbbs.directory import set_bio, set_bio_visible

    set_bio(db, bob, "Retro computing enthusiast")
    set_bio_visible(db, bob, True)

    async def scenario():
        session = FakeSession(["/finger bob", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, sysop), timeout=2)
        return session

    session = asyncio.run(scenario())
    assert "Retro computing enthusiast" in _written_text(session)


def test_finger_hides_private_bio(db, hub, presence, mailbox, history, sysop, bob, channel):
    from netbbs.directory import set_bio

    set_bio(db, bob, "Secret hobby list")

    async def scenario():
        session = FakeSession(["/finger bob", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, sysop), timeout=2)
        return session

    session = asyncio.run(scenario())
    assert "Secret hobby list" not in _written_text(session)
    assert "no public bio" in _written_text(session)


def test_finger_unknown_user_shows_friendly_message(db, hub, presence, mailbox, history, sysop, channel):
    async def scenario():
        session = FakeSession(["/finger nosuchuser", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, sysop), timeout=2)
        return session

    session = asyncio.run(scenario())
    assert "No such user" in _written_text(session)
