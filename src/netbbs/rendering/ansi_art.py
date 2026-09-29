"""
Decoding/encoding externally-authored ANSI art (design doc -- welcome
banner rounds A and B1): pure `bytes <-> ScreenBuffer` conversion, no
I/O, matching this package's existing boundary (nothing else in
`netbbs.rendering` touches `Database` or the filesystem).

`decode_ansi_bytes`'s output is trusted, SysOp-authored content, at the
same trust tier as `netbbs.rendering.ansi.colored()` output -- it is
meant to bypass `netbbs.rendering.sanitize.sanitize_text` entirely, not
pass through it. `sanitize_text` strips every Unicode "Control"
category character, including ESC (U+001B), which is exactly what real
ANSI art needs to keep (cursor positioning, SGR color codes). Do not
route this content through `sanitize_text` -- doing so would silently
strip every escape sequence and destroy the art.
"""

from __future__ import annotations

import re

from netbbs.rendering.ansi import BOLD, RESET
from netbbs.rendering.ansi import bg as ansi_bg
from netbbs.rendering.ansi import bg_rgb as ansi_bg_rgb
from netbbs.rendering.ansi import fg as ansi_fg
from netbbs.rendering.ansi import fg_rgb as ansi_fg_rgb
from netbbs.rendering.screen_buffer import ScreenBuffer

_Color = int | tuple[int, int, int]


def decode_ansi_bytes(data: bytes) -> str:
    """
    Decode raw ANSI art file content to text.

    Tries UTF-8 first; falls back to CP437 (the classic PC/MS-DOS code
    page real scene-authored `.ans` files are almost always encoded
    in) on failure. `cp437` is a total function over all 256 byte
    values -- every byte decodes to *some* code point, it never raises
    -- so once the fallback is reached, this function cannot fail. A
    genuine scene `.ans` file will almost always fail strict UTF-8
    decoding (its high-bit bytes rarely form valid UTF-8 sequences by
    chance), making the try/fallback a reliable, deterministic
    heuristic; a SysOp who directly authors valid UTF-8/Unicode content
    gets that path instead, automatically.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp437")


_CSI = re.compile(r"\x1b\[([0-9;?]*)([@-~])")


def _sgr_paints_spaces(params: str, painting: bool) -> bool:
    """Whether spaces are visible after the SGR sequence `params`, given
    whether they were before: a background colour or reverse video paints
    them, a reset or the default background (49) / no-reverse (27) stops
    that. Background and reverse are tracked as one flag, which errs
    towards keeping a row."""
    values = params.split(";") if params else ["0"]
    index = 0
    while index < len(values):
        value = int(values[index]) if values[index].isdigit() else 0
        if value in (38, 48):
            # 38/48;5;n and 38/48;2;r;g;b carry their own arguments.
            if value == 48:
                painting = True
            index += 3 if index + 1 < len(values) and values[index + 1] == "5" else 5
            continue
        if value == 0:
            painting = False
        elif value == 7 or 40 <= value <= 47 or 100 <= value <= 107:
            painting = True
        elif value in (27, 49):
            painting = False
        index += 1
    return painting


def _blank_rows(rows: list[str]) -> list[bool]:
    """For each row, whether it shows nothing: only spaces once its escape
    sequences are removed, and never painted by a background colour or
    reverse video -- including one set on an earlier row and still in
    force, as art from TheDraw or PabloDraw leaves it (review on #889)."""
    blank: list[bool] = []
    painting = False
    for row in rows:
        painted = painting
        for params, final in _CSI.findall(row):
            if final == "m":
                painting = _sgr_paints_spaces(params, painting)
                painted = painted or painting
        blank.append(not painted and not _CSI.sub("", row).strip(" \t\r"))
    return blank


def trim_trailing_blank_rows(text: str) -> str:
    """`text` without the empty rows at its end (issue #841).

    The art editor saves its whole canvas, 24 rows, blank ones included, so
    a seven-line signup banner arrived with 17 empty rows under it and
    scrolled its own text off an 80x25 screen before the caller could read
    it. A row is empty when it holds only spaces and escape sequences that
    paint nothing (no background colour, no reverse video). Rows inside the
    art are kept, however empty: only the tail goes."""
    rows = text.split("\n")
    blank = _blank_rows(rows)
    while rows and blank[len(rows) - 1]:
        rows.pop()
    return "\n".join(rows).rstrip("\r")


def decode_banner_bytes(data: bytes) -> str:
    """`decode_ansi_bytes` for art shown as a banner or masthead: decoded,
    then trimmed of the empty rows at its end (`trim_trailing_blank_rows`)."""
    return trim_trailing_blank_rows(decode_ansi_bytes(data))


def encode_ansi_bytes(buffer: ScreenBuffer) -> bytes:
    """
    The save-side counterpart to `decode_ansi_bytes` (design doc --
    welcome banner): walks `buffer` row by row, emitting a
    real SGR color-change sequence only where the style actually
    changes between adjacent cells (not per-cell, for a reasonably
    compact file), each character CP437-encoded with
    `errors="replace"` -- always succeeds (CP437 has no encode failure
    with that error mode, matching `decode_ansi_bytes`'s own "cannot
    fail by construction" property), producing a genuine CP437-encoded
    `.ans` file real scene tools/viewers expect, not merely a file that
    happens to display correctly in this project's own xterm.js/
    character-mode clients.
    """
    parts: list[str] = []
    current_style: tuple[_Color | None, _Color | None, bool] | None = None
    for row in buffer.snapshot():
        for cell in row:
            style = (cell.fg, cell.bg, cell.bold)
            if style != current_style:
                parts.append(RESET)
                if style[2]:
                    parts.append(BOLD)
                if style[0] is not None:
                    parts.append(ansi_fg_rgb(*style[0]) if isinstance(style[0], tuple) else ansi_fg(style[0]))
                if style[1] is not None:
                    parts.append(ansi_bg_rgb(*style[1]) if isinstance(style[1], tuple) else ansi_bg(style[1]))
                current_style = style
            parts.append(cell.char)
        parts.append("\r\n")
        current_style = None  # each row starts fresh so a mid-row style isn't assumed carried over
    parts.append(RESET)
    return "".join(parts).encode("cp437", errors="replace")
