"""SAUCE records on SysOp art (issue #929, step 3)."""

from __future__ import annotations

from netbbs.rendering import decode_ansi_bytes, decode_art_bytes, decode_banner_bytes
from netbbs.rendering.sauce import build_sauce, split_sauce

# A small piece of scene-style art: CP437 blocks in colour, CR LF rows.
ART = b"\x1b[0;1;34m\xdb\xdb\xb2\xb1\xb0 \x1b[33mNIB\r\n\x1b[0m\xc9\xcd\xcd\xbb\r\n\x1b[0m"


def _field(value: bytes, size: int) -> bytes:
    return value.ljust(size, b" ")


def _record(*, comments: int = 0, tflags: int = 0, width: int = 80, lines: int = 2, font: bytes = b"IBM VGA") -> bytes:
    """A SAUCE record written out field by field from the spec, not with
    the code under test."""
    record = (
        b"SAUCE"
        + b"00"
        + _field(b"Nib Logo", 35)
        + _field(b"InkWell", 20)
        + _field(b"Quill", 20)
        + b"20260930"
        + len(ART).to_bytes(4, "little")
        + bytes([1, 1])  # DataType Character, FileType ANSi
        + width.to_bytes(2, "little")
        + lines.to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + bytes([comments, tflags])
        + font.ljust(22, b"\x00")
    )
    assert len(record) == 128
    return record


def _scene_file(**kwargs: int | bytes) -> bytes:
    return ART + b"\x1a" + _record(**kwargs)


def test_a_sauce_record_is_stripped_and_read():
    body, sauce = split_sauce(_scene_file(tflags=1))
    assert body == ART
    assert sauce is not None
    assert (sauce.title, sauce.author, sauce.group, sauce.date) == ("Nib Logo", "InkWell", "Quill", "20260930")
    assert sauce.width == 80
    assert sauce.tinfo2 == 2
    assert sauce.ice_colors
    assert sauce.font_is_cp437
    assert sauce.credit == "Nib Logo by InkWell/Quill"


def test_the_comment_block_is_stripped_too():
    comments = b"COMNT" + _field(b"drawn for The Nib & Quill", 64) + _field(b"second line", 64)
    body, sauce = split_sauce(ART + b"\x1a" + comments + _record(comments=2))
    assert body == ART
    assert sauce is not None
    assert sauce.comments == ("drawn for The Nib & Quill", "second line")


def test_a_comment_count_pointing_outside_the_file_keeps_the_art():
    body, sauce = split_sauce(ART + b"\x1a" + _record(comments=9))
    assert body == ART
    assert sauce is not None and sauce.comments == ()


def test_without_sauce_the_file_is_unchanged():
    assert split_sauce(ART) == (ART, None)


def test_a_lone_eof_byte_ends_the_art():
    body, sauce = split_sauce(ART + b"\x1a" + b"trailing junk")
    assert (body, sauce) == (ART, None)


def test_decoding_scene_art_no_longer_shows_the_record():
    text = decode_ansi_bytes(_scene_file())
    assert "SAUCE" not in text
    assert "InkWell" not in text
    assert "\x1a" not in text
    assert text == ART.decode("cp437")


def test_banner_decoding_strips_the_record_as_well():
    text = decode_banner_bytes(_scene_file())
    assert "SAUCE" not in text and "Nib Logo" not in text
    assert "██" in text  # the CP437 full blocks


def test_a_file_with_sauce_is_read_as_cp437_even_when_it_is_valid_utf8():
    # 0xC3 0xA9 is "é" in UTF-8 but "├⌐" in CP437; with SAUCE the file is
    # classic art, so CP437 wins.
    art = b"\xc3\xa9"
    text, sauce = decode_art_bytes(art + b"\x1a" + _record())
    assert sauce is not None
    assert text == "├⌐"


def test_a_utf8_file_without_sauce_stays_utf8():
    text, sauce = decode_art_bytes("Nib & Quill — █".encode())
    assert sauce is None
    assert text == "Nib & Quill — █"


def test_other_fonts_are_recognised_as_not_cp437():
    _, sauce = split_sauce(_scene_file(font=b"IBM VGA 850"))
    assert sauce is not None and not sauce.font_is_cp437
    _, sauce = split_sauce(_scene_file(font=b"Amiga Topaz 2+"))
    assert sauce is not None and not sauce.font_is_cp437
    _, sauce = split_sauce(_scene_file(font=b"IBM VGA 437"))
    assert sauce is not None and sauce.font_is_cp437


def test_build_sauce_round_trips():
    tail = build_sauce(
        width=80, lines=24, title="Nib Logo", author="InkWell", group="Quill", date="20260930",
        ice_colors=True, comments=("hello",),
    )
    body, sauce = split_sauce(ART + tail)
    assert body == ART
    assert sauce is not None
    assert (sauce.width, sauce.tinfo2, sauce.ice_colors, sauce.font) == (80, 24, True, "IBM VGA")
    assert sauce.credit == "Nib Logo by InkWell/Quill"
    assert sauce.comments == ("hello",)


def test_the_credit_leaves_out_missing_parts():
    tail = build_sauce(width=80, lines=1, author="InkWell")
    _, sauce = split_sauce(ART + tail)
    assert sauce is not None and sauce.credit == "InkWell"
    _, sauce = split_sauce(ART + build_sauce(width=80, lines=1, title="Logo"))
    assert sauce is not None and sauce.credit == "Logo"
