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
from netbbs.rendering import clear_screen, reset_scroll_region
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


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

    async def read_line(
        self, echo: bool = True, history=None, completer=None, *,
        live_buffer=None, lock=None, list_candidates=None,
    ) -> str:
        # `history`/`completer`/`live_buffer`/`lock`/`list_candidates`
        # accepted only to satisfy the Session ABC signature (design doc
        # rounds 47/49/79, Tracks 5f/5g) -- this fake works from a
        # pre-scripted list of whole logical lines, not raw terminal
        # bytes/characters, so
        # there's no escape-sequence recognition here for Up/Down or Tab
        # to hook into, and no per-keystroke live_buffer updates either
        # (an idle/empty live_buffer is the accurate state for a fake
        # that never has anything genuinely "mid-typed"). Real recall/
        # completion/pinned-input behavior is covered directly in
        # tests/test_char_input_history.py, tests/test_web_line_editing.py,
        # tests/test_chat_completion.py, and tests/test_chat_pinned_input.py
        # instead.
        if self._lines:
            return self._lines.pop(0)
        await asyncio.Event().wait()  # blocks forever, like unread real input
        raise AssertionError("unreachable")

    async def read_key(self, echo: bool = True) -> str:
        # Real callers here: _chat_loop's own post-kick/ban "press any
        # key to continue" gate (design doc round 79) -- a real keypress
        # this fake has no scripted input for, so it stands in with an
        # immediate no-op keystroke rather than raising or blocking
        # forever, matching a user who's already reaching for a key.
        return " "

    async def read_editor_key(self):
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
def lane(db):
    # netbbs.net.chat_flow is migrated onto design doc round 91's
    # two-lane database execution model (issue #57/round 114) -- a
    # second, independent connection to the same file `db` opens.
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


def test_kick_forces_out_a_present_target(db, lane, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        target_session = FakeSession()  # never types anything
        target_task = asyncio.create_task(chat_flow._chat_loop(target_session, lane, hub, presence, mailbox, history, channel, bob))
        await asyncio.sleep(0)  # let target actually join before the kick is issued

        mod_session = FakeSession(["/kick bob disruptive", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2)

        await asyncio.wait_for(target_task, timeout=2)
        return target_session, mod_session

    target_session, mod_session = asyncio.run(scenario())

    assert "kicked" in _written_text(target_session)
    assert hub.participant_count(channel.name) == 0

    scrollback = get_scrollback(db, channel)
    assert any(m.kind == "kick" for m in scrollback)


def test_kicked_target_can_read_the_notice_before_the_screen_clears(
    db, lane, hub, presence, mailbox, history, sysop, bob, channel
):
    """
    Before this fix, the pinned-UI screen reset in `_chat_loop`'s
    `finally` block (design doc round 77) fired unconditionally the
    instant `receive_task` finished -- for a kicked/banned target, that
    meant the very next thing after the kick notice was printed was the
    screen getting wiped, with no chance to actually read it. Now a
    "press any key to continue" gate sits between the two: the notice,
    then the prompt, then (only once a key comes back) the reset/clear.
    """
    async def scenario():
        target_session = FakeSession()  # never types anything
        target_task = asyncio.create_task(
            chat_flow._chat_loop(target_session, lane, hub, presence, mailbox, history, channel, bob)
        )
        await asyncio.sleep(0)  # let target actually join before the kick is issued

        mod_session = FakeSession(["/kick bob disruptive", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(mod_session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2
        )

        await asyncio.wait_for(target_task, timeout=2)
        return target_session

    target_session = asyncio.run(scenario())
    text = _written_text(target_session)

    kicked_index = text.index("kicked")
    prompt_index = text.index("Press any key to continue")
    clear_index = text.index(reset_scroll_region() + clear_screen())

    assert kicked_index < prompt_index < clear_index


def test_kick_notice_is_broadcast_to_others(db, lane, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        target_session = FakeSession()
        target_task = asyncio.create_task(chat_flow._chat_loop(target_session, lane, hub, presence, mailbox, history, channel, bob))
        await asyncio.sleep(0)

        carol = create_user(db, "carol", password="hunter2", user_level=10)
        bystander_session = FakeSession()
        bystander_task = asyncio.create_task(
            chat_flow._chat_loop(bystander_session, lane, hub, presence, mailbox, history, channel, carol)
        )
        await asyncio.sleep(0)

        mod_session = FakeSession(["/kick bob spamming", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2)
        await asyncio.wait_for(target_task, timeout=2)

        bystander_task.cancel()
        await asyncio.gather(bystander_task, return_exceptions=True)
        return bystander_session

    bystander_session = asyncio.run(scenario())
    assert "kicked" in _written_text(bystander_session)


# -- ban, while target is present ------------------------------------------


def test_ban_forces_out_a_present_target(db, lane, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        target_session = FakeSession()
        target_task = asyncio.create_task(chat_flow._chat_loop(target_session, lane, hub, presence, mailbox, history, channel, bob))
        await asyncio.sleep(0)

        mod_session = FakeSession(["/ban bob 10m abuse", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2)

        await asyncio.wait_for(target_task, timeout=2)
        return target_session

    target_session = asyncio.run(scenario())
    assert "banned" in _written_text(target_session)


def test_banned_user_cannot_rejoin(db, lane, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        mod_session = FakeSession(["/ban bob abuse", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2)

        rejoin_session = FakeSession(["/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(rejoin_session, lane, hub, presence, mailbox, history, channel, bob), timeout=2)
        return rejoin_session

    rejoin_session = asyncio.run(scenario())
    assert "banned" in _written_text(rejoin_session)
    assert hub.participant_count(channel.name) == 0


# -- mute -------------------------------------------------------------------


def test_muted_user_message_is_not_broadcast(db, lane, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        mod_session = FakeSession(["/mute bob spamming", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2)

        target_session = FakeSession(["hello everyone", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(target_session, lane, hub, presence, mailbox, history, channel, bob), timeout=2)
        return target_session

    target_session = asyncio.run(scenario())
    assert "muted" in _written_text(target_session)

    scrollback = get_scrollback(db, channel)
    assert not any(m.kind == "message" and m.body == "hello everyone" for m in scrollback)


def test_muted_user_cannot_bypass_mute_with_me(db, lane, hub, presence, mailbox, history, sysop, bob, channel):
    """Regression test for GitHub issue #30: /me is dispatched as a
    slash command, reaching _handle_me before send_loop's own
    is_muted() check (which only guards the plain-message branch) --
    letting a muted user broadcast arbitrary visible text as an action
    event instead of an ordinary message."""

    async def scenario():
        mod_session = FakeSession(["/mute bob spamming", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2)

        target_session = FakeSession(["/me waves", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(target_session, lane, hub, presence, mailbox, history, channel, bob), timeout=2)
        return target_session

    target_session = asyncio.run(scenario())
    assert "muted" in _written_text(target_session)

    scrollback = get_scrollback(db, channel)
    assert not any(m.kind == "action" for m in scrollback)


def test_unmuted_user_me_still_works(db, lane, hub, presence, mailbox, history, bob, channel):
    async def scenario():
        session = FakeSession(["/me waves", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, bob), timeout=2)
        return session

    asyncio.run(scenario())
    scrollback = get_scrollback(db, channel)
    assert any(m.kind == "action" and "waves" in m.body for m in scrollback)


# -- GitHub issue #31: bounded queue overflow surfaces to the affected user --


def test_falling_behind_notice_appears_when_a_participant_queue_overflows(
    db, lane, presence, mailbox, history, sysop, bob, channel
):
    async def scenario():
        small_hub = ChatHub(queue_maxsize=2)
        target_session = FakeSession()  # never reads -- guaranteed to fall behind
        target_task = asyncio.create_task(
            chat_flow._chat_loop(target_session, lane, small_hub, presence, mailbox, history, channel, bob)
        )
        # Let target actually join before flooding -- polled, not a fixed
        # asyncio.sleep(0), since round 114's lane dispatch means joining
        # now involves real ThreadPoolExecutor round trips (the ban
        # check, scrollback fetch, join record) with genuine wall-clock
        # latency a single zero-duration sleep can no longer reliably
        # outlast.
        while small_hub.participant_count(channel.name) < 1:
            await asyncio.sleep(0)

        for i in range(10):  # far more than the queue's capacity of 2
            await small_hub.broadcast(channel.name, f"flood {i}")

        mod_session = FakeSession(["/kick bob disruptive", "/quit"])
        await asyncio.wait_for(
            chat_flow._chat_loop(mod_session, lane, small_hub, presence, mailbox, history, channel, sysop), timeout=2
        )
        await asyncio.wait_for(target_task, timeout=2)
        return target_session

    target_session = asyncio.run(scenario())
    assert "falling behind" in _written_text(target_session)


def test_unmuted_user_can_send_again(db, lane, hub, presence, mailbox, history, sysop, bob, channel):
    async def scenario():
        mod_session = FakeSession(["/mute bob", "/unmute bob", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(mod_session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2)

        target_session = FakeSession(["hello again", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(target_session, lane, hub, presence, mailbox, history, channel, bob), timeout=2)

    asyncio.run(scenario())
    scrollback = get_scrollback(db, channel)
    assert any(m.kind == "message" and m.body == "hello again" for m in scrollback)


# -- permission denial via the real command path ---------------------------


def test_non_moderator_cannot_kick(db, lane, hub, presence, mailbox, history, bob, channel):
    async def scenario():
        session = FakeSession(["/kick sysop nope", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, bob), timeout=2)
        return session

    session = asyncio.run(scenario())
    assert "do not have permission" in _written_text(session)


# -- /finger (design doc §13, sign-off round 38) ---------------------------


def test_finger_shows_public_bio(db, lane, hub, presence, mailbox, history, sysop, bob, channel):
    from netbbs.directory import set_bio, set_bio_visible

    set_bio(db, bob, "Retro computing enthusiast")
    set_bio_visible(db, bob, True)

    async def scenario():
        session = FakeSession(["/finger bob", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2)
        return session

    session = asyncio.run(scenario())
    assert "Retro computing enthusiast" in _written_text(session)


def test_finger_hides_private_bio(db, lane, hub, presence, mailbox, history, sysop, bob, channel):
    from netbbs.directory import set_bio

    set_bio(db, bob, "Secret hobby list")

    async def scenario():
        session = FakeSession(["/finger bob", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2)
        return session

    session = asyncio.run(scenario())
    assert "Secret hobby list" not in _written_text(session)
    assert "no public bio" in _written_text(session)


def test_finger_unknown_user_shows_friendly_message(db, lane, hub, presence, mailbox, history, sysop, channel):
    async def scenario():
        session = FakeSession(["/finger nosuchuser", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, sysop), timeout=2)
        return session

    session = asyncio.run(scenario())
    assert "No such user" in _written_text(session)
