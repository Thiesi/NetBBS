"""Tests for netbbs.rendering.reflow."""

from __future__ import annotations

import pytest

from netbbs.rendering.reflow import reflow


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
