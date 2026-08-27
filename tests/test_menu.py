"""Tests for netbbs.rendering.menu."""

from __future__ import annotations

import re

from netbbs.rendering.menu import menu_key
from netbbs.rendering.theme import MENU_KEY_COLOR

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


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


# -- prefix (mid-word hotkeys) --------------------------------------------


def test_menu_key_with_prefix_assembles_prefix_bracket_and_rest():
    result = menu_key("n", "nels", prefix="Cha")
    assert result.startswith("Cha[")
    assert "]nels" in result


def test_menu_key_lowercases_the_bracketed_letter_when_prefix_is_given():
    """A real word is never capitalized mid-way through -- `Cha[N]nels`
    reads as a grammar mistake, not a hotkey. Passing an uppercase `key`
    with a `prefix` must not leak that capital into the display; the
    brackets/bold/color already mark the hotkey without it."""
    result = menu_key("N", "nels", prefix="Cha")
    assert _visible(result) == "Cha[n]nels"


def test_menu_key_without_prefix_keeps_the_key_case_as_passed():
    """The no-prefix case is untouched -- a bare first-letter hotkey is
    already naturally capitalized as the start of a title-cased label,
    so there's nothing to lowercase."""
    result = menu_key("B", "oards")
    assert _visible(result) == "[B]oards"


def test_menu_key_capitalize_true_keeps_passed_case_despite_a_prefix():
    """Dogfood report: `prefix="Banners & "` before `"Mastheads"` isn't
    a mid-word hotkey at all -- it's a whole word's own natural leading
    capital, sitting right after a word-boundary space. The default
    lowercase-when-prefixed behavior can't tell this apart from a
    genuine mid-word case (`Cha[n]nels`) by inspecting `prefix` alone,
    so `capitalize=True` is each caller's own explicit opt-in back to
    its passed-in case."""
    result = menu_key("M", "astheads", prefix="Banners & ", capitalize=True)
    assert _visible(result) == "Banners & [M]astheads"


def test_menu_key_capitalize_false_is_the_unchanged_default():
    result = menu_key("M", "astheads", prefix="Banners & ")
    assert _visible(result) == "Banners & [m]astheads"
