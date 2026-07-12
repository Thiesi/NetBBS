"""
The "ANSI half" of the hybrid ANSI/TUI rendering framework (design doc
§4/§15) — color/cursor/screen-clearing helpers plus text reflow.

The "TUI half" (character-mode input, screen-buffer diffing for heavy
screens) is deliberately not part of this package yet — see the design
doc's phasing sign-off notes for why it's deferred until a real screen
needs it.
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
from netbbs.rendering.reflow import DEFAULT_WIDTH, reflow
from netbbs.rendering.theme import ACCENT_COLOR, HEADER_COLOR, MENU_KEY_COLOR, MUTED_COLOR

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
    "ACCENT_COLOR",
    "HEADER_COLOR",
    "MENU_KEY_COLOR",
    "MUTED_COLOR",
]
