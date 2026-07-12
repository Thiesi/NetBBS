"""
Text reflow: wraps long text to fit a target terminal width.

Uses Python's stdlib `textwrap` rather than a hand-rolled wrapper —
word-wrapping correctly (hyphenation edge cases, not breaking mid-word,
etc.) is a solved problem with no reason to reinvent it. This module
exists to apply stdlib `textwrap` with NetBBS-appropriate defaults:
specifically, preserving blank-line paragraph breaks, which a single
`textwrap.wrap()` call over multi-paragraph text does not do on its own
(it collapses all whitespace, including intentional blank lines,
uniformly).
"""

from __future__ import annotations

import textwrap

DEFAULT_WIDTH = 80


def reflow(text: str, width: int = DEFAULT_WIDTH) -> str:
    """
    Reflow `text` to fit `width` columns, preserving paragraph breaks.

    Splits on blank lines first, wraps each paragraph independently, then
    rejoins with blank lines restored — so intentional paragraph
    structure survives, even though within a paragraph all whitespace
    (including single line breaks) is still collapsed and rewrapped, the
    normal `textwrap` behavior.
    """
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")

    paragraphs = text.split("\n\n")
    wrapped_paragraphs = [
        "\n".join(textwrap.wrap(paragraph, width=width)) if paragraph.strip() else ""
        for paragraph in paragraphs
    ]
    return "\n\n".join(wrapped_paragraphs)
