"""
Tests for `netbbs.rendering.width` -- display-column-aware text
measurement (design doc, dogfood feature request: international
users reported poor handling of anything beyond 7-bit ASCII).
"""

from __future__ import annotations

import pytest

from netbbs.rendering.width import char_width, display_width, truncate_to_width, wrap_to_width

# "Hello" romanized greeting in Chinese -- 4 CJK characters, each 2
# columns wide on a real terminal.
_CJK = "你好世界"  # 你好世界


def test_char_width_of_ascii_is_one():
    assert char_width("a") == 1
    assert char_width(" ") == 1


def test_char_width_of_empty_string_is_zero():
    assert char_width("") == 0


def test_char_width_of_cjk_character_is_two():
    for ch in _CJK:
        assert char_width(ch) == 2


def test_char_width_of_combining_mark_is_zero():
    # "e" + COMBINING ACUTE ACCENT (U+0301) -- two code points forming
    # one visual "é", the accent contributing no width of its own.
    e, accent = "é"
    assert char_width(e) == 1
    assert char_width(accent) == 0


def test_char_width_of_control_character_is_zero():
    assert char_width("\x01") == 0


def test_display_width_of_pure_ascii_matches_len():
    text = "hello world"
    assert display_width(text) == len(text)


def test_display_width_of_cjk_text_is_double_len():
    assert display_width(_CJK) == len(_CJK) * 2


def test_display_width_of_mixed_ascii_and_cjk():
    text = "hi " + _CJK  # 3 ASCII columns + 8 CJK columns
    assert display_width(text) == 3 + 8


def test_display_width_ignores_combining_marks():
    base = "café"  # "cafe" + combining acute, reads as "café"
    assert display_width(base) == 4  # not 5


def test_truncate_to_width_pure_ascii_matches_old_len_based_behavior():
    assert truncate_to_width("hello world", 8) == "hello..."
    assert truncate_to_width("short", 20) == "short"


def test_truncate_to_width_rejects_a_width_below_one():
    with pytest.raises(ValueError):
        truncate_to_width("hello", 0)


def test_truncate_to_width_cuts_at_a_display_column_boundary_not_a_character_count():
    # _CJK is 4 characters, 8 display columns total. width=7 forces
    # real truncation; budget is 7 - 3 (for "...") = 4 columns, which
    # at 2 columns/character fits exactly 2 characters -- not the 4 a
    # naive len()-based `text[:4]` slice would have kept (that would
    # still be "truncating" nothing at all, since len(_CJK) is already
    # only 4).
    result = truncate_to_width(_CJK, 7)
    assert result == _CJK[:2] + "..."
    assert display_width(result) <= 7


def test_truncate_to_width_never_returns_something_wider_than_the_budget():
    for width in range(1, 12):
        result = truncate_to_width(_CJK, width)
        assert display_width(result) <= width


def test_truncate_to_width_with_a_width_too_narrow_for_the_ellipsis_itself():
    result = truncate_to_width("hello world", 2)
    assert result == ".."
    assert display_width(result) == 2


def test_truncate_to_width_no_truncation_needed_returns_the_original_text():
    text = _CJK  # width 8
    assert truncate_to_width(text, 8) == text
    assert truncate_to_width(text, 100) == text


# -- wrap_to_width -------------------------------------------------------


def test_wrap_to_width_rejects_a_width_below_one():
    with pytest.raises(ValueError):
        wrap_to_width("hello", 0)


def test_wrap_to_width_of_empty_text_is_no_lines():
    assert wrap_to_width("", 10) == []
    assert wrap_to_width("   ", 10) == []


def test_wrap_to_width_ascii_matches_textwraps_own_behavior():
    import textwrap

    text = "the quick brown fox jumps over the lazy dog"
    assert wrap_to_width(text, 15) == textwrap.wrap(text, width=15)


def test_wrap_to_width_never_produces_a_line_wider_than_the_budget_for_ascii():
    text = "a fairly long sentence with several words of varying length"
    for width in range(3, 20):
        for line in wrap_to_width(text, width):
            assert display_width(line) <= width


def test_wrap_to_width_breaks_cjk_text_at_display_column_boundaries():
    # No spaces at all -- one long "word" under the whitespace-split
    # rule -- must still wrap, not overflow into one unbroken line.
    text = _CJK * 3  # 12 characters, 24 display columns, no spaces
    lines = wrap_to_width(text, 8)
    assert len(lines) > 1
    for line in lines:
        assert display_width(line) <= 8
    # Every character survives, in order, none dropped or duplicated.
    assert "".join(lines) == text


def test_wrap_to_width_a_single_wide_character_narrower_than_budget_still_fits_alone():
    lines = wrap_to_width("你", 1)
    # Width 1 can't fit a 2-column character at all -- the documented
    # "take it anyway rather than looping forever" fallback -- but it
    # must still terminate and return the character, not raise or hang.
    assert lines == ["你"]


def test_wrap_to_width_mixed_ascii_and_cjk_wraps_by_words_where_possible():
    text = "hello " + _CJK
    lines = wrap_to_width(text, 6)
    assert lines[0] == "hello"
    for line in lines:
        assert display_width(line) <= 6


def test_wrap_to_width_break_long_words_false_overflows_instead_of_cutting():
    """Dogfood report: a filesystem path embedded in an otherwise-
    wrappable sentence (or standing alone) got corrupted by the default
    hard-break fallback -- correct for unspaced CJK prose, but a broken
    path is worse than one overflowing line for content meant to be
    read or copy-pasted intact."""
    path = "/" + "x" * 40
    lines = wrap_to_width(f"See {path} for details", 20, break_long_words=False)
    assert path in lines
    # Every other word still wraps normally around the unbroken token.
    assert "".join(lines).replace(" ", "") == f"See{path}fordetails".replace(" ", "")


def test_wrap_to_width_break_long_words_false_is_not_the_default():
    """The default stays exactly as before -- CJK and other genuinely
    unspaced-script content still needs the hard-break fallback."""
    text = _CJK * 3
    assert wrap_to_width(text, 8) == wrap_to_width(text, 8, break_long_words=True)
    assert len(wrap_to_width(text, 8, break_long_words=False)) == 1
