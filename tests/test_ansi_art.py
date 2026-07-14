"""Tests for netbbs.rendering.ansi_art (design doc -- welcome banner
rounds A and B1) -- pure decode (CP437-fallback) and encode
(ScreenBuffer -> CP437 bytes) logic, no Database/session involved."""

from __future__ import annotations

from netbbs.rendering.ansi_art import decode_ansi_bytes, encode_ansi_bytes
from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.screen_buffer import ScreenBuffer


def test_ascii_only_content_decodes_via_utf8_path():
    assert decode_ansi_bytes(b"Hello, NetBBS!") == "Hello, NetBBS!"


def test_valid_utf8_content_decodes_as_utf8():
    text = "Café ☃ welcome"  # accented char + snowman
    assert decode_ansi_bytes(text.encode("utf-8")) == text


def test_cp437_high_bit_bytes_decode_via_fallback():
    # 0xDB is FULL BLOCK, 0xB0/0xB1/0xB2 are the classic light/medium/dark
    # shade blocks in CP437 -- a byte sequence that is not valid UTF-8
    # (0xDB as a lone byte is a UTF-8 continuation-byte lead needing a
    # follow-up byte that isn't there), so this must hit the fallback.
    data = b"\xb0\xb1\xb2\xdb"
    assert decode_ansi_bytes(data) == "░▒▓█"


def test_ansi_escape_sequences_survive_the_round_trip():
    # A real ANSI-art file's whole point: cursor-position/color escape
    # sequences must come through byte-for-byte, not be interpreted or
    # stripped by this function (that's sanitize_text's job to avoid --
    # see ansi_art.py's own module docstring).
    data = b"\x1b[2J\x1b[1;1H\x1b[31mRed text\x1b[0m"
    assert decode_ansi_bytes(data) == "\x1b[2J\x1b[1;1H\x1b[31mRed text\x1b[0m"


def test_decode_never_raises_for_any_single_byte_value():
    for byte_value in range(256):
        result = decode_ansi_bytes(bytes([byte_value]))
        assert isinstance(result, str)


def test_decode_never_raises_for_arbitrary_high_bit_sequences():
    # A cross-section of byte sequences that look plausible as "garbage"
    # for a UTF-8 decoder to choke on -- confirms the CP437 fallback
    # (a total function over all 256 byte values) actually catches
    # every case that reaches it, not just the single-byte case above.
    sequences = [
        bytes([0xFF, 0xFE, 0xFD]),
        bytes(range(0x80, 0xA0)),
        bytes([0xC0, 0x80]),  # overlong/invalid UTF-8 encoding
        bytes([0xED, 0xA0, 0x80]),  # UTF-8-encoded surrogate, invalid
    ]
    for data in sequences:
        result = decode_ansi_bytes(data)
        assert isinstance(result, str)


# -- encode_ansi_bytes (design doc -- welcome banner round B1) --------------


def test_encode_plain_text():
    buf = ScreenBuffer(3, 1)
    buf.write_cell(0, 0, "a")
    buf.write_cell(0, 1, "b")
    buf.write_cell(0, 2, "c")
    encoded = encode_ansi_bytes(buf)
    assert b"abc" in encoded


def test_encode_cp437_glyph_round_trips_through_the_real_codec():
    buf = ScreenBuffer(1, 1)
    buf.write_cell(0, 0, "█")  # FULL BLOCK
    encoded = encode_ansi_bytes(buf)
    assert 0xDB in encoded


def test_encode_never_raises_for_a_character_outside_cp437():
    buf = ScreenBuffer(1, 1)
    buf.write_cell(0, 0, "中")  # a CJK character, not in CP437 at all
    encoded = encode_ansi_bytes(buf)  # must not raise
    assert isinstance(encoded, bytes)


def test_encode_then_decode_then_parse_round_trips_a_styled_buffer():
    buf = ScreenBuffer(10, 2)
    buf.write_cell(0, 0, "█", fg=1, bold=True)
    buf.write_cell(0, 1, "X", fg=1, bold=True)
    buf.write_cell(1, 0, "Y", fg=2)

    encoded = encode_ansi_bytes(buf)
    decoded_text = decode_ansi_bytes(encoded)
    result = ScreenBuffer(10, 2)
    parse_ansi_into_buffer(decoded_text, result)

    assert result.snapshot() == buf.snapshot()


def test_encode_of_an_empty_buffer_does_not_raise():
    buf = ScreenBuffer(5, 3)
    encoded = encode_ansi_bytes(buf)
    assert isinstance(encoded, bytes)
