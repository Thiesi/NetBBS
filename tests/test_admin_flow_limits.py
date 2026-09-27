"""Settings > Limits & retention (issue #725): the upload cap, the grace
before expired content is deleted, the default channel invitation
expiry and the chat scrollback limit, which the node always read but
no screen could set."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat.scrollback import get_scrollback_limit, set_scrollback_limit
from netbbs.config import (
    get_expiry_grace_period_days,
    get_invitation_expiry_days,
    get_max_upload_bytes,
    set_expiry_grace_period_days,
    set_invitation_expiry_days,
    set_max_upload_bytes,
    set_config,
)
from netbbs.moderation.log import list_recent_actions
from netbbs.net.admin_flow import admin_menu
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


MIB = 1024 * 1024


def test_settings_menu_offers_limits_and_retention(db, lane, sysop):
    session = FakeSession(["s", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert "Limit[s] & retention" in _visible(_written_text(session))


def test_limits_screen_shows_the_defaults(db, lane, sysop):
    # s: Settings, s: Limits & retention, b: back out of everything.
    session = FakeSession(["s", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _visible(_written_text(session))
    assert "Limits & retention" in text
    assert "100.0 MiB" in text
    assert "7 days" in text
    assert "100 messages per channel" in text


def test_limits_screen_saves_all_four_and_audits_the_change(db, lane, sysop):
    session = FakeSession([
        "s", "s", "u", "250", "g", "14", "i", "", "c", "500", "s", "b", "b", "b",
    ])
    asyncio.run(admin_menu(session, lane, sysop))
    assert get_max_upload_bytes(db) == 250 * MIB
    assert get_expiry_grace_period_days(db) == 14
    assert get_invitation_expiry_days(db) is None
    assert get_scrollback_limit(db) == 500
    assert "Saved. Applies from now on." in _visible(_written_text(session))
    audit = [a for a in list_recent_actions(db, limit=10) if a.action == "set_limits_and_retention"]
    assert len(audit) == 1
    assert "upload_bytes=262144000" in audit[0].detail and "invite_days=None" in audit[0].detail


def test_limits_screen_rejects_an_out_of_range_value_and_writes_nothing(db, lane, sysop):
    # A zero scrollback is refused at Save; the draft stays open with the
    # message, and backing out (b, y: discard) writes none of the fields.
    session = FakeSession(["s", "s", "g", "30", "c", "0", "s", "b", "y", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert "Chat scrollback must be 1-10000 messages." in _visible(_written_text(session))
    assert get_scrollback_limit(db) == 100
    assert get_expiry_grace_period_days(db) == 7
    assert not [a for a in list_recent_actions(db, limit=10) if a.action == "set_limits_and_retention"]


def test_limits_screen_keeps_an_odd_byte_upload_cap_unless_it_is_edited(db, lane, sysop):
    # A cap that is not a whole MiB (only reachable through the dev
    # script) must not be rounded away by saving an unrelated field.
    set_config(db, "max_upload_bytes", str(5 * MIB + 123))
    session = FakeSession(["s", "s", "g", "3", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert get_max_upload_bytes(db) == 5 * MIB + 123
    assert get_expiry_grace_period_days(db) == 3


def test_limits_screen_applies_the_floored_mib_when_the_sysop_chooses_it(db, lane, sysop):
    # Codex review: entering the number already shown for an odd-byte cap
    # is a choice of exactly that many MiB, not "unchanged".
    set_config(db, "max_upload_bytes", str(5 * MIB + 123))
    session = FakeSession(["s", "s", "u", "5", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert get_max_upload_bytes(db) == 5 * MIB


def test_getters_clamp_values_stored_before_the_ceilings(db):
    # Codex review: an upgraded node may hold values the old, unbounded
    # setters accepted; the new bounds must hold for it too.
    set_config(db, "chat_scrollback_limit", "50000")
    set_config(db, "post_expiry_grace_period_days", "99999999")
    set_config(db, "channel_invitation_expiry_days", "99999999")
    set_config(db, "max_upload_bytes", str(10 ** 15))
    assert get_scrollback_limit(db) == 10_000
    assert get_expiry_grace_period_days(db) == 3650
    assert get_invitation_expiry_days(db) == 3650
    assert get_max_upload_bytes(db) == 64 * 1024 * MIB


def test_limits_screen_save_with_nothing_changed_writes_no_audit(db, lane, sysop):
    session = FakeSession(["s", "s", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert "Nothing changed." in _visible(_written_text(session))
    assert not [a for a in list_recent_actions(db, limit=10) if a.action == "set_limits_and_retention"]


@pytest.mark.parametrize(
    ("setter", "bad"),
    [
        (set_max_upload_bytes, 64 * 1024 * MIB + 1),
        (set_expiry_grace_period_days, 3651),
        (set_invitation_expiry_days, 3651),
        (set_scrollback_limit, 10_001),
    ],
)
def test_setters_refuse_values_past_their_upper_bound(db, setter, bad):
    with pytest.raises(ValueError):
        setter(db, bad)
