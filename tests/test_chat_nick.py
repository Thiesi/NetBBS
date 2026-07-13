"""Tests for netbbs.chat.nick — transparent display aliases (design
doc round 32/41)."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.nick import MAX_NICK_LENGTH, NickError, display_label, get_nick, set_nick
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
