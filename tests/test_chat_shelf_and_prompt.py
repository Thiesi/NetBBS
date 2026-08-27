"""
Unit and integration tests for Issue #183:
- Styled chat input prompt (accent-colored prompt glyph, Unicode '❯ ' vs ASCII '> ')
- Visual divider shelf above the pinned chat status bar (row height - 2)
- VT100 scroll region reservation for 3 pinned rows (shelf, status, input)
- Scrollback history separator divider rule before the LIVE transition
"""

from __future__ import annotations

import asyncio
import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.scrollback import record_message
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import char_input, chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.unicode_style_preference import set_unicode_style_enabled
from netbbs.rendering import MUTED_COLOR, clear_line, colored, move_cursor, save_cursor, set_scroll_region
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


# =========================================================================
# Pure function tests for _input_prompt and _shelf_divider
# =========================================================================


def test_input_prompt_glyph_and_styling():
    """_input_prompt returns bold accent-colored prompt with Unicode '❯ ' or ASCII '> '."""
    unicode_prompt = chat_flow._input_prompt(accent_color=220, unicode_style=True)
    assert unicode_prompt == colored("❯ ", fg_color=220, bold=True)
    assert "❯ " in unicode_prompt

    ascii_prompt = chat_flow._input_prompt(accent_color=51, unicode_style=False)
    assert ascii_prompt == colored("> ", fg_color=51, bold=True)
    assert "> " in ascii_prompt


def test_shelf_divider_styling_unicode_and_truecolor():
    """_shelf_divider returns colored rule across terminal width with appropriate glyph and color."""
    width = 80

    # Unicode + truecolor: '─' with color 238
    shelf_u_tc = chat_flow._shelf_divider(width, unicode_style=True, truecolor=True)
    assert shelf_u_tc == colored("─" * width, fg_color=238)

    # Unicode + 256color/standard: '─' with MUTED_COLOR
    shelf_u_std = chat_flow._shelf_divider(width, unicode_style=True, truecolor=False)
    assert shelf_u_std == colored("─" * width, fg_color=MUTED_COLOR)

    # ASCII + truecolor: '-' with color 238
    shelf_a_tc = chat_flow._shelf_divider(width, unicode_style=False, truecolor=True)
    assert shelf_a_tc == colored("-" * width, fg_color=238)

    # ASCII + 256color/standard: '-' with MUTED_COLOR
    shelf_a_std = chat_flow._shelf_divider(width, unicode_style=False, truecolor=False)
    assert shelf_a_std == colored("-" * width, fg_color=MUTED_COLOR)


# =========================================================================
# Shelf and status bar repaint geometry
# =========================================================================


def test_repaint_status_line_paints_shelf_at_height_minus_two(db, lane, hub, presence, channel, alice):
    """_repaint_status_line repaints shelf divider at row height - 2, status at height - 1, and sets scroll region."""
    session = FakeSession()
    session.terminal_height = 24
    session.terminal_width = 80

    asyncio.run(
        chat_flow._repaint_status_line(
            session, lane, hub, presence, channel, alice,
            unicode_style=True, truecolor=True,
        )
    )
    written = "".join(session.written)

    # Scroll region: rows 1 to 21 (reserving 22, 23, 24)
    assert set_scroll_region(1, 21) in written

    # Shelf divider drawn at row 22
    shelf_marker = move_cursor(22, 1) + clear_line() + chat_flow._shelf_divider(80, unicode_style=True, truecolor=True)
    assert shelf_marker in written

    # Status line drawn at row 23
    assert move_cursor(23, 1) + clear_line() in written


def test_repaint_direct_chat_status_line_paints_shelf(presence, alice, bob):
    """_repaint_direct_chat_status_line paints shelf at height - 2, status at height - 1."""
    session = FakeSession()
    session.terminal_height = 40
    session.terminal_width = 100

    asyncio.run(
        chat_flow._repaint_direct_chat_status_line(
            session, alice, bob, presence,
            accent=chat_flow.ACCENT_COLOR,
            unicode_style=False, truecolor=False,
        )
    )
    written = "".join(session.written)

    # Scroll region: rows 1 to 37 (40 - 3)
    assert set_scroll_region(1, 37) in written

    # Shelf divider drawn at row 38
    shelf_marker = move_cursor(38, 1) + clear_line() + chat_flow._shelf_divider(100, unicode_style=False, truecolor=False)
    assert shelf_marker in written

    # Status line drawn at row 39
    assert move_cursor(39, 1) + clear_line() in written


# =========================================================================
# Pinned input row repaint and truncation with styled prompt
# =========================================================================


def test_repaint_input_row_renders_prompt_and_typed_buffer():
    """_repaint_input_row renders prompt and typed text on row height, respecting relative cursor."""
    session = FakeSession()
    session.terminal_height = 24
    session.terminal_width = 80

    live_buffer = char_input.LiveInputBuffer()
    live_buffer.update(list("test input"), 10)

    prompt = chat_flow._input_prompt(accent_color=220, unicode_style=True)
    asyncio.run(
        chat_flow._repaint_input_row(
            session, live_buffer, session.terminal_height,
            accent_color=220, unicode_style=True,
        )
    )
    written = "".join(session.written)

    # Drawn at row 24
    expected_start = move_cursor(24, 1) + clear_line() + prompt + "test input"
    assert expected_start in written


def test_repaint_input_row_truncation_accounts_for_prompt_width():
    """_repaint_input_row truncates typed text to terminal_width - 2 columns (prompt size)."""
    session = FakeSession()
    session.terminal_height = 24
    session.terminal_width = 12  # width 12 -> avail = 10 columns for typed text

    live_buffer = char_input.LiveInputBuffer()
    live_buffer.update(list("0123456789abcdefghij"), 20)

    asyncio.run(
        chat_flow._repaint_input_row(
            session, live_buffer, session.terminal_height,
            accent_color=220, unicode_style=False,
        )
    )
    written = "".join(session.written)
    # Prompt is '> ' (2 columns), avail is 10, total visible width <= 12
    assert "0123456789abcdefghij" not in written
    assert "..." in written


# =========================================================================
# Scrollback separator rule in _chat_loop
# =========================================================================


def test_scrollback_divider_rule_rendered_between_history_and_live(db, lane, hub, presence, mailbox, channel, alice):
    """When scrollback exists, a visual separator rule is drawn between history and the LIVE join notice."""
    record_message(db, channel, kind="message", author_label="bob", body="past message 1")
    record_message(db, channel, kind="message", author_label="bob", body="past message 2")

    set_unicode_style_enabled(db, alice, True)
    session = FakeSession(["/quit"])
    session.terminal_width = 80
    session.terminal_height = 24
    history = InputHistory()

    asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, alice),
            timeout=2,
        )
    )
    text = "".join(session.written)

    # Scrollback separator rule with unicode '─'
    rule_unicode = colored("─" * 78, fg_color=MUTED_COLOR)
    assert rule_unicode in text

    # Separator rule appears after history and before LIVE
    history_idx = text.index("past message 2")
    rule_idx = text.index(rule_unicode)
    live_idx = text.index("Joined")
    assert history_idx < rule_idx < live_idx


def test_scrollback_divider_rule_ascii_when_unicode_disabled(db, lane, hub, presence, mailbox, channel, alice):
    """When unicode_style is False, the scrollback separator uses ASCII '-' characters."""
    record_message(db, channel, kind="message", author_label="bob", body="past message")

    set_unicode_style_enabled(db, alice, False)
    session = FakeSession(["/quit"])
    session.terminal_width = 80
    session.terminal_height = 24
    history = InputHistory()

    asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, alice),
            timeout=2,
        )
    )
    text = "".join(session.written)

    # Scrollback separator rule with ASCII '-'
    rule_ascii = colored("-" * 78, fg_color=MUTED_COLOR)
    assert rule_ascii in text


# =========================================================================
# Terminal height boundary conditions
# =========================================================================


def test_chat_loop_pinned_ui_min_height_four_activates_three_pinned_rows(lane, hub, presence, mailbox, channel, alice):
    """Height 4 is the minimum height that activates the 3 pinned rows (1 scrolling + 3 pinned)."""
    session = FakeSession(["/quit"])
    session.terminal_height = 4
    session.terminal_width = 80
    history = InputHistory()

    asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, alice),
            timeout=2,
        )
    )
    text = "".join(session.written)
    # Scroll region set to row 1..1 (4 - 3)
    assert set_scroll_region(1, 1) in text
    assert move_cursor(2, 1) in text  # shelf row
    assert move_cursor(3, 1) in text  # status row
    assert move_cursor(4, 1) in text  # input row


def test_chat_loop_height_three_degrades_to_unpinned_scrolling(lane, hub, presence, mailbox, channel, alice):
    """Height 3 is below _PINNED_UI_MIN_HEIGHT (4) and does not set scroll region or pin rows."""
    session = FakeSession(["/quit"])
    session.terminal_height = 3
    session.terminal_width = 80
    history = InputHistory()

    asyncio.run(
        asyncio.wait_for(
            chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, alice),
            timeout=2,
        )
    )
    text = "".join(session.written)
    assert "\x1b[r" not in text
    assert "❯ " not in text and "> " not in text


def test_tab_completion_candidate_listing_preserves_prompt_styling(lane, hub, presence, mailbox, channel, alice):
    """Multiple completion candidates redraw input with preserved styled prompt (accent + Unicode)."""
    live_buf = chat_flow.LiveInputBuffer()
    session = FakeSession()
    session.terminal_height = 24
    session.terminal_width = 80

    asyncio.run(
        chat_flow._print_candidates_and_redraw_input(
            session, live_buf, 24, ["/who", "/whisper"], "/wh", 3,
            accent_color=208, unicode_style=True,
        )
    )
    text = "".join(session.written)
    assert "/who  /whisper" in text
    assert colored("❯ ", fg_color=208, bold=True) in text
