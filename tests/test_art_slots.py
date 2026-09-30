"""Art with live slots (issue #929, step 4): finding and checking the
tokens in a SysOp's menu art, laying out menu items in a region, and
drawing the art with its slots filled."""

from __future__ import annotations

from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.art_slots import (
    describe_slots,
    layout_menu_slot,
    parse_slot_art,
    render_slot_art,
)
from netbbs.rendering.menu import menu_key
from netbbs.rendering.screen_buffer import ScreenBuffer
from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.theme import MENU_KEY_COLOR

ESC = "\x1b"


def _art(*rows: str) -> str:
    return "\r\n".join(rows)


def _screen(text: str, width: int = 80, height: int = 25) -> list[str]:
    buffer = ScreenBuffer(width, height)
    parse_ansi_into_buffer(text, buffer)
    return ["".join(buffer.get_cell(r, c).char for c in range(width)).rstrip() for r in range(height)]


def test_tokens_are_found_with_position_size_and_style():
    art = parse_slot_art(_art(
        "+--------------------------------------+",
        f"| Hi {ESC}[1;33m{{user 12}}{ESC}[0m                        |",
        f"| {ESC}[36m{{menu 30x5}}{ESC}[0m                           |",
        "",
        "",
        "",
        "",
        "{prompt}",
    ))
    assert art.problems == ()
    user = art.fields[0]
    assert (user.name, user.row, user.col, user.width, user.height) == ("user", 1, 5, 12, 1)
    assert user.cell.bold and user.cell.fg is not None  # drawn bold yellow
    assert (art.menu.row, art.menu.col, art.menu.width, art.menu.height) == (2, 2, 30, 5)
    assert art.menu.cell.fg is not None
    assert (art.prompt.row, art.prompt.col) == (7, 0)
    assert art.height == 8


def test_tokens_are_blanked_in_their_own_style():
    art = parse_slot_art(_art(f"{ESC}[44m{{menu 20x2}}{ESC}[0m", ""))
    cell = art.buffer.get_cell(0, 0)
    assert cell.char == " " and cell.bg is not None


def test_a_field_without_a_width_is_as_wide_as_its_token():
    art = parse_slot_art(_art("{menu 10x1}", "{node}"))
    assert art.fields[0].width == len("{node}")


def test_unknown_brace_text_is_left_as_art():
    art = parse_slot_art(_art("{menu 10x1}", "{hello} {user", "{MENU 5x5}"))
    assert art.problems == ()
    assert art.fields == ()
    assert "{hello}" in "".join(art.buffer.get_cell(1, c).char for c in range(10))


def test_missing_menu_is_a_problem_only_when_required():
    assert any("no {menu" in p for p in parse_slot_art("{user 10}").problems)
    assert parse_slot_art("{user 10}", require_menu=False).problems == ()


def test_a_menu_without_a_size_is_a_problem():
    assert any("needs a size" in p for p in parse_slot_art("{menu}").problems)


def test_two_menus_and_two_prompts_are_problems():
    problems = parse_slot_art(_art("{menu 5x1}  {menu 5x1}", "{prompt} {prompt}")).problems
    assert any("2 {menu}" in p for p in problems)
    assert any("2 {prompt}" in p for p in problems)


def test_overlapping_slots_are_a_problem():
    problems = parse_slot_art(_art("{menu 20x3}", " {user 5}")).problems
    assert any("overlaps" in p for p in problems)


def test_a_slot_past_the_right_edge_is_a_problem():
    problems = parse_slot_art("{menu 70x1}".rjust(80), width=80).problems
    assert any("runs past column 80" in p for p in problems)


def test_a_field_with_a_row_count_is_a_problem():
    assert any("one row" in p for p in parse_slot_art(_art("{menu 5x1}", "{user 10x2}")).problems)


def test_describe_slots_lists_every_slot():
    lines = describe_slots(parse_slot_art(_art("{menu 20x4}", "{user 10}", "{prompt}")))
    assert lines == [
        "{menu 20x4} at row 1, column 1",
        "{prompt} at row 3, column 1",
        "{user} at row 2, column 1, 10 wide",
    ]


def test_the_height_counts_a_menu_region_below_the_last_drawn_row():
    assert parse_slot_art("{menu 10x6}").height == 6


def test_layout_fills_columns_top_to_bottom():
    rows = layout_menu_slot(["[A]lpha", "[B]eta", "[C]harlie", "[D]elta"], 30, 2)
    assert rows == ["[A]lpha  [C]harlie", "[B]eta   [D]elta"]


def test_layout_strips_styles_from_labels():
    rows = layout_menu_slot([menu_key("M", "essage boards")], 20, 1)
    assert rows == ["[M]essage boards"]


def test_layout_pads_to_the_region_height():
    assert layout_menu_slot(["[A]"], 10, 3) == ["[A]", "", ""]


def test_layout_reports_items_that_do_not_fit():
    assert layout_menu_slot(["[M]essage boards", "[C]hat", "[F]iles"], 16, 1) is None
    assert layout_menu_slot(["[M]essage boards"], 10, 5) is None


def test_render_fills_fields_and_menu_and_highlights_keys():
    art = parse_slot_art(_art("Welcome {user 8}!", "{menu 20x2}"))
    rows = layout_menu_slot(["[M]ail", "[L]ogoff"], 20, 2)
    out = render_slot_art(art, fields={"user": "Margo the librarian"}, menu_rows=rows)
    screen = _screen(out)
    assert screen[0] == "Welcome Margo...!"
    assert screen[1] == "[M]ail"
    assert screen[2] == "[L]ogoff"
    assert f"38;5;{MENU_KEY_COLOR}" in out
    assert "[2J" in out[:12]


def test_render_keeps_the_art_around_the_slots():
    art = parse_slot_art(_art("#### {node 6} ####", "{menu 10x1}"))
    screen = _screen(render_slot_art(art, fields={"node": "Nib"}, menu_rows=["[X]it"]))
    # A slot narrower than its token: the token's own cells beyond the
    # slot are blanked, and the art after the token stays where it was.
    assert screen[0] == "#### Nib      ####"


def test_render_uses_the_given_ellipsis():
    art = parse_slot_art(_art("{user 5}", "{menu 3x1}"))
    screen = _screen(render_slot_art(art, fields={"user": "abcdefgh"}, menu_rows=[""], ellipsis="…"))
    assert screen[0] == "abcd…"


def test_render_without_menu_rows_leaves_the_region_blank():
    art = parse_slot_art("{menu 10x1}")
    assert strip_ansi(render_slot_art(art, fields={}, menu_rows=None)).strip() == ""
