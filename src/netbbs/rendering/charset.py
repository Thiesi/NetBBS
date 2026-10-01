"""
The output character sets a session can have, and how composed Unicode
text is mapped to one of them (design doc §3.2, "Character set per
session", issue #929).

Screens compose Unicode. A session whose terminal reads CP437 (SyncTERM
and other classic BBS terminals) or only 7-bit ASCII gets that text
through `map_text` first, so no screen has to know which set a caller
has. Three rules hold for every mapping:

- A substitute has exactly the display width of the character it
  replaces (`netbbs.rendering.width.char_width`). Width is measured once,
  on the Unicode text, and every layout computed from it stays valid.
- Control characters, and with them every escape sequence, pass through
  unchanged. Only printable characters are mapped.
- The result always encodes in the target set without errors.

The order of preference for a character the set lacks: NetBBS's own
curated substitute (`_GLYPHS`), the character with its accents removed
(NFKD), a small table of letters and punctuation that do not decompose
(`_FOLD`), and finally `?` repeated to the character's width.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from typing import Literal

from netbbs.rendering.width import char_width, display_width

Charset = Literal["utf-8", "cp437", "ascii"]

UTF8: Charset = "utf-8"
CP437: Charset = "cp437"
ASCII: Charset = "ascii"

CHARSETS: tuple[Charset, ...] = (UTF8, CP437, ASCII)

# NetBBS's own glyphs: (CP437 substitute, ASCII substitute). A CP437 entry
# of None means the glyph exists in CP437 and passes through unchanged.
# Every substitute has the display width of its key (tests enforce it).
_GLYPHS: dict[str, tuple[str | None, str]] = {
    # Box drawing that CP437 has.
    "─": (None, "-"), "━": ("─", "-"), "═": (None, "="),
    "│": (None, "|"), "┃": ("│", "|"), "║": (None, "|"),
    "┌": (None, "+"), "┐": (None, "+"), "└": (None, "+"), "┘": (None, "+"),
    "├": (None, "+"), "┤": (None, "+"), "┬": (None, "+"), "┴": (None, "+"),
    "┼": (None, "+"),
    "╔": (None, "+"), "╗": (None, "+"), "╚": (None, "+"), "╝": (None, "+"),
    "╠": (None, "+"), "╣": (None, "+"), "╦": (None, "+"), "╩": (None, "+"),
    "╬": (None, "+"),
    # Box drawing that CP437 lacks: rounded and heavy corners become square.
    "╭": ("┌", "+"), "╮": ("┐", "+"), "╰": ("└", "+"), "╯": ("┘", "+"),
    "┏": ("┌", "+"), "┓": ("┐", "+"), "┗": ("└", "+"), "┛": ("┘", "+"),
    # Blocks and shades.
    "█": (None, "#"), "▓": (None, "#"), "▒": (None, ":"), "░": (None, "."),
    "▀": (None, "^"), "▄": (None, "_"), "▌": (None, "|"), "▐": (None, "|"),
    "■": (None, "#"),
    "▁": ("_", "_"), "▂": ("▄", "_"), "▃": ("▄", "_"), "▅": ("▄", "_"),
    "▆": ("█", "#"), "▇": ("█", "#"),
    "▰": ("█", "#"), "▱": ("░", "."),
    # Punctuation and symbols.
    "·": (None, "."), "»": (None, ">"), "«": (None, "<"),
    "›": ("»", ">"), "‹": ("«", "<"),
    "—": ("-", "-"), "–": ("-", "-"), "…": (".", "."),
    "•": ("∙", "*"), "●": ("∙", "*"), "○": ("o", "o"),
    "◆": ("*", "*"), "◇": ("*", "*"), "◈": ("*", "*"),
    "★": ("*", "*"), "☆": ("*", "*"), "✦": ("*", "*"), "✧": ("*", "*"),
    "✶": ("*", "*"), "⋆": ("*", "*"), "❖": ("*", "*"),
    "→": (">", ">"), "←": ("<", "<"), "↑": ("^", "^"), "↓": ("v", "v"),
    "▲": ("^", "^"), "▼": ("v", "v"), "▾": ("v", "v"), "▸": (">", ">"),
    "×": ("x", "x"), "⚠": ("!", "!"), "⚡": ("!!", "!!"),
    "⟦": ("[", "["), "⟧": ("]", "]"),
    "‘": ("'", "'"), "’": ("'", "'"), "‚": (",", ","),
    "“": ('"', '"'), "”": ('"', '"'), "„": ('"', '"'),
    " ": (None, " "),
}

# Letters and signs NFKD does not reduce to ASCII, for either set when the
# character itself is missing. Width 1 each.
_FOLD: dict[str, str] = {
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
    "ð": "d", "Ð": "D", "þ": "p", "Þ": "P", "ħ": "h", "Ħ": "H",
    "ı": "i", "ŀ": "l", "Ŀ": "L", "ß": "s", "æ": "a", "Æ": "A",
    "œ": "o", "Œ": "O", "ĸ": "k", "ŉ": "n",
    "€": "E", "£": "L", "¥": "Y", "¢": "c", "©": "c", "®": "r",
    "°": "o", "±": "+", "¿": "?", "¡": "!", "§": "S", "¶": "P",
    "¼": "4", "½": "2", "¾": "4", "µ": "u", "÷": "/",
    # Spacing accents, whose decomposition is only a space and a mark.
    "´": "'", "¨": '"', "¯": "-", "¸": ",", "ˆ": "^", "˜": "~", "˝": '"', "˘": "u", "˙": ".",
    # The rest of CP437's upper half, for a CP437 door or CP437 art shown
    # on an ASCII terminal (#929 PR 6): its Greek letters and maths signs.
    "₧": "P", "ƒ": "f", "⌐": "-", "¬": "-",
    "α": "a", "Γ": "G", "π": "p", "Σ": "E", "σ": "s", "τ": "t", "Φ": "O", "Θ": "O",
    "Ω": "O", "δ": "d", "∞": "8", "φ": "o", "ε": "e", "∩": "n", "≡": "=", "≥": ">",
    "≤": "<", "⌠": "|", "⌡": "|", "≈": "~", "∙": ".", "√": "v",
    # The pictographs of CP437's control range (`ART_PICTOGRAPHS`), for art
    # shown on an ASCII terminal and for the same characters in ordinary
    # text, which never reach a terminal as control bytes.
    "☺": "@", "☻": "@", "♥": "*", "♦": "*", "♣": "*", "♠": "*", "◘": "#", "◙": "#",
    "♂": "o", "♀": "o", "♪": "~", "♫": "~", "☼": "*", "►": ">", "◄": "<", "↕": "|",
    "‼": "!", "▬": "-", "↨": "|", "∟": "L", "↔": "-", "⌂": "^",
}


# A light CP437 line for each set of directions a box character draws.
_LIGHT_BOX = {
    frozenset("LR"): "─", frozenset("UD"): "│", frozenset("L"): "─", frozenset("R"): "─",
    frozenset("U"): "│", frozenset("D"): "│",
    frozenset("DR"): "┌", frozenset("DL"): "┐", frozenset("UR"): "└", frozenset("UL"): "┘",
    frozenset("UDR"): "├", frozenset("UDL"): "┤", frozenset("DLR"): "┬", frozenset("ULR"): "┴",
    frozenset("UDLR"): "┼",
}


def _box_substitute(ch: str, charset: Charset) -> str | None:
    """A substitute for a box-drawing character the table does not list,
    worked out from the directions its Unicode name says it draws: CP437
    gets the light line of that shape (it has no heavy, dashed or mixed
    light/heavy lines), ASCII gets `+`, `|`, `-` or `=`. CP437's own
    mixed single/double corners and tees (╒ ╡ ╫ ...) are why the ASCII
    half exists: a CP437 door or piece of art shown on an ASCII terminal
    is full of them."""
    name = unicodedata.name(ch, "")
    if not name.startswith("BOX DRAWINGS "):
        return None
    if "DIAGONAL" in name:
        if "CROSS" in name:
            return "X"
        return "/" if "UPPER RIGHT TO LOWER LEFT" in name else "\\"
    words = set(name.split())
    directions = set()
    if "VERTICAL" in words:
        directions |= {"U", "D"}
    if "HORIZONTAL" in words:
        directions |= {"L", "R"}
    directions |= {word[0] for word in words & {"UP", "DOWN", "LEFT", "RIGHT"}}
    if not directions:
        return None
    if charset == CP437:
        return _LIGHT_BOX.get(frozenset(directions))
    if directions <= {"L", "R"}:
        return "=" if "DOUBLE" in words and "DASH" not in words else "-"
    if directions <= {"U", "D"}:
        return "|"
    return "+"


def _encodable(text: str, charset: Charset) -> bool:
    try:
        text.encode(charset)
    except UnicodeEncodeError:
        return False
    return True


def _fit(candidate: str, width: int, charset: Charset) -> str | None:
    """`candidate` padded to `width` columns, or None if it is too wide,
    blank (empty or only spaces) for a visible character, or not
    encodable in `charset`."""
    # A spacing accent (´ ¨ ¯) decomposes to a space and a combining
    # mark: a blank is no substitute for a visible character.
    if not candidate.strip() or not _encodable(candidate, charset):
        return None
    if any(char_width(c) == 0 or ord(c) < 0x20 for c in candidate):
        return None
    candidate_width = display_width(candidate)
    if candidate_width == 0 or candidate_width > width:
        return None
    return candidate + " " * (width - candidate_width)


@lru_cache(maxsize=4096)
def _map_char(ch: str, charset: Charset) -> str:
    code = ord(ch)
    if code < 0x80:
        return ch
    width = char_width(ch)
    if width == 0:
        # Combining marks, format characters and C1 controls: nothing to
        # show, and a C1 control must never reach the terminal as one.
        return ""
    if unicodedata.category(ch) == "Zs" and not (charset == CP437 and _encodable(ch, CP437)):
        # Space separators (en space, thin space, ideographic space) are
        # blank by nature: a plain space of the same width is exact.
        return " " * width
    if charset == CP437 and ch not in _GLYPHS and _encodable(ch, CP437):
        return ch
    glyph = _GLYPHS.get(ch)
    if glyph is not None:
        cp437_substitute, ascii_substitute = glyph
        if charset == CP437:
            return ch if cp437_substitute is None else cp437_substitute
        return ascii_substitute
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    fitted = _fit(base, width, charset)
    if fitted is not None:
        return fitted
    folded = _FOLD.get(ch)
    if folded is not None:
        fitted = _fit(folded, width, charset)
        if fitted is not None:
            return fitted
    box = _box_substitute(ch, charset)
    if box is not None:
        fitted = _fit(box, width, charset)
        if fitted is not None:
            return fitted
    return "?" * width


def map_text(text: str, charset: Charset) -> str:
    """`text` with every character `charset` cannot show replaced by a
    substitute of the same display width. UTF-8 and pure-ASCII text are
    returned unchanged."""
    if charset == UTF8 or text.isascii():
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        i += 1
        # A character with combining marks after it: compose them first,
        # so "e" + U+0301 can reach CP437 as the single "é". Marks that do
        # not compose are dropped (`_map_char` maps them to nothing).
        j = i
        while j < n and unicodedata.combining(text[j]):
            j += 1
        if j > i:
            if char_width(ch) > 0:
                composed = unicodedata.normalize("NFC", text[i - 1:j])
                if len(composed) == 1:
                    ch = composed
            # The marks have no width and map to nothing either way; skip
            # the whole run once, so a long run is never scanned again.
            i = j
        out.append(ch if ord(ch) < 0x80 else _map_char(ch, charset))
    return "".join(out)


def encode_text(text: str, charset: Charset) -> bytes:
    """`map_text` followed by encoding: the bytes a terminal with
    `charset` should receive for `text`. Never raises."""
    return map_text(text, charset).encode(charset, errors="replace")


def input_codec(session: object) -> str:
    """The codec a caller's typed bytes are in: CP437 for a CP437
    terminal, UTF-8 otherwise (an ASCII terminal's bytes are valid UTF-8,
    and a UTF-8 terminal the caller chose ASCII for still types UTF-8)."""
    return "cp437" if getattr(session, "output_charset", UTF8) == CP437 else "utf-8"


def ellipsis(charset: Charset) -> str:
    """The truncation marker for `charset`: one column in UTF-8, three
    plain dots where the single-character ellipsis does not exist."""
    return "…" if charset == UTF8 else "..."


def ellipsis_for(session: object, *, unicode_style: bool = True) -> str:
    """The truncation marker a screen should append for `session`: "…"
    only on a UTF-8 terminal with decorated screens, three dots
    otherwise. `map_text` has to keep widths, so it can only turn "…"
    into a single "."; a screen that truncates knows the width and can
    afford the real three dots."""
    if not unicode_style:
        return "..."
    return ellipsis(getattr(session, "output_charset", UTF8))


# CP437's control range drawn as pictographs, as ANSI art uses it (issue
# #929). Only the art path gives these bytes glyphs (design doc §3.2,
# "SysOp art: storage and SAUCE"): BEL, BS, TAB, LF, CR, EOF and ESC keep
# their control meaning because art needs them as controls, and everywhere
# else the whole range stays control characters.
ART_PICTOGRAPHS: dict[int, str] = {
    0x01: "☺", 0x02: "☻", 0x03: "♥", 0x04: "♦", 0x05: "♣", 0x06: "♠",
    0x0B: "♂", 0x0C: "♀", 0x0E: "♫", 0x0F: "☼", 0x10: "►", 0x11: "◄",
    0x12: "↕", 0x13: "‼", 0x14: "¶", 0x15: "§", 0x16: "▬", 0x17: "↨",
    0x18: "↑", 0x19: "↓", 0x1C: "∟", 0x1D: "↔", 0x1E: "▲", 0x1F: "▼",
    0x7F: "⌂",
}
_ART_DECODE = {code: glyph for code, glyph in ART_PICTOGRAPHS.items()}
_ART_ENCODE = {ord(glyph): chr(code) for code, glyph in ART_PICTOGRAPHS.items()}


def art_pictographs_to_glyphs(text: str) -> str:
    """CP437-decoded art with its control-range pictograph bytes turned
    into the glyphs they draw. Only for text decoded from an art file as
    CP437, where those bytes are pictures, not commands."""
    return text.translate(_ART_DECODE)


def art_glyphs_to_cp437_controls(text: str) -> str:
    """Art bound for a CP437 terminal with its pictographs turned back into
    the bytes that draw them there (`ART_PICTOGRAPHS`). `map_text` passes
    control characters through, so the byte reaches the terminal as drawn.
    Only the art path may do this; in ordinary text these characters get
    printable substitutes instead."""
    return text.translate(_ART_ENCODE)
