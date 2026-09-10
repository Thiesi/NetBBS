"""Tests for netbbs.net.mrc_lastseen_preference (issue #378): the
LASTSEEN opt-out, on by default -- and the Profile field."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.net.mrc_lastseen_preference import (
    mrc_lastseen_for_username,
    mrc_lastseen_recorded,
    set_mrc_lastseen_recorded,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_on_by_default_and_round_trips(db, alice):
    assert mrc_lastseen_recorded(db, alice) is True
    # No explicit choice yet: the bridge sends nothing (the hub's default).
    assert mrc_lastseen_for_username(db, "alice") is None
    assert mrc_lastseen_for_username(db, "nobody") is None
    set_mrc_lastseen_recorded(db, alice, False)
    assert mrc_lastseen_recorded(db, alice) is False and mrc_lastseen_for_username(db, "alice") is False
    set_mrc_lastseen_recorded(db, alice, True)
    assert mrc_lastseen_for_username(db, "alice") is True  # explicit ON, sent as such


def test_profile_screen_toggles_it(db, alice):
    from netbbs.net import profile_flow
    from netbbs.storage.execution import DatabaseLane
    from tests.test_admin_flow import FakeSession, _visible, _written_text

    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["w", "b"])
        asyncio.run(profile_flow._edit_profile(session, lane, alice))
        text = _visible(_written_text(session))
        assert "MRC may remember when you were last seen: no" in text
        assert mrc_lastseen_recorded(db, alice) is False
        session = FakeSession(["w", "b"])
        asyncio.run(profile_flow._edit_profile(session, lane, alice))
        assert mrc_lastseen_recorded(db, alice) is True
    finally:
        lane.close()
