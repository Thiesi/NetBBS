"""The pictographs of CP437's control range in SysOp art (issue #929, step 3).

Scene art draws with bytes 0x01-0x1F and 0x7F: a CP437 terminal shows them
as ☺ ♥ ♫ ► and the rest. Before this they reached a UTF-8 terminal as raw
control bytes and were lost; the art editor dropped them.
"""

from __future__ import annotations

import asyncio

from netbbs.net.session import Session, write_preformatted_line
from netbbs.rendering import decode_ansi_bytes, decode_banner_bytes, encode_cp437_art
from netbbs.rendering.charset import ASCII, CP437, UTF8, ART_PICTOGRAPHS, map_text
from netbbs.rendering.width import char_width

# Art using pictographs alongside the controls it needs as controls.
ART = b"\x1b[31m\x03\x03 \x0e \x10\x1e\x7f\x01\x02\r\n\tx\x1b[0m"


class _Session(Session):
    """A caller whose output goes through the real write path, so mapping
    and art handling are exactly what a transport would send."""

    write = Session.write

    def __init__(self, charset: str) -> None:
        self.output_charset = charset
        self.terminal_width = 80
        self.terminal_wraps_immediately = False
        self.sent: list[str] = []

    async def _send_text(self, text: str) -> None:
        self.sent.append(text)

    async def read_line(self, *args, **kwargs) -> str:
        raise AssertionError("unused")

    async def read_key(self, *args, **kwargs) -> str:
        raise AssertionError("unused")

    async def read_editor_key(self):
        raise AssertionError("unused")

    async def close(self) -> None:
        pass


def _sent(charset: str, text: str) -> str:
    session = _Session(charset)
    asyncio.run(write_preformatted_line(session, text))
    return "".join(session.sent)


def test_cp437_art_decodes_pictographs_as_glyphs():
    text = decode_ansi_bytes(ART)
    assert "♥♥ ♫ ►▲⌂☺☻" in text
    # The controls art needs stay controls.
    assert "\x1b[31m" in text and "\r\n" in text and "\t" in text


def test_the_excluded_controls_are_not_pictographs():
    assert set(ART_PICTOGRAPHS) & {0x07, 0x08, 0x09, 0x0A, 0x0D, 0x1A, 0x1B} == set()
    assert all(char_width(glyph) == 1 for glyph in ART_PICTOGRAPHS.values())


def test_a_utf8_terminal_gets_the_glyph_not_a_control_byte():
    out = _sent(UTF8, decode_banner_bytes(ART))
    assert "♥♥" in out and "►▲⌂☺☻" in out
    assert not any(c in out for c in "\x01\x02\x03\x0e\x10\x1e\x7f")


def test_a_cp437_terminal_gets_the_original_bytes_back():
    out = _sent(CP437, decode_banner_bytes(ART))
    data = out.encode("cp437")
    assert b"\x03\x03 \x0e \x10\x1e\x7f\x01\x02" in data


def test_an_ascii_terminal_gets_plain_substitutes():
    out = _sent(ASCII, decode_banner_bytes(ART))
    assert out.isascii()
    assert "** ~ >^^@@" in out


def test_ordinary_text_on_a_cp437_terminal_never_becomes_a_control_byte():
    # A caller typing ♥ in a post: on a CP437 session it is a printable
    # substitute, not byte 0x03, which only the art path may send.
    mapped = map_text("I ♥ pens ☺", CP437)
    assert mapped == "I * pens @"


def test_the_editor_saves_pictographs_as_their_cp437_bytes():
    assert encode_cp437_art("♥☺⌂▲") == b"\x03\x01\x7f\x1e"
    # And reads them back.
    assert decode_ansi_bytes(b"\x03\x01\x7f\x1e") == "♥☺⌂▲"


def test_utf8_art_gets_pictographs_too():
    # A file of plain ASCII plus pictograph bytes is valid UTF-8, so the
    # glyphs apply on that branch as well; real UTF-8 characters stay.
    assert decode_ansi_bytes("\x01☺é".encode()) == "☺☺é"


def test_an_art_post_keeps_pictographs_as_glyphs_not_control_bytes():
    from netbbs.rendering.post_body import art_body_from_editor

    body = art_body_from_editor(encode_cp437_art("\x1b[0m♥ ☺ ►\r\n"))
    assert "♥ ☺ ►" in body
    assert not any(c in body for c in "\x01\x03\x10")
