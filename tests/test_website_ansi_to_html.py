"""The website converter paints block glyphs as cell fills, quadrants included."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "website_ansi_to_html", Path(__file__).resolve().parents[1] / "scripts/website_ansi_to_html.py")
assert SPEC and SPEC.loader
converter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = converter  # its dataclasses look their module up
SPEC.loader.exec_module(converter)

RED, BLUE = "rgb(255,0,0)", "rgb(0,0,255)"


def cell(glyph: str) -> str:
    return converter.render("\x1b[38;2;255;0;0;48;2;0;0;255m" + glyph, width=4, height=1)


def test_every_quadrant_glyph_is_painted_not_typed():
    for glyph in "▘▝▖▗▞▚▛▜▙▟":
        html = cell(glyph)
        assert "conic-gradient(" in html and f">{glyph}<" not in html, glyph


def test_quadrant_quarters_take_the_foreground_they_name():
    # The gradient runs clockwise from the top: upper right, lower right,
    # lower left, upper left.
    assert f"conic-gradient({BLUE} 0 25%,{BLUE} 25% 50%,{BLUE} 50% 75%,{RED} 75%)" in cell("▘")
    assert f"conic-gradient({RED} 0 25%,{RED} 25% 50%,{RED} 50% 75%,{BLUE} 75%)" in cell("▟")
    assert f"conic-gradient({RED} 0 25%,{BLUE} 25% 50%,{RED} 50% 75%,{BLUE} 75%)" in cell("▞")


def test_half_blocks_keep_their_linear_fill():
    assert f"linear-gradient({RED} 50%,{BLUE} 50%)" in cell("▀")
    assert "conic-gradient" not in cell("▀")
