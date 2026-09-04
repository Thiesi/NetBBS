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

`fg_rgb`/`bg_rgb` plus `netbbs.rendering.gradient` add a 24-bit truecolor
tier on top of this — gated per-session
(`netbbs.net.session.Session.supports_truecolor`), never assumed. 256-color
remains the safe universal baseline every existing call site continues to
target; truecolor is additive and opt-in, not a replacement for it.
"""

from __future__ import annotations

import re

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


def fg_rgb(r: int, g: int, b: int) -> str:
    """24-bit truecolor foreground SGR sequence (`38;2;r;g;b`). Each
    component is 0-255. Only safe to send to a session known to support
    truecolor — see `netbbs.net.session.Session.supports_truecolor`."""
    _validate_rgb(r, g, b)
    return f"{CSI}38;2;{r};{g};{b}m"


def bg_rgb(r: int, g: int, b: int) -> str:
    """24-bit truecolor background SGR sequence (`48;2;r;g;b`)."""
    _validate_rgb(r, g, b)
    return f"{CSI}48;2;{r};{g};{b}m"


def colored(
    text: str,
    *,
    fg_color: int | tuple[int, int, int] | None = None,
    bg_color: int | tuple[int, int, int] | None = None,
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

    `fg_color`/`bg_color` accept either a 256-color palette index (`int`,
    routed through `fg`/`bg`) or a 24-bit `(r, g, b)` tuple (routed
    through `fg_rgb`/`bg_rgb`) — this is the one recommended entry point
    for both color depths, rather than a parallel `rgb_colored()`
    function callers would have to remember to reach for instead.

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
        prefix += fg_rgb(*fg_color) if isinstance(fg_color, tuple) else fg(fg_color)
    if bg_color is not None:
        prefix += bg_rgb(*bg_color) if isinstance(bg_color, tuple) else bg(bg_color)
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


# Codex review (PR #232, then #233): an earlier version of this
# pattern only matched digit/semicolon-parameter CSI sequences and the
# bare save/restore-cursor pair -- correct for everything this
# module's own primitives can emit, but the real call site (a SysOp's
# own custom `.ans` welcome/registration banner, loaded verbatim and
# passed through this function too) can contain much richer classic
# ANSI-art syntax that narrower pattern let straight through as
# literal bytes, defeating the exact problem stripping exists to
# solve. A second round added a dedicated charset-select branch
# (`[()*+][A-Za-z0-9]`) plus a single-byte catch-all for everything
# else -- still wrong for any *other* bare ESC sequence with an
# intermediate byte before its final one (DECALN `ESC # 8`, UTF-8
# selection `ESC % G`, ...): the catch-all consumed only the
# intermediate (`#`/`%`), leaving the final byte (`8`/`G`) as visible
# garbage in the supposedly plain-text output. Now a general
# ECMA-48-shaped escape-sequence matcher, covering every form real
# ANSI-art tooling (SyncTERM, TheDraw, ACiDDraw, ...) actually emits:
#   - CSI ... final-byte, including private-mode markers like `?25l`
#     (cursor hide/show) and colon-form parameters -- `[0-?]` covers
#     the full parameter-byte range (digits, `;`, `:`, `<=>?`), not
#     just digits/`;`;
#   - OSC ... terminated by BEL or ST (window title, hyperlinks);
#   - every other escape sequence's real grammar (ECMA-48 6.3.7):
#     zero or more intermediate bytes (0x20-0x2f) followed by exactly
#     one final byte (0x30-0x7e) -- this single branch already covers
#     charset selection (`ESC ( 0` for DEC special graphics/box-
#     drawing glyphs: `(` is an intermediate, `0` the final byte),
#     multi-intermediate forms like the two examples above, and every
#     bare single-final-byte form (save/restore cursor `ESC 7`/`ESC 8`,
#     RIS reset, index/reverse-index, ...) as the zero-intermediates
#     case, with no separate enumeration needed. Tried after CSI/OSC
#     in alternation order, so a literal `[`/`]` immediately after ESC
#     is never mistaken for this branch's own final byte -- CSI/OSC's
#     dedicated branches already claim that position first.
ANSI_ESCAPE_RE = re.compile(
    ESC + r"(?:\[[0-?]*[ -/]*[@-~]" r"|\][^\x07\x1b]*(?:\x07|\x1b\\)" r"|[\x20-\x2f]*[\x30-\x7e])"
)


def strip_ansi(text: str) -> str:
    """
    Remove every ANSI/VT100 escape sequence, leaving plain text -- for
    trusted, already-composed output (e.g. a `colored()`/
    `gradient_text()` banner, or a SysOp's own custom `.ans`-file
    content) that needs to reach a display context which can't reliably
    render escape sequences at all, such as an SSH pre-auth banner
    (`SSH_MSG_USERAUTH_BANNER`, shown before any pty/terminal channel
    exists -- many clients route it through a display path that never
    runs an ANSI parser over it, dumping literal escape bytes instead
    of interpreting them, regardless of color depth chosen).

    A different concern from `netbbs.rendering.sanitize.sanitize_text`,
    which defuses *untrusted* text by stripping only the introducing
    ESC byte -- enough to prevent untrusted content from ever forming a
    sequence, but which would leave a real sequence as inert bracket-
    and-letter noise (`[38;5;208m`) rather than clean text. Never apply
    this to untrusted text expecting it to sanitize anything; it has no
    such guarantee (it only ever removes complete, well-formed escape
    sequences, not the injection risk a lone/malformed ESC byte poses).
    """
    return ANSI_ESCAPE_RE.sub("", text)


def _validate_color(color: int) -> None:
    if not 0 <= color <= 255:
        raise ValueError(f"color must be 0-255, got {color}")


def _validate_rgb(r: int, g: int, b: int) -> None:
    for name, value in (("r", r), ("g", g), ("b", b)):
        if not 0 <= value <= 255:
            raise ValueError(f"{name} must be 0-255, got {value}")
