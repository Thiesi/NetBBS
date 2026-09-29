"""Tests for netbbs.chat.nick — transparent display aliases."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.nick import (
    MAX_NICK_LENGTH,
    NickError,
    chat_stream_label,
    display_label,
    get_nick,
    set_nick,
)
from netbbs.rendering.ansi import strip_ansi
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


def test_get_nick_none_when_unset(db, alice):
    assert get_nick(db, alice) is None


def test_set_then_get_nick(db, alice):
    set_nick(db, alice, "DeepParse")
    assert get_nick(db, alice) == "DeepParse"


def test_clear_nick_with_empty_string(db, alice):
    set_nick(db, alice, "DeepParse")
    set_nick(db, alice, "")
    assert get_nick(db, alice) is None


def test_set_nick_rejects_too_long(db, alice):
    with pytest.raises(NickError):
        set_nick(db, alice, "x" * (MAX_NICK_LENGTH + 1))


def test_set_nick_allows_exactly_max_length(db, alice):
    nick = "x" * MAX_NICK_LENGTH
    set_nick(db, alice, nick)  # must not raise
    assert get_nick(db, alice) == nick


def test_set_nick_rejects_another_users_username(db, alice, bob):
    with pytest.raises(NickError):
        set_nick(db, alice, "bob")


def test_set_nick_rejects_another_users_username_case_insensitively(db, alice, bob):
    with pytest.raises(NickError):
        set_nick(db, alice, "BOB")


def test_set_nick_allows_own_username(db, alice):
    set_nick(db, alice, "alice")  # must not raise -- harmless, not impersonation
    assert get_nick(db, alice) == "alice"


@pytest.mark.parametrize("nick", ["Deep|Parse", "InkWell[sysop]", "<bob>", "*** notice", "~Deep~", "|"])
def test_set_nick_rejects_characters_the_chat_screen_frames_names_with(db, alice, nick):
    # Issue #843: the separator, a status-bar tag's brackets, a speaker's
    # angle brackets, the "*" of /me and notices, the old "~" marker.
    with pytest.raises(NickError, match="cannot contain"):
        set_nick(db, alice, nick)


@pytest.fixture
def inkwell(db):
    return create_user(db, "InkWell", password="hunter2", user_level=255)


@pytest.mark.parametrize("nick", ["InkWeII", "Ink Well", "ink_well", "lnkwell", "\u0406nkWell", "Ínkwéll", "1nkWe11"])
def test_set_nick_rejects_look_alikes_of_the_sysops_username(db, alice, inkwell, nick):
    # F106: every one of these was accepted and read as the SysOp.
    with pytest.raises(NickError, match="SysOp"):
        set_nick(db, alice, nick)


@pytest.mark.parametrize("nick", ["B0B", "b o b", "b.o.b"])
def test_set_nick_rejects_look_alikes_of_any_other_username(db, alice, bob, nick):
    with pytest.raises(NickError, match="another caller"):
        set_nick(db, alice, nick)


@pytest.mark.parametrize("nick", [
    "SysOp", "Sys Op", "The SysOp", "Admin", "moderator", "Staff", "5ysop",
    # Greek and Cyrillic capitals that read as Latin ones (Claude review):
    # a Greek Upsilon, a Cyrillic S and O, and a Greek-lettered "ADMIN".
    "S\u03a5SOP", "\u0405YS\u041eP", "\u0391D\u039cI\u039d",
])
def test_set_nick_rejects_staff_titles(db, alice, nick):
    with pytest.raises(NickError, match="staff title"):
        set_nick(db, alice, nick)


def test_a_sysop_may_take_a_staff_title_as_alias(db, inkwell):
    set_nick(db, inkwell, "SysOp")
    assert get_nick(db, inkwell) == "SysOp"


def test_look_alike_of_own_username_is_allowed(db, alice):
    set_nick(db, alice, "Al1ce")
    assert get_nick(db, alice) == "Al1ce"


def test_ordinary_aliases_with_spaces_and_accents_still_work(db, alice, bob):
    set_nick(db, alice, "Dame Plume de l'Encre")
    assert get_nick(db, alice) == "Dame Plume de l'Encre"


# -- display_label --------------------------------------------------------


def test_display_label_is_bare_username_when_no_nick(db, alice):
    assert display_label(db, alice) == "alice"


def test_display_label_combines_nick_and_username(db, alice):
    set_nick(db, alice, "DeepParse")
    assert display_label(db, alice) == "DeepParse|alice"


def test_display_label_reverts_after_clearing(db, alice):
    set_nick(db, alice, "DeepParse")
    set_nick(db, alice, "")
    assert display_label(db, alice) == "alice"


# -- chat_stream_label -------------------------------------------------------


def test_chat_stream_label_is_bare_username_when_no_nick(db, alice):
    assert chat_stream_label(db, alice) == "alice"


def test_chat_stream_label_colors_the_nick_and_shows_the_username(db, alice):
    # Issue #843: never the alias alone in the live stream.
    set_nick(db, alice, "DeepParse")
    label = chat_stream_label(db, alice)
    assert strip_ansi(label) == "DeepParse|alice"
    assert "\x1b[" in label  # actually colored, not plain text


def test_chat_stream_label_no_nick_case_has_no_color_codes(db, alice):
    assert "\x1b[" not in chat_stream_label(db, alice)


def test_chat_stream_label_reverts_after_clearing(db, alice):
    set_nick(db, alice, "DeepParse")
    set_nick(db, alice, "")
    assert chat_stream_label(db, alice) == "alice"
