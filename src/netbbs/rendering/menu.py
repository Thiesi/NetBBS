"""
Menu-option rendering: highlighting the actual valid keystroke within a
menu label, so users can see which inputs are valid at a glance rather
than reading the whole option text. Direct response to feedback that
valid menu inputs should visually stand out.
"""

from __future__ import annotations

import re

from netbbs.rendering.ansi import colored
from netbbs.rendering.theme import MENU_KEY_COLOR

Color = int | tuple[int, int, int]


def menu_key(key: str, rest: str = "", *, prefix: str = "", capitalize: bool = False) -> str:
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
    still reads as a real word, e.g. `menu_key("n", "nels", prefix="Cha")`
    for `Cha[n]nels` rather than truncating to a nonsense `[H]annels`.

    Whenever `prefix` is given, `key` is displayed lowercase rather than
    however the caller passed it — a real word is never capitalized
    mid-way through (`Cha[N]nels`/`Bac[K]up` read as a grammar mistake,
    not a hotkey), and the brackets/bold/color already mark the hotkey
    unambiguously on their own, so capitalization was never doing any of
    that work. A bare first-letter hotkey (`prefix=""`) is untouched —
    that position is already naturally capitalized as the start of a
    title-cased label, so there's nothing to fix there. Case is display
    only in both cases: dispatch always lowercases the actual keystroke
    before comparing it, regardless of what's shown here.

    `capitalize=True` (dogfood report) opts a `prefix`-using call back
    into its passed-in case instead of the forced-lowercase default
    above -- for the *other* real shape `prefix` covers: a `prefix`
    ending at a genuine word boundary (a space, e.g. `"Banners & "`
    before `"Mastheads"`), where the hotkey isn't a mid-word letter at
    all but a whole word's own natural leading capital. The default
    can't safely auto-detect this from `prefix` alone -- several
    existing callers deliberately use a *lowercase*, sentence-style
    `prefix` ending in a space too (e.g. `menu_key("b", "oard",
    prefix="message ")`, mid-sentence prose, not a menu heading), where
    forcing a capital would be wrong -- so this stays each caller's own
    explicit choice, defaulting to today's unchanged behavior.
    """
    display_key = key if capitalize else (key.lower() if prefix else key)
    highlighted = colored(display_key, fg_color=MENU_KEY_COLOR, bold=True)
    return f"{prefix}[{highlighted}]{rest}"


# One key in brackets, not a word or a number: `[S]`, never `[10]` or `[ok]`.
_BRACKETED_KEY = re.compile(r"\[([A-Za-z0-9])\]")


def highlight_hotkeys(text: str, *, color: Color | None = None) -> str:
    """`text` with every bracketed key coloured the way `menu_key` colours
    it (issue #974), for a prompt that offers its keys in running text:
    ``"Unsaved changes. [S]ave, [D]iscard, or [C]ancel? "``. The brackets
    stay, so ASCII-only and colourless terminals still show the keys.

    `color`, when given, is the colour of everything but the keys -- the
    key's reset would otherwise drop the rest of the text back to the
    terminal's default.

    Only for text whose keys answer what is on screen. A key merely
    mentioned in prose ("use [P]review to check it") stays plain, the way
    the help screens write keys."""
    parts: list[str] = []
    position = 0
    for match in _BRACKETED_KEY.finditer(text):
        parts.append(_in_color(text[position:match.start()], color))
        parts.append(menu_key(match.group(1), capitalize=True))
        position = match.end()
    if not parts:
        return _in_color(text, color)
    parts.append(_in_color(text[position:], color))
    return "".join(parts)


def _in_color(text: str, color: Color | None) -> str:
    return colored(text, fg_color=color) if color is not None and text else text
