"""Menu items the SysOp draws into the main-menu art (issue #929, step
5): found from their bracketed key with no token, blanked for a caller
who can't use them, and the caller's undrawn items sent to `{menu}`."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from netbbs.auth.users import create_user
from netbbs.chat.mailbox import MessageMailbox
from netbbs.net.main_menu import MAIN_MENU_KEYS, _draw_main_menu, main_menu_entries, menu_label_key
from netbbs.net.main_menu_banner import (
    SLOTS_MODE,
    main_menu_banner_path,
    set_main_menu_art_mode,
    set_main_menu_banner_enabled,
)
from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.art_slots import blank_cell, describe_items, parse_slot_art, render_slot_art
from netbbs.rendering.screen_buffer import Cell, ScreenBuffer
from netbbs.storage.database import Database

ESC = "\x1b"
BLUE = f"{ESC}[44m"
RESET = f"{ESC}[0m"


def _screen_buffer(text: str, width: int = 80, height: int = 24) -> ScreenBuffer:
    buffer = ScreenBuffer(width, height)
    parse_ansi_into_buffer(text, buffer)
    return buffer


def _screen(text: str, width: int = 80, height: int = 24) -> list[str]:
    buffer = _screen_buffer(text, width, height)
    return ["".join(buffer.get_cell(r, c).char for c in range(width)).rstrip() for r in range(height)]


# -- finding drawn items ------------------------------------------------------


def test_each_bracketed_key_is_an_item_spanning_its_run():
    art = parse_slot_art("  [B]oards      Moder[a]tion (3)   {menu 10x1}")
    assert [(i.key, i.col, i.width, i.text) for i in art.items] == [
        ("b", 2, 8, "[B]oards"),
        ("a", 16, 16, "Moder[a]tion (3)"),
    ]


def test_a_frame_one_space_away_is_not_part_of_the_item():
    art = parse_slot_art("║ [S]ysOp console ║\r\n{menu 10x1}")
    item = art.items[0]
    assert (item.key, item.text) == ("s", "[S]ysOp console")
    assert item.col == 2


def test_two_keys_in_one_run_are_one_item_holding_both_and_are_noted():
    art = parse_slot_art("[B]oards [E]-mail\r\n{menu 10x1}")
    assert [(i.key, i.keys, i.text) for i in art.items] == [("b", ("b", "e"), "[B]oards [E]-mail")]
    assert any("holds 2 keys" in note for note in art.notes)


def test_a_frame_character_between_two_keys_separates_their_items():
    # Issue #1070: panels with a "│" gutter one space from the next item.
    art = parse_slot_art("■ [M]essage boards│ ■ [N]ew scan\r\n{menu 10x1}")
    assert [(i.key, i.keys, i.col, i.text) for i in art.items] == [
        ("m", ("m",), 0, "■ [M]essage boards"),
        ("n", ("n",), 20, "■ [N]ew scan"),
    ]
    assert art.notes == ()


def test_a_block_character_between_two_keys_separates_them_too():
    art = parse_slot_art("[B]oards █ [E]-mail ▌[L]ogoff\r\n{menu 10x1}")
    assert [(i.key, i.text) for i in art.items] == [("b", "[B]oards"), ("e", "[E]-mail"), ("l", "[L]ogoff")]
    assert art.notes == ()


def test_a_key_drawn_as_a_frame_character_is_not_split():
    art = parse_slot_art("[─] Divider [B]oards\r\n{menu 10x1}")
    assert [(i.key, i.keys, i.text) for i in art.items] == [("─", ("─", "b"), "[─] Divider [B]oards")]


def test_bracketed_text_inside_a_slot_is_not_an_item():
    art = parse_slot_art("{menu 20x2}\r\n[X] under the menu")
    assert art.items == ()


def test_art_that_draws_its_items_needs_no_menu_slot():
    art = parse_slot_art("[M]essage boards  [L]ogoff")
    assert art.problems == ()
    assert art.menu is None


def test_art_with_neither_a_menu_slot_nor_items_is_a_problem():
    assert any("no {menu" in p for p in parse_slot_art("just a picture").problems)


def test_describe_items_names_each_item_for_the_check():
    art = parse_slot_art("[M]essage boards  [L]ogoff")
    assert describe_items(art) == [
        "[m] '[M]essage boards' at row 1, column 1",
        "[l] '[L]ogoff' at row 1, column 19",
    ]


# -- blanking -----------------------------------------------------------------


def test_a_blanked_cell_keeps_the_background_it_shows():
    assert blank_cell(Cell("S", fg=3, bg=4, bold=True, underline=True)) == Cell(" ", bg=4)
    # Reverse video shows the foreground as the background.
    assert blank_cell(Cell("S", fg=3, bg=4, reverse=True)) == Cell(" ", bg=3)


def test_hidden_items_are_painted_over_and_the_frame_stays():
    art = parse_slot_art(f"{BLUE}║  [B]oards  [S]ysOp  ║{RESET}")
    sysop = [i for i in art.items if i.key == "s"]
    text = render_slot_art(art, fields={}, menu_rows=None, hidden=sysop)
    buffer = _screen_buffer(text)
    row = "".join(buffer.get_cell(0, c).char for c in range(24))
    assert row.rstrip() == "║  [B]oards           ║"
    # The blanked cells are still blue, like the fill around them.
    item = sysop[0]
    assert all(buffer.get_cell(0, c).bg == 4 for c in range(item.col, item.col + item.width))


# -- the main menu ------------------------------------------------------------

# Every item an ordinary caller has, drawn, plus the SysOp's console.
ALL_DRAWN = "\r\n".join([
    "  [M]essage boards  [C]hat  [F]iles  [N]ew scan  [/] Find  [?] Help",
    "  [D]irectory  [P]rofile  [E]-mail  [H]istory  [R]ecent callers",
    "  [S]ysOp console  [V]erify  [O]perators  [L]ogoff",
    "Choice here: {prompt}",
]).encode("utf-8")


class FakeSession:
    def __init__(self, *, width: int = 80, height: int = 24):
        self.written: list[str] = []
        self.terminal_width = width
        self.physical_width = width
        self.terminal_height = height
        self.output_charset = "utf-8"
        self.node_display_name = "The Nib & Quill"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\r\n")


def _setup(tmp_path, art: bytes, *, level: int = 10):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "OldNib", password="parker51", user_level=level)
    main_menu_banner_path(db).write_bytes(art)
    set_main_menu_banner_enabled(db, True)
    set_main_menu_art_mode(db, SLOTS_MODE)
    return db, user


def _draw(db, user, session=None) -> str:
    session = session or FakeSession()
    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))
    return "".join(session.written)


def test_a_caller_does_not_see_a_drawn_item_they_cannot_use(tmp_path):
    db, user = _setup(tmp_path, ALL_DRAWN)
    text = _draw(db, user)
    assert "Main menu" not in text  # the art, not the generated menu
    screen = _screen(text)
    shown = "\n".join(screen)
    assert "[S]ysOp" not in shown and "[V]erify" not in shown
    assert screen[2].strip() == "[O]perators  [L]ogoff"
    assert "[M]essage boards" in screen[0]
    db.close()


def test_the_sysop_sees_the_drawn_sysop_item(tmp_path):
    db, user = _setup(tmp_path, ALL_DRAWN, level=255)
    screen = _screen(_draw(db, user))
    assert "[S]ysOp console  [V]erify" in screen[2]
    db.close()


def test_undrawn_items_go_into_the_menu_slot(tmp_path):
    db, user = _setup(tmp_path, b"  [M]essage boards  [L]ogoff\r\n{menu 70x4}\r\n\r\n\r\n\r\n{prompt}")
    screen = _screen(_draw(db, user))
    region = "\n".join(screen[1:5])
    assert "[C]hat" in region and "[P]rofile" in region
    # Drawn items aren't repeated in the region.
    assert "[M]essage" not in region and "[L]ogoff" not in region
    db.close()


def test_art_without_a_menu_slot_that_misses_an_item_falls_back(tmp_path):
    db, user = _setup(tmp_path, b"  [M]essage boards  [L]ogoff\r\n{prompt}")
    assert "Main menu" in _draw(db, user)
    db.close()


def test_a_second_key_in_a_run_is_not_repeated_in_the_menu_slot(tmp_path):
    db, user = _setup(tmp_path, b"  [M]essage boards [E]-mail  [L]ogoff\r\n{menu 70x4}\r\n\r\n\r\n\r\n{prompt}")
    screen = _screen(_draw(db, user))
    assert "[M]essage boards [E]-mail" in screen[0]
    region = "\n".join(screen[1:5])
    assert "[E]-mail" not in region and "[C]hat" in region
    db.close()


def test_a_run_is_kept_while_the_caller_can_use_any_of_its_keys(tmp_path):
    # [S]ysOp is a SysOp's, [L]ogoff everyone's: one run, so it stays.
    db, user = _setup(tmp_path, b"  [S]ysOp [L]ogoff\r\n{menu 74x6}")
    assert "[S]ysOp [L]ogoff" in _screen(_draw(db, user))[0]
    db.close()


def test_a_run_whose_keys_the_caller_cannot_use_is_blanked(tmp_path):
    db, user = _setup(tmp_path, b"  [S]ysOp [V]erify  [L]ogoff\r\n{menu 74x6}")
    row = _screen(_draw(db, user))[0]
    assert "[S]ysOp" not in row and "[V]erify" not in row and "[L]ogoff" in row
    db.close()


def test_items_split_by_a_frame_character_are_blanked_one_by_one(tmp_path):
    # Issue #1070: [S]ysOp shares a row with [L]ogoff, a "│" between them.
    art = b"  [S]ysOp console\xe2\x94\x82 [L]ogoff\r\n{menu 74x6}"
    db, user = _setup(tmp_path, art)
    row = _screen(_draw(db, user))[0]
    assert "[S]ysOp" not in row and row.strip() == "│ [L]ogoff"
    db.close()


def test_the_fallback_is_logged_once_however_the_unread_count_changes(tmp_path, caplog):
    import logging

    from netbbs.mail import send_mail
    from netbbs.net import main_menu

    main_menu._overflow_logged.clear()
    db, user = _setup(tmp_path, b"  [M]essage boards  [L]ogoff\r\n{prompt}")
    other = create_user(db, "harold", password="parker51", user_level=10)
    with caplog.at_level(logging.INFO, logger=main_menu._logger.name):
        for n in range(3):
            send_mail(db, sender=other, recipient=user, subject=f"hi {n}", body="hello")
            assert "Main menu" in _draw(db, user)
    assert sum("drawing the generated menu" in r.getMessage() for r in caplog.records) == 1
    db.close()


def test_a_drawn_key_that_is_no_menu_key_stays_as_drawn(tmp_path):
    art = ALL_DRAWN.replace(b"[S]ysOp console", b"[x] marks the spot")
    db, user = _setup(tmp_path, art)
    screen = _screen(_draw(db, user))
    assert "[x] marks the spot" in screen[2]
    db.close()


def test_art_drawn_with_the_old_staff_list_key_is_blanked_and_listed_anew(tmp_path):
    # Issue #1158: `[T]` was the Staff list and is Topics now; art drawn
    # before still says `S[t]aff list`, which is no longer the caller's item.
    db, user = _setup(tmp_path, b"  S[t]aff list  [L]ogoff\r\n{menu 74x6}\r\n\r\n\r\n\r\n\r\n\r\n{prompt}")
    screen = _screen(_draw(db, user))
    assert "S[t]aff" not in screen[0] and "[L]ogoff" in screen[0]
    assert "[O]perators" in "\n".join(screen[1:7])
    db.close()


def test_every_main_menu_label_key_is_a_known_menu_key(tmp_path):
    db = Database(tmp_path / "node.db")
    session = FakeSession()
    for level in (0, 10, 255):
        user = create_user(db, f"user{level}", password="parker51", user_level=level)
        user = replace(user, can_verify_identity=True)
        entries = main_menu_entries(session, db, user, whos_online=True)
        for label in entries.labels:
            assert menu_label_key(label) in MAIN_MENU_KEYS, label
    db.close()
