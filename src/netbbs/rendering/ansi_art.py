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
# SGR parameters that make a space visible: a background colour (40-48,
# 100-107) or reverse video (7).
_VISIBLE_BLANK_SGR = {7, *range(40, 49), *range(100, 108)}


def _row_is_blank(row: str) -> bool:
    """Whether `row` shows nothing: only spaces once its escape sequences
    are removed, and no background colour or reverse video that would
    paint those spaces."""
    if _CSI.sub("", row).strip(" \t\r"):
        return False
    for params, final in _CSI.findall(row):
        if final != "m":
            continue
        values = params.split(";")
        index = 0
        while index < len(values):
            value = values[index]
            if value.isdigit() and int(value) in (38, 48):
                # 38/48;5;n and 38/48;2;r;g;b carry their own arguments.
                if int(value) == 48:
                    return False
                index += 3 if index + 1 < len(values) and values[index + 1] == "5" else 5
                continue
            if value.isdigit() and int(value) in _VISIBLE_BLANK_SGR:
                return False
            index += 1
    return True


def trim_trailing_blank_rows(text: str) -> str:
    """`text` without the empty rows at its end (issue #841).

    The art editor saves its whole canvas, 24 rows, blank ones included, so
    a seven-line signup banner arrived with 17 empty rows under it and
    scrolled its own text off an 80x25 screen before the caller could read
    it. A row is empty when it holds only spaces and escape sequences that
    paint nothing (no background colour, no reverse video). Rows inside the
    art are kept, however empty: only the tail goes."""
    rows = text.split("\n")
    while rows and _row_is_blank(rows[-1]):
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
