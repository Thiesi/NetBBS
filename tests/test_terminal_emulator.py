"""The server-side copy of a caller's screen (issue #764)."""

from __future__ import annotations

import pytest

from netbbs.rendering.ansi import clear_screen, colored, move_cursor, reset_scroll_region, set_scroll_region
from netbbs.rendering.terminal_emulator import Pen, TerminalEmulator, apply_sgr, pen_sgr


def _emu(text: str, width: int = 20, height: int = 5) -> TerminalEmulator:
    emulator = TerminalEmulator(width, height)
    emulator.feed(text)
    return emulator


def _rows(emulator: TerminalEmulator) -> list[str]:
    return [row.rstrip() for row in emulator.text_rows()]


def test_text_crlf_and_bare_lf():
    emulator = _emu("one\r\ntwo\nthree")
    assert _rows(emulator)[:3] == ["one", "two", "   three"]


def test_cursor_addressing_and_relative_moves():
    emulator = _emu(move_cursor(3, 5) + "X" + "\x1b[2A" + "Y" + "\x1b[3D" + "Z" + "\x1b[G" + "<")
    rows = _rows(emulator)
    assert rows[2] == "    X"
    assert rows[0] == "<  Z Y"


def test_wrapping_is_deferred_until_the_next_character():
    emulator = _emu("x" * 20)
    assert (emulator.row, emulator.col) == (0, 19)
    emulator.feed("\r\n!")
    # Filling the last column must not have produced an extra blank line.
    assert _rows(emulator)[:2] == ["x" * 20, "!"]


def test_scrolling_at_the_bottom():
    emulator = _emu("\r\n".join(str(i) for i in range(7)))
    assert _rows(emulator) == ["2", "3", "4", "5", "6"]


def test_scroll_region_keeps_pinned_rows_still():
    # Chat's layout: a scroll region above two pinned rows.
    emulator = _emu(move_cursor(4, 1) + "status" + move_cursor(5, 1) + "input" + set_scroll_region(1, 3))
    emulator.feed(move_cursor(3, 1) + "a\r\nb\r\nc\r\nd")
    assert _rows(emulator) == ["b", "c", "d", "status", "input"]
    emulator.feed(reset_scroll_region())
    assert (emulator.top, emulator.bottom) == (0, 4)


def test_erase_display_and_line():
    emulator = _emu("hello world" + "\x1b[1;6H" + "\x1b[K")
    assert _rows(emulator)[0] == "hello"
    emulator.feed("\x1b[1;3H\x1b[1K")
    assert _rows(emulator)[0] == "   lo"
    emulator.feed(clear_screen())
    assert _rows(emulator) == [""] * 5
    assert (emulator.row, emulator.col) == (0, 0)


def test_insert_and_delete_characters_and_lines():
    emulator = _emu("abcdef\x1b[1;3H\x1b[2@")
    assert _rows(emulator)[0] == "ab  cdef"
    emulator.feed("\x1b[2P")
    assert _rows(emulator)[0] == "abcdef"
    emulator.feed("\r\n2\r\n3\x1b[1;1H\x1b[L")
    assert _rows(emulator)[:4] == ["", "abcdef", "2", "3"]
    emulator.feed("\x1b[M")
    assert _rows(emulator)[:3] == ["abcdef", "2", "3"]


def test_cursor_save_and_restore_both_forms():
    emulator = _emu("\x1b[2;2H\x1b7\x1b[5;5H\x1b8*")
    assert _rows(emulator)[1] == " *"
    emulator.feed("\x1b[3;3H\x1b[s\x1b[1;1H\x1b[u#")
    assert _rows(emulator)[2] == "  #"


def test_colours_bold_underline_reverse():
    emulator = _emu(
        colored("r", fg_color=196) + colored("t", fg_color=(1, 2, 3), bold=True)
        + "\x1b[4;7;41mu\x1b[0m" + "\x1b[92mg"
    )
    cells = emulator.snapshot()[0]
    assert cells[0].fg == 196
    assert cells[1].fg == (1, 2, 3) and cells[1].bold
    assert cells[2].underline and cells[2].reverse and cells[2].bg == 1
    assert cells[3].fg == 10 and not cells[3].bold


def test_wide_glyphs_take_two_columns_and_wrap_whole():
    emulator = _emu("漢字", width=5)
    assert (emulator.row, emulator.col) == (0, 4)
    emulator.feed("字")
    assert emulator.text_rows()[1].startswith("字")


def test_combining_mark_joins_its_base():
    emulator = _emu("éx")
    assert emulator.snapshot()[0][0].char == "é"
    assert emulator.snapshot()[0][1].char == "x"


def test_a_sequence_split_across_writes_still_parses():
    emulator = TerminalEmulator(20, 5)
    for chunk in ("\x1b", "[3", "1", "mR", "\x1b[0", "m."):
        emulator.feed(chunk)
    cells = emulator.snapshot()[0]
    assert cells[0].char == "R" and cells[0].fg == 1
    assert cells[1].char == "." and cells[1].fg is None


@pytest.mark.parametrize(
    "garbage",
    [
        "\x1b]0;window title\x07after",
        "\x1b]0;title\x1b\\after",
        "\x1b(Bafter",
        "\x1b[?1049hafter",
        "\x1b[" + "9" * 200 + "mafter",
        "\x1b[99;99;99zafter",
        "\x1b[38;5mafter",
    ],
)
def test_unknown_and_malformed_sequences_are_consumed(garbage):
    emulator = _emu(garbage)
    assert "after" in _rows(emulator)[0]


def test_cursor_visibility_is_tracked():
    emulator = _emu("\x1b[?25l")
    assert not emulator.cursor_visible
    emulator.feed("\x1b[?25h")
    assert emulator.cursor_visible


def test_resize_keeps_the_top_left():
    emulator = _emu("abcdef\r\nghijkl", width=10, height=3)
    emulator.resize(4, 2)
    assert emulator.text_rows() == ["abcd", "ghij"]
    emulator.resize(6, 3)
    assert _rows(emulator) == ["abcd", "ghij", ""]


def test_restore_reproduces_the_screen_on_a_fresh_terminal():
    source = _emu(
        colored("red", fg_color=1) + "\r\n" + set_scroll_region(2, 4) + move_cursor(3, 7) + "\x1b[1;4m" + "\x1b[?25l",
        width=20, height=5,
    )
    replica = TerminalEmulator(20, 5)
    replica.feed("junk that was on screen\r\nmore junk")
    replica.feed(source.restore_ansi())
    assert replica.snapshot() == source.snapshot()
    assert (replica.row, replica.col) == (source.row, source.col)
    assert (replica.top, replica.bottom) == (source.top, source.bottom)
    assert replica.pen == source.pen
    assert replica.cursor_visible is False


def test_pen_sgr_round_trips():
    for pen in (Pen(), Pen(fg=3, bold=True), Pen(fg=(9, 8, 7), bg=200, underline=True, reverse=True)):
        assert apply_sgr(Pen(fg=5), [int(p) for p in pen_sgr(pen)[2:-1].split(";") if p]) == pen


def test_huge_sizes_are_clamped():
    emulator = TerminalEmulator(100_000, 100_000)
    assert (emulator.width, emulator.height) == (500, 200)
