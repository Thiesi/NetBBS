"""Mapping composed Unicode text to a session's character set (issue #929)."""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

import netbbs
from netbbs.rendering.charset import (
    _FOLD,
    _GLYPHS,
    ASCII,
    CP437,
    UTF8,
    ellipsis,
    encode_text,
    map_text,
)
from netbbs.rendering.width import char_width, display_width

_SRC = Path(netbbs.__file__).parent


def _literal_glyphs() -> set[str]:
    """Every non-ASCII character in a string literal of NetBBS's source,
    docstrings included: anything a screen could send."""
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.update(ch for ch in node.value if ord(ch) > 0x7F)
    return found


def _assert_mapped(original: str, charset) -> str:
    mapped = map_text(original, charset)
    assert display_width(mapped) == display_width(original), (original, mapped)
    mapped.encode(charset)  # never raises
    return mapped


@pytest.mark.parametrize("charset", [CP437, ASCII])
def test_every_curated_substitute_keeps_width_and_encodes(charset):
    for glyph, (cp437_substitute, ascii_substitute) in _GLYPHS.items():
        substitute = ascii_substitute if charset == ASCII else (cp437_substitute or glyph)
        assert display_width(substitute) == char_width(glyph), glyph
        substitute.encode(charset)


def test_fold_entries_are_one_ascii_column():
    for letter, folded in _FOLD.items():
        assert char_width(letter) == 1 and folded.isascii() and len(folded) == 1, letter


@pytest.mark.parametrize("charset", [CP437, ASCII])
def test_every_glyph_in_the_source_maps_at_its_own_width(charset):
    glyphs = _literal_glyphs()
    assert "╭" in glyphs and "—" in glyphs  # the inventory really read the source
    for glyph in glyphs:
        _assert_mapped(glyph, charset)


@pytest.mark.parametrize("charset", [CP437, ASCII])
def test_random_unicode_keeps_width_and_always_encodes(charset):
    rng = random.Random(929)
    pools = [range(0x20, 0x7F), range(0xA0, 0x250), range(0x370, 0x530), range(0x2000, 0x2C00),
             range(0x3040, 0x3100), range(0x4E00, 0x4F00), range(0xFF01, 0xFF60), range(0x1F300, 0x1F650),
             range(0x0300, 0x0370)]
    for _ in range(400):
        text = "".join(chr(rng.choice(rng.choice(pools))) for _ in range(rng.randint(1, 30)))
        _assert_mapped(text, charset)


def test_ascii_is_seven_bit():
    assert map_text("Grüße, café — naïve…", ASCII) == "Gruse, cafe - naive."
    assert encode_text("Ωmega ★ 東", ASCII).isascii()


def test_cp437_keeps_the_letters_it_has():
    assert map_text("Grüße, café, niño", CP437) == "Grüße, café, niño"
    assert map_text("Łódź", CP437) == "Lódz"


def test_a_combining_accent_composes_before_mapping():
    assert map_text("café", CP437) == "café"
    assert map_text("café", ASCII) == "cafe"


def test_spacing_accents_never_become_blanks():
    # Review on #936: they decompose to a space and a combining mark.
    assert map_text("¯\\_(ツ)_/¯", ASCII) == "-\\_(??)_/-"
    assert map_text("´", CP437) == "'"


def test_space_separators_stay_blank():
    # Review on #936: they decompose to a plain space, which is exact.
    assert map_text("a b　c", ASCII) == "a b  c"
    assert map_text("a b", CP437) == "a b"
    assert map_text("a b", ASCII) == "a b"


def test_a_long_run_of_combining_marks_is_mapped_in_linear_time():
    import time

    text = "a" + "́̂" * 25_000 + "b"
    started = time.monotonic()
    assert map_text(text, CP437) == "ab"
    assert time.monotonic() - started < 2.0


def test_wide_characters_fill_their_two_columns():
    assert map_text("東京", CP437) == "????"
    assert map_text("Ａ", ASCII) == "A "


def test_control_characters_and_escape_sequences_pass_unchanged():
    text = "\x1b[1;31m╔══╗\x1b[0m\r\n\x07"
    assert map_text(text, CP437) == text
    assert map_text(text, ASCII) == "\x1b[1;31m+==+\x1b[0m\r\n\x07"


def test_c1_controls_never_reach_the_terminal():
    assert map_text("a\x9bb\x90c", CP437) == "abc"


def test_netbbs_chrome_gets_its_curated_substitutes():
    assert map_text("╭─ Help ─╮", CP437) == "┌─ Help ─┐"
    assert map_text("Main › Boards", CP437) == "Main » Boards"
    assert map_text("Main › Boards", ASCII) == "Main > Boards"


def test_utf8_is_untouched():
    text = "╭ 東 ★ café"
    assert map_text(text, UTF8) is text
    assert encode_text(text, UTF8) == text.encode("utf-8")


def test_every_cp437_character_round_trips_byte_for_byte():
    # SysOp art authored in CP437 is decoded to Unicode on load; a CP437
    # terminal must get the original bytes back.
    for value in range(0x20, 0x100):
        if value == 0x7F:
            continue
        original = bytes([value])
        assert encode_text(original.decode("cp437"), CP437) == original, hex(value)


def test_ellipsis_follows_the_charset():
    assert ellipsis(UTF8) == "…"
    assert ellipsis(CP437) == "..." and ellipsis(ASCII) == "..."


# -- #929 PR 6: CP437's whole upper half, and every box-drawing character,
# -- reach an ASCII terminal as something other than "?".


def test_every_cp437_character_has_an_ascii_stand_in():
    """A CP437 door or CP437 art on an ASCII session: its mixed
    single/double box corners and its Greek and maths signs all read as
    something, not "?" (only the inverted question mark is one)."""
    unshown = [
        f"{byte:02X}"
        for byte in range(0x80, 0x100)
        if (char := bytes([byte]).decode("cp437")) != "\u00bf"
        and "?" in map_text(char, ASCII)
    ]
    assert unshown == []


@pytest.mark.parametrize("charset", [CP437, ASCII])
def test_every_box_drawing_character_maps_to_a_line_of_the_same_shape(charset):
    for code in range(0x2500, 0x2580):
        char = chr(code)
        mapped = map_text(char, charset)
        assert "?" not in mapped, (hex(code), charset)
        assert display_width(mapped) == display_width(char)
        mapped.encode(charset)


def test_mixed_box_characters_keep_their_shape():
    assert map_text("\u2552\u2550\u2555\u255e\u256a\u2561", ASCII) == "+=++++"
    assert map_text("\u250f\u2501\u2533\u2501\u2513\u2521\u254d\u2529", CP437) == "\u250c\u2500\u252c\u2500\u2510\u251c\u2500\u2524"
    assert map_text("\u2502\u2551\u2500\u2550", ASCII) == "||-="
