"""
Text reflow: wraps long text to fit a target terminal width.

Word-wrapping via `netbbs.rendering.width.wrap_to_width`, not stdlib
`textwrap` (design doc, dogfood feature request) -- `textwrap.wrap`
measures line width in characters, which undercounts every East Asian
Wide/Fullwidth character (2 real terminal columns, not 1); `wrap_to_
width` is display-column-aware instead, see that module's own
docstring for the full reasoning. This module exists to apply that
wrapping with NetBBS-appropriate defaults on top: specifically,
preserving blank-line paragraph breaks, which one `wrap_to_width()`
call over multi-paragraph text does not do on its own (it collapses
all whitespace, including intentional blank lines, uniformly, matching
`textwrap.wrap`'s own longstanding behavior here -- unchanged by the
underlying wrapper swap).
"""

from __future__ import annotations

from typing import Callable, Sequence

from netbbs.rendering.ansi import colored
from netbbs.rendering.width import cut_to_width, display_width, truncate_to_width, wrap_to_width

DEFAULT_WIDTH = 80


def reflow(text: str, width: int = DEFAULT_WIDTH) -> str:
    """
    Reflow `text` to fit `width` columns, preserving paragraph breaks.

    Splits on blank lines first, wraps each paragraph independently, then
    rejoins with blank lines restored — so intentional paragraph
    structure survives, even though within a paragraph all whitespace
    (including single line breaks) is still collapsed and rewrapped.
    """
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")

    paragraphs = text.split("\n\n")
    wrapped_paragraphs = [
        "\n".join(wrap_to_width(paragraph, width)) if paragraph.strip() else ""
        for paragraph in paragraphs
    ]
    return "\n\n".join(wrapped_paragraphs)


def truncate(text: str, width: int, *, ellipsis: str = "...") -> str:
    """
    Truncate `text` to fit within `width` display columns, appending
    `ellipsis` if truncation actually occurred.

    Unlike `reflow`, this always produces a single line, never wrapping
    — for contexts like a one-line list entry (e.g. `netbbs.net.picker`)
    where multi-line wrapping would break the list's visual structure.

    Delegates to `netbbs.rendering.width.truncate_to_width` (design
    doc, dogfood feature request) -- `width` is display columns, not
    characters, so any East Asian Wide/Fullwidth character counts as
    2, not 1.
    """
    return truncate_to_width(text, width, ellipsis=ellipsis)


#: Public alias (dogfood report, `netbbs.net.picker`'s own multi-segment
#: row rendering) -- previously private/module-internal since
#: `colored_truncate` was this module's only caller of it; a second
#: caller building its own `(text, color)` segment list ahead of time
#: needs to spell this type out too, so it's exported like every other
#: shared rendering type instead of duplicated.
SegmentColor = int | tuple[int, int, int] | None | Callable[[str], str]
_SegmentColor = SegmentColor


def _render_segment(text: str, color: _SegmentColor) -> str:
    if callable(color):
        return color(text)
    return colored(text, fg_color=color)


def colored_truncate(
    segments: Sequence[tuple[str, _SegmentColor]], width: int, *, ellipsis: str = "..."
) -> str:
    """
    Like `truncate`, but for a line built from several differently-
    colored fields (`(text, fg_color)` pairs, `fg_color=None` for
    uncolored, or a truecolor `(r, g, b)` triple -- issue #162's
    node-wide accent-color override) -- coloring each segment only
    *after* the truncation budget is decided against the plain,
    unescaped text.

    `fg_color` also accepts an arbitrary `Callable[[str], str]` instead
    of a plain color (issue #175's node-name gradient breadcrumb
    segment) -- the already-width-budgeted plain text for that segment
    is handed to the callable, which renders it however it likes (e.g.
    `netbbs.rendering.gradient.gradient_text`) instead of a single flat
    `colored()` span. Everything about the truncation budget itself is
    computed from the plain, unstyled text either way, so a gradiented
    segment truncates at the same real column boundary a solid-colored
    one would.

    Coloring first and truncating the ANSI-escaped result the way
    `truncate()` alone would is unsafe: SGR escape sequences count
    toward `truncate`'s character budget just like visible text, and a
    cut mid-sequence leaves an unterminated code that bleeds its color
    into everything printed afterward (see `colored()`'s own docstring
    on exactly that failure mode).

    `width` is display columns, not characters (design doc, dogfood
    feature request) -- each segment's own share of the budget is
    measured/cut with `netbbs.rendering.width`'s `display_width`/
    `cut_to_width`, the same primitives `truncate` itself now delegates
    to, so a multi-field row (e.g. `netbbs.net.picker`'s numbered list
    rows) truncates each colored field at a real column boundary
    instead of a character count.
    """
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")

    plain = "".join(text for text, _ in segments)
    if display_width(plain) <= width:
        return "".join(_render_segment(text, color) for text, color in segments if text)
    ellipsis_width = display_width(ellipsis)
    if width <= ellipsis_width:
        return cut_to_width(ellipsis, width)

    budget = width - ellipsis_width
    rendered: list[str] = []
    for text, color in segments:
        if budget <= 0:
            break
        piece = cut_to_width(text, budget)
        if piece:
            rendered.append(_render_segment(piece, color))
        budget -= display_width(piece)
    rendered.append(ellipsis)
    return "".join(rendered)
