"""`netbbs.rendering.pipe_codes` (issue #298): Mystic `|NN` colour
codes become SGR after sanitization, everything else after a pipe is
dropped, and a rendered span always resets after itself."""

from __future__ import annotations

import pytest

from netbbs.rendering.ansi import RESET, bg, fg, strip_ansi
from netbbs.rendering.layout import visible_width
from netbbs.rendering.pipe_codes import render_pipe_codes, strip_non_color_pipe_codes, strip_pipe_codes
from netbbs.rendering.sanitize import sanitize_text


@pytest.mark.parametrize("code", range(0, 16))
def test_every_foreground_code_maps_to_the_matching_palette_entry(code):
    assert render_pipe_codes(f"|{code:02d}x") == f"{fg(code)}x{RESET}"


@pytest.mark.parametrize("code", range(17, 24))
def test_every_background_code_maps_to_the_matching_palette_entry(code):
    assert render_pipe_codes(f"|{code:02d}x") == f"{bg(code - 16)}x{RESET}"


def test_black_background_is_the_terminal_default_not_painted_black():
    rendered = render_pipe_codes("|16|07x")
    assert "\x1b[49m" in rendered and bg(0) not in rendered
    assert rendered.endswith(f"x{RESET}")


def test_non_colour_tokens_are_dropped_and_plain_pipes_survive():
    # `|UN` (an MCI variable), `|99` (no such colour) and `|9x` (two
    # alphanumerics, the token grammar every client strips) all go; a
    # pipe followed by a space, a single digit, or nothing stays text.
    assert render_pipe_codes("|UNname |99 |9x | end|") == "name   | end|"
    assert render_pipe_codes("a |9 b| c") == "a |9 b| c"
    assert render_pipe_codes("no codes at all") == "no codes at all"


def test_a_code_inside_a_word_splits_the_word_without_adding_spaces():
    assert strip_ansi(render_pipe_codes("wo|12rd")) == "word"


def test_rendering_never_emits_a_reset_without_a_colour():
    assert RESET not in render_pipe_codes("|UN only")


def test_strip_variants():
    assert strip_pipe_codes("|03<|11Alice|03> |UNhi |99") == "<Alice> hi "
    assert strip_non_color_pipe_codes("|03<|11Alice|03> |UNhi |99") == "|03<|11Alice|03> hi "


def test_display_width_ignores_the_emitted_sequences():
    rendered = render_pipe_codes("|03<|11Alice|03>|16|07 hello")
    assert visible_width(rendered) == len("<Alice> hello")


def test_sanitize_then_render_leaves_no_foreign_escape():
    # The order the bridge uses: foreign sequences and control bytes go
    # first, then the surviving pipe tokens become this module's own
    # sequences -- the only escapes left are the ones it emitted.
    hostile = "|12hi \x1b[31mthere\x07 |04!"
    rendered = render_pipe_codes(sanitize_text(strip_ansi(hostile)))
    assert "\x1b[31m" not in rendered and "\x07" not in rendered
    assert fg(12) in rendered and fg(4) in rendered
    assert strip_ansi(rendered) == "hi there !"
