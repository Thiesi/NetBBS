"""
Draw www.netbbs.org's icon: a small computer terminal with a prompt.

The icon is one 16x16 pixel grid below, drawn for the size a browser tab
shows it at. Every file is made from that grid, so the tab, a bookmark and a
phone's home screen show the same picture:

    favicon.svg           the grid as squares; browsers that take SVG
    favicon.ico           16, 32 and 48 px, each a whole-pixel scale
    apple-touch-icon.png  180 px on the site's background colour

They are written twice, to the website (`web/`) and to the browser
terminal every node serves (`src/netbbs/web/static/`, issue #1199), so the
project's site and a board's terminal show the same icon and cannot drift
apart; a test checks the two copies are the same bytes.

Whole-pixel scaling keeps every edge sharp; a smooth resize of a 16 px
drawing blurs it. Standard library only (zlib, struct), like the rest of
the site tooling.

    python scripts/website_favicon.py [directory]

With a directory, only that one is written.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

# The site's own palette (web/netbbs-index.html's :root).
COLORS = {
    "C": (0xFF, 0x6B, 0x52),  # --coral: the terminal's case
    "D": (0x0C, 0x0F, 0x16),  # --bg: its screen
    "T": (0x54, 0xD6, 0xA8),  # --teal: the prompt
}
BACKGROUND = (0x0C, 0x0F, 0x16)  # behind the phone icon, which may not be transparent

GRID = [
    "................",
    "..CCCCCCCCCCCC..",
    ".CDDDDDDDDDDDDC.",
    ".CDDDDDDDDDDDDC.",
    ".CDTTDDDDDDDDDC.",
    ".CDDTTDDDDDDDDC.",
    ".CDDDTTDDDDDDDC.",
    ".CDDTTDDDDDDDDC.",
    ".CDTTDDTTTTDDDC.",
    ".CDDDDDDDDDDDDC.",
    ".CDDDDDDDDDDDDC.",
    "..CCCCCCCCCCCC..",
    "......CCCC......",
    "......CCCC......",
    "....CCCCCCCC....",
    "................",
]
SIZE = len(GRID)
assert all(len(row) == SIZE for row in GRID)

Pixel = tuple[int, int, int, int]


def pixels(scale: int, *, margin: int = 0, background: tuple[int, int, int] | None = None) -> list[list[Pixel]]:
    """The grid at `scale` screen pixels per grid pixel, inside `margin`."""
    side = SIZE * scale + 2 * margin
    fill: Pixel = (*background, 255) if background else (0, 0, 0, 0)
    image = [[fill] * side for _ in range(side)]
    for y, row in enumerate(GRID):
        for x, cell in enumerate(row):
            if cell == ".":
                continue
            color: Pixel = (*COLORS[cell], 255)
            for dy in range(scale):
                for dx in range(scale):
                    image[margin + y * scale + dy][margin + x * scale + dx] = color
    return image


def png(image: list[list[Pixel]]) -> bytes:
    """`image` as an 8-bit RGBA PNG."""
    height, width = len(image), len(image[0])
    raw = b"".join(b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in image)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico(images: list[bytes], sizes: list[int]) -> bytes:
    """PNG images in one .ico, the form every current browser reads."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, data = b"", b""
    for image, size in zip(images, sizes):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset + len(data))
        data += image
    return header + entries + data


def svg() -> str:
    """The grid as squares, one per run of a colour along a row."""
    rects = []
    for y, row in enumerate(GRID):
        x = 0
        while x < SIZE:
            cell = row[x]
            run = 1
            while x + run < SIZE and row[x + run] == cell:
                run += 1
            if cell != ".":
                r, g, b = COLORS[cell]
                rects.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="#{r:02x}{g:02x}{b:02x}"/>')
            x += run
    body = "".join(rects)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" '
        f'shape-rendering="crispEdges">{body}</svg>\n'
    )


#: Where the icons are written by default (see the module docstring).
TARGETS = ("web", "src/netbbs/web/static")

#: The file names, in both places.
FILES = ("favicon.svg", "favicon.ico", "apple-touch-icon.png")


def icon_files() -> dict[str, bytes]:
    """Every icon file, by name."""
    sizes = [16, 32, 48]
    return {
        "favicon.svg": svg().encode("ascii"),
        "favicon.ico": ico([png(pixels(size // SIZE)) for size in sizes], sizes),
        # 180 px: ten pixels per grid pixel and a ten-pixel border of background.
        "apple-touch-icon.png": png(pixels(10, margin=10, background=BACKGROUND)),
    }


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    targets = [Path(argv[1])] if len(argv) > 1 else [root / target for target in TARGETS]
    files = icon_files()
    for target in targets:
        for name in FILES:
            (target / name).write_bytes(files[name])
            print(f"{target / name}: {(target / name).stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
