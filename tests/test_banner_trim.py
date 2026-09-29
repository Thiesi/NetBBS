"""Trailing blank rows are trimmed from every banner and masthead as it
is shown (issue #841): the art editor saves all 24 canvas rows, so a short
banner used to scroll its own text off an 80x25 screen."""

from __future__ import annotations

import pytest

from netbbs.rendering import ScreenBuffer
from netbbs.rendering.ansi_art import encode_ansi_bytes, trim_trailing_blank_rows
from netbbs.storage.database import Database

ESC = chr(27)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def test_empty_rows_after_the_art_are_removed():
    text = "PEN\r\n   \r\n" + ESC + "[0m" + ESC + "[37m    \r\n" + ESC + "[0m"
    assert trim_trailing_blank_rows(text) == "PEN"


def test_empty_rows_inside_the_art_are_kept():
    assert trim_trailing_blank_rows("TOP\r\n\r\n\r\nBOTTOM\r\n\r\n") == "TOP\r\n\r\n\r\nBOTTOM"


@pytest.mark.parametrize(
    "sgr",
    ["44", "104", "48;5;19", "48;2;10;20;30", "7", "0;41"],
)
def test_a_row_of_spaces_with_a_background_colour_is_art_not_blank(sgr):
    text = "PEN\r\n" + ESC + "[" + sgr + "m      " + ESC + "[0m\r\n"
    assert trim_trailing_blank_rows(text).endswith("      " + ESC + "[0m")


@pytest.mark.parametrize("sgr", ["38;5;41", "38;2;44;44;44", "1;37", "0"])
def test_foreground_only_styling_on_spaces_is_still_blank(sgr):
    text = "PEN\r\n" + ESC + "[" + sgr + "m      \r\n"
    assert trim_trailing_blank_rows(text) == "PEN"


def test_a_background_set_on_an_earlier_row_still_paints_the_rows_below():
    # Review on #889: TheDraw-style art writes a colour only when it
    # changes, so a blue bar under the title is rows of plain spaces.
    text = "TITLE" + ESC + "[44m\r\n      \r\n      \r\n" + ESC + "[0m\r\n   \r\n"
    rows = trim_trailing_blank_rows(text).split("\n")
    assert len(rows) == 4  # title, two painted rows, and the row holding the reset


def test_a_reset_ends_the_carried_background():
    text = ESC + "[44mBAR" + ESC + "[0m\r\n      \r\n"
    assert trim_trailing_blank_rows(text) == ESC + "[44mBAR" + ESC + "[0m"


def test_all_blank_art_trims_to_nothing():
    assert trim_trailing_blank_rows("   \r\n   \r\n") == ""


def test_an_editor_saved_seven_line_banner_keeps_seven_rows():
    buffer = ScreenBuffer(80, 24)
    for row in range(7):
        buffer.write_cell(row, 0, "x")
    text = encode_ansi_bytes(buffer).decode("cp437")
    assert len(trim_trailing_blank_rows(text).split("\n")) == 7


@pytest.mark.parametrize(
    ("module", "path_fn", "enable_fn", "load_fn"),
    [
        ("welcome_banner", "banner_path", "set_welcome_banner_enabled", "load_welcome_banner"),
        ("new_account_banner_before", "new_account_banner_before_path", "set_new_account_banner_before_enabled", "load_new_account_banner_before"),
        ("new_account_banner_after", "new_account_banner_after_path", "set_new_account_banner_after_enabled", "load_new_account_banner_after"),
        ("logoff_banner", "logoff_banner_path", "set_logoff_banner_enabled", "load_logoff_banner"),
        ("main_menu_banner", "main_menu_banner_path", "set_main_menu_banner_enabled", "load_main_menu_banner"),
        ("board_list_banner", "board_list_banner_path", "set_board_list_banner_enabled", "load_board_list_banner"),
        ("file_area_banner", "file_area_banner_path", "set_file_area_banner_enabled", "load_file_area_banner"),
        ("chat_channel_picker_banner", "chat_channel_picker_banner_path", "set_chat_channel_picker_banner_enabled", "load_chat_channel_picker_banner"),
    ],
)
def test_every_banner_loader_trims_the_saved_canvas(db, module, path_fn, enable_fn, load_fn):
    import importlib

    mod = importlib.import_module(f"netbbs.net.{module}")
    buffer = ScreenBuffer(80, 24)
    for col, char in enumerate("HELLO"):
        buffer.write_cell(0, col, char)
    getattr(mod, path_fn)(db).write_bytes(encode_ansi_bytes(buffer))
    getattr(mod, enable_fn)(db, True)
    shown = getattr(mod, load_fn)(db)
    assert "HELLO" in shown
    assert shown.count("\n") == 0
