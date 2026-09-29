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
}


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


def ellipsis(charset: Charset) -> str:
    """The truncation marker for `charset`: one column in UTF-8, three
    plain dots where the single-character ellipsis does not exist."""
    return "…" if charset == UTF8 else "..."
