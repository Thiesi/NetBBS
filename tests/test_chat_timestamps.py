"""Tests for netbbs.chat.timestamps (design doc round 32 point 3, round
42 point 6, sign-off round 62) — the per-user chat timestamp preference
store and formatting helper, in isolation from the /timestamps command
and chat-loop wiring that drives it (covered separately in
tests/test_chat_flow_timestamps.py)."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.timestamps import format_with_preference, set_timestamps_enabled, timestamps_enabled
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_defaults_to_disabled(db, alice):
    assert timestamps_enabled(db, alice) is False


def test_set_enabled_then_read_back(db, alice):
    set_timestamps_enabled(db, alice, True)
    assert timestamps_enabled(db, alice) is True


def test_set_disabled_after_enabled(db, alice):
    set_timestamps_enabled(db, alice, True)
    set_timestamps_enabled(db, alice, False)
    assert timestamps_enabled(db, alice) is False


def test_preference_is_per_user(db, alice):
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_timestamps_enabled(db, alice, True)
    assert timestamps_enabled(db, alice) is True
    assert timestamps_enabled(db, bob) is False


def test_format_with_preference_returns_text_unchanged_when_disabled(db, alice):
    assert format_with_preference(db, alice, "hello", "2026-01-01T00:00:00.000000Z") == "hello"


def test_format_with_preference_prepends_a_timestamp_when_enabled(db, alice):
    set_timestamps_enabled(db, alice, True)
    result = format_with_preference(db, alice, "hello", "2026-01-01T12:34:00.000000Z")
    assert result != "hello"
    assert "hello" in result
    assert "01.01.2026 12:34" in result
