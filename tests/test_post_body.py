"""
Color in message board post bodies (issue #711): the filter, the three
display modes, and rows that stand alone.
"""

from __future__ import annotations

import re

import pytest

from netbbs.rendering.post_body import (
    colored_body_rows,
    plain_post_body,
    post_body_mode,
    post_body_rows,
    post_body_text,
    styled_post_body,
)
from netbbs.rendering.width import display_width

ESC = "\x1b"
_SGR = re.compile(ESC + r"\[([0-9;]*)m")
_ALLOWED_SINGLE = {0, 1, 4, 5, 22, 24, 25, 39, 49} | set(range(30, 38)) | set(range(40, 48)) | set(range(90, 98)) | set(range(100, 108))


def _only_allowed_sgr(text: str) -> bool:
    """Every escape in `text` is an SGR made of allowed codes."""
    if ESC in _SGR.sub("", text):
        return False
    for match in _SGR.finditer(text):
        params = [int(p) if p else 0 for p in match.group(1).split(";")] if match.group(1) else [0]
        index = 0
        while index < len(params):
            code = params[index]
            if code in (38, 48):
                width = 3 if params[index + 1] == 5 else 5
                index += width
                continue
            if code not in _ALLOWED_SINGLE:
                return False
            index += 1
    return True


# Every class of sequence a post must not be able to send (issue #711).
HOSTILE = {
    "cursor up": ESC + "[5A",
    "cursor position": ESC + "[10;10H",
    "cursor position f": ESC + "[3;4f",
    "clear screen": ESC + "[2J",
    "clear line": ESC + "[K",
    "hide cursor": ESC + "[?25l",
    "alternate screen": ESC + "[?1049h",
    "scroll region": ESC + "[1;5r",
    "insert lines": ESC + "[3L",
    "window title (BEL)": ESC + "]0;pwned" + "\x07",
    "window title (ST)": ESC + "]2;pwned" + ESC + "\\",
    "unterminated OSC": ESC + "]0;runs to the end",
    "DCS": ESC + "Pq#0;2;0;0;0" + ESC + "\\",
    "APC": ESC + "_payload" + ESC + "\\",
    "PM": ESC + "^privacy" + ESC + "\\",
    "SOS": ESC + "Xstring" + ESC + "\\",
    "8-bit CSI": "\x9b2J",
    "8-bit OSC": "\x9d0;pwned\x9c",
    "bell": "\x07",
    "carriage return": "\r",
    "reset terminal": ESC + "c",
    "save cursor": ESC + "7",
    "charset": ESC + "(0",
    "private SGR": ESC + "[?4m",
    "SGR with intermediates": ESC + "[1 m",
    "lone ESC at the end": ESC,
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_sequences_never_reach_a_reader(name):
    body = f"before{HOSTILE[name]}after"

    for rendered in (styled_post_body(body), plain_post_body(body), post_body_text(body)):
        assert _only_allowed_sgr(rendered), (name, rendered)
        assert "before" in rendered
        assert "[2J" not in rendered and "pwned" not in rendered and "payload" not in rendered


@pytest.mark.parametrize("code", [2, 3, 7, 8, 9, 21, 53])
def test_sgr_outside_color_bold_underline_blink_is_dropped(code):
    """Dim, italic, inverse, conceal (which hides text), strike..."""
    styled = styled_post_body(f"{ESC}[31;{code}mtext")

    assert f";{code}m" not in styled and f"[{code}m" not in styled
    assert f"{ESC}[31m" in styled


def test_allowed_sgr_survives():
    styled = styled_post_body(f"{ESC}[1;4;5;31;42mhi{ESC}[38;5;196m!{ESC}[0m")

    assert f"{ESC}[1;4;5;31;42m" in styled
    assert f"{ESC}[38;5;196m" in styled
    assert _only_allowed_sgr(styled)


def test_truecolor_is_kept_only_where_the_session_has_it():
    body = f"{ESC}[38;2;255;0;0mred"

    assert f"{ESC}[38;2;255;0;0m" in styled_post_body(body, truecolor=True)
    downgraded = styled_post_body(body, truecolor=False)
    assert "38;2" not in downgraded and "38;5;" in downgraded


def test_a_malformed_extended_color_drops_what_follows_it():
    styled = styled_post_body(f"{ESC}[38;5;31mtext")  # 38;5 then 31 is an index, fine
    assert f"{ESC}[38;5;31m" in styled
    assert _only_allowed_sgr(styled_post_body(f"{ESC}[38;9;1mtext"))


def test_pipe_codes_become_color_and_other_pipes_stay_text():
    body = "|12red |20on blue ls |grep |99 done"

    styled = styled_post_body(body)
    assert f"{ESC}[38;5;9m" in styled  # CGA 12 is bright red
    assert "ls |grep |99 done" in styled
    assert plain_post_body(body) == "red on blue ls |grep |99 done"
    assert post_body_text(body) == body  # a board without color shows them as typed


def test_a_colored_body_always_ends_reset():
    assert styled_post_body("|12red").endswith(f"{ESC}[0m")
    assert styled_post_body("no color at all") == "no color at all"


def test_modes():
    assert post_body_mode(board_allows_color=True, reader_wants_color=True) == "color"
    assert post_body_mode(board_allows_color=True, reader_wants_color=False) == "plain"
    assert post_body_mode(board_allows_color=False, reader_wants_color=True) == "text"


@pytest.mark.parametrize("width", [40, 80])
def test_colored_rows_fit_and_stand_alone(width):
    body = "|12" + " ".join(f"word{i}" for i in range(80)) + "|07 tail\n\n> quoted |10green " + "q " * 60
    rows = colored_body_rows(styled_post_body(body), width)

    assert len(rows) > 3
    for row in rows:
        assert display_width(_SGR.sub("", row)) <= width
        if _SGR.search(row):
            assert row.endswith(f"{ESC}[0m")
    # A row inside the red run restates red: it cannot rely on the row above.
    assert rows[1].startswith(f"{ESC}[38;5;9m")
    quote = next(row for row in rows if "quoted" in _SGR.sub("", row))
    assert _SGR.sub("", quote).startswith("> ")


def test_a_color_flood_costs_each_row_one_short_prefix():
    body = "".join(f"|{i % 16:02d}x " for i in range(5000))
    rows = colored_body_rows(styled_post_body(body), 40)

    for row in rows[1:]:
        prefix = _SGR.match(row)
        assert prefix is not None and len(prefix.group(0)) < 40


@pytest.mark.parametrize("mode", ["plain", "text"])
def test_uncolored_modes_lay_out_like_the_plain_reader(mode):
    rows = post_body_rows("|12Hello\n\n> a quote", 40, mode, truecolor=False)

    assert ESC not in "".join(_SGR.sub("", row) for row in rows)
    assert ("|12" in rows[0]) == (mode == "text")


# -- art posts (issue #711) ---------------------------------------------------------

from netbbs.rendering import ScreenBuffer, encode_ansi_bytes, parse_ansi_into_buffer  # noqa: E402
from netbbs.rendering.post_body import art_body_from_editor, art_body_rows  # noqa: E402


def _canvas(text: str, width: int = 80, height: int = 10) -> bytes:
    buffer = ScreenBuffer(width, height)
    parse_ansi_into_buffer(text, buffer)
    return encode_ansi_bytes(buffer)


def test_a_canvas_becomes_a_body_as_wide_and_tall_as_the_drawing():
    body = art_body_from_editor(_canvas(f"{ESC}[31mHI{ESC}[0m there\r\n{ESC}[44m    {ESC}[0m"))

    lines = body.split("\n")
    assert len(lines) == 2  # the eight blank rows below are dropped
    assert _SGR.sub("", lines[0]) == "HI there"  # blank default cells trimmed
    assert _SGR.sub("", lines[1]) == "    "  # a colored background is part of the picture


def test_an_art_post_keeps_its_lines():
    body = "short\nlines stay\nas drawn"

    rows = art_body_rows(styled_post_body(body), 80)

    assert [_SGR.sub("", row) for row in rows] == ["short", "lines stay", "as drawn"]
    # The same body as prose is one reflowed paragraph.
    assert len(post_body_rows(body, 80, "color", truecolor=True)) == 1


def test_a_wide_art_line_wraps_at_the_column_with_its_color():
    body = f"{ESC}[31m" + "#" * 80

    rows = art_body_rows(styled_post_body(body), 40)

    assert [len(_SGR.sub("", row)) for row in rows] == [40, 40]
    assert rows[1].startswith(f"{ESC}[31m")


@pytest.mark.parametrize("mode", ["plain", "text"])
def test_an_art_post_keeps_its_lines_without_color(mode):
    rows = post_body_rows("|12one\ntwo", 80, mode, truecolor=False, layout="art")

    assert len(rows) == 2 and ESC not in rows[0] + rows[1]

# -- Codex review on #750 ---------------------------------------------------------


def test_a_color_code_between_spaces_is_not_a_word():
    rows = colored_body_rows(styled_post_body("hello |12 world |07 again"), 80)

    assert _SGR.sub("", rows[0]) == "hello world again"


@pytest.mark.parametrize("body", [" |12> quoted", "|12 > quoted", f" {ESC}[31m > quoted"])
def test_a_quote_marker_behind_indentation_and_color_is_drawn_once(body):
    rows = colored_body_rows(styled_post_body(body), 80)

    assert _SGR.sub("", rows[0]) == "> quoted"
