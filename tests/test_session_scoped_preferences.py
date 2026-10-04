"""A guest session's display preferences last for the call (issue #1073).

A guest on a plain-ASCII or 16-colour terminal still needs to pick a
character set or colour depth, but every guest shares one account: what one
of them chooses must not become what the next one gets. For a session that
signed in without a credential, preference writes stay in memory
(`netbbs.user_preferences.session_scoped_preferences`) and the account's
stored values are untouched.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest

from netbbs.auth.users import create_user
from netbbs.net import login_flow, profile_flow
from netbbs.net.redraw_preference import redraw_in_place_enabled, set_redraw_in_place_enabled
from netbbs.net.unicode_style_preference import charset_preference, set_charset_preference
from netbbs.rendering.charset import ASCII
from netbbs.sort_preferences import (
    clear_sort_preference,
    get_effective_sort_mode,
    list_sort_preferences,
    set_sort_preference,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.user_preferences import (
    get_user_preference,
    session_preferences_for,
    session_scoped_preferences,
    set_user_preference,
)
from tests.test_guest_profile_refusals import FakeSession


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
def guest(db):
    account = create_user(db, "guest", password="hunter2", user_level=1)
    set_charset_preference(db, account, "unicode")
    return account


def _stored_rows(db, user) -> list[tuple]:
    return sorted(tuple(row) for row in db.connection.execute(
        "SELECT key, value FROM user_preferences WHERE user_id = ?", (user.id,)
    ))


def test_a_write_inside_the_scope_stays_off_the_database(db, guest):
    before = _stored_rows(db, guest)

    with session_scoped_preferences(guest):
        set_user_preference(db, guest, "redraw_in_place", "off")
        assert get_user_preference(db, guest, "redraw_in_place") == "off"

    assert _stored_rows(db, guest) == before
    assert get_user_preference(db, guest, "redraw_in_place") is None


def test_the_scope_only_covers_its_own_account(db, guest):
    other = create_user(db, "alice", password="hunter2", user_level=10)

    with session_scoped_preferences(guest):
        set_redraw_in_place_enabled(db, other, True)

    assert redraw_in_place_enabled(db, other) is True


def test_a_lane_job_runs_in_the_callers_context(db, guest, lane):
    """The getters run on the lane's worker thread; without the caller's
    context there they would read the stored value, not the session's."""

    async def scenario():
        with session_scoped_preferences(guest):
            await lane.run(set_redraw_in_place_enabled, guest, True)
            inside = await lane.run(redraw_in_place_enabled, guest)
        outside = await lane.run(redraw_in_place_enabled, guest)
        return inside, outside

    assert asyncio.run(scenario()) == (True, False)
    assert redraw_in_place_enabled(db, guest) is False


def test_a_guest_changes_the_character_set_for_this_call_only(db, lane, guest):
    """Profile's Character set entry, pressed as a guest: the session renders
    with the new set, the account keeps the old one, and the next guest gets
    the account's."""
    session = FakeSession(keys=["u", "u", "b"], guest=True)

    async def first_guest():
        with session_scoped_preferences(guest):
            await profile_flow._edit_profile(session, lane, guest)
            return await lane.run(charset_preference, guest)

    # unicode -> cp437 -> ascii
    assert asyncio.run(first_guest()) == "ascii"
    assert session.output_charset is ASCII
    assert charset_preference(db, guest) == "unicode"

    async def second_guest():
        with session_scoped_preferences(guest):
            return await lane.run(charset_preference, guest)

    assert asyncio.run(second_guest()) == "unicode"


def test_a_guest_redraw_style_change_does_not_reach_the_account(db, lane, guest):
    before = _stored_rows(db, guest)
    session = FakeSession(keys=["r", "b"], guest=True)

    async def scenario():
        with session_scoped_preferences(guest):
            await profile_flow._edit_profile(session, lane, guest)
            return await lane.run(redraw_in_place_enabled, guest)

    assert asyncio.run(scenario()) is True
    assert _stored_rows(db, guest) == before


def test_an_ordinary_session_still_saves_its_preferences(db, lane, guest):
    session = FakeSession(keys=["r", "b"], guest=False)

    asyncio.run(profile_flow._edit_profile(session, lane, guest))

    assert redraw_in_place_enabled(db, guest) is True


# -- sort orders ------------------------------------------------------------


def _stored_sort(db, user) -> list[tuple]:
    return [tuple(row) for row in db.connection.execute(
        "SELECT resource_kind, community_id, category_id, sort_mode FROM user_sort_preferences WHERE user_id = ?",
        (user.id,),
    )]


def test_a_guest_sort_order_lasts_for_the_call(db, guest):
    set_sort_preference(db, guest, "board", "alphabetical")
    before = _stored_sort(db, guest)

    with session_scoped_preferences(guest):
        set_sort_preference(db, guest, "board", "recent")
        set_sort_preference(db, guest, "channel", "volume")
        assert get_effective_sort_mode(db, guest, "board") == "recent"
        assert get_effective_sort_mode(db, guest, "channel") == "volume"
        listed = {(p.resource_kind, p.sort_mode) for p in list_sort_preferences(db, guest)}
        assert listed == {("board", "recent"), ("channel", "volume")}

        clear_sort_preference(db, guest, "board")
        # Cleared for this session: the next scope down, the default.
        assert get_effective_sort_mode(db, guest, "board") == "sysop"
        assert {p.resource_kind for p in list_sort_preferences(db, guest)} == {"channel"}

    assert _stored_sort(db, guest) == before
    assert get_effective_sort_mode(db, guest, "board") == "alphabetical"


# -- where the scope is entered ---------------------------------------------


@pytest.mark.parametrize("guest_session", [True, False])
def test_signing_in_without_a_credential_enters_the_scope(monkeypatch, guest, guest_session):
    seen = {}

    async def fake_body(session, db, hub, presence, mailbox, user, **options):
        seen["scope"] = session_preferences_for(user)

    monkeypatch.setattr(login_flow, "_run_signed_in", fake_body)
    session = FakeSession(guest=guest_session)

    asyncio.run(login_flow.run_authenticated_session(session, None, None, None, None, guest))

    assert (seen["scope"] is not None) is guest_session
    # Left again on the way out.
    assert contextvars.copy_context().run(session_preferences_for, guest) is None
