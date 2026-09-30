"""
SAUCE records on ANSI art files (issue #929; design doc §3.2, "SysOp art:
storage and SAUCE").

SAUCE (Standard Architecture for Universal Comment Extensions,
https://www.acid.org/info/sauce/sauce.htm) is the 128-byte metadata record
the art scene appends to its files: title, author, group, width, the iCE
colour flag, the font. A file that carries one ends

    <art> EOF(0x1A) [ "COMNT" + 64 bytes per comment line ] "SAUCE00" ...

and a viewer that does not know SAUCE shows all of that under the picture.
`split_sauce` separates the art from the rest; nothing here touches the
filesystem.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SAUCE_RECORD_SIZE = 128
COMMENT_LINE_SIZE = 64
EOF = 0x1A

_RECORD_ID = b"SAUCE"
_COMMENT_ID = b"COMNT"
# ID(5) Version(2) Title(35) Author(20) Group(20) Date(8) FileSize(u32)
# DataType(u8) FileType(u8) TInfo1-4(u16 each) Comments(u8) TFlags(u8)
# TInfoS(22), little-endian.
_LAYOUT = struct.Struct("<5s2s35s20s20s8sIBBHHHHBB22s")

# DataType 1 is "Character"; its FileType 1 is ANSi, 0 ASCII, 2 ANSiMation.
DATA_TYPE_CHARACTER = 1
_ICE_COLORS_FLAG = 0x01
# Font names that mean the IBM PC character set, which is all NetBBS draws.
_CP437_FONTS = ("ibm vga", "ibm vga50", "ibm vga25g", "ibm ega", "ibm ega43")


def _text(field: bytes) -> str:
    return field.decode("cp437").rstrip("\x00 ")


@dataclass(frozen=True)
class Sauce:
    title: str
    author: str
    group: str
    date: str
    file_size: int
    data_type: int
    file_type: int
    tinfo1: int
    tinfo2: int
    tinfo3: int
    tinfo4: int
    tflags: int
    font: str
    comments: tuple[str, ...]

    @property
    def is_character_art(self) -> bool:
        return self.data_type == DATA_TYPE_CHARACTER

    @property
    def width(self) -> int | None:
        """Columns the art was drawn for, when the record says (TInfo1 of a
        character file); `None` when it doesn't."""
        if self.is_character_art and self.tinfo1 > 0:
            return self.tinfo1
        return None

    @property
    def ice_colors(self) -> bool:
        return self.is_character_art and bool(self.tflags & _ICE_COLORS_FLAG)

    @property
    def font_is_cp437(self) -> bool:
        """Whether the font is one NetBBS draws: the IBM PC set, named as
        such (optionally with its code page, "IBM VGA 437"), or not named."""
        name = self.font.strip().lower()
        if not name:
            return True
        if name in _CP437_FONTS:
            return True
        family, _, page = name.rpartition(" ")
        return family in _CP437_FONTS and page == "437"

    @property
    def credit(self) -> str:
        """"Title by Author/Group", leaving out whatever is missing."""
        who = "/".join(part for part in (self.author, self.group) if part)
        if self.title and who:
            return f"{self.title} by {who}"
        return self.title or who


def split_sauce(data: bytes) -> tuple[bytes, Sauce | None]:
    """`data` without its SAUCE record, comment block and EOF marker, and
    the record if there was one.

    Anything after the first EOF byte is dropped as well, SAUCE or not: DOS
    stopped reading a text file there, and art written for DOS relies on
    it. A record whose comment count points outside the file keeps its
    fields but drops the comments."""
    sauce: Sauce | None = None
    body = data
    if len(data) >= SAUCE_RECORD_SIZE and data[-SAUCE_RECORD_SIZE:].startswith(_RECORD_ID):
        fields = _LAYOUT.unpack(data[-SAUCE_RECORD_SIZE:])
        body = data[:-SAUCE_RECORD_SIZE]
        comment_count = fields[13]
        comments: tuple[str, ...] = ()
        block = len(_COMMENT_ID) + COMMENT_LINE_SIZE * comment_count
        if comment_count and len(body) >= block and body[-block:].startswith(_COMMENT_ID):
            raw = body[-block + len(_COMMENT_ID):]
            comments = tuple(
                _text(raw[i : i + COMMENT_LINE_SIZE]) for i in range(0, len(raw), COMMENT_LINE_SIZE)
            )
            body = body[:-block]
        sauce = Sauce(
            title=_text(fields[2]),
            author=_text(fields[3]),
            group=_text(fields[4]),
            date=_text(fields[5]),
            file_size=fields[6],
            data_type=fields[7],
            file_type=fields[8],
            tinfo1=fields[9],
            tinfo2=fields[10],
            tinfo3=fields[11],
            tinfo4=fields[12],
            tflags=fields[14],
            font=_text(fields[15]),
            comments=comments,
        )
    cut = body.find(bytes([EOF]))
    if cut >= 0:
        body = body[:cut]
    return body, sauce


def build_sauce(
    *,
    width: int,
    lines: int,
    title: str = "",
    author: str = "",
    group: str = "",
    date: str = "",
    ice_colors: bool = False,
    font: str = "IBM VGA",
    file_size: int = 0,
    comments: tuple[str, ...] = (),
) -> bytes:
    """EOF, an optional comment block and a SAUCE record for an ANSi file:
    what the art editor appends when it saves. Text fields are CP437,
    space-padded and cut to their field size."""

    def field(value: str, size: int) -> bytes:
        return value.encode("cp437", errors="replace")[:size].ljust(size, b" ")

    comment_lines = comments[:255]
    record = _LAYOUT.pack(
        _RECORD_ID,
        b"00",
        field(title, 35),
        field(author, 20),
        field(group, 20),
        field(date, 8),
        file_size & 0xFFFFFFFF,
        DATA_TYPE_CHARACTER,
        1,
        max(0, min(width, 0xFFFF)),
        max(0, min(lines, 0xFFFF)),
        0,
        0,
        len(comment_lines),
        _ICE_COLORS_FLAG if ice_colors else 0,
        font.encode("ascii", errors="replace")[:22].ljust(22, b"\x00"),
    )
    block = b""
    if comment_lines:
        block = _COMMENT_ID + b"".join(field(line, COMMENT_LINE_SIZE) for line in comment_lines)
    return bytes([EOF]) + block + record
