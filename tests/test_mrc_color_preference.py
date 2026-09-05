"""Tests for netbbs.net.mrc_color_preference (issue #298), the per-user
"show the colours MRC users put in their lines" setting -- mirrors
tests/test_unicode_style_preference.py's shape."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.net.mrc_color_preference import mrc_colors_enabled, set_mrc_colors_enabled
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


def test_defaults_to_on(db, alice):
    # Colour is part of how the MRC rooms talk, and the text is sanitized
    # before any code becomes a colour, so the rich default is safe.
    assert mrc_colors_enabled(db, alice) is True


def test_can_be_disabled_and_reenabled(db, alice):
    set_mrc_colors_enabled(db, alice, False)
    assert mrc_colors_enabled(db, alice) is False
    set_mrc_colors_enabled(db, alice, True)
    assert mrc_colors_enabled(db, alice) is True


def test_is_per_user(db, alice, bob):
    set_mrc_colors_enabled(db, alice, False)
    assert mrc_colors_enabled(db, bob) is True
