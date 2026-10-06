"""
A SysOp, or a staff member with manage accounts, corrects a caller's display
name and birthdate (issue #1110): the caller's own rules, the same reach as a
password reset, an audit record, and verified values left alone.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from netbbs.attestation import (
    ProfileFieldError,
    attest_age,
    change_birthdate,
    change_display_name,
    get_attestation,
    get_birthdate,
    get_display_name,
    is_birthdate_visible,
    is_display_name_visible,
    meets_age,
    set_birthdate,
    set_birthdate_visible,
    set_display_name,
)
from netbbs.auth.users import (
    SYSOP_LEVEL,
    StaffPermission,
    UserManagementError,
    create_user,
    set_staff_permissions,
)
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text

MANAGE = StaffPermission.MANAGE_ACCOUNTS
APPROVE = StaffPermission.APPROVE_ACCOUNTS


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
    return create_user(db, "inkwell", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def carol(db):
    return create_user(db, "carol", password="hunter2pw")


def _staff(db, sysop, name, permissions):
    user = create_user(db, name, password="hunter2", user_level=10)
    return set_staff_permissions(db, user, permissions, changed_by=sysop)


def _actions(db, user, action):
    return [entry for entry in list_actions_for_target_user(db, user.id) if entry.action == action]


# -- the rules ------------------------------------------------------------------


def test_a_sysop_changes_a_display_name_and_the_history_keeps_both(db, sysop, carol):
    set_display_name(db, carol, "Caro")
    assert change_display_name(db, carol, "Carol R.", changed_by=sysop) is True
    assert get_display_name(db, carol) == "Carol R."
    [entry] = _actions(db, carol, "set_display_name")
    assert entry.actor_user_id == sysop.id
    assert "'Caro'" in entry.detail and "'Carol R.'" in entry.detail


def test_a_blank_display_name_clears_it(db, sysop, carol):
    set_display_name(db, carol, "Caro")
    assert change_display_name(db, carol, "  ", changed_by=sysop) is True
    assert get_display_name(db, carol) is None
    [entry] = _actions(db, carol, "set_display_name")
    assert "cleared" in entry.detail


def test_a_display_name_the_callers_own_rules_refuse_is_refused(db, sysop, carol):
    with pytest.raises(ProfileFieldError):
        change_display_name(db, carol, "Carol =verified=", changed_by=sysop)
    # A look-alike of a SysOp's username, as the caller's own Profile refuses.
    with pytest.raises(ProfileFieldError):
        change_display_name(db, carol, "InkWell", changed_by=sysop)
    assert get_display_name(db, carol) is None
    assert _actions(db, carol, "set_display_name") == []


def test_a_sysop_sets_a_birthdate_and_the_history_never_names_the_date(db, sysop, carol):
    assert change_birthdate(db, carol, date(1990, 5, 1), changed_by=sysop) is True
    assert get_birthdate(db, carol) == date(1990, 5, 1)
    assert change_birthdate(db, carol, date(1991, 6, 2), changed_by=sysop) is True
    assert change_birthdate(db, carol, None, changed_by=sysop) is True
    assert get_birthdate(db, carol) is None
    details = [entry.detail for entry in _actions(db, carol, "set_birthdate")]
    assert details == ["birthdate set", "birthdate changed", "birthdate cleared"]
    assert not any("199" in detail for detail in details)


@pytest.mark.parametrize("birthdate", [date(2999, 1, 1), date(1899, 12, 31), date(198, 5, 1)])
def test_an_impossible_birthdate_is_refused_for_the_sysop_and_the_caller(db, sysop, carol, birthdate):
    with pytest.raises(ProfileFieldError):
        change_birthdate(db, carol, birthdate, changed_by=sysop)
    with pytest.raises(ProfileFieldError):
        set_birthdate(db, carol, birthdate)
    assert get_birthdate(db, carol) is None


def test_an_unchanged_value_records_nothing(db, sysop, carol):
    set_display_name(db, carol, "Caro")
    assert change_display_name(db, carol, "Caro", changed_by=sysop) is False
    assert _actions(db, carol, "set_display_name") == []


def test_the_callers_visibility_settings_are_untouched(db, sysop, carol):
    set_birthdate_visible(db, carol, True)
    change_birthdate(db, carol, date(1990, 5, 1), changed_by=sysop)
    change_display_name(db, carol, "Caro", changed_by=sysop)
    assert is_birthdate_visible(db, carol) is True
    assert is_display_name_visible(db, carol) is False


def test_a_verified_age_is_left_alone_and_still_decides_the_gate(db, sysop, carol):
    attest_age(db, carol, date(1980, 1, 1), verifier=sysop)
    change_birthdate(db, carol, date(2020, 1, 1), changed_by=sysop)
    assert get_attestation(db, carol, "age") is not None
    assert meets_age(db, carol, 18) is True


# -- who may ----------------------------------------------------------------------


def test_an_account_manager_corrects_a_members_details(db, sysop, carol):
    helper = _staff(db, sysop, "helper", MANAGE)
    assert change_display_name(db, carol, "Caro", changed_by=helper) is True
    assert change_birthdate(db, carol, date(1990, 5, 1), changed_by=helper) is True


def test_staff_without_manage_accounts_are_refused(db, sysop, carol):
    helper = _staff(db, sysop, "helper", APPROVE)
    with pytest.raises(UserManagementError):
        change_display_name(db, carol, "Caro", changed_by=helper)
    with pytest.raises(UserManagementError):
        change_birthdate(db, carol, date(1990, 5, 1), changed_by=helper)
    assert get_display_name(db, carol) is None


def test_a_manager_never_reaches_the_sysop_other_staff_or_themselves(db, sysop):
    helper = _staff(db, sysop, "helper", MANAGE)
    other = _staff(db, sysop, "other", APPROVE)
    for target in (sysop, other, helper):
        with pytest.raises(UserManagementError):
            change_display_name(db, target, "Someone", changed_by=helper)
        with pytest.raises(UserManagementError):
            change_birthdate(db, target, date(1990, 5, 1), changed_by=helper)


def test_a_plain_account_changes_nobodys_details(db, carol):
    dave = create_user(db, "dave", password="hunter2pw")
    with pytest.raises(UserManagementError):
        change_display_name(db, carol, "Caro", changed_by=dave)


def test_a_sysop_may_correct_another_sysops_details(db, sysop):
    other = create_user(db, "quill", password="hunter2", user_level=SYSOP_LEVEL)
    assert change_display_name(db, other, "Quill Keeper", changed_by=sysop) is True


# -- the account screen -------------------------------------------------------------


def _detail(session, lane, actor, target):
    from netbbs.net.admin_flow import _user_detail_screen

    asyncio.run(_user_detail_screen(session, lane, actor, target, None))
    return _visible(_written_text(session))


def test_the_account_screen_shows_and_edits_the_display_name(db, lane, sysop, carol):
    session = FakeSession(["n", "Carol R.", "b"])
    text = _detail(session, lane, sysop, carol)
    assert "Display name:" in text and "Display [n]ame" in text
    assert get_display_name(db, carol) == "Carol R."
    assert "Display name for 'carol' is now 'Carol R.'." in text


def test_the_account_screen_edits_the_birthdate_and_refuses_a_bad_one(db, lane, sysop, carol):
    session = FakeSession(["e", "1990-05-01", "e", "not a date", "b"])
    text = _detail(session, lane, sysop, carol)
    assert get_birthdate(db, carol) == date(1990, 5, 1)
    assert "Not a valid date" in text


def test_the_account_screen_says_when_an_age_is_verified(db, lane, sysop, carol):
    attest_age(db, carol, date(1980, 1, 1), verifier=sysop)
    text = _detail(FakeSession(["b"]), lane, sysop, carol)
    assert "age verified" in text


def test_a_staff_member_without_manage_accounts_is_not_offered_the_edits(db, lane, sysop, carol):
    helper = _staff(db, sysop, "helper", APPROVE)
    text = _detail(FakeSession(["n", "b"]), lane, helper, carol)
    assert "Display [n]ame" not in text
    assert get_display_name(db, carol) is None


def test_an_approver_reviewing_a_signup_sees_no_birthdate(db, lane, sysop):
    # Review on #1112: the fields are shown only to whoever may edit them.
    helper = _staff(db, sysop, "helper", APPROVE)
    pending = create_user(db, "dana", password="hunter2pw", pending_approval=True)
    set_birthdate(db, pending, date(1977, 3, 4))
    set_display_name(db, pending, "Dana D.")
    text = _detail(FakeSession(["b"]), lane, helper, pending)
    assert "1977-03-04" not in text and "Dana D." not in text
    assert "Birthdate" not in text
