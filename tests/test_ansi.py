"""Tests for netbbs.rendering.ansi."""

from __future__ import annotations

import pytest

from netbbs.rendering.ansi import (
    bg,
    clear_line,
    clear_screen,
    colored,
    fg,
    move_cursor,
    reset_scroll_region,
    restore_cursor,
    save_cursor,
    set_scroll_region,
)


def test_fg_produces_valid_sgr_sequence():
    assert fg(196) == "\x1b[38;5;196m"


def test_bg_produces_valid_sgr_sequence():
    assert bg(21) == "\x1b[48;5;21m"


def test_fg_rejects_out_of_range_color():
    with pytest.raises(ValueError):
        fg(256)
    with pytest.raises(ValueError):
        fg(-1)


def test_bg_rejects_out_of_range_color():
    with pytest.raises(ValueError):
        bg(300)


def test_fg_accepts_boundary_values():
    fg(0)
    fg(255)  # must not raise


def test_colored_with_no_options_returns_text_unchanged():
    assert colored("hello") == "hello"


def test_colored_with_fg_wraps_and_resets():
    result = colored("hello", fg_color=196)
    assert result.startswith("\x1b[38;5;196m")
    assert result.endswith("\x1b[0m")
    assert "hello" in result


def test_colored_with_bg_wraps_and_resets():
    result = colored("hello", bg_color=21)
    assert result.startswith("\x1b[48;5;21m")
    assert result.endswith("\x1b[0m")


def test_colored_with_bold_wraps_and_resets():
    result = colored("hello", bold=True)
    assert result.startswith("\x1b[1m")
    assert result.endswith("\x1b[0m")


def test_colored_combines_bold_fg_bg():
    result = colored("hello", fg_color=196, bg_color=21, bold=True)
    assert result.startswith("\x1b[1m\x1b[38;5;196m\x1b[48;5;21m")
    assert result.endswith("hello\x1b[0m")


def test_colored_with_underline_wraps_and_resets():
    result = colored("hello", underline=True)
    assert result.startswith("\x1b[4m")
    assert result.endswith("\x1b[0m")


def test_colored_combines_underline_with_a_distinct_fg_per_call():
    # The chat status line's own reason for this combination (design
    # doc round 77's redesign): each field gets its own color, but the
    # underline must still run continuously once several such calls are
    # concatenated -- unlike `reverse`, which fights over one shared
    # background per row.
    first = colored("alice", fg_color=201, underline=True)
    second = colored("bob", fg_color=220, underline=True)
    assert first == "\x1b[4m\x1b[38;5;201malice\x1b[0m"
    assert second == "\x1b[4m\x1b[38;5;220mbob\x1b[0m"


def test_clear_screen_moves_cursor_home():
    result = clear_screen()
    assert result == "\x1b[2J\x1b[H"


def test_clear_line():
    assert clear_line() == "\x1b[2K"


def test_move_cursor():
    assert move_cursor(5, 10) == "\x1b[5;10H"


def test_move_cursor_rejects_non_positive_coordinates():
    with pytest.raises(ValueError):
        move_cursor(0, 5)
    with pytest.raises(ValueError):
        move_cursor(5, 0)
    with pytest.raises(ValueError):
        move_cursor(-1, 5)


# -- design doc round 75: scroll region + save/restore cursor --------------
# -- (the chat status line's underlying primitives) -------------------------


def test_set_scroll_region():
    assert set_scroll_region(1, 23) == "\x1b[1;23r"


def test_set_scroll_region_rejects_top_below_one():
    with pytest.raises(ValueError):
        set_scroll_region(0, 23)


def test_set_scroll_region_rejects_bottom_before_top():
    with pytest.raises(ValueError):
        set_scroll_region(10, 5)


def test_set_scroll_region_accepts_a_single_row_region():
    set_scroll_region(5, 5)  # top == bottom -- must not raise


def test_reset_scroll_region():
    assert reset_scroll_region() == "\x1b[r"


def test_save_cursor():
    assert save_cursor() == "\x1b7"


def test_restore_cursor():
    assert restore_cursor() == "\x1b8"
