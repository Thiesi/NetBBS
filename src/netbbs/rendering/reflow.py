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

import os
import sys
from typing import Callable, Sequence, TextIO

from netbbs.rendering.ansi import ANSI_ESCAPE_RE, colored
from netbbs.rendering.width import (
    char_width,
    cut_to_width,
    display_width,
    truncate_to_width,
    wrap_to_width,
)

DEFAULT_WIDTH = 80


def terminal_wrapped(
    text: str,
    *,
    width: int | None = None,
    stream: TextIO | None = None,
) -> str:
    """Return CLI prose wrapped for its destination stream using LF rows."""
    if width is None:
        width = _terminal_columns(stream or sys.stdout)
    return wrap_terminal_text(text, width=max(1, width)).replace("\r\n", "\n")


def print_wrapped(
    text: str,
    *,
    width: int | None = None,
    file: TextIO | None = None,
) -> None:
    """Print CLI prose within the current terminal's display width."""
    destination = file or sys.stdout
    print(terminal_wrapped(text, width=width, stream=destination), file=destination)


def _terminal_columns(stream: TextIO) -> int:
    """Measure the stream which will actually receive the text."""
    try:
        configured = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        configured = 0
    if configured > 0:
        return configured
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, OSError, ValueError):
        return DEFAULT_WIDTH


def wrap_terminal_text(text: str, width: int) -> str:
    """Wrap terminal text at display-column-aware word boundaries.

    Unlike :func:`reflow`, this accepts text after trusted ANSI styling has
    been applied.  Styling and cursor controls remain intact.  Cursor movement
    is modeled while measuring each row, including save/restore and bare
    carriage-return resets, so it cannot disguise an overflow.  Existing
    logical line breaks are preserved (and normalized to terminal CRLF); only
    an over-width logical line gains additional breaks.

    Whitespace chosen as a wrap point is removed instead of becoming a
    trailing space on the old line or a leading space on the continuation.
    A token with no usable whitespace boundary is split only as the unavoidable
    fallback needed to keep every physical line inside ``width``.  In normal
    prose that fallback is reserved for identifiers, URLs, and unspaced CJK;
    authored words must fit the supported 40-column minimum.
    """
    if width < 1:
        raise ValueError(f"width must be >= 1, got {width}")

    logical_lines = text.replace("\r\n", "\n").split("\n")
    return "\r\n".join(
        physical
        for logical in logical_lines
        for physical in _wrap_terminal_line(logical, width)
    )


def _wrap_terminal_line(text: str, width: int) -> list[str]:
    """Wrap one logical line while retaining safe ANSI behavior."""
    # raw bytes, visible character, display width, optional cursor operation
    atoms: list[tuple[str, str, int, tuple[str, int] | None]] = []
    pending_escape = ""
    leading_whitespace = True
    leading_width = 0

    def append_character(ch: str) -> None:
        nonlocal pending_escape, leading_whitespace, leading_width
        # Tabs have terminal-column semantics which depend on the current tab
        # stops.  Terminal prose treats whitespace as a word boundary, so one
        # predictable blank preserves that meaning without under-counting it.
        if ch == "\t":
            ch = " "
        width_here = char_width(ch)
        if leading_whitespace and ch.isspace():
            # Indentation must leave room for visible content.  Otherwise a
            # cursor-forward sequence or a very deep indent can manufacture
            # blank physical rows before the first token.
            if leading_width + width_here > max(0, width - 1):
                return
            leading_width += width_here
        elif not ch.isspace():
            leading_whitespace = False
        atoms.append((pending_escape + ch, ch, width_here, None))
        pending_escape = ""

    def append_control(raw: str, effect: tuple[str, int]) -> None:
        nonlocal pending_escape, leading_whitespace, leading_width
        atoms.append((pending_escape + raw, "", 0, effect))
        pending_escape = ""
        if effect[0] in ("absolute", "restore"):
            leading_whitespace = True
            leading_width = 0

    def append_text(fragment: str) -> None:
        for ch in fragment:
            if ch == "\r":
                append_control(ch, ("absolute", 0))
            else:
                append_character(ch)

    position = 0
    for match in ANSI_ESCAPE_RE.finditer(text):
        append_text(text[position : match.start()])
        control = match.group(0)
        effect = _cursor_effect(control, width)
        if effect is not None:
            append_control(control, effect)
        else:
            pending_escape += control
        position = match.end()
    append_text(text[position:])

    if not atoms:
        return [pending_escape]

    lines: list[str] = []
    start = 0
    saved_column = 0
    while start < len(atoms):
        column = 0
        candidate_saved = saved_column
        overflow = len(atoms)
        segment_start = start
        for index in range(start, len(atoms)):
            effect = atoms[index][3]
            if effect is not None:
                column, candidate_saved = _apply_cursor_effect(
                    effect,
                    column=column,
                    saved_column=candidate_saved,
                    width=width,
                )
            if effect is not None and effect[0] in ("absolute", "restore"):
                segment_start = index + 1
            next_width = column + atoms[index][2]
            if next_width > width:
                overflow = index
                break
            column = next_width

        if overflow == len(atoms):
            lines.append("".join(raw for raw, _, _, _ in atoms[start:]) + pending_escape)
            pending_escape = ""
            break

        # A space immediately after a full-width word is itself the ideal
        # boundary even though it did not fit in the measured prefix.
        whitespace = overflow if atoms[overflow][1].isspace() else None
        if whitespace is None:
            for index in range(overflow - 1, segment_start - 1, -1):
                if atoms[index][1].isspace():
                    whitespace = index
                    break

        whitespace_start = whitespace
        if whitespace is not None:
            while whitespace_start > segment_start and atoms[whitespace_start - 1][1].isspace():
                whitespace_start -= 1
            # Leading indentation is content, not a boundary before content.
            # Breaking there would emit a spurious empty physical row.
            if whitespace_start == segment_start:
                whitespace = None

        if whitespace is None:
            # One indivisible token is wider than the terminal.  There is no
            # word boundary to use, so bound the output rather than allowing
            # the terminal to hide the remainder off its right edge.
            end = max(start + 1, overflow)
            lines.append("".join(raw for raw, _, _, _ in atoms[start:end]))
            saved_column = _saved_column_after(
                atoms,
                start=start,
                end=end,
                saved_column=saved_column,
                width=width,
            )
            start = end
            continue

        whitespace_end = whitespace
        while whitespace_end < len(atoms) and atoms[whitespace_end][1].isspace():
            whitespace_end += 1

        # Keep styling codes attached to discarded boundary whitespace.  They
        # take effect at the end of this physical row and carry across CRLF,
        # preserving the style intended for the continuation without leaving
        # a visible blank at either edge.
        boundary_escapes = "".join(
            raw[: -len(ch)] if ch else raw
            for raw, ch, _, _ in atoms[whitespace_start:whitespace_end]
        )
        lines.append(
            "".join(raw for raw, _, _, _ in atoms[start:whitespace_start])
            + boundary_escapes
        )
        saved_column = _saved_column_after(
            atoms,
            start=start,
            end=whitespace_end,
            saved_column=saved_column,
            width=width,
        )

        start = whitespace_end

    if pending_escape:
        lines[-1] += pending_escape
    return lines


def _cursor_effect(sequence: str, width: int) -> tuple[str, int] | None:
    """Return a bounded horizontal cursor operation for one ANSI control."""
    if sequence == "\x1b7":
        return "save", 0
    if sequence == "\x1b8":
        return "restore", 0
    if not sequence.startswith("\x1b["):
        return None
    final = sequence[-1]
    raw_params = sequence[2:-1]
    if final in ("C", "a"):
        return "forward", _ansi_parameter(
            raw_params, 0, default=1, maximum=width
        )
    if final == "D":
        return "back", _ansi_parameter(
            raw_params, 0, default=1, maximum=width
        )
    if final in ("G", "`"):
        column = _ansi_parameter(raw_params, 0, default=1, maximum=width)
        return "absolute", column - 1
    if final in ("H", "f"):
        column = _ansi_parameter(raw_params, 1, default=1, maximum=width)
        return "absolute", column - 1
    if final in ("E", "F"):
        return "absolute", 0
    if final == "I":
        tabs = _ansi_parameter(raw_params, 0, default=1, maximum=width)
        return "forward", min(width, tabs * 8)
    if final == "Z":
        tabs = _ansi_parameter(raw_params, 0, default=1, maximum=width)
        return "back", min(width, tabs * 8)
    if final == "s" and not raw_params:
        return "save", 0
    if final == "u" and not raw_params:
        return "restore", 0
    return None


def _ansi_parameter(
    raw: str,
    index: int,
    *,
    default: int,
    maximum: int,
) -> int:
    parts = raw.split(";") if raw else []
    if index >= len(parts) or not parts[index]:
        return default
    try:
        return min(maximum, max(1, int(parts[index])))
    except ValueError:
        return default


def _apply_cursor_effect(
    effect: tuple[str, int],
    *,
    column: int,
    saved_column: int,
    width: int,
) -> tuple[int, int]:
    operation, value = effect
    if operation == "forward":
        column = min(width - 1, column + value)
    elif operation == "back":
        column = max(0, column - value)
    elif operation == "absolute":
        column = min(width - 1, value)
    elif operation == "save":
        saved_column = column
    elif operation == "restore":
        column = min(width - 1, saved_column)
    return column, saved_column


def _saved_column_after(
    atoms: list[tuple[str, str, int, tuple[str, int] | None]],
    *,
    start: int,
    end: int,
    saved_column: int,
    width: int,
) -> int:
    column = 0
    for _, _, char_columns, effect in atoms[start:end]:
        if effect is not None:
            column, saved_column = _apply_cursor_effect(
                effect,
                column=column,
                saved_column=saved_column,
                width=width,
            )
        column = min(width, column + char_columns)
    return saved_column


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
