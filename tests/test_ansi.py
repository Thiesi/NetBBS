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
