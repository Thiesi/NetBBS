"""
Tests for the chat status line (design doc round 75): a pinned row at
the bottom of the terminal, kept out of ordinary scrolling via a VT100
scroll region (`netbbs.rendering.set_scroll_region`), showing the
current channel, live participant count, this user's own away/mute
state, and a clock.

Split into two layers: `_render_chat_status_line` is tested directly as
a pure function (no session/terminal concerns at all), and the
surrounding mechanics (scroll-region setup/teardown, repaint triggers)
are tested by driving the real `_chat_loop`/`_chat_loop`-via-`_run`
through a `FakeSession`, inspecting the raw escape sequences it wrote.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel, get_channel_by_name
from netbbs.chat.hub import ChatHub, ParticipantId
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.moderation import mute_user
from netbbs.chat.nick import set_nick
from netbbs.chat.presence import PresenceRegistry
from netbbs.moderation import ChannelPermission, grant_permissions
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
    return create_channel(db, "lobby", creator=alice)


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


async def _run(db, hub, presence, mailbox, channel, user, lines, *, session_registry=None):
    session = FakeSession(lines)
    history = InputHistory()
    action = await asyncio.wait_for(
        chat_flow._chat_loop(
            session, db, hub, presence, mailbox, history, channel, user, session_registry=session_registry
        ),
        timeout=2,
    )
    return session, action


# -- _render_chat_status_line (pure function) --------------------------


def test_render_shows_channel_name_and_online_count(db, hub, presence, channel, alice):
    hub.join(channel.name, ParticipantId(username="alice", session_key=1))
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert "#lobby" in text
    assert "1 online" in text


def test_render_reflects_the_live_participant_count(db, hub, presence, channel, alice):
    hub.join(channel.name, ParticipantId(username="alice", session_key=1))
    hub.join(channel.name, ParticipantId(username="bob", session_key=2))
    hub.join(channel.name, ParticipantId(username="carol", session_key=3))
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert "3 online" in text


def test_render_reflects_the_away_count_among_current_participants(db, hub, presence, channel, alice, bob):
    hub.join(channel.name, ParticipantId(username="alice", session_key=1))
    hub.join(channel.name, ParticipantId(username="bob", session_key=2))
    presence.set_away(bob.username, "brb")
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert "2 online(1 away)" in text


def test_render_shows_channel_type(db, hub, presence, channel, alice):
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert "[pub]" in text


def test_render_shows_own_username(db, hub, presence, channel, alice):
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert "you:alice" in text


def test_render_shows_own_nick_when_set(db, hub, presence, channel, alice):
    set_nick(db, alice, "night_owl")
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert "you:alice(night_owl)" in text


def test_render_shows_topic_when_set(db, hub, presence, channel, alice):
    # Sets the column directly rather than going through set_topic() --
    # that requires ChannelPermission.EDIT, an orthogonal concern this
    # test isn't exercising (see test_render_shows_own_privileges for
    # the permission-gated case).
    db.connection.execute("UPDATE channels SET topic = ? WHERE id = ?", ("Welcome to the lounge!", channel.id))
    db.connection.commit()
    channel = get_channel_by_name(db, channel.name)
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert '"Welcome to the lounge!"' in text


def test_render_omits_topic_when_unset(db, hub, presence, channel, alice):
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert '"' not in text


def test_render_shows_own_privileges(db, hub, presence, channel, alice):
    _grant_moderate(db, alice, channel)
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert "you:alice[mod]" in text


def test_render_shows_sysop_privilege_label_instead_of_enumerating_bits(db, hub, presence, channel, alice):
    from netbbs.auth.users import set_user_level

    sysop_actor = create_user(db, "root", password="hunter2", user_level=255)
    promoted = set_user_level(db, alice, 255, changed_by=sysop_actor)
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, promoted)
    assert "you:alice[sysop]" in text


def test_render_shows_no_indicators_by_default(db, hub, presence, channel, alice):
    assert "[away]" not in chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert "[muted" not in chat_flow._render_chat_status_line(db, hub, presence, channel, alice)


def test_render_shows_away_indicator(db, hub, presence, channel, alice):
    presence.set_away(alice.username, "brb")
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert "[away]" in text


def _grant_moderate(db, user, channel):
    grant_permissions(
        db, user, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MODERATE, granted_by=user,
    )


def test_render_shows_indefinite_mute_indicator(db, hub, presence, channel, alice, bob):
    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=None, reason=None, muted_by=alice)
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, bob)
    assert "[muted]" in text


def test_render_shows_timed_mute_indicator_with_expiry(db, hub, presence, channel, alice, bob):
    import datetime

    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=datetime.timedelta(minutes=10), reason=None, muted_by=alice)
    text = chat_flow._render_chat_status_line(db, hub, presence, channel, bob)
    assert "muted until" in text


def test_render_clock_is_time_only_not_a_full_date(db, hub, presence, channel, alice):
    """Deliberately a bare HH:MM (design doc round 75), not the node's
    full configured display format (which includes the date) --
    wasted width on a bar that only ever shows the current moment."""
    import re

    text = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    assert re.search(r"\b\d{2}:\d{2}\b", text)
    assert not re.search(r"\d{4}", text)  # no 4-digit year anywhere


# -- scroll region setup/teardown, via the real _chat_loop --------------


def test_chat_loop_sets_a_scroll_region_reserving_the_last_two_rows(db, hub, presence, mailbox, channel, alice):
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/quit"]))
    # Default FakeSession terminal is 80x24 (netbbs.net.session.Session's
    # own class defaults) -- rows 1-22 scroll, row 23 is the pinned
    # input row, row 24 is the status row (design doc round 79).
    assert "\x1b[1;22r" in _written_text(session)


def test_chat_loop_clears_the_screen_on_entry(db, hub, presence, mailbox, channel, alice):
    """Setting a scroll region moves the real terminal cursor home as
    an unavoidable side effect of the escape sequence itself -- entry
    clears the screen first so that jump lands on a blank canvas
    rather than overwriting whatever screen preceded chat."""
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/quit"]))
    assert _written_text(session).startswith("\x1b[2J\x1b[H")


def test_chat_loop_resets_the_scroll_region_on_exit(db, hub, presence, mailbox, channel, alice):
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/quit"]))
    assert "\x1b[r" in _written_text(session)


def test_chat_loop_clears_the_screen_on_exit(db, hub, presence, mailbox, channel, alice):
    """Design doc round 77 bugfix: neither the channel picker (/leave)
    nor the main menu (/quit) ever clear the screen themselves, so
    without an exit-side clear here the last screenful of chat stayed
    visible until unrelated output happened to overwrite it."""
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/quit"]))
    assert _written_text(session).endswith("\x1b[r\x1b[2J\x1b[H")


def test_chat_loop_skips_the_pinned_ui_on_a_too_short_terminal(db, hub, presence, mailbox, channel, alice):
    session = FakeSession(["/quit"])
    session.terminal_height = 1  # below _PINNED_UI_MIN_HEIGHT (3)
    history = InputHistory()
    asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )
    )
    text = _written_text(session)
    assert "\x1b[r" not in text  # scroll region was never set, so never reset
    assert not text.startswith("\x1b[2J\x1b[H")  # no forced clear either


def test_chat_loop_resets_the_scroll_region_even_if_that_write_itself_fails(
    db, hub, presence, mailbox, channel, alice
):
    """A session that's already gone (the common reason _chat_loop is
    unwinding at all) must not crash the cleanup path or mask whatever
    exception is already propagating -- the reset write is best-effort."""
    from netbbs.net.session import SessionClosedError

    class _DyingSession(FakeSession):
        async def write(self, text: str) -> None:
            if "\x1b[r" in text:
                raise SessionClosedError("gone")
            await super().write(text)

    session = _DyingSession(["/quit"])
    history = InputHistory()

    # Must complete cleanly, not raise SessionClosedError out of the
    # finally block's own best-effort reset write.
    action = asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, alice), timeout=2
        )
    )
    assert isinstance(action, chat_flow._Quit)


# -- repaint triggers -----------------------------------------------------


def test_status_line_repaints_after_a_self_state_changing_command(db, hub, presence, mailbox, channel, alice):
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["/away brb", "/quit"]))
    text = _written_text(session)
    # At least one repaint after the /away command shows the new state.
    assert "[away]" in text


def test_status_line_repaints_when_a_muted_message_is_rejected(db, hub, presence, mailbox, channel, alice, bob):
    _grant_moderate(db, bob, channel)
    mute_user(db, channel, alice, duration=None, reason=None, muted_by=bob)
    session, _ = asyncio.run(_run(db, hub, presence, mailbox, channel, alice, ["hello", "/quit"]))
    text = _written_text(session)
    assert "You are muted" in text
    assert "[muted]" in text


def test_status_line_reflects_a_second_participant_joining(db, hub, presence, mailbox, channel, alice, bob):
    """alice's own status line must pick up bob's arrival -- driven via
    receive_loop's handling of the join notice bob's own _chat_loop
    broadcasts, not anything alice typed herself."""

    async def scenario():
        alice_session = FakeSession([])  # never types anything -- observes only
        alice_task = asyncio.create_task(
            chat_flow._chat_loop(
                alice_session, db, hub, presence, mailbox, InputHistory(), channel, alice
            )
        )
        await asyncio.sleep(0.05)  # let alice actually join first

        bob_session, _ = await _run(db, hub, presence, mailbox, channel, bob, ["/quit"])

        alice_task.cancel()
        try:
            await alice_task
        except asyncio.CancelledError:
            pass
        return alice_session

    alice_session = asyncio.run(scenario())
    text = _written_text(alice_session)
    assert "2 online" in text  # alice's status line updated once bob joined
