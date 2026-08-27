"""
The ANSI rendering framework (design doc §4/§15) — color/cursor/
screen-clearing helpers, text reflow, and `sanitize_text` (design doc,
issue #8) for the untrusted-content half of what reaches a terminal.

Transport-independent character-mode input lives in
`netbbs.net.char_input`, not here. The screen-buffer/diff abstraction
for heavy cursor-addressable screens ("TUI", design doc)
lives in `netbbs.rendering.screen_buffer`, built alongside its first
real consumer, `netbbs.net.ansi_editor` (design doc -- the welcome
banner editor).
"""

from netbbs.rendering.ansi import (
    BOLD,
    RESET,
    REVERSE,
    bg,
    bg_rgb,
    clear_line,
    clear_screen,
    colored,
    fg,
    fg_rgb,
    move_cursor,
    reject_keystroke,
    reset_scroll_region,
    restore_cursor,
    save_cursor,
    set_scroll_region,
)
from netbbs.rendering.ansi_art import decode_ansi_bytes, encode_ansi_bytes
from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.gradient import GRADIENTS, gradient_color, gradient_text, nearest_256
from netbbs.rendering.layout import (
    MenuEntry,
    action_bar,
    badge,
    counts_row,
    double_frame,
    empty_state,
    field_row,
    menu_grid,
    screen_title,
    status_badge,
    telemetry_gauge,
    visible_width,
)
from netbbs.rendering.menu import menu_key
from netbbs.rendering.reflow import DEFAULT_WIDTH, colored_truncate, reflow, truncate
from netbbs.rendering.sanitize import sanitize_text
from netbbs.rendering.screen_buffer import Cell, ScreenBuffer, Snapshot, diff_ansi, full_render_ansi
from netbbs.rendering.width import char_width, cut_to_width, display_width, truncate_to_width, wrap_to_width
from netbbs.rendering.theme import (
    ACCENT_COLOR,
    ALERT_COLOR,
    CHAT_BODY_COLOR,
    CHANNEL_TYPE_COLOR,
    CLOCK_COLOR,
    HEADER_COLOR,
    ERROR_COLOR,
    LABEL_COLOR,
    MENU_KEY_COLOR,
    MUTED_COLOR,
    METADATA_COLOR,
    NICK_COLOR,
    PRIVILEGE_COLOR,
    SELF_COLOR,
    STATUS_BAR_BACKGROUND,
    SUCCESS_COLOR,
    TOPIC_COLOR,
    VERIFIED_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
)

__all__ = [
    "BOLD",
    "RESET",
    "REVERSE",
    "bg",
    "bg_rgb",
    "clear_line",
    "clear_screen",
    "colored",
    "decode_ansi_bytes",
    "encode_ansi_bytes",
    "parse_ansi_into_buffer",
    "fg",
    "fg_rgb",
    "GRADIENTS",
    "gradient_color",
    "gradient_text",
    "nearest_256",
    "MenuEntry",
    "action_bar",
    "badge",
    "counts_row",
    "double_frame",
    "empty_state",
    "field_row",
    "menu_grid",
    "screen_title",
    "status_badge",
    "telemetry_gauge",
    "visible_width",
    "move_cursor",
    "reject_keystroke",
    "reset_scroll_region",
    "restore_cursor",
    "save_cursor",
    "set_scroll_region",
    "menu_key",
    "DEFAULT_WIDTH",
    "colored_truncate",
    "reflow",
    "truncate",
    "char_width",
    "cut_to_width",
    "display_width",
    "truncate_to_width",
    "wrap_to_width",
    "sanitize_text",
    "Cell",
    "ScreenBuffer",
    "Snapshot",
    "diff_ansi",
    "full_render_ansi",
    "ACCENT_COLOR",
    "ALERT_COLOR",
    "CHAT_BODY_COLOR",
    "CHANNEL_TYPE_COLOR",
    "CLOCK_COLOR",
    "HEADER_COLOR",
    "ERROR_COLOR",
    "LABEL_COLOR",
    "MENU_KEY_COLOR",
    "MUTED_COLOR",
    "METADATA_COLOR",
    "NICK_COLOR",
    "PRIVILEGE_COLOR",
    "SELF_COLOR",
    "STATUS_BAR_BACKGROUND",
    "SUCCESS_COLOR",
    "TOPIC_COLOR",
    "VERIFIED_COLOR",
    "VALUE_COLOR",
    "WARNING_COLOR",
]
