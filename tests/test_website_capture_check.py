"""Website captures stay audited when accessible markup or preview art is used."""
from __future__ import annotations

import importlib.util
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
