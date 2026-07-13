"""
The ANSI rendering framework (design doc §4/§15) — color/cursor/
screen-clearing helpers plus text reflow.

Transport-independent character-mode input lives in
`netbbs.net.char_input`, not here. A future screen-buffer/diff
abstraction for heavy cursor-addressable screens ("TUI") is Phase 2
scope, alongside the fullscreen editor it's actually needed for — see
design doc round 26.
"""

from netbbs.rendering.ansi import (
    BOLD,
    RESET,
    bg,
    clear_line,
    clear_screen,
    colored,
    fg,
    move_cursor,
)
from netbbs.rendering.menu import menu_key
from netbbs.rendering.reflow import DEFAULT_WIDTH, reflow, truncate
from netbbs.rendering.theme import ACCENT_COLOR, HEADER_COLOR, MENU_KEY_COLOR, MUTED_COLOR, SELF_COLOR

__all__ = [
    "BOLD",
    "RESET",
    "bg",
    "clear_line",
    "clear_screen",
    "colored",
    "fg",
    "move_cursor",
    "menu_key",
    "DEFAULT_WIDTH",
    "reflow",
    "truncate",
    "ACCENT_COLOR",
    "HEADER_COLOR",
    "MENU_KEY_COLOR",
    "MUTED_COLOR",
    "SELF_COLOR",
]
