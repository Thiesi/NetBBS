"""
Display-width-aware text measurement (design doc, dogfood feature
request: international users reported "extremely poor handling of
anything beyond 7-bit ASCII"). Every width calculation elsewhere in
this codebase used plain `len()` (or stdlib `textwrap`, which is
`len()`-based internally) as a stand-in for terminal columns -- correct
for ASCII, wrong for any East Asian Wide/Fullwidth character (2
columns on a real terminal, not 1) or zero-width combining mark (0
columns, not 1). A board/channel name, post subject, or bio containing
CJK text therefore truncated at the wrong point, wrapped at the wrong
column, and (see `netbbs.net.char_input`/`netbbs.net.ansi_editor`, a
separate, later piece of this same fix) visibly desynced the cursor
from where the user was actually typing.

Built entirely on stdlib `unicodedata`'s `east_asian_width`/
`combining` -- no `wcwidth`-style third-party dependency needed, and
one less thing to track down through pkgsrc for a NetBSD target
(CLAUDE.md's own external-dependency preference). This is a real,
deliberate simplification, not full Unicode conformance: it does not
attempt UAX #11's "Ambiguous" category (treated as narrow, matching
most East Asian legacy terminal conventions) or emoji-specific width
tables (`east_asian_width` alone does not correctly widen most modern
emoji -- a real, accepted gap, not silently "handled"). CJK and
combining-mark text, the reported complaint, are both covered
correctly.
"""

from __future__ import annotations

import unicodedata

_ZERO_WIDTH_CATEGORIES = frozenset({"Cc", "Cf"})
_WIDE_EAST_ASIAN = frozenset({"W", "F"})


def char_width(ch: str) -> int:
    """Display columns occupied by one character: 0 for a combining
    mark or control/format character, 2 for an East Asian Wide/
    Fullwidth character, 1 otherwise. `ch` is assumed to already be a
    single character (or empty) -- callers iterating a `str` (which
    yields one Unicode code point per step) already satisfy this."""
    if not ch:
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in _ZERO_WIDTH_CATEGORIES:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in _WIDE_EAST_ASIAN else 1


def display_width(text: str) -> int:
    """Total display columns `text` occupies -- the width-aware
    replacement for `len(text)` everywhere `len` was standing in for
    "how many terminal columns does this take.\""""
    return sum(char_width(ch) for ch in text)


def cut_to_width(text: str, width: int) -> str:
    """The longest prefix of `text` whose `display_width` does not
    exceed `width` -- a character-by-character walk, not a slice
    (`text[:n]` assumes one column per character, the exact assumption
    this module exists to stop making). Public (unlike `truncate_to_
    width`'s ellipsis handling, which is specific to that one use)
    because `netbbs.rendering.reflow.colored_truncate` needs this same
    bare per-segment cut, with no ellipsis of its own -- the ellipsis
    there is a separate, final segment appended once, after every
    colored field's own budget is already spent."""
    total = 0
    cut = 0
    for ch in text:
        w = char_width(ch)
        if total + w > width:
            break
        total += w
        cut += 1
    return text[:cut]


def wrap_to_width(text: str, width: int, *, break_long_words: bool = True) -> list[str]:
    """
    Greedy word-wrap using display columns, not stdlib `textwrap`'s
    character count. Splits on whitespace the same way `textwrap.wrap`
    does, but measures each word against `display_width` instead of
    `len`, and rejoins a wrapped line's words with single spaces (the
    same internal-whitespace normalization `textwrap.wrap` already
    applies -- `netbbs.rendering.prose_buffer.wrap_lines`'s own
    `line.index(segment, col)` re-location trick already accounts for
    this, unaffected by which wrapping function produced the segments).

    A single whitespace-delimited "word" wider than `width` itself (a
    long URL, or -- the case that actually matters here -- an entire
    run of CJK text, since that script doesn't use spaces between
    words at all) is hard-broken at a real display-column boundary
    instead of overflowing the line, by default. That fallback is what
    makes CJK wrap correctly with no script-specific line-breaking
    logic of its own: an unspaced CJK paragraph is just one long "word"
    under this splitting rule, and breaking between any two characters
    at the width boundary is already the linguistically correct
    behavior for that script -- not an approximation adopted for lack
    of a better option.

    `break_long_words=False` (same name/meaning as stdlib `textwrap`'s
    own parameter) turns that fallback off: an over-width word is
    instead emitted whole, on its own line, overflowing `width` rather
    than being split. It is retained for non-terminal formatting where
    preserving an indivisible token matters more than the requested width;
    terminal-facing output must leave this at its bounded default because
    off-screen text is invisible to the caller."""
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")

    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current: list[str] = []
    current_width = 0
    for word in words:
        word_width = display_width(word)
        if word_width > width:
            if current:
                lines.append(" ".join(current))
                current = []
                current_width = 0
            if not break_long_words:
                lines.append(word)
                continue
            while display_width(word) > width:
                piece = cut_to_width(word, width)
                if not piece:
                    # Even this word's first character alone exceeds
                    # `width` (e.g. one CJK character, width=1) -- take
                    # it anyway rather than looping forever; the line
                    # unavoidably overflows by one character's width,
                    # the same "can't split a character in half"
                    # reality `wrap_to_width`'s own callers already
                    # accept elsewhere for a too-narrow budget.
                    piece = word[:1]
                lines.append(piece)
                word = word[len(piece):]
            if word:
                current = [word]
                current_width = display_width(word)
            continue

        separator_width = 1 if current else 0
        if current and current_width + separator_width + word_width > width:
            lines.append(" ".join(current))
            current = []
            current_width = 0
            separator_width = 0
        current.append(word)
        current_width += separator_width + word_width
    if current:
        lines.append(" ".join(current))
    return lines


def truncate_to_width(text: str, width: int, *, ellipsis: str = "...") -> str:
    """Truncate `text` to fit within `width` display columns, appending
    `ellipsis` if truncation actually occurred -- the width-aware
    counterpart to `netbbs.rendering.reflow.truncate`, same shape and
    edge-case handling (a `width` too narrow even for `ellipsis` alone
    truncates `ellipsis` itself), just measured in columns rather than
    characters."""
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")
    if display_width(text) <= width:
        return text
    ellipsis_width = display_width(ellipsis)
    if width <= ellipsis_width:
        return cut_to_width(ellipsis, width)
    return cut_to_width(text, width - ellipsis_width) + ellipsis
