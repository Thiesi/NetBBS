"""Website captures stay audited when accessible markup or preview art is used."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "website_check_pages", Path(__file__).resolve().parents[1] / "scripts/website_check_pages.py")
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def document(body: str, *, preview: bool = False) -> bytes:
    kind = '<meta name="netbbs-capture-kind" content="concept-art">' if preview else ""
    return ('<!DOCTYPE html><html><head><meta charset="utf-8">' + kind
            + '<style>.term{}.shot{}.term-title{}.term-body{}.concept-art{}</style>'
            + '</head><body>' + body + '</body></html>').encode()


def test_accessible_capture_still_detects_bad_terminal_markup():
    body = ('<div class="term shot"><span class="term-title">screen</span>'
            '<pre class="term-body" tabindex="0" role="img" aria-label="screen">'
            '<b>unescaped markup</b></pre></div>')
    assert len(list(checker.captures(body))) == 1
    assert not checker.check("accessible capture", document(body))


def test_terminal_page_cannot_pass_without_captures():
    assert not checker.check("empty page", document("<p>No screens</p>"))


def test_development_page_requires_labelled_concept_art():
    art = ('<figure class="concept-art"><img src="hero.jpg" alt="Moon">'
           '<figcaption>Project concept art; in development.</figcaption></figure>')
    assert checker.check("preview", document(art, preview=True))
    assert not checker.check("missing art", document("", preview=True))
    assert not checker.check("unlabelled art", document(art.replace("concept art", "image"),
                                                       preview=True))


def test_preview_declaration_cannot_hide_a_terminal_capture():
    body = ('<figure class="concept-art"><img src="hero.jpg" alt="Moon">'
            '<figcaption>Project concept art</figcaption></figure>'
            '<div class="term shot"><span class="term-title">screen</span>'
            '<pre class="term-body"><span>x</span></pre></div>')
    assert not checker.check("mixed preview", document(body, preview=True))


CONVERTER_SPEC = importlib.util.spec_from_file_location(
    "website_ansi_to_html", Path(__file__).resolve().parents[1] / "scripts/website_ansi_to_html.py")
assert CONVERTER_SPEC and CONVERTER_SPEC.loader
converter = importlib.util.module_from_spec(CONVERTER_SPEC)
# Its dataclasses look their module up while being defined.
sys.modules.setdefault(CONVERTER_SPEC.name, converter)
CONVERTER_SPEC.loader.exec_module(converter)


def test_block_glyphs_are_painted_as_cell_fills_not_font_glyphs():
    # A font's half block spans neither the 1ch cell nor the line height, so
    # thin strips of background showed between half-block pixels.
    html = converter.render("\x1b[38;2;1;2;3;48;2;4;5;6m▀▄█\x1b[0m▀A", width=8, height=1)
    assert "▀" not in html and "▄" not in html and "█" not in html
    assert "background:linear-gradient(rgb(1,2,3) 50%,rgb(4,5,6) 50%)" in html
    assert "background:linear-gradient(rgb(4,5,6) 50%,rgb(1,2,3) 50%)" in html
    assert "background:rgb(1,2,3);display:inline-block;width:1ch" in html
    # Without a background the lower half stays transparent; text is untouched.
    assert "linear-gradient(rgb(199,202,212) 50%,transparent 50%)" in html
    assert ">A</span>" in html
