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
import re

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


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_text(session: FakeSession) -> str:
    """`_written_text`, with SGR/cursor escape sequences stripped --
    needed for substring checks that span more than one `_StatusSpan`
    (e.g. "2 online"), since the online-count field is now multiple
    independently-colored spans (a bright count, a muted label) with a
    color-reset/color-start escape sequence sitting between them in the
    raw written bytes, even though a real terminal still renders them
    as one unbroken run of visible characters."""
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


def _plain(groups) -> str:
    """Flattens `_render_chat_status_line`'s colored field groups back
    to plain text for substring assertions -- groups themselves join
    with `_STATUS_SEPARATOR`, spans within one group concatenate with
    no gap (see `_StatusSpan`'s own docstring), matching exactly how
    `_compose_status_line` lays out the same data for real."""
    return chat_flow._STATUS_SEPARATOR.join("".join(span.text for span in group) for group in groups)


async def _run(lane, hub, presence, mailbox, channel, user, lines, *, session_registry=None):
    session = FakeSession(lines)
    history = InputHistory()
    action = await asyncio.wait_for(
        chat_flow._chat_loop(
            session, lane, hub, presence, mailbox, history, channel, user, session_registry=session_registry
        ),
        timeout=2,
    )
    return session, action


# -- _render_chat_status_line (pure function) --------------------------


def test_render_shows_channel_name_and_online_count(db, hub, presence, channel, alice):
    hub.join(channel.name, ParticipantId(username="alice", session_key=1))
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))
    assert "#lobby" in text
    assert "1 online" in text


def test_render_gives_the_online_count_a_different_color_than_the_channel_name(db, hub, presence, channel, alice):
    # ACCENT_COLOR already means "channel name" (the same gold used for
    # "Joined #channel" system messages elsewhere in chat) -- the counts
    # need their own distinct color rather than reusing it, so the two
    # unrelated facts don't visually blend into one field.
    from netbbs.rendering import ACCENT_COLOR, HEADER_COLOR

    hub.join(channel.name, ParticipantId(username="alice", session_key=1))
    groups = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    channel_group, online_group = groups[0], groups[1]
    assert channel_group[0].fg_color == ACCENT_COLOR
    assert online_group[0].fg_color == HEADER_COLOR
    assert online_group[0].fg_color != channel_group[0].fg_color


def test_render_reflects_the_live_participant_count(db, hub, presence, channel, alice):
    hub.join(channel.name, ParticipantId(username="alice", session_key=1))
    hub.join(channel.name, ParticipantId(username="bob", session_key=2))
    hub.join(channel.name, ParticipantId(username="carol", session_key=3))
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))
    assert "3 online" in text


def test_render_reflects_the_away_count_among_current_participants(db, hub, presence, channel, alice, bob):
    hub.join(channel.name, ParticipantId(username="alice", session_key=1))
    hub.join(channel.name, ParticipantId(username="bob", session_key=2))
    presence.set_away(bob.username, "brb")
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))
    assert "2 online (1 away)" in text


def test_render_shows_channel_type(db, hub, presence, channel, alice):
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))
    assert "[pub]" in text


def test_render_shows_own_username(db, hub, presence, channel, alice):
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))
    assert "alice" in text


def test_render_shows_own_nick_when_set(db, hub, presence, channel, alice):
    set_nick(db, alice, "night_owl")
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))
    assert "alice(night_owl)" in text


def test_render_shows_topic_when_set(db, hub, presence, channel, alice):
    # Sets the column directly rather than going through set_topic() --
    # that requires ChannelPermission.EDIT, an orthogonal concern this
    # test isn't exercising (see test_render_shows_own_privileges for
    # the permission-gated case).
    db.connection.execute("UPDATE channels SET topic = ? WHERE id = ?", ("Welcome to the lounge!", channel.id))
    db.connection.commit()
    channel = get_channel_by_name(db, channel.name)
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))
    assert '"Welcome to the lounge!"' in text


def test_render_omits_topic_when_unset(db, hub, presence, channel, alice):
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))
    assert '"' not in text


def test_render_shows_own_privileges(db, hub, presence, channel, alice):
    _grant_moderate(db, alice, channel)
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))
    assert "alice[mod]" in text


def test_render_shows_sysop_privilege_label_instead_of_enumerating_bits(db, hub, presence, channel, alice):
    from netbbs.auth.users import set_user_level

    sysop_actor = create_user(db, "root", password="hunter2", user_level=255)
    promoted = set_user_level(db, alice, 255, changed_by=sysop_actor)
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, promoted))
    assert "alice[sysop]" in text


def test_render_shows_no_indicators_by_default(db, hub, presence, channel, alice):
    assert "[muted" not in _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))


def test_render_never_shows_an_inline_away_tag(db, hub, presence, channel, alice):
    """Away is shown by reversing the whole composed row
    (`test_compose_reverses_the_whole_row_when_away`), not an inline
    `[away]` field group -- there is nothing for `_render_chat_status_line`
    itself to add or omit for it, away or not."""
    presence.set_away(alice.username, "brb")
    assert "[away]" not in _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))


def _grant_moderate(db, user, channel):
    grant_permissions(
        db, user, object_type="channel", object_id=channel.id,
        permissions=ChannelPermission.MODERATE, granted_by=user,
    )


def test_render_shows_indefinite_mute_indicator(db, hub, presence, channel, alice, bob):
    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=None, reason=None, muted_by=alice)
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, bob))
    assert "[muted]" in text


def test_render_shows_timed_mute_indicator_with_expiry(db, hub, presence, channel, alice, bob):
    import datetime

    _grant_moderate(db, alice, channel)
    mute_user(db, channel, bob, duration=datetime.timedelta(minutes=10), reason=None, muted_by=alice)
    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, bob))
    assert "muted until" in text


def test_render_clock_is_time_only_not_a_full_date(db, hub, presence, channel, alice):
    """Deliberately a bare HH:MM (design doc round 75), not the node's
    full configured display format (which includes the date) --
    wasted width on a bar that only ever shows the current moment."""
    import re

    text = _plain(chat_flow._render_chat_status_line(db, hub, presence, channel, alice))
    assert re.search(r"\b\d{2}:\d{2}\b", text)
    assert not re.search(r"\d{4}", text)  # no 4-digit year anywhere


# -- _compose_status_line (pure function): colors, separators, background --


def test_compose_uses_ascii_pipe_separators_between_fields(db, hub, presence, channel, alice):
    groups = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    line = chat_flow._compose_status_line(groups, width=200)
    assert chat_flow._STATUS_SEPARATOR in line


def test_compose_colors_each_field_distinctly_and_has_no_background_by_default(db, hub, presence, channel, alice):
    from netbbs.rendering import ACCENT_COLOR, MUTED_COLOR, SELF_COLOR, STATUS_BAR_BACKGROUND
    from netbbs.rendering.ansi import bg, fg

    groups = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    line = chat_flow._compose_status_line(groups, width=200)
    # Channel name and this user's own identity get their own distinct
    # foreground colors rather than sharing one -- and `active` defaults
    # to off, so there's no shared background band.
    assert fg(ACCENT_COLOR) in line
    assert fg(SELF_COLOR) in line
    assert fg(MUTED_COLOR) in line
    assert bg(STATUS_BAR_BACKGROUND) not in line


def test_compose_gives_every_span_the_same_background_when_active(db, hub, presence, channel, alice):
    """`active=True` (driven by `_repaint_status_line`'s default,
    not-away look -- `test_status_line_has_a_background_by_default_when_not_away`
    below) gives every span the same shared `STATUS_BAR_BACKGROUND`, on
    top of its own `fg_color`, rather than replacing per-field color
    with one flat background -- so the background color and each
    field's own distinct fg color both appear."""
    from netbbs.rendering import ACCENT_COLOR, SELF_COLOR, STATUS_BAR_BACKGROUND
    from netbbs.rendering.ansi import bg, fg

    groups = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    line = chat_flow._compose_status_line(groups, width=200, active=True)
    assert bg(STATUS_BAR_BACKGROUND) in line
    assert fg(ACCENT_COLOR) in line
    assert fg(SELF_COLOR) in line


def test_render_gives_channel_type_topic_and_privileges_their_own_colors(db, hub, presence, channel, alice):
    from netbbs.rendering import CHANNEL_TYPE_COLOR, PRIVILEGE_COLOR, TOPIC_COLOR
    from netbbs.rendering.ansi import fg

    db.connection.execute("UPDATE channels SET topic = ? WHERE id = ?", ("Welcome!", channel.id))
    db.connection.commit()
    topical_channel = get_channel_by_name(db, channel.name)
    _grant_moderate(db, alice, topical_channel)

    groups = chat_flow._render_chat_status_line(db, hub, presence, topical_channel, alice)
    line = chat_flow._compose_status_line(groups, width=200)
    assert fg(CHANNEL_TYPE_COLOR) in line
    assert fg(TOPIC_COLOR) in line
    assert fg(PRIVILEGE_COLOR) in line


def test_compose_underlines_the_full_row_including_padding_when_not_active(db, hub, presence, channel, alice):
    import re

    from netbbs.rendering.ansi import RESET, UNDERLINE

    groups = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    line = chat_flow._compose_status_line(groups, width=200, active=False)
    # The away look's underline must still reach the padding at the
    # row's far right, not just the real field text, so it reads as one
    # continuous rule even with no background band to do that job.
    pattern = re.escape(UNDERLINE) + r" +" + re.escape(RESET) + r"$"
    assert re.search(pattern, line)


def test_compose_backgrounds_the_full_row_including_padding_when_active(db, hub, presence, channel, alice):
    import re

    from netbbs.rendering import STATUS_BAR_BACKGROUND
    from netbbs.rendering.ansi import RESET, bg

    groups = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    line = chat_flow._compose_status_line(groups, width=200, active=True)
    # Same "reaches the padding" property as the away look, just via the
    # background band instead of an underline.
    pattern = re.escape(bg(STATUS_BAR_BACKGROUND)) + r" +" + re.escape(RESET) + r"$"
    assert re.search(pattern, line)


def test_compose_drops_whole_groups_from_the_right_on_a_narrow_terminal(db, hub, presence, channel, alice):
    groups = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    # Wide enough for the channel name alone, nowhere near enough for
    # everything -- must drop later (lower-priority) groups whole
    # rather than character-truncating mid-field.
    line = chat_flow._compose_status_line(groups, width=10)
    assert "#lobby" in line
    assert "alice" not in line


def test_compose_character_truncates_only_when_even_the_first_group_does_not_fit(db, hub, presence, channel, alice):
    groups = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
    line = chat_flow._compose_status_line(groups, width=3)
    assert "..." in line


# -- scroll region setup/teardown, via the real _chat_loop --------------


def test_chat_loop_sets_a_scroll_region_reserving_the_last_two_rows(lane, hub, presence, mailbox, channel, alice):
    session, _ = asyncio.run(_run(lane, hub, presence, mailbox, channel, alice, ["/quit"]))
    # Default FakeSession terminal is 80x24 (netbbs.net.session.Session's
    # own class defaults) -- rows 1-22 scroll, row 23 is the pinned
    # status row, row 24 is the input row.
    assert "\x1b[1;22r" in _written_text(session)


def test_chat_loop_clears_the_screen_on_entry(lane, hub, presence, mailbox, channel, alice):
    """Setting a scroll region moves the real terminal cursor home as
    an unavoidable side effect of the escape sequence itself -- entry
    clears the screen first so that jump lands on a blank canvas
    rather than overwriting whatever screen preceded chat."""
    session, _ = asyncio.run(_run(lane, hub, presence, mailbox, channel, alice, ["/quit"]))
    assert _written_text(session).startswith("\x1b[2J\x1b[H")


def test_chat_loop_resets_the_scroll_region_on_exit(lane, hub, presence, mailbox, channel, alice):
    session, _ = asyncio.run(_run(lane, hub, presence, mailbox, channel, alice, ["/quit"]))
    assert "\x1b[r" in _written_text(session)


def test_chat_loop_clears_the_screen_on_exit(lane, hub, presence, mailbox, channel, alice):
    """Design doc round 77 bugfix: neither the channel picker (/leave)
    nor the main menu (/quit) ever clear the screen themselves, so
    without an exit-side clear here the last screenful of chat stayed
    visible until unrelated output happened to overwrite it."""
    session, _ = asyncio.run(_run(lane, hub, presence, mailbox, channel, alice, ["/quit"]))
    assert _written_text(session).endswith("\x1b[r\x1b[2J\x1b[H")


def test_chat_loop_skips_the_pinned_ui_on_a_too_short_terminal(lane, hub, presence, mailbox, channel, alice):
    session = FakeSession(["/quit"])
    session.terminal_height = 1  # below _PINNED_UI_MIN_HEIGHT (3)
    history = InputHistory()
    asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, alice), timeout=2
        )
    )
    text = _written_text(session)
    assert "\x1b[r" not in text  # scroll region was never set, so never reset
    assert not text.startswith("\x1b[2J\x1b[H")  # no forced clear either


def test_chat_loop_resets_the_scroll_region_even_if_that_write_itself_fails(
    lane, hub, presence, mailbox, channel, alice
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
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, alice), timeout=2
        )
    )
    assert isinstance(action, chat_flow._Quit)


# -- repaint triggers -----------------------------------------------------


def test_status_line_has_a_background_by_default_when_not_away(lane, hub, presence, mailbox, channel, alice):
    from netbbs.rendering import STATUS_BAR_BACKGROUND
    from netbbs.rendering.ansi import bg

    session, _ = asyncio.run(_run(lane, hub, presence, mailbox, channel, alice, ["/quit"]))
    assert bg(STATUS_BAR_BACKGROUND) in _written_text(session)


def test_status_line_loses_its_background_when_the_viewer_is_away(lane, hub, presence, mailbox, channel, alice):
    from netbbs.rendering import STATUS_BAR_BACKGROUND, save_cursor
    from netbbs.rendering.ansi import bg

    session, _ = asyncio.run(_run(lane, hub, presence, mailbox, channel, alice, ["/away brb", "/quit"]))
    # Status-line repaints are the only pinned-row writes that save/
    # restore the cursor (`_repaint_status_line`'s own docstring) --
    # isolates them from the input-row repaints interleaved in between,
    # so this is the *last* status-line paint, reflecting away having
    # taken effect -- not the very first one on entry, which still has
    # the default, not-away background.
    status_repaints = [chunk for chunk in session.written if chunk.startswith(save_cursor())]
    assert len(status_repaints) >= 2
    assert bg(STATUS_BAR_BACKGROUND) in status_repaints[0]  # entry: not away yet, default background
    assert bg(STATUS_BAR_BACKGROUND) not in status_repaints[-1]  # after /away brb: quieter, no background


def test_status_line_regains_its_background_once_away_is_cleared(lane, hub, presence, mailbox, channel, alice):
    from netbbs.rendering import STATUS_BAR_BACKGROUND, save_cursor
    from netbbs.rendering.ansi import bg

    session, _ = asyncio.run(
        _run(lane, hub, presence, mailbox, channel, alice, ["/away brb", "/away", "/quit"])
    )
    status_repaints = [chunk for chunk in session.written if chunk.startswith(save_cursor())]
    assert len(status_repaints) >= 2
    assert bg(STATUS_BAR_BACKGROUND) not in status_repaints[-2]  # no background: /away brb just took effect
    assert bg(STATUS_BAR_BACKGROUND) in status_repaints[-1]  # background back: the very next /away cleared it


def test_status_line_repaints_when_a_muted_message_is_rejected(db, lane, hub, presence, mailbox, channel, alice, bob):
    _grant_moderate(db, bob, channel)
    mute_user(db, channel, alice, duration=None, reason=None, muted_by=bob)
    session, _ = asyncio.run(_run(lane, hub, presence, mailbox, channel, alice, ["hello", "/quit"]))
    text = _written_text(session)
    assert "You are muted" in text
    assert "[muted]" in text


def test_status_line_reflects_a_second_participant_joining(lane, hub, presence, mailbox, channel, alice, bob):
    """alice's own status line must pick up bob's arrival -- driven via
    receive_loop's handling of the join notice bob's own _chat_loop
    broadcasts, not anything alice typed herself."""

    async def scenario():
        alice_session = FakeSession([])  # never types anything -- observes only
        alice_task = asyncio.create_task(
            chat_flow._chat_loop(
                alice_session, lane, hub, presence, mailbox, InputHistory(), channel, alice
            )
        )
        while hub.participant_count(channel.name) < 1:
            await asyncio.sleep(0)

        bob_session, _ = await _run(lane, hub, presence, mailbox, channel, bob, ["/quit"])

        alice_task.cancel()
        try:
            await alice_task
        except asyncio.CancelledError:
            pass
        return alice_session

    alice_session = asyncio.run(scenario())
    text = _visible_text(alice_session)
    assert "2 online" in text  # alice's status line updated once bob joined
