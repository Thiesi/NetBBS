"""
ANSI/VT100 escape sequence helpers: color, cursor control, screen
clearing.

Design doc §4/§15: the ANSI rendering framework, built now since it
benefits every existing feature (menu, boards, chat) immediately. A
future screen-buffer/diff abstraction for heavy screens like a file
browser or the fullscreen editor ("TUI") is Phase 2 scope — see the
design doc.

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
UNDERLINE = f"{CSI}4m"
REVERSE = f"{CSI}7m"


def fg(color: int) -> str:
    """256-color foreground SGR sequence. `color` is 0-255 (the standard
    xterm 256-color palette)."""
    _validate_color(color)
    return f"{CSI}38;5;{color}m"


def bg(color: int) -> str:
    """256-color background SGR sequence."""
    _validate_color(color)
    return f"{CSI}48;5;{color}m"


def colored(
    text: str,
    *,
    fg_color: int | None = None,
    bg_color: int | None = None,
    bold: bool = False,
    reverse: bool = False,
    underline: bool = False,
) -> str:
    """
    Wrap `text` in the given SGR codes, always resetting afterward.

    This is the recommended way to apply color/bold/reverse/underline —
    not calling `fg`/`bg`/`BOLD`/`REVERSE`/`UNDERLINE` directly and
    forgetting to reset — since formatting that bleeds into whatever
    comes next is probably the single most common real-world bug with
    raw ANSI codes. Returns `text` unchanged if no formatting is
    requested, rather than emitting empty escape sequences.

    `reverse` (SGR 7, design doc) swaps foreground/background
    at the terminal level rather than picking specific colors for
    both — the chat status line originally used this so it read as a
    solid, inverted bar regardless of whatever the client's own default
    foreground/background happen to be, the same reason real terminal
    status lines (tmux, screen, IRC clients) use reverse video rather
    than a hardcoded color pair.

    `underline` (SGR 4) is what the status line redesign replaced that
    solid reverse-video bar with (Thiesi's own explicit choice, over
    invented background-color banding) — chosen specifically because,
    unlike `reverse`, it composes with a *different* `fg_color` per
    call and still reads as one continuous rule once several `colored()`
    calls for adjacent fields are concatenated, rather than each field
    fighting over one shared inverted background.
    """
    prefix = ""
    if bold:
        prefix += BOLD
    if underline:
        prefix += UNDERLINE
    if reverse:
        prefix += REVERSE
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


def set_scroll_region(top: int, bottom: int) -> str:
    """
    DECSTBM (`CSI {top};{bottom} r`) — confines *ordinary* scrolling
    (a newline written past the bottom of the screen) to rows `top`
    through `bottom` (1-indexed, inclusive), leaving anything outside
    that range untouched by it. The chat status line (design doc)
    is the first consumer: excluding the terminal's last row from
    the region keeps a status line pinned there while ordinary chat
    text scrolls normally within the rest of the screen — the same
    mechanism real BBS/IRC status bars and tools like `tmux` use, not
    a repaint-after-every-line trick.

    A cursor move to any row, including inside the excluded region, is
    still possible via `move_cursor` regardless of the active
    region — DECSTBM only affects what *scrolling* touches, not
    direct addressing. Must be paired with `reset_scroll_region()`
    before returning control to any other screen; a caller that exits
    without resetting leaves every subsequent screen scrolling inside
    the same shrunk region, an easy-to-miss bug with no `move_cursor`
    call anywhere near it to make it obvious.
    """
    if top < 1 or bottom < top:
        raise ValueError(f"top must be >= 1 and <= bottom, got top={top}, bottom={bottom}")
    return f"{CSI}{top};{bottom}r"


def reset_scroll_region() -> str:
    """Restores the scroll region to the whole screen — see
    `set_scroll_region`'s docstring for why every caller that narrows
    the region must call this before giving up control of the
    session."""
    return f"{CSI}r"


def save_cursor() -> str:
    """DEC save-cursor (`ESC 7`) — the classic VT100 sequence, not the
    ANSI.SYS `CSI s` variant, for the widest real-terminal support.
    Saves position *and* character attributes; paired with
    `restore_cursor()` so a caller can jump elsewhere (e.g. to repaint
    the chat status line's pinned row) and return to exactly where
    the user was typing without disturbing it."""
    return f"{ESC}7"


def restore_cursor() -> str:
    """The other half of `save_cursor()` (`ESC 8`)."""
    return f"{ESC}8"


def reject_keystroke(count: int = 1) -> str:
    """
    Erase the `count` most recently echoed characters and sound the
    bell -- the standard "that key doesn't do anything here" response
    for single-keystroke menu dispatch (`netbbs.net.char_input.
    read_key`).

    Necessary because that echo happens inside `read_key` itself, as
    each byte is read, before the caller can know whether the
    keystroke will turn out to be recognized (design doc:
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
