"""Tests for netbbs.user_preferences — the generic per-user key-value
store (design doc §13)."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.storage.database import Database
from netbbs.user_preferences import get_user_preference, set_user_preference


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


def test_get_returns_default_when_unset(db, alice):
    assert get_user_preference(db, alice, "theme") is None
    assert get_user_preference(db, alice, "theme", default="dark") == "dark"


def test_set_then_get_returns_value(db, alice):
    set_user_preference(db, alice, "theme", "dark")
    assert get_user_preference(db, alice, "theme") == "dark"


def test_set_overwrites_existing_value(db, alice):
    set_user_preference(db, alice, "theme", "dark")
    set_user_preference(db, alice, "theme", "light")
    assert get_user_preference(db, alice, "theme") == "light"


def test_preferences_are_independent_per_user(db, alice, bob):
    set_user_preference(db, alice, "theme", "dark")
    assert get_user_preference(db, bob, "theme") is None


def test_preferences_are_independent_per_key(db, alice):
    set_user_preference(db, alice, "theme", "dark")
    assert get_user_preference(db, alice, "timezone") is None
