"""Tests for netbbs.net.mrc_private_preference (issue #305): the opt-in
to private MRC messages, off by default -- and the Profile field that
flips it."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.net.mrc_private_preference import (
    mrc_private_messages_enabled,
    mrc_private_messages_for_username,
    set_mrc_private_messages_enabled,
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


def test_off_by_default_including_for_unknown_accounts(db, alice):
    assert mrc_private_messages_enabled(db, alice) is False
    assert mrc_private_messages_for_username(db, "alice") is False
    assert mrc_private_messages_for_username(db, "nobody") is False


def test_round_trips(db, alice):
    set_mrc_private_messages_enabled(db, alice, True)
    assert mrc_private_messages_enabled(db, alice) is True
    assert mrc_private_messages_for_username(db, "alice") is True
    set_mrc_private_messages_enabled(db, alice, False)
    assert mrc_private_messages_for_username(db, "alice") is False
    # A stray stored value is off, never on.
    from netbbs.user_preferences import set_user_preference
    set_user_preference(db, alice, "mrc_private_messages", "maybe")
    assert mrc_private_messages_enabled(db, alice) is False


def test_profile_screen_toggles_it(db, alice):
    from netbbs.net import profile_flow
    from netbbs.storage.execution import DatabaseLane
    from tests.test_admin_flow import FakeSession, _visible, _written_text

    lane = DatabaseLane(db.path)
    try:
        # "p" jumps to the Communication section and turns the opt-in on;
        # the second press turns it back off. Each press persists at once.
        session = FakeSession(["p", "b"])
        asyncio.run(profile_flow._edit_profile(session, lane, alice))
        text = _visible(_written_text(session))
        assert "Private messages from MRC users: accepted" in text
        assert mrc_private_messages_enabled(db, alice) is True
        session = FakeSession(["p", "b"])
        asyncio.run(profile_flow._edit_profile(session, lane, alice))
        assert "Private messages from MRC users: not accepted" in _visible(_written_text(session))
        assert mrc_private_messages_enabled(db, alice) is False
    finally:
        lane.close()
