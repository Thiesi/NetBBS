"""
Issue #611: a password could be written once, at account creation, and
never changed by anyone. These tests drive every surface that now
changes one through a scripted session -- the shared screen
(`netbbs.net.password_screen`), the Profile field, the SysOp user
detail action and the admin CLI subcommand -- and assert on what a
*login* does afterwards, never on the stored hash. The domain function
is covered on its own in `tests/test_user_management.py`.

`FakeSession` is `tests/test_admin_flow.py`'s: one ordered queue for
every kind of read, which raises when exhausted, so a screen that asks
one prompt more than the test scripted fails rather than hangs.
"""

from __future__ import annotations

import asyncio

import nacl.signing
import pytest

from netbbs.admin.__main__ import build_parser, run_reset_password
from netbbs.auth.users import (
    SYSOP_LEVEL,
    AuthError,
    authenticate_password,
    create_user,
    has_password,
)
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.net.admin_flow import admin_menu
from netbbs.net.password_screen import manage_password_screen
from netbbs.net.profile_flow import _edit_profile
from netbbs.net.throttle import LoginThrottle
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


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _logs_in(db, username: str, password: str) -> bool:
    try:
        authenticate_password(db, username, password)
    except AuthError:
        return False
    return True


def _tight_throttle() -> LoginThrottle:
    """One attempt, then nothing until a refill that never comes inside
    a test."""
    return LoginThrottle(
        per_source_capacity=1, per_source_refill_per_minute=0,
        per_username_capacity=1, per_username_refill_per_minute=0,
        global_capacity=100, global_refill_per_minute=0,
        max_tracked_keys=100, max_concurrent_unauthenticated_sessions=10,
    )


# -- the shared screen, self-service -------------------------------------


def test_self_service_change_requires_the_current_password_and_applies_to_the_next_login(db, lane, alice):
    session = FakeSession(["c", "hunter2", "n3w-pass", "n3w-pass", "b"])
    asyncio.run(manage_password_screen(session, lane, alice, changed_by=alice))

    assert _logs_in(db, "alice", "n3w-pass")
    assert not _logs_in(db, "alice", "hunter2")
    assert "Password changed" in _written_text(session)
    entry = list_actions_for_target_user(db, alice.id)[-1]
    assert entry.action == "set_password"
    assert entry.actor_user_id == alice.id
    assert entry.detail is None


def test_a_wrong_current_password_changes_nothing_and_says_so(db, lane, alice):
    session = FakeSession(["c", "wrong", "b"])
    asyncio.run(manage_password_screen(session, lane, alice, changed_by=alice))

    assert _logs_in(db, "alice", "hunter2")
    assert "not the current password" in _written_text(session)
    assert list_actions_for_target_user(db, alice.id) == []


def test_a_mismatched_confirmation_changes_nothing(db, lane, alice):
    session = FakeSession(["c", "hunter2", "n3w-pass", "n3w-pass-typo", "b"])
    asyncio.run(manage_password_screen(session, lane, alice, changed_by=alice))

    assert _logs_in(db, "alice", "hunter2")
    assert "did not match" in _written_text(session)


def test_a_blank_new_password_cancels(db, lane, alice):
    session = FakeSession(["c", "hunter2", "", "b"])
    asyncio.run(manage_password_screen(session, lane, alice, changed_by=alice))

    assert _logs_in(db, "alice", "hunter2")
    assert "Cancelled" in _written_text(session)


def test_back_leaves_without_asking_anything(db, lane, alice):
    session = FakeSession(["b"])
    asyncio.run(manage_password_screen(session, lane, alice, changed_by=alice))

    text = _visible(_written_text(session))
    assert "Password on your account" in text
    assert "set" in text
    assert "Current password" not in text


def test_the_current_password_check_charges_the_login_throttle(db, lane, alice):
    # The throttle admits one attempt. The first activation spends it on
    # a wrong guess; the second is refused *before* a password is even
    # read -- proven by the queue: no "Current password" line is
    # consumed, the next scripted key is [B]ack.
    throttle = _tight_throttle()
    session = FakeSession(["c", "wrong", "c", "b"])
    session.login_throttle = throttle
    session.peer_address = "203.0.113.9"
    asyncio.run(manage_password_screen(session, lane, alice, changed_by=alice))

    text = _written_text(session)
    assert text.count("Current password") == 1
    assert "Too many password attempts" in text
    assert _logs_in(db, "alice", "hunter2")
    # And the budget it charged is the login prompt's own budget.
    assert throttle.allow_attempt(source="203.0.113.9", username="alice") is False


def test_a_correct_current_password_also_charges_the_throttle(db, lane, alice):
    throttle = _tight_throttle()
    session = FakeSession(["c", "hunter2", "n3w-pass", "n3w-pass", "b"])
    session.login_throttle = throttle
    asyncio.run(manage_password_screen(session, lane, alice, changed_by=alice))

    assert _logs_in(db, "alice", "n3w-pass")
    assert throttle.allow_attempt(source="unknown", username="alice") is False


def test_a_guest_session_cannot_change_the_password(db, lane, alice):
    # `read_key` raises when the queue is empty, so reaching the screen's
    # menu at all would fail this rather than quietly pressing [B]ack.
    session = FakeSession([])
    session.authenticated_without_credential = True
    result = asyncio.run(manage_password_screen(session, lane, alice, changed_by=alice))

    assert "signed in without a password" in _written_text(session)
    assert result is alice
    assert _logs_in(db, "alice", "hunter2")


def test_a_key_only_account_sets_its_first_password_without_a_current_one(db, lane):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    bob = create_user(db, "bob", verify_key=verify_key, user_level=10)
    session = FakeSession(["c", "first-pass", "first-pass", "b"])
    asyncio.run(manage_password_screen(session, lane, bob, changed_by=bob))

    assert _logs_in(db, "bob", "first-pass")
    text = _visible(_written_text(session))
    assert "key login only" in text or "key only" in text
    assert "Current password" not in text


def test_removing_the_password_needs_the_current_one_and_a_key(db, lane):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    carol = create_user(db, "carol", password="hunter2", verify_key=verify_key, user_level=10)
    session = FakeSession(["r", "hunter2", "y", "b"])
    asyncio.run(manage_password_screen(session, lane, carol, changed_by=carol))

    assert not _logs_in(db, "carol", "hunter2")
    assert not has_password(db, carol)
    assert "signs in by key only" in _written_text(session)
    assert list_actions_for_target_user(db, carol.id)[-1].action == "clear_password"


def test_remove_is_not_offered_to_a_password_only_account(db, lane, alice):
    # [R] with no key is an unhandled key: the screen redraws and the
    # next scripted key is [B]ack. Nothing was asked, nothing changed.
    session = FakeSession(["r", "b"])
    asyncio.run(manage_password_screen(session, lane, alice, changed_by=alice))

    assert has_password(db, alice)
    assert "[R]" not in _visible(_written_text(session))
    assert "Current password" not in _written_text(session)


# -- the shared screen, SysOp on someone else's account ------------------


def test_a_sysop_sets_another_accounts_password_without_the_current_one(db, lane, sysop, alice):
    session = FakeSession(["c", "reset-pass", "reset-pass", "b"])
    asyncio.run(manage_password_screen(session, lane, alice, changed_by=sysop))

    assert _logs_in(db, "alice", "reset-pass")
    text = _written_text(session)
    assert "Current password" not in text
    assert "Password set for 'alice'" in text
    entry = list_actions_for_target_user(db, alice.id)[-1]
    assert entry.action == "set_password"
    assert entry.actor_user_id == sysop.id


def test_a_sysop_on_their_own_account_still_proves_the_current_password(db, lane, sysop):
    session = FakeSession(["c", "hunter2", "own-pass", "own-pass", "b"])
    asyncio.run(manage_password_screen(session, lane, sysop, changed_by=sysop))

    assert _logs_in(db, "sysop", "own-pass")
    assert "Current password" in _written_text(session)


# -- reached from the Profile screen -------------------------------------


def test_profile_account_password_field_reaches_the_screen(db, lane, alice):
    session = FakeSession(["a", "c", "hunter2", "via-profile", "via-profile", "b", "b"])
    asyncio.run(_edit_profile(session, lane, alice))

    assert _logs_in(db, "alice", "via-profile")
    text = _visible(_written_text(session))
    assert "Password" in text
    assert "ccount password" in text


def test_profile_shows_whether_a_password_is_set(db, lane):
    verify_key = nacl.signing.SigningKey.generate().verify_key
    bob = create_user(db, "bob", verify_key=verify_key, user_level=10)
    # The Profile screen is sectioned; Account is the last of four.
    session = FakeSession(["PAGE_DOWN", "PAGE_DOWN", "PAGE_DOWN", "b"])
    asyncio.run(_edit_profile(session, lane, bob))

    text = _visible(_written_text(session))
    assert "Section 4 of 4" in text
    assert "key login only" in text


# -- reached from the SysOp user detail screen ---------------------------


def test_user_detail_password_action_resets_another_account(db, lane, sysop, alice):
    session = FakeSession(["u", "l", "g", str(alice.id), "p", "c", "by-sysop", "by-sysop", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    assert _logs_in(db, "alice", "by-sysop")
    assert not _logs_in(db, "alice", "hunter2")
    text = _visible(_written_text(session))
    assert "Password" in text
    assert "(by sysop)" in text  # the recent-actions list on redraw


def test_user_detail_shows_the_password_line_and_help(db, lane, sysop, alice):
    session = FakeSession(["u", "l", "g", str(alice.id), "CTRL+H", " ", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    text = _visible(_written_text(session))
    assert "Password" in text
    assert "never shown or recovered" in text


def test_user_detail_arrow_nav_reaches_the_password_field(db, lane, sysop, alice):
    # Down five times from nothing highlighted lands on "p", the fifth
    # entry of _USER_DETAIL_FIELD_ORDER (l, t, i, k, p, r); Space then
    # opens the password screen, whose [B]ack returns to the detail
    # screen.
    session = FakeSession(
        ["u", "l", "g", str(alice.id), "DOWN", "DOWN", "DOWN", "DOWN", "DOWN", " ", "b", "b", "b", "b"]
    )
    asyncio.run(admin_menu(session, lane, sysop))

    assert "Password on alice's account" in _visible(_written_text(session))


# -- the admin CLI -------------------------------------------------------


def test_cli_reset_password_sets_a_new_password(db, sysop, alice):
    session = FakeSession(["new-cli-pass", "new-cli-pass"])
    status = asyncio.run(run_reset_password(session, db, "sysop", "alice"))

    assert status == 0
    assert _logs_in(db, "alice", "new-cli-pass")
    entry = list_actions_for_target_user(db, alice.id)[-1]
    assert entry.action == "set_password"
    assert entry.actor_user_id == sysop.id


def test_cli_reset_password_works_on_the_sysops_own_locked_out_account(db, sysop):
    session = FakeSession(["back-in", "back-in"])
    status = asyncio.run(run_reset_password(session, db, "sysop", "sysop"))

    assert status == 0
    assert _logs_in(db, "sysop", "back-in")
    entry = list_actions_for_target_user(db, sysop.id)[-1]
    assert entry.actor_user_id == sysop.id


def test_cli_reset_password_refuses_an_unknown_account(db, sysop):
    session = FakeSession([])
    status = asyncio.run(run_reset_password(session, db, "sysop", "nobody"))

    assert status == 1
    assert "No account named 'nobody'" in _written_text(session)


def test_cli_reset_password_mismatch_changes_nothing(db, sysop, alice):
    session = FakeSession(["one", "two"])
    status = asyncio.run(run_reset_password(session, db, "sysop", "alice"))

    assert status == 1
    assert _logs_in(db, "alice", "hunter2")


def test_cli_reset_password_is_case_insensitive_about_the_username(db, sysop, alice):
    session = FakeSession(["mixed", "mixed"])
    asyncio.run(run_reset_password(session, db, "sysop", "ALICE"))

    assert _logs_in(db, "alice", "mixed")


def test_cli_parser_accepts_the_common_options_on_either_side_of_the_subcommand():
    parser = build_parser()
    before = parser.parse_args(["--db", "x.db", "--as", "sysop", "reset-password", "bob"])
    after = parser.parse_args(["reset-password", "bob", "--db", "x.db", "--as", "sysop"])
    assert (str(before.db), before.as_username, before.command, before.username) == ("x.db", "sysop", "reset-password", "bob")
    assert (str(after.db), after.as_username, after.command, after.username) == ("x.db", "sysop", "reset-password", "bob")
    plain = parser.parse_args([])
    assert plain.command is None
