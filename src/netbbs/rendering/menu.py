"""
Menu-option rendering: highlighting the actual valid keystroke within a
menu label, so users can see which inputs are valid at a glance rather
than reading the whole option text. Direct response to feedback that
valid menu inputs should visually stand out.
"""

from __future__ import annotations

from netbbs.rendering.ansi import colored
from netbbs.rendering.theme import MENU_KEY_COLOR


def menu_key(key: str, rest: str = "", *, prefix: str = "") -> str:
    """
    Render a menu option like `[B]oards` with the bracketed key
    highlighted (bold + a color reserved for exactly this purpose — see
    `netbbs.rendering.theme.MENU_KEY_COLOR`), distinct from the
    descriptive rest of the label and from any other color used
    elsewhere on screen (board/channel names, headers), so a valid input
    is unambiguous at a glance.

    `prefix` covers the case where the natural hotkey isn't the word's
    first letter (e.g. when that letter is already claimed by another
    option in the same menu) — pass the letters before it so the label
    still reads as a real word, e.g. `menu_key("N", "nels", prefix="Cha")`
    for `Cha[N]nels` rather than truncating to a nonsense `[H]annels`.
    """
    highlighted = colored(key, fg_color=MENU_KEY_COLOR, bold=True)
    return f"{prefix}[{highlighted}]{rest}"
