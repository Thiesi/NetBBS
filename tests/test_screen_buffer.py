"""Tests for netbbs.rendering.screen_buffer (design doc round 26 / --
welcome banner round B1) -- the TUI screen-buffer/diff abstraction, in
isolation from its first real consumer (netbbs.net.ansi_editor,
covered separately)."""

from __future__ import annotations

from netbbs.rendering.ansi import clear_screen, colored, move_cursor
from netbbs.rendering.screen_buffer import Cell, ScreenBuffer, diff_ansi, full_render_ansi


def test_fresh_buffer_is_all_blank_cells():
    buf = ScreenBuffer(3, 2)
    for row in range(2):
        for col in range(3):
            assert buf.get_cell(row, col) == Cell()


def test_write_cell_then_get_cell():
    buf = ScreenBuffer(3, 2)
    buf.write_cell(1, 2, "X", fg=1, bg=2, bold=True)
    assert buf.get_cell(1, 2) == Cell(char="X", fg=1, bg=2, bold=True)


def test_write_cell_out_of_bounds_raises():
    buf = ScreenBuffer(3, 2)
    for row, col in [(-1, 0), (0, -1), (2, 0), (0, 3)]:
        try:
            buf.write_cell(row, col, "X")
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for ({row}, {col})")


def test_clear_resets_every_cell():
    buf = ScreenBuffer(2, 2)
    buf.write_cell(0, 0, "X", fg=1)
    buf.clear()
    assert buf.get_cell(0, 0) == Cell()


def test_snapshot_is_independent_of_later_mutation():
    buf = ScreenBuffer(2, 1)
    buf.write_cell(0, 0, "A")
    snap = buf.snapshot()
    buf.write_cell(0, 0, "B")
    assert snap[0][0].char == "A"
    assert buf.get_cell(0, 0).char == "B"


# -- diff_ansi ----------------------------------------------------------


def test_diff_of_identical_snapshots_is_empty():
    buf = ScreenBuffer(3, 2)
    buf.write_cell(0, 0, "X", fg=1)
    snap = buf.snapshot()
    assert diff_ansi(snap, snap) == ""


def test_diff_emits_only_the_changed_cell():
    buf = ScreenBuffer(3, 1)
    before = buf.snapshot()
    buf.write_cell(0, 1, "X", fg=1)
    after = buf.snapshot()
    result = diff_ansi(before, after)
    assert result == move_cursor(1, 2) + colored("X", fg_color=1)


def test_diff_groups_consecutive_same_style_cells_into_one_run():
    buf = ScreenBuffer(5, 1)
    before = buf.snapshot()
    buf.write_cell(0, 0, "A", fg=1, bold=True)
    buf.write_cell(0, 1, "B", fg=1, bold=True)
    buf.write_cell(0, 2, "C", fg=1, bold=True)
    after = buf.snapshot()
    result = diff_ansi(before, after)
    assert result == move_cursor(1, 1) + colored("ABC", fg_color=1, bold=True)


def test_diff_breaks_the_run_on_a_style_change():
    buf = ScreenBuffer(5, 1)
    before = buf.snapshot()
    buf.write_cell(0, 0, "A", fg=1)
    buf.write_cell(0, 1, "B", fg=2)
    after = buf.snapshot()
    result = diff_ansi(before, after)
    assert result == (
        move_cursor(1, 1) + colored("A", fg_color=1) + move_cursor(1, 2) + colored("B", fg_color=2)
    )


def test_diff_breaks_the_run_on_an_unchanged_cell_in_between():
    buf = ScreenBuffer(5, 1)
    buf.write_cell(0, 1, "keep", fg=3)  # pretend this is already on screen
    before = buf.snapshot()
    buf.write_cell(0, 0, "A", fg=1)
    buf.write_cell(0, 2, "C", fg=1)
    after = buf.snapshot()
    result = diff_ansi(before, after)
    # col 1 is unchanged between before/after, so col 0 and col 2 must be
    # two separate runs, not accidentally merged across the gap.
    assert result == (
        move_cursor(1, 1) + colored("A", fg_color=1) + move_cursor(1, 3) + colored("C", fg_color=1)
    )


def test_diff_spans_multiple_rows_independently():
    buf = ScreenBuffer(3, 2)
    before = buf.snapshot()
    buf.write_cell(0, 0, "A", fg=1)
    buf.write_cell(1, 0, "B", fg=2)
    after = buf.snapshot()
    result = diff_ansi(before, after)
    assert result == (
        move_cursor(1, 1) + colored("A", fg_color=1) + move_cursor(2, 1) + colored("B", fg_color=2)
    )


# -- full_render_ansi -----------------------------------------------------


def test_full_render_starts_with_clear_screen():
    buf = ScreenBuffer(2, 2)
    result = full_render_ansi(buf.snapshot())
    assert result.startswith(clear_screen())


def test_full_render_of_a_blank_buffer_emits_no_cell_content():
    buf = ScreenBuffer(2, 2)
    result = full_render_ansi(buf.snapshot())
    assert result == clear_screen()


def test_full_render_includes_non_blank_cells():
    buf = ScreenBuffer(3, 1)
    buf.write_cell(0, 1, "X", fg=5)
    result = full_render_ansi(buf.snapshot())
    assert result == clear_screen() + move_cursor(1, 2) + colored("X", fg_color=5)


def test_full_render_treats_a_styled_space_as_non_blank():
    # A space with a background color is visually meaningful (e.g. a
    # colored block) even though its character looks empty -- must not
    # be silently skipped the way a truly default-style blank cell is.
    buf = ScreenBuffer(2, 1)
    buf.write_cell(0, 0, " ", bg=4)
    result = full_render_ansi(buf.snapshot())
    assert result == clear_screen() + move_cursor(1, 1) + colored(" ", bg_color=4)
