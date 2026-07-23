"""
Rasterizing externally-authored ANSI text into a `ScreenBuffer`
(design doc -- welcome banner). Pure, no I/O.

`netbbs.rendering.ansi_art.decode_ansi_bytes` only ever does byte -> str
decoding -- it never interprets a file's embedded cursor-positioning/
SGR-color escape sequences, since displaying a banner only ever needed
to hand the decoded text to a real terminal emulator and let it
interpret the codes. Editing an existing file needs the server side to
actually know what's in each cell, which this module provides.

Deliberately a minimal, best-effort interpreter, not a full VT100/xterm
emulator -- scoped the same honest way this project's own Zmodem
implementation is ("CRC-16 only, no resume, no batch," stated plainly
rather than silently gapped): recognizes the CSI cursor-movement and
SGR sequences real scene `.ans` art actually uses, plus bare CR/LF.
Anything else is skipped rather than raised -- a file this parser
mishandles still can't corrupt anything irrecoverably, since the
original bytes on disk are untouched until the SysOp explicitly saves
from inside the editor.
"""

from __future__ import annotations

from netbbs.rendering.screen_buffer import ScreenBuffer


def parse_ansi_into_buffer(text: str, buffer: ScreenBuffer) -> None:
    """Walk `text`, writing decoded characters into `buffer` cell by
    cell while tracking a virtual cursor position and current SGR
    (fg/bg/bold) state. `buffer` is written into in place, starting
    from wherever its cursor-equivalent state begins (row 0, col 0) --
    callers wanting a clean slate should call `buffer.clear()` first."""
    row = 0
    col = 0
    fg: int | None = None
    bg: int | None = None
    bold = False
    # Real terminals defer wrapping past the last column until another
    # character actually needs to be placed ("deferred wrap"/auto-margin
    # behavior) rather than physically advancing the cursor the instant
    # the last column is written. Without this, a full-width row (real
    # scene `.ans` art is almost always exactly 80 columns, i.e. every
    # row ends right at the last column) immediately followed by an
    # explicit CRLF -- extremely common, not an edge case -- would
    # double-advance: once from the eager wrap, once more from the
    # CRLF, silently skipping a row. Any explicit cursor move (CR/LF/
    # CSI positioning) clears this flag with no effect, exactly as a
    # real terminal's pending wrap is superseded by an explicit move.
    pending_wrap = False

    i = 0
    length = len(text)
    while i < length:
        char = text[i]

        if char == "\x1b" and i + 1 < length and text[i + 1] == "[":
            end = _find_csi_final_byte(text, i + 2)
            if end is None:
                i += 1  # a lone/unterminated ESC[ -- skip just the ESC and continue
                continue
            params_str = text[i + 2 : end]
            final = text[end]
            params = _parse_params(params_str)
            i = end + 1

            if final in ("H", "f"):
                target_row = (params[0] if params and params[0] else 1) - 1
                target_col = (params[1] if len(params) > 1 and params[1] else 1) - 1
                row, col = max(0, target_row), max(0, target_col)
                pending_wrap = False
            elif final == "A":
                row = max(0, row - (params[0] if params and params[0] else 1))
                pending_wrap = False
            elif final == "B":
                row += params[0] if params and params[0] else 1
                pending_wrap = False
            elif final == "C":
                col += params[0] if params and params[0] else 1
                pending_wrap = False
            elif final == "D":
                col = max(0, col - (params[0] if params and params[0] else 1))
                pending_wrap = False
            elif final == "J" and (not params or params[0] in (0, 2)):
                buffer.clear()
                row, col = 0, 0
                pending_wrap = False
            elif final == "m":
                fg, bg, bold = _apply_sgr(params, fg, bg, bold)
            # Any other final byte (K, other cursor moves, etc.) is
            # recognized-but-ignored -- consumed above, no effect.
            continue

        if char == "\r":
            col = 0
            pending_wrap = False
            i += 1
            continue
        if char == "\n":
            row += 1
            pending_wrap = False
            i += 1
            continue
        if char == "\x1b":
            i += 1  # a lone ESC not starting a CSI sequence -- skip it
            continue

        if pending_wrap:
            col = 0
            row += 1
            pending_wrap = False

        if row < buffer.height and col < buffer.width:
            buffer.write_cell(row, col, char, fg=fg, bg=bg, bold=bold)
        if col >= buffer.width - 1:
            pending_wrap = True
        else:
            col += 1
        i += 1


def _find_csi_final_byte(text: str, start: int) -> int | None:
    """CSI final bytes are 0x40-0x7E; everything before that in a
    well-formed sequence is parameter/intermediate bytes (digits,
    `;`, etc. for what this parser recognizes). Returns the index of
    the final byte, or `None` if the string ends first."""
    i = start
    while i < len(text):
        if "\x40" <= text[i] <= "\x7e":
            return i
        i += 1
    return None


def _parse_params(params_str: str) -> list[int]:
    if not params_str:
        return []
    params: list[int] = []
    for part in params_str.split(";"):
        try:
            params.append(int(part))
        except ValueError:
            params.append(0)
    return params


def _apply_sgr(
    params: list[int], fg: int | None, bg: int | None, bold: bool
) -> tuple[int | None, int | None, bool]:
    if not params:
        params = [0]
    i = 0
    while i < len(params):
        code = params[i]
        if code == 0:
            fg, bg, bold = None, None, False
        elif code == 1:
            bold = True
        elif code == 22:
            bold = False
        elif 30 <= code <= 37:
            fg = code - 30
        elif 90 <= code <= 97:
            fg = code - 90 + 8
        elif code == 39:
            fg = None
        elif 40 <= code <= 47:
            bg = code - 40
        elif 100 <= code <= 107:
            bg = code - 100 + 8
        elif code == 49:
            bg = None
        elif code == 38 and i + 2 < len(params) and params[i + 1] == 5:
            fg = params[i + 2]
            i += 2
        elif code == 48 and i + 2 < len(params) and params[i + 1] == 5:
            bg = params[i + 2]
            i += 2
        i += 1
    return fg, bg, bold
