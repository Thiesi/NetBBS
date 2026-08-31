"""Tests for netbbs.rendering.ansi_parse.parse_ansi_into_buffer --
rasterizing externally-authored ANSI text into a ScreenBuffer, the
loading half of what makes editing an
existing file possible (netbbs.rendering.ansi_art.decode_ansi_bytes
only ever does byte decoding, never escape-sequence interpretation)."""

from __future__ import annotations

from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.screen_buffer import Cell, ScreenBuffer


def test_plain_text_is_written_left_to_right():
    buf = ScreenBuffer(10, 1)
    parse_ansi_into_buffer("abc", buf)
    assert buf.get_cell(0, 0).char == "a"
    assert buf.get_cell(0, 1).char == "b"
    assert buf.get_cell(0, 2).char == "c"


def test_crlf_moves_to_the_start_of_the_next_row():
    buf = ScreenBuffer(10, 2)
    parse_ansi_into_buffer("ab\r\ncd", buf)
    assert buf.get_cell(0, 0).char == "a"
    assert buf.get_cell(0, 1).char == "b"
    assert buf.get_cell(1, 0).char == "c"
    assert buf.get_cell(1, 1).char == "d"


def test_bare_lf_alone_advances_row_without_resetting_column():
    buf = ScreenBuffer(10, 2)
    parse_ansi_into_buffer("ab\nc", buf)
    # No \r before \n -- column carries over from where "ab" left off.
    assert buf.get_cell(1, 2).char == "c"


def test_sgr_foreground_color():
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[31mR", buf)
    assert buf.get_cell(0, 0) == Cell(char="R", fg=1)


def test_sgr_bright_foreground_color():
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[91mR", buf)
    assert buf.get_cell(0, 0).fg == 9


def test_sgr_background_color():
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[44mB", buf)
    assert buf.get_cell(0, 0) == Cell(char="B", bg=4)


def test_sgr_bold():
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[1mX", buf)
    assert buf.get_cell(0, 0) == Cell(char="X", bold=True)


def test_sgr_extended_256_color_foreground():
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[38;5;200mX", buf)
    assert buf.get_cell(0, 0).fg == 200


def test_sgr_extended_256_color_background():
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[48;5;201mX", buf)
    assert buf.get_cell(0, 0).bg == 201


def test_sgr_truecolor_foreground():
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[38;2;255;42;109mX", buf)
    assert buf.get_cell(0, 0).fg == (255, 42, 109)


def test_sgr_truecolor_background():
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[48;2;10;20;30mX", buf)
    assert buf.get_cell(0, 0).bg == (10, 20, 30)


def test_sgr_truecolor_does_not_corrupt_subsequent_state():
    # GitHub issue #210 (2026-08-31 ReLink dogfood): before the 38;2/
    # 48;2 branches existed, a truecolor sequence matched no `_apply_sgr`
    # branch at all -- its "2" and the r/g/b integers that followed fell
    # through and got reinterpreted as unrelated, independent SGR codes
    # (e.g. "255" as a 256-color index via a *different* 38;5 sequence
    # earlier in the same params list, "42" as green foreground, "109"
    # as nothing recognized) instead of being consumed as one unit.
    # Confirms both the color itself and the character immediately after
    # the sequence land correctly, with no leftover corrupted style.
    buf = ScreenBuffer(10, 1)
    parse_ansi_into_buffer("\x1b[38;2;255;42;109mX\x1b[0mY", buf)
    assert buf.get_cell(0, 0) == Cell(char="X", fg=(255, 42, 109))
    assert buf.get_cell(0, 1) == Cell(char="Y")


def test_sgr_out_of_range_truecolor_component_is_clamped_not_stored_raw():
    # Code review follow-up (PR #211): an out-of-range but well-formed
    # sequence like 38;2;999;1;2 used to be stored on the Cell
    # unvalidated -- harmless at parse time, but this module's own
    # promise of best-effort parsing that "still can't corrupt anything
    # irrecoverably" broke the moment such a Cell reached full_render_
    # ansi -> colored() -> fg_rgb(), which raises ValueError on an
    # out-of-range component. Confirms the parser itself never produces
    # an invalid value in the first place.
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[38;2;999;1;2mX", buf)
    fg = buf.get_cell(0, 0).fg
    assert isinstance(fg, tuple)
    assert all(0 <= component <= 255 for component in fg)


def test_sgr_out_of_range_256_color_index_is_clamped_not_stored_raw():
    # Same class of bug, the pre-existing 256-color form -- 38;5;999 is
    # equally well-formed-looking and equally capable of reaching fg(),
    # which raises ValueError on an out-of-range palette index.
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[38;5;999mX", buf)
    assert 0 <= buf.get_cell(0, 0).fg <= 255


def test_sgr_reset_clears_style():
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[31;1mR\x1b[0mN", buf)
    assert buf.get_cell(0, 0) == Cell(char="R", fg=1, bold=True)
    assert buf.get_cell(0, 1) == Cell(char="N")


def test_style_carries_across_characters_until_changed():
    buf = ScreenBuffer(5, 1)
    parse_ansi_into_buffer("\x1b[32mAB", buf)
    assert buf.get_cell(0, 0).fg == 2
    assert buf.get_cell(0, 1).fg == 2


def test_absolute_cursor_position():
    buf = ScreenBuffer(10, 5)
    parse_ansi_into_buffer("\x1b[3;5HX", buf)
    assert buf.get_cell(2, 4).char == "X"  # 1-indexed in the sequence, 0-indexed in the buffer


def test_cursor_position_with_no_params_defaults_to_home():
    buf = ScreenBuffer(10, 5)
    parse_ansi_into_buffer("\x1b[3;3H\x1b[HX", buf)
    assert buf.get_cell(0, 0).char == "X"


def test_relative_cursor_movement():
    buf = ScreenBuffer(10, 5)
    # Move to (3,3), then down 1 / right 2, write there.
    parse_ansi_into_buffer("\x1b[3;3H\x1b[1B\x1b[2CX", buf)
    assert buf.get_cell(3, 4).char == "X"


def test_clear_screen_sequence_resets_buffer_and_cursor():
    buf = ScreenBuffer(5, 2)
    parse_ansi_into_buffer("AB\x1b[2JC", buf)
    # The clear wipes "AB" and homes the cursor -- "C" lands at (0,0).
    assert buf.get_cell(0, 0).char == "C"
    assert buf.get_cell(0, 1) == Cell()


def test_full_width_row_immediately_followed_by_crlf_does_not_skip_a_row():
    # The deferred-wrap regression this parser must not reintroduce:
    # filling the very last column of a row must not eagerly advance
    # to the next row if an explicit CRLF immediately follows -- real
    # scene .ans art is almost always exactly as wide as the canvas.
    buf = ScreenBuffer(3, 3)
    parse_ansi_into_buffer("abc\r\ndef", buf)
    assert buf.get_cell(1, 0).char == "d"
    assert buf.get_cell(1, 1).char == "e"
    assert buf.get_cell(1, 2).char == "f"


def test_typing_past_the_last_column_wraps_to_the_next_row():
    buf = ScreenBuffer(3, 2)
    parse_ansi_into_buffer("abcd", buf)  # no CR/LF at all -- pure overflow
    assert buf.get_cell(0, 0).char == "a"
    assert buf.get_cell(0, 2).char == "c"
    assert buf.get_cell(1, 0).char == "d"


def test_unrecognized_csi_sequence_is_skipped_without_crashing():
    buf = ScreenBuffer(10, 1)
    # ESC[?25h (cursor visibility, not in this parser's recognized set)
    parse_ansi_into_buffer("\x1b[?25hX", buf)
    assert buf.get_cell(0, 0).char == "X"


def test_unterminated_csi_sequence_is_skipped_without_crashing():
    buf = ScreenBuffer(10, 1)
    parse_ansi_into_buffer("\x1b[123", buf)  # never reaches a final byte
    # Must not raise -- nothing meaningful to assert beyond that.


def test_lone_escape_not_starting_a_sequence_is_skipped():
    buf = ScreenBuffer(10, 1)
    parse_ansi_into_buffer("A\x1bZ", buf)
    assert buf.get_cell(0, 0).char == "A"
    assert buf.get_cell(0, 1).char == "Z"


def test_writes_past_buffer_bounds_are_silently_dropped():
    buf = ScreenBuffer(3, 2)
    parse_ansi_into_buffer("\x1b[10;10HX", buf)  # way outside the buffer
    # Must not raise; nothing was written anywhere meaningful to check
    # beyond "the whole buffer is still blank."
    for row in range(2):
        for col in range(3):
            assert buf.get_cell(row, col) == Cell()


def test_starts_writing_from_whatever_cursor_state_the_buffer_already_implies():
    # parse_ansi_into_buffer always starts its own virtual cursor at
    # (0, 0) regardless of prior buffer content -- confirms callers
    # wanting a clean slate are responsible for calling buffer.clear()
    # first, per the function's own docstring.
    buf = ScreenBuffer(5, 1)
    buf.write_cell(0, 0, "X")
    parse_ansi_into_buffer("Y", buf)
    assert buf.get_cell(0, 0).char == "Y"
