"""
ANSI/VT100 escape sequence helpers: color, cursor control, screen
clearing.

Design doc §4/§15: the ANSI rendering framework, built now since it
benefits every existing feature (menu, boards, chat) immediately. A
future screen-buffer/diff abstraction for heavy screens like a file
browser or the fullscreen editor ("TUI") is Phase 2 scope — see design
doc round 26.

Targets 256-color / extended ANSI (SGR), per Thiesi's explicit choice —
richer than classic 16-color BBS ANSI art, at the cost of some very old
or "dumb" clients not rendering it correctly. No fallback/downgrade path
to 16-color is built here; if that turns out to matter in practice, it's
a later addition, not a Phase 1 concern.
"""

from __future__ import annotations

ESC = "\x1b"
CSI = ESC + "["  # Control Sequence Introducer

RESET = f"{CSI}0m"
BOLD = f"{CSI}1m"


def fg(color: int) -> str:
    """256-color foreground SGR sequence. `color` is 0-255 (the standard
    xterm 256-color palette)."""
    _validate_color(color)
    return f"{CSI}38;5;{color}m"


def bg(color: int) -> str:
    """256-color background SGR sequence."""
    _validate_color(color)
    return f"{CSI}48;5;{color}m"


def colored(text: str, *, fg_color: int | None = None, bg_color: int | None = None, bold: bool = False) -> str:
    """
    Wrap `text` in the given SGR codes, always resetting afterward.

    This is the recommended way to apply color/bold — not calling `fg`/
    `bg`/`BOLD` directly and forgetting to reset — since formatting that
    bleeds into whatever comes next is probably the single most common
    real-world bug with raw ANSI codes. Returns `text` unchanged if no
    formatting is requested, rather than emitting empty escape sequences.
    """
    prefix = ""
    if bold:
        prefix += BOLD
    if fg_color is not None:
        prefix += fg(fg_color)
    if bg_color is not None:
        prefix += bg(bg_color)
    if not prefix:
        return text
    return f"{prefix}{text}{RESET}"


def clear_screen() -> str:
    """Clear the entire screen and move the cursor to the home position."""
    return f"{CSI}2J{CSI}H"


def clear_line() -> str:
    """Clear the current line."""
    return f"{CSI}2K"


def move_cursor(row: int, col: int) -> str:
    """Move the cursor to an absolute (1-indexed) row/column position."""
    if row < 1 or col < 1:
        raise ValueError(f"row and col must be >= 1, got row={row}, col={col}")
    return f"{CSI}{row};{col}H"


def reject_keystroke(count: int = 1) -> str:
    """
    Erase the `count` most recently echoed characters and sound the
    bell -- the standard "that key doesn't do anything here" response
    for single-keystroke menu dispatch (`netbbs.net.char_input.
    read_key`).

    Necessary because that echo happens inside `read_key` itself, as
    each byte is read, before the caller can know whether the
    keystroke will turn out to be recognized (design doc round 52:
    "character echo is a real transport's job") -- by the time an
    unrecognized keystroke reaches a dispatch loop's `else` branch,
    its character is already on screen, with no way to have withheld
    it. Backspace, overwrite with a space, backspace again -- repeated
    `count` times for multi-character reads like a picker's two-digit
    selection -- leaves no visible trace before the bell rings,
    instead of the character piling up on screen with every rejected
    keystroke.
    """
    if count < 1:
        raise ValueError(f"count must be >= 1, got {count}")
    return ("\b \b" * count) + "\a"


def _validate_color(color: int) -> None:
    if not 0 <= color <= 255:
        raise ValueError(f"color must be 0-255, got {color}")
