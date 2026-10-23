"""
Tests for netbbs.rendering.prose_buffer -- the pure, I/O-free text
buffer/word-wrap core behind the fullscreen prose editor.
"""

from __future__ import annotations

import pytest

from netbbs.rendering.prose_buffer import (
    ProseBuffer,
    VisualRow,
    cells,
    logical_position,
    soft_wrap,
    visual_position,
    wrap_lines,
)


# -- wrap_lines -----------------------------------------------------------


def test_short_line_produces_one_row():
    rows = wrap_lines(["hello world"], width=80)
    assert rows == [VisualRow(line_index=0, start_col=0, text="hello world")]


def test_empty_line_produces_one_empty_row_not_zero():
    rows = wrap_lines(["", "text"], width=80)
    assert rows[0] == VisualRow(line_index=0, start_col=0, text="")
    assert rows[1] == VisualRow(line_index=1, start_col=0, text="text")


def test_long_line_wraps_at_word_boundaries():
    line = "one two three four five six seven eight nine ten"
    rows = wrap_lines([line], width=20)
    assert len(rows) > 1
    assert all(row.line_index == 0 for row in rows)
    # Every row's text must actually be a substring of the original line
    # starting at its claimed start_col -- proves start_col tracking is
    # correct, not just that wrapping happened.
    for row in rows:
        assert line[row.start_col : row.start_col + len(row.text)] == row.text


def test_multiple_logical_lines_each_wrap_independently():
    rows = wrap_lines(["short", "a somewhat longer line that needs wrapping here"], width=15)
    line_indices = {row.line_index for row in rows}
    assert line_indices == {0, 1}
    assert rows[0].text == "short"


def test_wrap_lines_rejects_non_positive_width():
    with pytest.raises(ValueError):
        wrap_lines(["x"], width=0)


def test_wrap_lines_wraps_cjk_text_at_display_column_boundaries():
    # Dogfood report: international users found non-ASCII handling poor.
    # Each CJK character is 2 display columns; a len()-based wrap would
    # let a line run twice as wide as the terminal actually is.
    from netbbs.rendering.width import display_width

    line = "你好世界" * 3  # 12 characters, 24 display columns, no spaces
    rows = wrap_lines([line], width=8)
    assert len(rows) > 1
    assert all(row.line_index == 0 for row in rows)
    for row in rows:
        assert display_width(row.text) <= 8
        # start_col/text bookkeeping is still character-offset-based
        # (Phase 3 scope, unchanged here) -- must still correctly
        # re-locate each segment in the original line.
        assert line[row.start_col : row.start_col + len(row.text)] == row.text
    assert "".join(row.text for row in rows) == line


def test_wrap_lines_does_not_crash_on_a_double_space_between_words():
    # Real crash reproduced live (ValueError: substring not found):
    # wrap_to_width rejoins wrapped words with a single normalized
    # space, so a segment spanning two+ words that had a double space
    # (or any other multi-character whitespace run) between them in
    # the original line is nowhere a literal substring of that line --
    # the old `line.index(segment, col)` full-segment search raised.
    # The row is the line itself, double space and all: the editor draws
    # what was typed (a squeezed row put the cursor a column short after
    # every double space).
    line = "hello  world this is a test line"
    rows = wrap_lines([line], width=80)
    assert rows == [VisualRow(line_index=0, start_col=0, text=line)]


def test_wrap_lines_does_not_crash_on_a_tab_between_words():
    line = "a\tb\tc word here"
    rows = wrap_lines([line], width=80)
    assert rows == [VisualRow(line_index=0, start_col=0, text=line)]


def test_wrap_lines_start_col_is_correct_after_a_double_space_forces_a_wrap():
    # The double space must not just avoid crashing -- start_col still
    # has to land on each row's real first word so cursor placement
    # stays correct.
    line = "multiple   spaces   everywhere and a long tail of more words to force wrapping"
    rows = wrap_lines([line], width=20)
    assert len(rows) > 1
    for row in rows:
        first_word = row.text.split(" ", 1)[0]
        assert line[row.start_col : row.start_col + len(first_word)] == first_word


# -- visual_position / logical_position round-trip -------------------------


def test_visual_position_on_unwrapped_line():
    pos = visual_position(["hello"], width=80, cursor_line=0, cursor_col=3)
    assert pos.row_index == 0
    assert pos.col == 3


def test_visual_position_maps_into_the_correct_wrapped_segment():
    line = "one two three four five six seven eight nine ten"
    rows = wrap_lines([line], width=20)
    # Put the cursor inside the second row's text and confirm it maps there.
    second_row = rows[1]
    cursor_col = second_row.start_col + 2
    pos = visual_position([line], width=20, cursor_line=0, cursor_col=cursor_col)
    assert pos.row_index == 1
    assert pos.col == 2


def test_visual_and_logical_position_round_trip_across_many_positions():
    lines = ["", "one two three four five six seven eight", "short", "another longer line here"]
    width = 12
    rows = wrap_lines(lines, width)
    for line_index, line in enumerate(lines):
        for col in range(len(line) + 1):
            pos = visual_position(lines, width, line_index, col)
            back = logical_position(rows, pos.row_index, pos.col)
            assert back == (line_index, col), f"round-trip failed for ({line_index}, {col})"


def test_logical_position_clamps_row_index_out_of_range():
    rows = wrap_lines(["hello"], width=80)
    # row_index clamps to the only real row (0); col is independently
    # honored (and separately clamped) against *that* row's length.
    assert logical_position(rows, row_index=99, col=0) == (0, 0)
    assert logical_position(rows, row_index=99, col=99) == (0, 5)
    assert logical_position(rows, row_index=-5, col=0) == (0, 0)


# -- ProseBuffer: construction / round trip --------------------------------


def test_from_text_splits_on_newlines():
    buf = ProseBuffer.from_text("first\nsecond\nthird")
    assert buf.lines == ["first", "second", "third"]


def test_from_text_empty_string_gives_one_blank_line():
    buf = ProseBuffer.from_text("")
    assert buf.lines == [""]


def test_to_text_round_trips():
    original = "first\nsecond\n\nfourth"
    assert ProseBuffer.from_text(original).to_text() == original


# -- ProseBuffer: typing ---------------------------------------------------


def test_insert_char_at_cursor_and_advances():
    buf = ProseBuffer(lines=["ac"], cursor_col=1)
    buf.insert_char("b")
    assert buf.lines == ["abc"]
    assert buf.cursor_col == 2


def test_insert_char_at_end_of_line():
    buf = ProseBuffer(lines=["ab"], cursor_col=2)
    buf.insert_char("c")
    assert buf.lines == ["abc"]
    assert buf.cursor_col == 3


def test_insert_newline_splits_the_line_at_cursor():
    buf = ProseBuffer(lines=["helloworld"], cursor_col=5)
    buf.insert_newline()
    assert buf.lines == ["hello", "world"]
    assert buf.cursor_line == 1
    assert buf.cursor_col == 0


def test_insert_newline_at_start_of_line_leaves_an_empty_line_above():
    buf = ProseBuffer(lines=["hello"], cursor_col=0)
    buf.insert_newline()
    assert buf.lines == ["", "hello"]
    assert buf.cursor_line == 1
    assert buf.cursor_col == 0


# -- ProseBuffer: backspace -------------------------------------------------


def test_backspace_mid_line_removes_preceding_char():
    buf = ProseBuffer(lines=["abc"], cursor_col=2)
    buf.backspace()
    assert buf.lines == ["ac"]
    assert buf.cursor_col == 1


def test_backspace_at_start_of_line_merges_into_previous_line():
    buf = ProseBuffer(lines=["hello", "world"], cursor_line=1, cursor_col=0)
    buf.backspace()
    assert buf.lines == ["helloworld"]
    assert buf.cursor_line == 0
    assert buf.cursor_col == 5  # positioned exactly at the join point


def test_backspace_at_very_start_of_buffer_is_a_no_op():
    buf = ProseBuffer(lines=["hello"], cursor_line=0, cursor_col=0)
    buf.backspace()
    assert buf.lines == ["hello"]
    assert (buf.cursor_line, buf.cursor_col) == (0, 0)


# -- ProseBuffer: delete -----------------------------------------------------


def test_delete_mid_line_removes_char_under_cursor_without_moving_it():
    buf = ProseBuffer(lines=["abc"], cursor_col=1)
    buf.delete()
    assert buf.lines == ["ac"]
    assert buf.cursor_col == 1


def test_delete_at_end_of_line_merges_the_next_line_up():
    buf = ProseBuffer(lines=["hello", "world"], cursor_line=0, cursor_col=5)
    buf.delete()
    assert buf.lines == ["helloworld"]
    assert (buf.cursor_line, buf.cursor_col) == (0, 5)


def test_delete_at_very_end_of_buffer_is_a_no_op():
    buf = ProseBuffer(lines=["hello"], cursor_line=0, cursor_col=5)
    buf.delete()
    assert buf.lines == ["hello"]


# -- ProseBuffer: cursor movement --------------------------------------------


def test_move_left_within_a_line():
    buf = ProseBuffer(lines=["abc"], cursor_col=2)
    buf.move_left()
    assert buf.cursor_col == 1


def test_move_left_at_column_zero_wraps_to_end_of_previous_line():
    buf = ProseBuffer(lines=["hello", "world"], cursor_line=1, cursor_col=0)
    buf.move_left()
    assert (buf.cursor_line, buf.cursor_col) == (0, 5)


def test_move_left_at_very_start_is_a_no_op():
    buf = ProseBuffer(lines=["hello"], cursor_line=0, cursor_col=0)
    buf.move_left()
    assert (buf.cursor_line, buf.cursor_col) == (0, 0)


def test_move_right_within_a_line():
    buf = ProseBuffer(lines=["abc"], cursor_col=1)
    buf.move_right()
    assert buf.cursor_col == 2


def test_move_right_at_end_of_line_wraps_to_start_of_next_line():
    buf = ProseBuffer(lines=["hello", "world"], cursor_line=0, cursor_col=5)
    buf.move_right()
    assert (buf.cursor_line, buf.cursor_col) == (1, 0)


def test_move_right_at_very_end_is_a_no_op():
    buf = ProseBuffer(lines=["hello"], cursor_line=0, cursor_col=5)
    buf.move_right()
    assert (buf.cursor_line, buf.cursor_col) == (0, 5)


def test_move_home_and_end():
    buf = ProseBuffer(lines=["hello world"], cursor_col=5)
    buf.move_home()
    assert buf.cursor_col == 0
    buf.move_end()
    assert buf.cursor_col == 11


# -- ProseBuffer: clamp_cursor ------------------------------------------------


def test_clamp_cursor_pulls_col_back_after_the_line_shrank():
    buf = ProseBuffer(lines=["ab"], cursor_line=0, cursor_col=10)
    buf.clamp_cursor()
    assert buf.cursor_col == 2


def test_clamp_cursor_pulls_line_back_after_lines_shrank():
    buf = ProseBuffer(lines=["only"], cursor_line=5, cursor_col=0)
    buf.clamp_cursor()
    assert buf.cursor_line == 0


# -- soft_wrap keeps every typed character (the eaten-space report) --------


def test_a_trailing_space_is_part_of_the_row():
    # Typing a space showed nothing until the next letter: the row dropped
    # the trailing space, so the cursor, measured against the row, stayed.
    rows = wrap_lines(["hello "], width=80)
    assert rows == [VisualRow(line_index=0, start_col=0, text="hello ")]
    pos = visual_position(["hello "], 80, 0, 6)
    assert (pos.row_index, pos.col) == (0, 6)


def test_leading_and_repeated_spaces_are_kept():
    assert soft_wrap("  indented  twice ", 80) == ["  indented  twice "]


def test_a_word_that_does_not_fit_moves_down_whole():
    assert soft_wrap("one two three", 9) == ["one two ", "three"]


def test_the_space_after_a_word_ending_at_the_edge_hangs_there():
    # No stray space at the start of the next row; a full last row is
    # followed by an empty one, where the cursor after it is.
    assert soft_wrap("hello world", 5) == ["hello ", "world", ""]
    assert soft_wrap("hello ", 5) == ["hello ", ""]
    # Only one character hangs; the rest of the run shows on the next row.
    assert soft_wrap("hello   x", 5) == ["hello ", "  x"]


def test_a_space_typed_at_the_edge_moves_the_cursor_to_the_next_row():
    lines = ["hello "]
    rows = wrap_lines(lines, 5)
    pos = visual_position(lines, 5, 0, 6, rows)
    assert (pos.row_index, pos.col) == (1, 0)


def test_a_word_wider_than_the_row_is_cut_at_the_edge():
    assert soft_wrap("abcdefghij", 4) == ["abcd", "efgh", "ij"]


def test_a_combining_mark_stays_with_its_character_at_a_cut():
    assert soft_wrap("abc" + "é" + "fg", 4) == ["abcé", "fg"]


@pytest.mark.parametrize("width", [1, 2, 3, 5, 8, 13])
def test_soft_wrap_rows_join_back_to_the_text(width):
    samples = [
        "", " ", "     ", "a", "one two three four", "  lead", "trail  ", "a  b   c    d",
        "hello world", "x" * 30, "tab\there", "你好 世界", "word " * 9,
    ]
    for text in samples:
        rows = soft_wrap(text, width)
        assert "".join(rows) == text, (text, width)
        for row in rows:
            # A row fits, except for the one whitespace character that may
            # hang past the edge.
            body = row[:-1] if row and row[-1].isspace() else row
            assert cells(body) <= max(width, 2), (text, width, rows)


def test_round_trip_with_spaces_at_every_position():
    lines = ["a  b   c    d    e", "hello ", "  x", "word word word "]
    for width in (3, 5, 7, 80):
        rows = wrap_lines(lines, width)
        for line_index, line in enumerate(lines):
            for col in range(len(line) + 1):
                pos = visual_position(lines, width, line_index, col, rows)
                back = logical_position(rows, pos.row_index, pos.col)
                assert back == (line_index, col), (width, line_index, col)


def test_the_wrap_cache_only_keeps_the_current_lines():
    cache: dict[str, list[str]] = {}
    wrap_lines(["first version", "other"], 80, cache)
    wrap_lines(["first version!", "other"], 80, cache)
    assert set(cache) == {"first version!", "other"}
