"""
Issue #1115: a verified age or real name can be revoked, on its own and on
purpose; clearing the self-entered field never does it; and a caller's own
Profile shows what this node verified apart from what they typed, and lets
them clear what they typed.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from netbbs.age_requirement import VERIFIED
from netbbs.attestation import (
    AttestationError,
    attest_age,
    attest_name,
    change_birthdate,
    clear_own_profile_field,
    get_attestation,
    get_birthdate,
    get_display_name,
    get_location,
    meets_age,
    meets_name_requirement,
    revoke_attestation,
    set_birthdate,
    set_display_name,
    set_location,
)
from netbbs.auth.users import SYSOP_LEVEL, StaffPermission, create_user, set_can_verify_identity, set_staff_permissions
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.net import profile_flow
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession as AdminSession, _visible, _written_text
from tests.test_login_flow_identity_details_screen import FakeSession as ProfileSession
from tests.test_login_flow_identity_details_screen import _visible as profile_visible
from tests.test_login_flow_identity_details_screen import squeezed

BORN = date(1980, 1, 1)


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
    return create_user(db, "carol", password="hunter2pw", user_level=10)


def _actions(db, user, action):
    return [entry for entry in list_actions_for_target_user(db, user.id) if entry.action == action]


# -- revoking ---------------------------------------------------------------------


def test_revoking_a_verified_age_reopens_the_gate_and_is_recorded(db, sysop, carol):
    attest_age(db, carol, BORN, verifier=sysop)
    assert meets_age(db, carol, 18, VERIFIED) is True

    assert revoke_attestation(db, carol, "age", actor=sysop) is True

    assert get_attestation(db, carol, "age") is None
    assert meets_age(db, carol, 18, VERIFIED) is False
    [entry] = _actions(db, carol, "revoke_age")
    assert entry.actor_user_id == sysop.id


def test_revoking_a_verified_name_reopens_the_name_gate(db, sysop, carol):
    attest_name(db, carol, "Carol Example", verifier=sysop)
    assert meets_name_requirement(db, carol, "verified") is True
    assert revoke_attestation(db, carol, "name", actor=sysop) is True
    assert meets_name_requirement(db, carol, "verified") is False


def test_a_sysop_still_passes_after_their_own_verification_is_revoked(db, sysop):
    other = create_user(db, "deputy", password="hunter2", user_level=SYSOP_LEVEL)
    attest_age(db, sysop, BORN, verifier=other)
    revoke_attestation(db, sysop, "age", actor=other)
    # The bypass of issue #1096 does not depend on a verification.
    assert meets_age(db, sysop, 18, VERIFIED) is True


def test_nothing_to_revoke_records_nothing(db, sysop, carol):
    assert revoke_attestation(db, carol, "age", actor=sysop) is False
    assert _actions(db, carol, "revoke_age") == []


def test_only_a_verifier_may_revoke(db, sysop, carol):
    attest_age(db, carol, BORN, verifier=sysop)
    helper = create_user(db, "helper", password="hunter2", user_level=10)
    helper = set_staff_permissions(db, helper, StaffPermission.MANAGE_ACCOUNTS, changed_by=sysop)
    with pytest.raises(AttestationError):
        revoke_attestation(db, carol, "age", actor=helper)
    assert get_attestation(db, carol, "age") is not None

    verifier = set_can_verify_identity(db, helper, True, changed_by=sysop)
    assert revoke_attestation(db, carol, "age", actor=verifier) is True


def test_clearing_the_birthdate_never_revokes_the_verified_age(db, sysop, carol):
    set_birthdate(db, carol, date(2001, 1, 1))
    attest_age(db, carol, BORN, verifier=sysop)
    change_birthdate(db, carol, None, changed_by=sysop)
    clear_own_profile_field(db, carol, "birthdate")
    assert get_birthdate(db, carol) is None
    assert get_attestation(db, carol, "age") is not None
    assert meets_age(db, carol, 18, VERIFIED) is True


# -- the SysOp console ------------------------------------------------------------


def _detail(session, lane, actor, target):
    from netbbs.net import admin_flow

    asyncio.run(admin_flow._user_detail_screen(session, lane, actor, target, None))
    return _visible(_written_text(session))


def test_the_account_screen_offers_revoke_only_with_a_verification(db, lane, sysop, carol):
    assert "erification: revoke" not in _detail(AdminSession(["b"]), lane, sysop, carol)
    attest_age(db, carol, BORN, verifier=sysop)
    assert "[V]erification: revoke" in _detail(AdminSession(["b"]), lane, sysop, carol)


def test_the_account_screen_revokes_after_a_yes(db, lane, sysop, carol):
    attest_age(db, carol, BORN, verifier=sysop)
    text = _detail(AdminSession(["v", "y", "b"]), lane, sysop, carol)
    assert get_attestation(db, carol, "age") is None
    assert "Revoked the verified age of 'carol'." in text


def test_the_account_screen_keeps_it_on_a_no(db, lane, sysop, carol):
    attest_age(db, carol, BORN, verifier=sysop)
    _detail(AdminSession(["v", "n", "b"]), lane, sysop, carol)
    assert get_attestation(db, carol, "age") is not None


def test_with_both_on_record_the_sysop_picks_one(db, lane, sysop, carol):
    attest_age(db, carol, BORN, verifier=sysop)
    attest_name(db, carol, "Carol Example", verifier=sysop)
    _detail(AdminSession(["v", "n", "y", "b"]), lane, sysop, carol)
    assert get_attestation(db, carol, "name") is None
    assert get_attestation(db, carol, "age") is not None


def test_the_verify_screen_revokes_too(db, sysop, carol):
    attest_name(db, carol, "Carol Example", verifier=sysop)
    session = ProfileSession(["r", "y", "x", "b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, carol))
    assert get_attestation(db, carol, "name") is None
    assert "Verified real name revoked." in profile_visible(session)


# -- the caller's own Profile -----------------------------------------------------


def test_the_profile_shows_the_verified_value_apart_from_the_callers_own(db, lane, sysop, carol):
    set_birthdate(db, carol, date(2001, 2, 3))
    attest_age(db, carol, BORN, verifier=sysop)
    session = ProfileSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, carol))
    text = squeezed(profile_visible(session))
    assert "Verified by this node: born 1980-01-01" in text
    assert "Birthdate: 2001-02-03" in text


def test_a_caller_clears_their_own_birthdate_and_the_verified_one_stays(db, lane, sysop, carol):
    set_birthdate(db, carol, date(2001, 2, 3))
    attest_age(db, carol, BORN, verifier=sysop)
    session = ProfileSession(["a", "", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, carol))
    assert get_birthdate(db, carol) is None
    assert get_attestation(db, carol, "age") is not None
    assert "Birthdate cleared." in profile_visible(session)


def test_a_caller_clears_their_own_display_name_and_location(db, lane, carol):
    set_display_name(db, carol, "Caro")
    set_location(db, carol, "Leipzig")
    session = ProfileSession(["d", "", "l", "", "b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, carol))
    assert get_display_name(db, carol) is None
    assert get_location(db, carol) is None


def test_a_birthdate_a_sysop_cleared_is_gone_from_the_profile(db, lane, sysop, carol):
    """What the maintainer saw after a SysOp cleared a birthdate was not the
    stored value: once cleared, a freshly opened Profile shows it unset."""
    set_birthdate(db, carol, date(2001, 2, 3))
    change_birthdate(db, carol, None, changed_by=sysop)
    session = ProfileSession(["b"])
    asyncio.run(profile_flow._identity_details_screen(session, lane, carol))
    assert "Birthdate: (not set)" in squeezed(profile_visible(session))


def test_the_verify_screen_says_so_when_someone_else_revoked_first(db, sysop, carol, monkeypatch):
    # Review on #1118: the success line was printed whatever revoking returned.
    attest_name(db, carol, "Carol Example", verifier=sysop)
    real_revoke = profile_flow.revoke_attestation

    def revoke_after_another(db_, subject, attribute, *, actor):
        real_revoke(db_, subject, attribute, actor=actor)  # the other session
        return real_revoke(db_, subject, attribute, actor=actor)

    monkeypatch.setattr(profile_flow, "revoke_attestation", revoke_after_another)
    session = ProfileSession(["r", "y", "x", "b"])
    asyncio.run(profile_flow._verify_user(session, db, sysop, carol))
    text = profile_visible(session)
    assert "Verified real name revoked." not in text
    assert "No verified real name on record for 'carol' any more." in text


def test_the_account_screen_says_so_when_someone_else_revoked_first(db, lane, sysop, carol, monkeypatch):
    # Review on #1118: the account screen said nothing at all in that case.
    from netbbs.net import admin_flow

    attest_age(db, carol, BORN, verifier=sysop)
    real_revoke = admin_flow.revoke_attestation

    def revoke_after_another(db_, subject, attribute, *, actor):
        real_revoke(db_, subject, attribute, actor=actor)  # the other session
        return real_revoke(db_, subject, attribute, actor=actor)

    monkeypatch.setattr(admin_flow, "revoke_attestation", revoke_after_another)
    text = _detail(AdminSession(["v", "y", "b"]), lane, sysop, carol)
    assert "Revoked the verified age" not in text
    assert "No verified age on record for 'carol' any more." in text
