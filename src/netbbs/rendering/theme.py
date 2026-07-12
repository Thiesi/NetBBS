"""
NetBBS's chosen color palette — the actual color numbers used across
screens, kept in one place so screens stay visually consistent and a
future palette change doesn't mean hunting through every module that
prints something.

Deliberately restrained (a header color, an accent color, a muted color
for system/meta messages, a distinct color for valid menu inputs) rather
than a full theming system, which doesn't exist yet and isn't needed for
the current, still-small set of screens. Every screen that prints
anything colored should pull from here rather than picking its own
numbers — the gap this module fixes is exactly that boards and chat had
started drifting toward defining their own local color constants
independently.
"""

from __future__ import annotations

HEADER_COLOR = 51  # bright cyan — section headers, banners
ACCENT_COLOR = 220  # gold — navigable items: board/channel names, other users' names
MUTED_COLOR = 244  # gray — system/meta messages (join/leave notices, etc.)
MENU_KEY_COLOR = 46  # bright green — the actual valid keystroke in a menu option
SELF_COLOR = 201  # bright magenta — the user's own name/messages in chat, distinct
                  # from ACCENT_COLOR (used for everyone else's), so a user's own
                  # messages visually stand out from the rest of the conversation
