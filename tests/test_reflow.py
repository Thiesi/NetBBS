"""Tests for netbbs.rendering.reflow."""

from __future__ import annotations

import os
import re

import pytest

from netbbs.rendering.ansi import colored, strip_ansi
from netbbs.rendering.reflow import (
    colored_truncate,
    print_wrapped,
    reflow,
    terminal_wrapped,
    wrap_terminal_text,
)
from netbbs.rendering.width import display_width

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def test_short_text_unchanged():
    assert reflow("hello world", width=80) == "hello world"


def test_long_line_wraps_at_width():
    text = "word " * 30  # well over 80 chars
    result = reflow(text.strip(), width=20)
    for line in result.split("\n"):
        assert len(line) <= 20


def test_preserves_paragraph_breaks():
    text = "First paragraph here.\n\nSecond paragraph here."
    result = reflow(text, width=80)
    assert "\n\n" in result
    assert "First paragraph here." in result
    assert "Second paragraph here." in result


def test_wraps_each_paragraph_independently():
    long_para = "word " * 30
    text = f"{long_para.strip()}\n\nshort"
    result = reflow(text, width=20)
    paragraphs = result.split("\n\n")
    assert len(paragraphs) == 2
    assert paragraphs[1] == "short"
    for line in paragraphs[0].split("\n"):
        assert len(line) <= 20


def test_empty_string():
    assert reflow("", width=80) == ""


def test_rejects_non_positive_width():
    with pytest.raises(ValueError):
        reflow("hello", width=0)
    with pytest.raises(ValueError):
        reflow("hello", width=-5)


def test_single_long_word_not_broken_mid_word_by_default():
    # textwrap's default behavior can still break extremely long single
    # words if they exceed the width entirely; this test just confirms
    # normal multi-word text doesn't get mangled at a reasonable width.
    text = "supercalifragilisticexpialidocious is a long word"
    result = reflow(text, width=80)
    assert "supercalifragilisticexpialidocious" in result


def test_narrow_width_like_40_columns():
    # Design doc requirement: must degrade gracefully above 40x24 minimum.
    text = "This is a reasonably long sentence that should wrap cleanly."
    result = reflow(text, width=40)
    for line in result.split("\n"):
        assert len(line) <= 40


def test_reflow_wraps_cjk_text_at_display_column_boundaries_not_character_count():
    # Dogfood report: international users found non-ASCII handling poor.
    # No spaces at all -- one long "word" under whitespace-splitting --
    # must still wrap at real column boundaries (2 columns/character),
    # not overflow into one unbroken line the way a len()-based wrap
    # would (12 characters, 24 display columns, all "fitting" under an
    # 8-character-count budget).
    text = "你好世界" * 3
    result = reflow(text, width=8)
    lines = result.split("\n")
    assert len(lines) > 1
    from netbbs.rendering.width import display_width

    for line in lines:
        assert display_width(line) <= 8
    assert "".join(lines) == text


# -- terminal-output safety net ----------------------------------------


def test_wrap_terminal_text_wraps_at_words_without_edge_spaces():
    result = wrap_terminal_text("alpha beta gamma delta", width=11)
    assert result.split("\r\n") == ["alpha beta", "gamma delta"]
    assert all(not line.startswith(" ") and not line.endswith(" ") for line in result.split("\r\n"))


def test_wrap_terminal_text_keeps_styled_text_and_escape_sequences_intact():
    original = colored("alpha beta gamma", fg_color=51, bold=True)
    result = wrap_terminal_text(original, width=10)
    visible_lines = strip_ansi(result).split("\r\n")
    assert visible_lines == ["alpha beta", "gamma"]
    assert all(display_width(line) <= 10 for line in visible_lines)
    assert result.count("\x1b[38;5;51m") == 1
    assert result.count("\x1b[0m") == 1


def test_wrap_terminal_text_never_hides_an_indivisible_token():
    result = wrap_terminal_text("0123456789abcdef", width=5)
    lines = result.split("\r\n")
    assert "".join(lines) == "0123456789abcdef"
    assert all(display_width(line) <= 5 for line in lines)


def test_wrap_terminal_text_normalizes_tabs_before_measuring():
    result = wrap_terminal_text("1234\t56789", width=10)
    assert "\t" not in result
    assert result == "1234 56789"
    assert all(display_width(line) <= 10 for line in result.split("\r\n"))


def test_wrap_terminal_text_does_not_emit_an_empty_row_before_indented_token():
    result = wrap_terminal_text("  0123456789", width=5)
    assert result.split("\r\n") == ["  012", "34567", "89"]


def test_wrap_terminal_text_tracks_horizontal_cursor_movement():
    result = wrap_terminal_text("\x1b[35Cabcdefghij", width=40)
    assert result == "\x1b[35Cabcde\r\nfghij"


def test_wrap_terminal_text_keeps_absolute_row_but_bounds_its_column():
    result = wrap_terminal_text("\x1b[2;35Habcdefghij", width=40)
    assert result == "\x1b[2;35Habcdef\r\nghij"


def test_absolute_row_position_resets_column_accounting():
    result = wrap_terminal_text("abc\x1b[2;10Hx", width=10)
    assert result == "abc\x1b[2;10Hx"


def test_cursor_accounting_caps_huge_numeric_parameters_without_expansion():
    result = wrap_terminal_text("\x1b[999999999Cx", width=40)
    assert result == "\x1b[999999999Cx"


def test_bare_carriage_return_resets_column_without_adding_a_row():
    assert wrap_terminal_text("abc\rX", width=40) == "abc\rX"


def test_absolute_column_control_preserves_already_rendered_cells():
    assert wrap_terminal_text("abc\x1b[10Gx", width=10) == "abc\x1b[10Gx"


def test_saved_cursor_column_participates_in_wrapping():
    original = "\x1b[39G\x1b7\x1b[1Gabc\x1b8xyz"
    assert wrap_terminal_text(original, width=40) == original[:-1] + "\r\nz"


def test_wrap_terminal_text_preserves_existing_blank_lines():
    assert wrap_terminal_text("first\r\n\r\nsecond", width=80) == "first\r\n\r\nsecond"


def test_wrap_terminal_text_rejects_non_positive_width():
    with pytest.raises(ValueError):
        wrap_terminal_text("hello", 0)


def test_print_wrapped_bounds_cli_prose_without_edge_spaces(capsys):
    print_wrapped("alpha beta gamma delta", width=11)

    lines = capsys.readouterr().out.splitlines()
    assert lines == ["alpha beta", "gamma delta"]
    assert all(len(line) <= 11 and line == line.strip() for line in lines)


def test_terminal_wrapped_uses_lf_for_exception_messages():
    assert terminal_wrapped("alpha beta gamma", width=10) == "alpha beta\ngamma"


def test_terminal_wrapped_measures_the_destination_stream(monkeypatch):
    class _Stderr:
        def fileno(self) -> int:
            return 2

    monkeypatch.delenv("COLUMNS", raising=False)
    measured: list[int] = []

    def terminal_size(fd: int) -> os.terminal_size:
        measured.append(fd)
        return os.terminal_size((12, 24))

    monkeypatch.setattr(os, "get_terminal_size", terminal_size)
    result = terminal_wrapped("alpha beta gamma delta", stream=_Stderr())

    assert measured == [2]
    assert result.splitlines() == ["alpha beta", "gamma delta"]


# -- colored_truncate ---------------------------------------------------


def test_colored_truncate_untruncated_colors_every_segment():
    result = colored_truncate([("abc", 51), ("def", None), ("ghi", 220)], width=80)
    assert result == colored("abc", fg_color=51) + "def" + colored("ghi", fg_color=220)
    assert _visible(result) == "abcdefghi"


def test_colored_truncate_skips_empty_segments():
    # An empty segment (e.g. no description on this item) must not emit
    # a stray color-on/color-off pair with nothing between them.
    result = colored_truncate([("abc", 51), ("", 220)], width=80)
    assert result == colored("abc", fg_color=51)


def test_colored_truncate_cuts_across_a_segment_boundary():
    # width=5, ellipsis="..." -> a 2-character budget: all of the first
    # segment ("a") plus one character of the second ("b") survive,
    # each still individually colored, followed by the ellipsis.
    result = colored_truncate([("a", 51), ("bcdef", 220)], width=5)
    assert _visible(result) == "ab..."
    assert result == colored("a", fg_color=51) + colored("b", fg_color=220) + "..."


def test_colored_truncate_never_splits_an_escape_sequence():
    # Regression check for the exact failure mode the module docstring
    # warns about: every SGR "on" code in the output must be paired with
    # a RESET, i.e. no truncation ever lands mid-escape-sequence.
    result = colored_truncate([("hello world", 51), ("more text here", 220)], width=10)
    on_codes = result.count("\x1b[38;5;")
    reset_codes = result.count("\x1b[0m")
    assert on_codes == reset_codes
    assert on_codes > 0


def test_colored_truncate_exactly_at_width_is_unchanged():
    result = colored_truncate([("abcde", 51)], width=5)
    assert result == colored("abcde", fg_color=51)


def test_colored_truncate_cuts_cjk_segments_at_a_display_column_boundary():
    """Dogfood report: international users found non-ASCII handling
    poor. Each CJK character in a segment is 2 display columns, not 1
    -- a picker row's own colored name field (netbbs.net.picker) must
    truncate at a real column boundary, not a character count."""
    # "你好" (2 chars, 4 columns) + "世界" (2 chars, 4 columns) = 8
    # columns total. width=7 forces truncation; budget 4 after "..."
    # fits exactly "你好" from the first segment, none of the second.
    result = colored_truncate([("你好", 51), ("世界", 220)], width=7)
    assert _visible(result) == "你好..."


def test_colored_truncate_width_at_or_below_ellipsis_length():
    assert colored_truncate([("hello", 51)], width=3) == "..."
    assert colored_truncate([("hello", 51)], width=2) == ".."


def test_colored_truncate_rejects_non_positive_width():
    with pytest.raises(ValueError):
        colored_truncate([("hello", 51)], width=0)


def test_colored_truncate_accepts_a_callable_segment_renderer():
    # GitHub issue #175 (node-name gradient breadcrumb segment): a
    # segment's color can be an arbitrary Callable[[str], str] instead
    # of a plain color, invoked on that segment's own plain text.
    result = colored_truncate([("abc", lambda text: f"<{text}>"), ("def", 51)], width=80)
    assert result == "<abc>" + colored("def", fg_color=51)


def test_colored_truncate_callable_segment_receives_already_truncated_text():
    # The width budget is decided against plain text first (module
    # docstring); a callable segment must only ever see the piece that
    # survived truncation, never the original full segment text.
    seen: list[str] = []

    def _render(text: str) -> str:
        seen.append(text)
        return text.upper()

    result = colored_truncate([("hello", _render), ("world", 51)], width=5)
    assert seen == ["he"]
    assert _visible(result) == "HE..."
