"""
Issue #1115, findings 1 and 2: the Co-SysOp preset asks whether the new
Co-SysOp may also verify identity, instead of pointing at `[i]` on a screen
the SysOp is not looking at; and the account's sub-screens (staff
permissions, password, SSH keys) redraw in place instead of scrolling, with
each outcome carried into the redraw above the prompt.
"""

from __future__ import annotations

import asyncio
import base64

import nacl.signing
import pytest

from netbbs.auth.users import (
    CO_SYSOP_PRESET,
    SYSOP_LEVEL,
    create_user,
    get_user_by_id,
    set_can_verify_identity,
)
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.rendering.ansi import clear_screen
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
    user = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    # The viewer's own preference decides the shared screens (new accounts
    # start with it on).
    set_redraw_in_place_enabled(db, user, True)
    return user


def _staff_screen(session, lane, actor, target, *, redraw_in_place=True):
    from netbbs.net.admin_flow import _staff_permissions_screen

    return asyncio.run(_staff_permissions_screen(
        session, lane, actor, target, None, description_level="brief", redraw_in_place=redraw_in_place,
    ))


def _last_screen(session) -> str:
    """What is on the terminal after the last clear: the screen the caller sees."""
    return _visible(_written_text(session).rsplit(clear_screen(), 1)[-1])


# -- finding 1: the Co-SysOp preset asks about verifying ----------------------------


def test_the_preset_then_yes_grants_verifying_and_audits_it(db, lane, sysop):
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["c", "y", "y", "b"])
    result = _staff_screen(session, lane, sysop, carol)

    stored = get_user_by_id(db, carol.id)
    assert stored.staff_permissions == int(CO_SYSOP_PRESET)
    assert stored.can_verify_identity is True
    assert result.can_verify_identity is True
    assert "Also let 'carol' verify identity?" in _visible(_written_text(session))
    actions = [entry.action for entry in list_actions_for_target_user(db, carol.id)]
    # The same audit row `[i]` on the account writes.
    assert "set_can_verify_identity" in actions


def test_the_preset_then_no_leaves_verifying_off(db, lane, sysop):
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["c", "y", "n", "b"])
    _staff_screen(session, lane, sysop, carol)

    stored = get_user_by_id(db, carol.id)
    assert stored.staff_permissions == int(CO_SYSOP_PRESET)
    assert stored.can_verify_identity is False


def test_no_verify_question_when_they_can_already_verify(db, lane, sysop):
    carol = create_user(db, "carol", password="hunter2pw")
    set_can_verify_identity(db, carol, True, changed_by=sysop)
    carol = get_user_by_id(db, carol.id)
    # One "y" only: a second question would read "b" and never reach Back.
    session = FakeSession(["c", "y", "b"])
    _staff_screen(session, lane, sysop, carol)
    assert "verify identity?" not in _visible(_written_text(session))


def test_a_declined_preset_asks_nothing_more(db, lane, sysop):
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["c", "n", "b"])
    _staff_screen(session, lane, sysop, carol)
    stored = get_user_by_id(db, carol.id)
    assert stored.staff_permissions == 0
    assert stored.can_verify_identity is False
    assert "verify identity?" not in _visible(_written_text(session))


# -- finding 2: the staff screen redraws in place -------------------------------------


def test_the_staff_screen_redraws_from_the_top_with_the_outcome_above_the_prompt(db, lane, sysop):
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["a", "y", "b"])
    _staff_screen(session, lane, sysop, carol)

    text = _written_text(session)
    # Drawn twice (before and after the change), each from the top.
    assert text.count(clear_screen()) == 2
    screen = _last_screen(session)
    assert "Staff permissions" in screen
    assert "'carol' staff permissions: approve accounts." in screen
    # The outcome sits above the prompt, not under a scrolled copy.
    assert screen.index("staff permissions: approve accounts") < screen.rindex("Choice:")


def test_without_redraw_in_place_the_staff_screen_does_not_clear(db, lane, sysop):
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["b"])
    _staff_screen(session, lane, sysop, carol, redraw_in_place=False)
    assert clear_screen() not in _written_text(session)


# -- the password and SSH-key screens, shared with Profile -----------------------------


def test_the_password_screen_redraws_in_place_and_carries_its_outcome(db, lane, sysop):
    from netbbs.net.password_screen import manage_password_screen

    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["c", "new-password-1", "new-password-1", "b"])
    asyncio.run(manage_password_screen(session, lane, carol, changed_by=sysop))

    assert _written_text(session).count(clear_screen()) == 2
    screen = _last_screen(session)
    assert "Password set for 'carol'" in screen
    assert screen.index("Password set for 'carol'") > screen.index("Password on carol's account:")


def test_the_ssh_key_screen_redraws_in_place_and_carries_its_outcome(db, lane, sysop):
    from netbbs.net.ssh_key_screen import manage_ssh_keys_screen

    carol = create_user(db, "carol", password="hunter2pw")
    key = base64.b64encode(nacl.signing.SigningKey.generate().verify_key.encode()).decode()
    session = FakeSession(["a", key, "phone", "b"])
    asyncio.run(manage_ssh_keys_screen(session, lane, carol, changed_by=sysop))

    assert _written_text(session).count(clear_screen()) == 2
    screen = _last_screen(session)
    assert "Key 'phone' added." in screen
    assert "phone" in screen.split("Key 'phone' added.")[0]


def test_the_viewers_own_preference_decides(db, lane, sysop):
    from netbbs.net.password_screen import manage_password_screen

    set_redraw_in_place_enabled(db, sysop, False)
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["b"])
    asyncio.run(manage_password_screen(session, lane, carol, changed_by=sysop))
    assert clear_screen() not in _written_text(session)
