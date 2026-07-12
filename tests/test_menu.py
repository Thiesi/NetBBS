"""Tests for netbbs.rendering.menu."""

from __future__ import annotations

from netbbs.rendering.menu import menu_key
from netbbs.rendering.theme import MENU_KEY_COLOR


def test_menu_key_wraps_key_in_brackets():
    result = menu_key("B", "oards")
    assert result.startswith("[")
    assert "]oards" in result


def test_menu_key_highlights_the_key_with_theme_color():
    result = menu_key("B", "oards")
    assert f"\x1b[38;5;{MENU_KEY_COLOR}m" in result
    assert "B" in result


def test_menu_key_resets_after_key():
    result = menu_key("B", "oards")
    assert "\x1b[0m" in result


def test_menu_key_with_no_rest():
    result = menu_key("Q")
    assert result.startswith("[")
    assert result.endswith("]")


def test_menu_key_supports_multi_char_keys():
    result = menu_key("/quit", " to leave")
    assert "/quit" in result
    assert result.endswith(" to leave")
