"""Art with live slots on list screens (issue #929): the `{list WxH}`
region, its fields, and `pick_item` drawing one page of a list into the
SysOp's art instead of the generated list."""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.picker import pick_item
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.art_slots import (
    ListSlotRow,
    fill_field_slots,
    list_slot_fits,
    parse_slot_art,
    render_list_slot_art,
)
from netbbs.rendering.screen_buffer import ScreenBuffer
from netbbs.rendering.theme import MENU_KEY_COLOR
from tests.test_mail_flow import FakeSession, _visible_text

ESC = "\x1b"
DOWN = EditorKey(EditorKeyKind.DOWN)
ENTER = EditorKey(EditorKeyKind.ENTER)


def _art(*rows: str) -> str:
    return "\r\n".join(rows)


LIST_ART = _art(
    "+------------------------------------------+",
    # Title in columns 2-21, page in 24-29, count in 32-40.
    f"| {ESC}[1;33m{{title 20}}{ESC}[0m" + " " * 12 + "{page 6}{count 9}" + " " * 2 + "|",
    f"| {ESC}[36m{{list 40x5}}{ESC}[0m                              |",
    "|                                          |",
    "|                                          |",
    "|                                          |",
    "|                                          |",
    "+------------------------------------------+",
)


def _screen(text: str, width: int = 80, height: int = 25) -> list[str]:
    buffer = ScreenBuffer(width, height)
    parse_ansi_into_buffer(text, buffer)
    return ["".join(buffer.get_cell(r, c).char for c in range(width)).rstrip() for r in range(height)]


def _buffer(text: str, width: int = 80, height: int = 25) -> ScreenBuffer:
    buffer = ScreenBuffer(width, height)
    parse_ansi_into_buffer(text, buffer)
    return buffer


def _reversed(text: str, word: str) -> bool:
    """Whether `word` is drawn in reverse video: the screen buffer's parser
    keeps colours but not the reverse attribute, so this reads the run
    that holds it."""
    run = text[:text.index(word)]
    return "\x1b[7m" in run[run.rfind("\x1b[0m"):]


# -- parsing -----------------------------------------------------------------


def test_a_list_region_is_found_with_its_size_and_style():
    art = parse_slot_art(LIST_ART, require_menu=False, require_list=True)
    assert art.problems == ()
    assert (art.list.row, art.list.col, art.list.width, art.list.height) == (2, 2, 40, 5)
    assert art.list.cell.fg is not None
    assert {field.name for field in art.fields} == {"title", "page", "count"}


def test_a_list_without_a_size_is_a_problem():
    art = parse_slot_art("{list}", require_menu=False, require_list=True)
    assert any("needs a size" in problem for problem in art.problems)


def test_list_art_needs_a_list_and_cannot_use_a_menu():
    assert any("no {list" in p for p in parse_slot_art("{user 9}", require_menu=False, require_list=True).problems)
    art = parse_slot_art("{list 20x3}\r\n{menu 20x3}", require_menu=False, require_list=True)
    assert any("only works on the main menu" in problem for problem in art.problems)


def test_main_menu_art_cannot_use_a_list():
    art = parse_slot_art("{menu 20x3}\r\n\r\n\r\n{list 20x3}")
    assert any("only works on a list screen" in problem for problem in art.problems)


def test_banner_fields_fill_list_art_and_blank_the_list_region():
    """Issue #1057: a `{list}` slot in a banner is blanked, not sent raw."""
    out = strip_ansi(fill_field_slots("{list 20x3}\r\n{user 9}", {"user": "OldNib"}))
    assert "{" not in out and "OldNib" in out


# -- the region --------------------------------------------------------------


def test_rows_fill_the_region_with_number_name_and_a_right_aligned_value():
    art = parse_slot_art(LIST_ART, require_menu=False, require_list=True)
    drawn = render_list_slot_art(
        art, fields={"title": "Message boards", "page": "1/2", "count": "7 total"},
        rows=[ListSlotRow("01.", "General", "12 new"), ListSlotRow("02.", "Inks", "")],
    )
    screen = _screen(drawn)
    assert screen[1].startswith("| Message boards")
    assert "1/2" in screen[1] and "7 total" in screen[1]
    assert screen[2] == "| 01. General" + " " * 23 + "12 new |"
    assert screen[3].startswith("| 02. Inks ")
    assert screen[4] == "|                                          |"


def test_the_number_takes_the_key_colour_and_the_name_the_tokens():
    art = parse_slot_art(LIST_ART, require_menu=False, require_list=True)
    buffer = _buffer(render_list_slot_art(art, fields={}, rows=[ListSlotRow("01.", "General")]))
    number, name = buffer.get_cell(2, 2), buffer.get_cell(2, 6)
    assert number.fg == MENU_KEY_COLOR and number.bold
    assert name.fg == art.list.cell.fg and not name.reverse


def test_the_highlighted_row_is_drawn_reversed_across_the_region():
    art = parse_slot_art(LIST_ART, require_menu=False, require_list=True)
    drawn = render_list_slot_art(
        art, fields={}, rows=[ListSlotRow("01.", "General"), ListSlotRow("02.", "Inks")], highlighted=1,
    )
    # The whole row, number included, is one reversed run.
    assert _reversed(drawn, "02. Inks")
    assert not _reversed(drawn, "General")


def test_too_small_a_region_or_too_many_rows_draws_nothing():
    small = parse_slot_art("{list 30x2}", require_menu=False, require_list=True)
    assert render_list_slot_art(small, fields={}, rows=[]) is None
    narrow = parse_slot_art("{list 15x5}", require_menu=False, require_list=True)
    assert not list_slot_fits(narrow.list, 0)
    art = parse_slot_art(LIST_ART, require_menu=False, require_list=True)
    six = [ListSlotRow(f"{n:02d}.", "x") for n in range(1, 7)]
    assert render_list_slot_art(art, fields={}, rows=six) is None
    # A wide value leaves the name too little room.
    assert not list_slot_fits(art.list, 25)


# -- the picker --------------------------------------------------------------


class SlotSession(FakeSession):
    def __init__(self, keys, *, charset="cp437", width=80, height=24):
        super().__init__()
        self._editor_keys = iter(keys)
        self.terminal_width = width
        self.physical_width = width
        self.terminal_height = height
        self.output_charset = charset

    async def read_editor_key(self, distinguish_ctrl_h: bool = False) -> EditorKey:
        key = next(self._editor_keys, None)
        if key is None:
            raise AssertionError("no more scripted keys")
        return key if isinstance(key, EditorKey) else EditorKey(EditorKeyKind.CHAR, char=key)

    async def read_key(self, echo: bool = True) -> str:
        return "q"

    async def discard_buffered_input(self) -> None:
        pass

    async def read_any_key(self) -> str:
        return " "


BOARDS = ["General", "Inks", "Nibs", "Paper", "Restoration", "Trading Post", "Calligraphy"]


def _pick(keys, *, art_text=LIST_ART, **session_args):
    session = SlotSession(keys, **session_args)
    art = parse_slot_art(art_text, require_menu=False, require_list=True)
    picked = asyncio.run(pick_item(
        session, BOARDS, name_of=lambda item: item, stable_id_of=BOARDS.index,
        title="Message boards", empty_message="No boards.",
        slot_art=art, slot_column_of=lambda item: f"{len(item)} new", slot_fields={"user": "OldNib"},
    ))
    return picked, session


def _last_screen(session) -> list[str]:
    text = "".join(session.written)
    return _screen(text[text.rfind(ESC + "[2J"):])


def test_a_page_fills_the_region_and_the_prompt_goes_below_the_art():
    picked, session = _pick(["b"])
    assert picked is None
    screen = _last_screen(session)
    assert screen[1].startswith("| Message boards") and "1/2" in screen[1] and " 7 " in screen[1]
    assert screen[2].startswith("| 01. General") and screen[2].endswith("7 new |")
    assert screen[6].startswith("| 05. Restoration")
    text = "".join(session.written)
    after_art = text[text.rfind(ESC + "[9;1H"):]
    assert "[N]ext" in strip_ansi(after_art) and "Choice:" in after_art


def test_a_page_holds_as_many_entries_as_the_region_has_rows():
    _, session = _pick(["n", "b"])
    screen = _last_screen(session)
    assert "2/2" in screen[1]
    assert screen[2].startswith("| 01. Trading Post")
    assert screen[3].startswith("| 02. Calligraphy")


def test_numbers_and_the_cursor_pick_as_on_the_generated_list():
    assert _pick(["0", "3"])[0] == "Nibs"
    assert _pick([DOWN, DOWN, ENTER])[0] == "Inks"
    assert _pick(["n", "0", "2"])[0] == "Calligraphy"


def test_the_highlighted_entry_is_reversed_in_the_art():
    _, session = _pick([DOWN, "b"])
    text = "".join(session.written)
    last = text[text.rfind(ESC + "[2J"):]
    assert _reversed(last, "01. General")
    assert not _reversed(last, "Inks")


def test_a_prompt_slot_places_the_prompt_in_the_art():
    art_text = LIST_ART + "\r\n  {prompt}"
    _, session = _pick(["b"], art_text=art_text)
    text = "".join(session.written)
    assert text[text.rfind(f"{ESC}[9;3H"):].startswith(f"{ESC}[9;3HChoice:")


def test_ascii_callers_and_small_terminals_get_the_generated_list():
    for args in ({"charset": "ascii"}, {"width": 40}, {"height": 10}):
        _, session = _pick(["b"], **args)
        text = "".join(session.written)
        assert "Message boards" in text and "+-----" not in text, args


def test_art_with_problems_or_too_small_a_region_gets_the_generated_list():
    for art_text in ("{list}", "{list 40x2}", "{list 14x5}"):
        _, session = _pick(["b"], art_text=art_text)
        assert "  01. General" in _visible_text(session), art_text


def test_no_art_draws_the_list_as_before():
    plain = SlotSession(["b"])
    asyncio.run(pick_item(
        plain, BOARDS, name_of=lambda item: item, stable_id_of=BOARDS.index,
        title="Message boards", empty_message="No boards.",
    ))
    art_off = SlotSession(["b"])
    asyncio.run(pick_item(
        art_off, BOARDS, name_of=lambda item: item, stable_id_of=BOARDS.index,
        title="Message boards", empty_message="No boards.", slot_art=None,
    ))
    assert plain.written == art_off.written


def test_list_art_goes_out_as_art_with_its_pictographs():
    # A heart drawn in the art (CP437 0x03) reaches a CP437 terminal as the
    # byte that draws it, as the main menu's art does, not as a substitute.
    art_text = LIST_ART.replace("+------------------------------------------+", "+-------------------\u2665----------------------+", 1)
    _, session = _pick(["b"], art_text=art_text)
    assert "\x03" in "".join(session.written)


def test_an_answer_to_a_choice_at_a_prompt_slot_starts_below_the_screen():
    """Issue #1083: a choice typed at a prompt inside the art ends its line
    below the art and the navigation lines, not on the row under the
    prompt, where it would overwrite what is drawn there."""
    art_text = LIST_ART + "\r\n  {prompt}"
    _, session = _pick(["b"], art_text=art_text)
    text = "".join(session.written)
    # The art is 9 rows, the prompt sits on its last; the two navigation
    # lines take rows 10 and 11, so the answer starts on row 12.
    assert text[text.rfind("Choice:"):].startswith(f"Choice: b{ESC}[12;1H")
