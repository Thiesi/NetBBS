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

from netbbs.digits import is_ascii_number
from netbbs.rendering.ansi import BOLD, RESET
from netbbs.rendering.ansi import bg as ansi_bg
from netbbs.rendering.ansi import bg_rgb as ansi_bg_rgb
from netbbs.rendering.ansi import fg as ansi_fg
from netbbs.rendering.ansi import fg_rgb as ansi_fg_rgb
from netbbs.rendering.charset import art_glyphs_to_cp437_controls, art_pictographs_to_glyphs
from netbbs.rendering.screen_buffer import ScreenBuffer
from netbbs.rendering.sauce import Sauce, split_sauce

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
    return decode_art_bytes(data)[0]


def decode_art_bytes(data: bytes) -> tuple[str, Sauce | None]:
    """`decode_ansi_bytes` that also returns the file's SAUCE record.

    The record, its comment block and the EOF byte before them are removed
    first (`netbbs.rendering.sauce.split_sauce`), so scene art no longer
    shows its title, author and group as junk under the picture. A file
    with a SAUCE record is classic ANSI art and is read as CP437 without
    the UTF-8 attempt; one without keeps the UTF-8-then-CP437 guess.

    The pictographs of CP437's control range (☺ ♥ ♫ ► and the rest,
    `netbbs.rendering.charset.ART_PICTOGRAPHS`) become the glyphs they draw,
    not control characters that would reach a UTF-8 terminal as raw control
    bytes. That holds for UTF-8 art too: in art those bytes have no other
    use, and a file of plain ASCII plus pictograph bytes is valid UTF-8."""
    body, sauce = split_sauce(data)
    if sauce is not None:
        return decode_cp437_art(body), sauce
    try:
        return art_pictographs_to_glyphs(body.decode("utf-8")), None
    except UnicodeDecodeError:
        return decode_cp437_art(body), None


def decode_cp437_art(data: bytes) -> str:
    """CP437 art bytes as text, control-range pictographs included."""
    return art_pictographs_to_glyphs(data.decode("cp437"))


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
        value = int(values[index]) if is_ascii_number(values[index]) else 0
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


_SGR_OR_CHAR = re.compile(r"\x1b\[[0-9;?]*[@-~]|.", re.DOTALL)


def _trim_row_end(row: str, painting: bool) -> tuple[str, bool]:
    """`row` without the plain spaces at its end, and whether spaces are
    painted once the row is over. Styling codes after the last kept
    character stay, because they carry into the next row."""
    cut = 0
    position = 0
    for token in _SGR_OR_CHAR.findall(row):
        position += len(token)
        match = _CSI.fullmatch(token)
        if match is not None and match.group(2) == "m":
            painting = _sgr_paints_spaces(match.group(1), painting)
            continue
        if token in (" ", "\t") and not painting:
            continue
        # Visible content, a painted blank, or a control (a cursor move, a
        # carriage return) that the art may rely on.
        cut = position
    tail = "".join(token for token in _SGR_OR_CHAR.findall(row[cut:]) if _CSI.fullmatch(token))
    return row[:cut] + tail, painting


def trim_row_ends(text: str) -> str:
    """`text` with each row's trailing plain spaces removed (issue #964).

    The art editor saves every row of its 80-column canvas in full, so a
    60-column banner reached the caller as rows of exactly 80 columns. A
    terminal that wraps as soon as it writes the last column (SyncTERM,
    DOS ANSI.SYS) then turned each row's CR LF into a second line break,
    double-spacing the art. A space painted by a background colour or
    reverse video is kept, including one painted by a colour set on an
    earlier row, as `trim_trailing_blank_rows` decides it."""
    rows = text.split("\n")
    painting = False
    trimmed: list[str] = []
    for row in rows:
        carriage_return = row.endswith("\r")
        body, painting = _trim_row_end(row[:-1] if carriage_return else row, painting)
        trimmed.append(body + ("\r" if carriage_return else ""))
    return "\n".join(trimmed)


def decode_banner_bytes(data: bytes) -> str:
    """`decode_ansi_bytes` for art shown as a banner or masthead: decoded,
    trimmed of the plain spaces at the end of each row (`trim_row_ends`)
    and of the empty rows at its end (`trim_trailing_blank_rows`)."""
    return trim_trailing_blank_rows(trim_row_ends(decode_ansi_bytes(data)))


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
    return encode_cp437_art("".join(parts))


def encode_cp437_art(text: str) -> bytes:
    """Art text as CP437 bytes, its control-range pictographs as the bytes
    that draw them. Never raises: what CP437 lacks becomes "?"."""
    return art_glyphs_to_cp437_controls(text).encode("cp437", errors="replace")
