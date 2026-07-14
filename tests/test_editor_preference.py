"""Tests for netbbs.net.editor_preference (design doc -- prose editor
round B2), the per-user fullscreen-editor opt-in preference -- mirrors
tests/test_chat_timestamps.py's shape for the analogous /timestamps
preference."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.net.editor_preference import fullscreen_editor_enabled, set_fullscreen_editor_enabled
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_defaults_to_off(db, alice):
    assert fullscreen_editor_enabled(db, alice) is False


def test_can_be_enabled(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    assert fullscreen_editor_enabled(db, alice) is True


def test_can_be_disabled_again(db, alice):
    set_fullscreen_editor_enabled(db, alice, True)
    set_fullscreen_editor_enabled(db, alice, False)
    assert fullscreen_editor_enabled(db, alice) is False


def test_is_per_user(db, alice):
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_fullscreen_editor_enabled(db, alice, True)
    assert fullscreen_editor_enabled(db, alice) is True
    assert fullscreen_editor_enabled(db, bob) is False
