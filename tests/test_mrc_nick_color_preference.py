"""Tests for netbbs.net.mrc_nick_color_preference (issue #304), the CGA
colour a caller's handle wears on MRC -- and the Profile field that
sets it."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.mrc.protocol import DEFAULT_NICK_COLOR, format_room_body
from netbbs.net.mrc_nick_color_preference import (
    mrc_nick_color,
    mrc_nick_color_for_username,
    set_mrc_nick_color,
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


def test_defaults_to_the_house_yellow(db, alice):
    assert DEFAULT_NICK_COLOR == 14
    assert mrc_nick_color(db, alice) == 14
    assert mrc_nick_color_for_username(db, "alice") == 14
    assert mrc_nick_color_for_username(db, "nobody") == 14


def test_round_trips_and_rejects_non_cga_values(db, alice):
    set_mrc_nick_color(db, alice, 12)
    assert mrc_nick_color(db, alice) == 12
    assert mrc_nick_color_for_username(db, "alice") == 12
    assert format_room_body("alice", "hi", nick_color=12) == "|08<|12alice|08>|16|07 hi"
    with pytest.raises(ValueError):
        set_mrc_nick_color(db, alice, 16)
    with pytest.raises(ValueError):
        set_mrc_nick_color(db, alice, -1)
    # An out-of-range stored value (an older row, a hand edit) falls back.
    from netbbs.user_preferences import set_user_preference
    set_user_preference(db, alice, "mrc_nick_color", "99")
    assert mrc_nick_color(db, alice) == 14


def test_profile_screen_cycles_the_colour(db, alice, tmp_path):
    from netbbs.net import profile_flow
    from netbbs.storage.execution import DatabaseLane
    from tests.test_admin_flow import FakeSession, _visible, _written_text

    lane = DatabaseLane(db.path)
    try:
        # The screen opens on its first section; the first "y" jumps to
        # the Display section and advances yellow (14) to white (15), the
        # second wraps to black (0); b: back. Each press persists at once.
        session = FakeSession(["y", "y", "b"])
        asyncio.run(profile_flow._edit_profile(session, lane, alice))
        text = _visible(_written_text(session))
        assert "MRC nick colour" in text and "white (|15)" in text and "black (|00)" in text
        assert mrc_nick_color(db, alice) == 0
    finally:
        lane.close()
