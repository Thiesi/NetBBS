"""Tests for netbbs.rendering.ansi."""

from __future__ import annotations

import pytest

from netbbs.rendering.ansi import (
    bg,
    bg_rgb,
    clear_line,
    clear_screen,
    colored,
    fg,
    fg_rgb,
    move_cursor,
    reset_scroll_region,
    restore_cursor,
    save_cursor,
    set_scroll_region,
    strip_ansi,
)
from netbbs.rendering.gradient import gradient_text


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


def test_fg_rgb_produces_valid_truecolor_sgr_sequence():
    assert fg_rgb(255, 0, 0) == "\x1b[38;2;255;0;0m"


def test_bg_rgb_produces_valid_truecolor_sgr_sequence():
    assert bg_rgb(0, 128, 255) == "\x1b[48;2;0;128;255m"


def test_fg_rgb_rejects_out_of_range_component():
    with pytest.raises(ValueError):
        fg_rgb(256, 0, 0)
    with pytest.raises(ValueError):
        fg_rgb(0, -1, 0)
    with pytest.raises(ValueError):
        fg_rgb(0, 0, 300)


def test_fg_rgb_accepts_boundary_values():
    fg_rgb(0, 0, 0)
    fg_rgb(255, 255, 255)  # must not raise


def test_colored_with_tuple_fg_uses_truecolor():
    result = colored("hello", fg_color=(255, 0, 0))
    assert result == "\x1b[38;2;255;0;0mhello\x1b[0m"


def test_colored_with_tuple_bg_uses_truecolor():
    result = colored("hello", bg_color=(0, 128, 255))
    assert result == "\x1b[48;2;0;128;255mhello\x1b[0m"


def test_colored_combines_bold_with_tuple_fg():
    result = colored("hello", fg_color=(255, 0, 0), bold=True)
    assert result == "\x1b[1m\x1b[38;2;255;0;0mhello\x1b[0m"


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
    # The chat status line's own reason for this combination: each
    # field gets its own color, but the underline must still run
    # continuously once several such calls are
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


# -- scroll region + save/restore cursor -------------------------------------
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


def test_strip_ansi_removes_colored_output():
    assert strip_ansi(colored("hello", fg_color=208, bold=True)) == "hello"


def test_strip_ansi_removes_truecolor_and_gradient_output():
    text = gradient_text("NETBBS", "rainbow", bold=True, truecolor=True)
    assert "\x1b" in text  # sanity: the fixture actually contains escapes
    assert strip_ansi(text) == "NETBBS"


def test_strip_ansi_removes_cursor_and_screen_control_sequences():
    composed = (
        f"{clear_screen()}{move_cursor(3, 5)}{save_cursor()}text{restore_cursor()}"
        f"{set_scroll_region(1, 23)}{reset_scroll_region()}{clear_line()}"
    )
    assert strip_ansi(composed) == "text"


def test_strip_ansi_is_a_no_op_on_plain_text():
    assert strip_ansi("just plain text, no escapes here") == "just plain text, no escapes here"


def test_strip_ansi_leaves_no_esc_byte_behind():
    text = colored("multi", fg_color=(10, 20, 30)) + colored("part", bg_color=99, underline=True)
    assert "\x1b" not in strip_ansi(text)
