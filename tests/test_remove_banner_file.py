"""
Issue #1119: a banner's or masthead's saved file can be deleted from its own
screen, not only switched off. `[R]emove file` is offered while a file is
saved; it asks once, deletes the file, switches the piece off and records it.
"""

from __future__ import annotations

import pytest

from netbbs.moderation.log import list_recent_actions
from tests.test_admin_flow import FakeSession, _run, _visible, _written_text, db, lane, sysop  # noqa: F401 -- fixtures

# Settings > Mastheads & banners > Banners > Welcome banner.
_WELCOME = ["s", "m", "n", "w"]
_BACK_OUT = ["b"] * 6


def test_removing_the_welcome_banner_deletes_it_and_switches_it_off(db, lane, sysop):
    from netbbs.net.welcome_banner import banner_path, is_welcome_banner_enabled, set_welcome_banner_enabled

    banner_path(db).write_bytes(b"MY CUSTOM BANNER")
    set_welcome_banner_enabled(db, True)
    session = FakeSession([*_WELCOME, "r", "y", *_BACK_OUT])
    _run(session, lane, sysop)
    text = " ".join(_visible(_written_text(session)).split())
    assert not banner_path(db).exists()
    assert is_welcome_banner_enabled(db) is False
    assert "and switched it off" in text
    assert list_recent_actions(db, limit=1)[0].action == "remove_welcome_banner"


def test_declining_keeps_the_file(db, lane, sysop):
    from netbbs.net.welcome_banner import banner_path

    banner_path(db).write_bytes(b"MY CUSTOM BANNER")
    session = FakeSession([*_WELCOME, "r", "n", *_BACK_OUT])
    _run(session, lane, sysop)
    assert banner_path(db).read_bytes() == b"MY CUSTOM BANNER"
    assert "Cancelled. Nothing was deleted." in _visible(_written_text(session))


def test_remove_file_is_offered_only_while_a_file_is_saved(db, lane, sysop):
    from netbbs.net.welcome_banner import banner_path

    session = FakeSession([*_WELCOME, *_BACK_OUT])
    _run(session, lane, sysop)
    assert "emove file" not in _visible(_written_text(session))

    banner_path(db).write_bytes(b"MY CUSTOM BANNER")
    session = FakeSession([*_WELCOME, *_BACK_OUT])
    _run(session, lane, sysop)
    assert "[R]emove file" in _visible(_written_text(session))


@pytest.mark.parametrize(
    ("keys", "module", "path_fn", "audit"),
    [
        (["s", "m", "m", "m"], "netbbs.net.main_menu_banner", "main_menu_banner_path", "remove_main_menu_banner"),
        (["s", "m", "m", "o"], "netbbs.net.board_list_banner", "board_list_banner_path", "remove_board_list_banner"),
        (["s", "m", "m", "f"], "netbbs.net.file_area_banner", "file_area_banner_path", "remove_file_area_banner"),
        (["s", "m", "m", "c"], "netbbs.net.chat_channel_picker_banner", "chat_channel_picker_banner_path",
         "remove_chat_channel_picker_banner"),
        (["s", "m", "n", "l"], "netbbs.net.logoff_banner", "logoff_banner_path", "remove_logoff_banner"),
        (["s", "m", "n", "e"], "netbbs.net.new_account_banner_before", "new_account_banner_before_path",
         "remove_new_account_banner_before"),
        (["s", "m", "n", "f"], "netbbs.net.new_account_banner_after", "new_account_banner_after_path",
         "remove_new_account_banner_after"),
    ],
)
def test_every_masthead_and_banner_can_remove_its_file(db, lane, sysop, keys, module, path_fn, audit):
    import importlib

    path = getattr(importlib.import_module(module), path_fn)(db)
    path.write_bytes(b"ART")
    session = FakeSession([*keys, "r", "y", *_BACK_OUT])
    _run(session, lane, sysop)
    assert not path.exists()
    assert list_recent_actions(db, limit=1)[0].action == audit
