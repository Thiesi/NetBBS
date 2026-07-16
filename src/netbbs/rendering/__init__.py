"""
The ANSI rendering framework (design doc §4/§15) — color/cursor/
screen-clearing helpers, text reflow, and `sanitize_text` (round 29,
issue #8) for the untrusted-content half of what reaches a terminal.

Transport-independent character-mode input lives in
`netbbs.net.char_input`, not here. The screen-buffer/diff abstraction
for heavy cursor-addressable screens ("TUI", design doc round 26)
lives in `netbbs.rendering.screen_buffer`, built alongside its first
real consumer, `netbbs.net.ansi_editor` (design doc -- welcome banner
round B1).
"""

from netbbs.rendering.ansi import (
    BOLD,
    RESET,
    REVERSE,
    bg,
    clear_line,
    clear_screen,
    colored,
    fg,
    move_cursor,
    reject_keystroke,
    reset_scroll_region,
    restore_cursor,
    save_cursor,
    set_scroll_region,
)
from netbbs.rendering.ansi_art import decode_ansi_bytes, encode_ansi_bytes
from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.menu import menu_key
from netbbs.rendering.reflow import DEFAULT_WIDTH, reflow, truncate
from netbbs.rendering.sanitize import sanitize_text
from netbbs.rendering.screen_buffer import Cell, ScreenBuffer, Snapshot, diff_ansi, full_render_ansi
from netbbs.rendering.theme import (
    ACCENT_COLOR,
    HEADER_COLOR,
    MENU_KEY_COLOR,
    MUTED_COLOR,
    NICK_COLOR,
    SELF_COLOR,
    VERIFIED_COLOR,
)

__all__ = [
    "BOLD",
    "RESET",
    "REVERSE",
    "bg",
    "clear_line",
    "clear_screen",
    "colored",
    "decode_ansi_bytes",
    "encode_ansi_bytes",
    "parse_ansi_into_buffer",
    "fg",
    "move_cursor",
    "reject_keystroke",
    "reset_scroll_region",
    "restore_cursor",
    "save_cursor",
    "set_scroll_region",
    "menu_key",
    "DEFAULT_WIDTH",
    "reflow",
    "truncate",
    "sanitize_text",
    "Cell",
    "ScreenBuffer",
    "Snapshot",
    "diff_ansi",
    "full_render_ansi",
    "ACCENT_COLOR",
    "HEADER_COLOR",
    "MENU_KEY_COLOR",
    "MUTED_COLOR",
    "NICK_COLOR",
    "SELF_COLOR",
    "VERIFIED_COLOR",
]
