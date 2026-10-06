"""Who can verify identity, and how a caller finds out (issue #1103).

The pre-release re-check couldn't find where a caller enters a birthdate,
or how to verify one. The maintainer decided: a level-255 SysOp verifies
identity without the per-account flag; staff, Co-SysOps included, need it
granted separately. The refusals and the editor help now say where a
birthdate goes and who verifies it."""

from __future__ import annotations

from datetime import date

import pytest

from netbbs.age_requirement import age_verification_refusal, name_verification_refusal
from netbbs.attestation import AttestationError, attest_age, attest_name
from netbbs.auth.users import CO_SYSOP_PRESET, SYSOP_LEVEL, create_user, set_can_verify_identity, set_staff_permissions
from netbbs.net import main_menu
from netbbs.net.admin_flow import _NAME_REQUIREMENT_HELP, _VERIFIED_AGE_HELP, _co_sysop_question
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "nb.db")
    yield database
    database.close()


def _user(db, name, level=0):
    return create_user(db, name, password="correct horse battery", user_level=level)


def test_a_sysop_verifies_without_the_flag(db):
    sysop = _user(db, "InkWell", SYSOP_LEVEL)
    caller = _user(db, "OldNib", 20)
    assert not sysop.can_verify_identity
    attest_age(db, caller, date(1960, 5, 1), verifier=sysop)
    attest_name(db, caller, "Harold Nib", verifier=sysop)
    assert main_menu.offers_verify(sysop)


def test_staff_need_the_flag_granted_separately(db):
    sysop = _user(db, "InkWell", SYSOP_LEVEL)
    helper = set_staff_permissions(db, _user(db, "Copperplate", 20), CO_SYSOP_PRESET, changed_by=sysop)
    caller = _user(db, "OldNib", 20)
    assert not main_menu.offers_verify(helper)
    with pytest.raises(AttestationError):
        attest_age(db, caller, date(1960, 5, 1), verifier=helper)
    helper = set_can_verify_identity(db, helper, True, changed_by=sysop)
    assert main_menu.offers_verify(helper)
    attest_age(db, caller, date(1960, 5, 1), verifier=helper)


def test_the_co_sysop_question_says_verifying_is_separate():
    question = _co_sysop_question("Copperplate")
    assert "Verifying identity is granted separately" in question
    assert "[i]" in question


def test_the_age_refusal_says_where_a_birthdate_goes_and_who_to_ask():
    text = age_verification_refusal("This message board")
    assert "Your profile › Name & details" in text
    assert "Staff list" in text


def test_the_name_refusal_says_who_to_ask():
    text = name_verification_refusal("This channel")
    assert text.startswith("This channel needs a verified real name.")
    assert "Staff list" in text


def test_the_editor_help_says_where_callers_go_and_who_verifies():
    for help_text in (_VERIFIED_AGE_HELP, _NAME_REQUIREMENT_HELP):
        assert "[V]erify" in help_text
        assert "Can verify identity" in help_text
    assert "Name & details" in _VERIFIED_AGE_HELP
