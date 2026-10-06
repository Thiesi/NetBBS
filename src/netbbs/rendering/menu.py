"""
Menu-option rendering: highlighting the actual valid keystroke within a
menu label, so users can see which inputs are valid at a glance rather
than reading the whole option text. Direct response to feedback that
valid menu inputs should visually stand out.
"""

from __future__ import annotations

import re

from netbbs.rendering.ansi import colored
from netbbs.rendering.theme import EMPHASIS_COLOR, MENU_KEY_COLOR, MUTED_COLOR, SUCCESS_COLOR, VALUE_COLOR

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
# `[Enter]` is the one named key (issue #1083): the pause prompt's.
_BRACKETED_KEY = re.compile(r"\[([A-Za-z0-9]|Enter)\]")


def highlight_hotkeys(text: str, *, color: Color | None = None) -> str:
    """`text` with every bracketed key coloured the way `menu_key` colours
    it (issue #974), for a prompt that offers its keys in running text:
    ``"Unsaved changes. [S]ave, [D]iscard, or [C]ancel? "``. The brackets
    stay, so ASCII-only and colourless terminals still show the keys.

    `color`, when given, is the colour of everything but the keys -- the
    key's reset would otherwise drop the rest of the text back to the
    terminal's default.

    Also for a key a message mentions ("Use [P]review to verify it looks
    right."): issue #1083 reversed #974's choice to leave those plain, so a
    result or a report shows the key to press where the eye already is.
    Help pages still write their keys plain."""
    parts: list[str] = []
    position = 0
    for match in _BRACKETED_KEY.finditer(text):
        parts.append(_in_color(text[position:match.start()], color))
        # The brackets take the text's colour too; only the key is the menu's.
        key = colored(match.group(1), fg_color=_key_color(color), bold=True)
        parts.append(_in_color("[", color) + key + _in_color("]", color))
        position = match.end()
    if not parts:
        return _in_color(text, color)
    parts.append(_in_color(text[position:], color))
    return "".join(parts)


# The menu key's own green (46) beside the success green (82) is the same
# colour to the eye, so a key in a green result line vanished into it (the
# pre-release re-check, #1103): on a green line the key is drawn in the
# emphasis colour instead. Every other line keeps the menu key's colour.
_KEY_COLOR_CLASHES = frozenset({MENU_KEY_COLOR, SUCCESS_COLOR})


def _key_color(color: Color | None) -> Color:
    return EMPHASIS_COLOR if color in _KEY_COLOR_CLASHES else MENU_KEY_COLOR


# An absolute path a result names ("Saved to /var/lib/netbbs/x.ans."): a
# slash that starts a word, then no spaces; a Windows drive path likewise.
# Not "[/] Find", not "1/2", not the slashes inside a URL.
_PATH = re.compile(r"(?<![\w./\]\[:])(?:/[\w.~+-][^\s,;'\"()\]]*|[A-Za-z]:\\[^\s,;'\"()]+)")


def highlight_result(text: str, *, color: Color | None = None) -> str:
    """A result or status line (an announced outcome) in `color`: the keys
    it mentions highlighted as `highlight_hotkeys` does, and any absolute
    path it names in the value colour, so "Uploaded 839 bytes to
    /var/lib/netbbs/x.ans. Use [P]review." shows what was written and what
    to press (issue #1103). A sentence's own full stop after the path
    stays in the line's colour."""
    parts: list[str] = []
    position = 0
    for match in _PATH.finditer(text):
        path = match.group(0).rstrip(".:")
        if not path or path == "/":
            continue
        parts.append(highlight_hotkeys(text[position:match.start()], color=color))
        parts.append(colored(path, fg_color=VALUE_COLOR))
        position = match.start() + len(path)
    parts.append(highlight_hotkeys(text[position:], color=color))
    return "".join(part for part in parts if part)


def _in_color(text: str, color: Color | None) -> str:
    return colored(text, fg_color=color) if color is not None and text else text


def continue_prompt(action: str = "Continue") -> str:
    """The pause that waits for a key before going on (issue #1083), in
    place of "Press any key to continue...": `[Enter] Continue`, or
    `[Enter] Back to the door list` with `action`. Any key still goes on;
    the bracketed key is what a click in the browser terminal sends, so a
    caller with only a mouse is not stuck behind the pause."""
    return highlight_hotkeys(f"[Enter] {action}", color=MUTED_COLOR)


# A key, or a number: "12", "3.5", "1,024", "38x10" (two numbers).
_REPORT_TOKEN = re.compile(r"\[([A-Za-z0-9]|Enter)\]|\d+(?:[.,]\d+)*")


def highlight_report(text: str, *, color: Color | None = None) -> str:
    """A report line (a check's verdict, a slot's size) in `color`, with
    its keys highlighted as `highlight_hotkeys` does and every number in
    the emphasis colour (issue #1083), so "fits, 12 entries a page, names
    up to 60 columns" reads at a glance rather than as one flat line."""
    parts: list[str] = []
    position = 0
    for match in _REPORT_TOKEN.finditer(text):
        parts.append(_in_color(text[position:match.start()], color))
        if match.group(1) is not None:
            key = colored(match.group(1), fg_color=_key_color(color), bold=True)
            parts.append(_in_color("[", color) + key + _in_color("]", color))
        else:
            parts.append(colored(match.group(0), fg_color=EMPHASIS_COLOR, bold=True))
        position = match.end()
    parts.append(_in_color(text[position:], color))
    return "".join(parts)
