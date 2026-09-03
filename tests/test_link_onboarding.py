"""
Tests for `netbbs.link.onboarding` (design doc §16, issue #219): the
node-wide participation decision and the two-source resolution of
effective Link enablement.
"""

from __future__ import annotations

import pytest

from netbbs.config import is_node_display_name_placeholder, set_node_display_name
from netbbs.link.onboarding import (
    Participation,
    get_configured_link_enabled,
    get_participation,
    participation_accepted,
    resolve_link_enabled,
    set_configured_link_enabled,
    set_participation,
)
from netbbs.storage.database import Database


def test_participation_defaults_to_undecided(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_participation(db) is Participation.UNDECIDED
    assert participation_accepted(db) is False


def test_participation_round_trips(tmp_path):
    db = Database(tmp_path / "node.db")
    set_participation(db, Participation.ACCEPTED)
    assert get_participation(db) is Participation.ACCEPTED
    assert participation_accepted(db) is True
    set_participation(db, Participation.DECLINED)
    assert get_participation(db) is Participation.DECLINED
    assert participation_accepted(db) is False


@pytest.mark.parametrize("configured,decision,expected", [
    (True, Participation.UNDECIDED, True),
    (True, Participation.DECLINED, True),
    (False, Participation.ACCEPTED, False),
    (None, Participation.UNDECIDED, False),
    (None, Participation.DECLINED, False),
    (None, Participation.ACCEPTED, True),
])
def test_explicit_configuration_wins_and_a_silent_one_defers(tmp_path, configured, decision, expected):
    db = Database(tmp_path / "node.db")
    set_participation(db, decision)
    assert resolve_link_enabled(configured, db) is expected


def test_configured_link_enabled_cache_distinguishes_unknown_from_unset(tmp_path):
    db = Database(tmp_path / "node.db")
    assert get_configured_link_enabled(db) == "unknown"
    set_configured_link_enabled(db, None)
    assert get_configured_link_enabled(db) is None
    set_configured_link_enabled(db, True)
    assert get_configured_link_enabled(db) is True
    set_configured_link_enabled(db, False)
    assert get_configured_link_enabled(db) is False


def test_placeholder_display_name_detection_is_case_insensitive(tmp_path):
    db = Database(tmp_path / "node.db")
    assert is_node_display_name_placeholder(db) is True
    set_node_display_name(db, "netbbs")
    assert is_node_display_name_placeholder(db) is True
    set_node_display_name(db, "The Lighthouse")
    assert is_node_display_name_placeholder(db) is False
