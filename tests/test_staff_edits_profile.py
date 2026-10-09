"""
A SysOp or account manager editing a member's Profile from the user editor
(`[E]dit profile`, design doc §5.6): who is offered it, that each write lands
on the member and in their admin history, and that nothing reaches the
SysOp's own account or session.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from netbbs.attestation import get_birthdate, get_location, set_location
from netbbs.auth.users import (
    SYSOP_LEVEL,
    StaffPermission,
    UserManagementError,
    create_user,
    get_user_by_id,
    set_staff_permissions,
)
from netbbs.directory import get_bio
from netbbs.guest import set_guest_user
from netbbs.mail import list_mail_blocks
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.net.admin_flow import _user_detail_keys
from netbbs.net.animation_preference import animations_enabled
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.profile_flow import _edit_profile, _identity_details_screen
from netbbs.net.unicode_style_preference import charset_preference
from netbbs.profile_admin import EDIT_PROFILE_ACTION, write_as_staff
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_directory_ui import FakeSession, squeezed

# Profile's fields are numbered straight through its four sections.
_DESCRIPTIONS = ["1", "4"]
_ANIMATIONS = ["1", "6"]
_CHARSET = ["1", "7"]
_BIO = ["0", "1"]
_BLOCKED = ["0", "7"]
_SSH_KEYS = ["2", "3"]


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
    return create_user(db, "sysop", password="correct horse", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _manager(db, sysop, name="mia", permissions=StaffPermission.MANAGE_ACCOUNTS):
    staff = create_user(db, name, password="hunter2", user_level=20)
    return set_staff_permissions(db, staff, permissions, changed_by=sysop)


def _profile_edits(db, user):
    return [entry for entry in list_actions_for_target_user(db, user.id) if entry.action == EDIT_PROFILE_ACTION]


# -- who is offered [E]dit profile ---------------------------------------------


def test_a_sysop_is_offered_a_members_profile_but_not_their_own(db, sysop, alice):
    assert "e" in _user_detail_keys(sysop, alice)
    assert "e" not in _user_detail_keys(sysop, sysop)


def test_only_a_sysop_is_offered_the_guest_defaults(db, sysop, alice):
    """The guest account's Profile is what every guest starts with: the
    whole node's, so a SysOp's and not a manager's."""
    assert "e" in _user_detail_keys(sysop, alice, guest=True)
    assert "e" not in _user_detail_keys(_manager(db, sysop), alice, guest=True)


def test_a_manager_is_offered_a_members_profile_but_not_a_sysops_or_staffs(db, sysop, alice):
    manager = _manager(db, sysop)
    other_staff = _manager(db, sysop, name="otto", permissions=StaffPermission.APPROVE_ACCOUNTS)
    assert "e" in _user_detail_keys(manager, alice)
    assert "e" not in _user_detail_keys(manager, sysop)
    assert "e" not in _user_detail_keys(manager, other_staff)
    assert "e" not in _user_detail_keys(manager, manager)


def test_staff_without_manage_accounts_is_not_offered_it(db, sysop, alice):
    approver = _manager(db, sysop, name="otto", permissions=StaffPermission.APPROVE_ACCOUNTS)
    assert "e" not in _user_detail_keys(approver, alice)


# -- the database refuses what the screen would not offer -----------------------


def _write_nothing(db, target):
    return None, "nothing"


@pytest.mark.parametrize("target_name", ["sysop", "otto", "mia"])
def test_write_as_staff_refuses_a_sysop_staff_or_yourself(db, sysop, alice, target_name):
    manager = _manager(db, sysop)
    _manager(db, sysop, name="otto", permissions=StaffPermission.APPROVE_ACCOUNTS)
    target = next(u for u in (get_user_by_id(db, i) for i in range(1, 10)) if u and u.username == target_name)
    with pytest.raises(UserManagementError):
        write_as_staff(db, manager, target, _write_nothing)


def test_a_change_whose_record_fails_is_not_kept(db, sysop, alice, monkeypatch):
    """The change and its admin-history row are one transaction: a record
    that cannot be saved takes the change with it."""
    from netbbs import profile_admin
    from netbbs.directory import set_bio

    def fail(*args, **kwargs):
        raise RuntimeError("database is full")

    monkeypatch.setattr(profile_admin, "record_action_without_commit", fail)

    def write(db, target):
        set_bio(db, target, "Written by staff")
        return None, "Bio changed"

    with pytest.raises(RuntimeError):
        write_as_staff(db, sysop, alice, write)
    assert get_bio(db, alice) is None
    assert _profile_edits(db, alice) == []


def test_write_as_staff_refuses_plain_staff(db, sysop, alice):
    approver = _manager(db, sysop, name="otto", permissions=StaffPermission.APPROVE_ACCOUNTS)
    with pytest.raises(UserManagementError):
        write_as_staff(db, approver, alice, _write_nothing)
    assert _profile_edits(db, alice) == []


def test_only_a_sysop_writes_the_guest_defaults(db, sysop, alice):
    manager = _manager(db, sysop)
    set_guest_user(db, alice)
    with pytest.raises(UserManagementError, match="only a SysOp"):
        write_as_staff(db, manager, alice, _write_nothing)
    write_as_staff(db, sysop, alice, _write_nothing)
    assert [entry.actor_user_id for entry in _profile_edits(db, alice)] == [sysop.id]


def test_a_sysop_editing_their_own_account_here_is_refused(db, sysop):
    with pytest.raises(UserManagementError):
        write_as_staff(db, sysop, sysop, _write_nothing)


# -- what a change does ---------------------------------------------------------


def test_a_sysop_toggling_a_members_setting_changes_theirs_and_records_it(db, lane, sysop, alice):
    before = menu_description_level(db, sysop)
    session = FakeSession(keys=[*_DESCRIPTIONS, "b"])
    asyncio.run(_edit_profile(session, lane, alice, actor=sysop))
    changed = menu_description_level(db, alice)
    assert changed != before
    # The SysOp's own setting is left as it was.
    assert menu_description_level(db, sysop) == before
    [entry] = _profile_edits(db, alice)
    assert entry.actor_user_id == sysop.id
    assert entry.detail == f"Menu descriptions: {changed}"


def test_a_members_character_set_never_reaches_the_sysops_session(db, lane, sysop, alice):
    session = FakeSession(keys=[*_CHARSET, *_ANIMATIONS, "b"])
    session.output_charset = "utf-8"
    session.animations_enabled = True
    asyncio.run(_edit_profile(session, lane, alice, actor=sysop))
    assert charset_preference(db, alice) != "auto"
    assert charset_preference(db, sysop) == "auto"
    assert animations_enabled(db, alice) is False
    assert session.output_charset == "utf-8"
    assert session.animations_enabled is True
    assert len(_profile_edits(db, alice)) == 2


# -- the guest defaults -----------------------------------------------------------

# On the guest defaults, Name & details and read receipts are left out, so
# Menu descriptions is the twelfth field.
_GUEST_DESCRIPTIONS = ["1", "2"]


def test_the_guest_defaults_leave_out_what_a_shared_account_has_no_use_for(db, lane, sysop, alice):
    set_guest_user(db, alice)
    # Every section in turn: the fields are paged by section on 80x24.
    session = FakeSession(keys=[">", ">", "b"])
    asyncio.run(_edit_profile(session, lane, alice, actor=sysop))
    screen = squeezed(session.visible_output)
    assert "Guest defaults" in screen and "What every guest starts with" in screen
    for gone in ("Name & details", "Let senders see when I've read their mail", "Sort preferences",
                 "SSH public key", "Password"):
        assert gone not in screen, gone
    for kept in ("Bio", "Signature", "Direct messages", "Blocked people", "Character set", "Menu descriptions"):
        assert kept in screen, kept


def test_a_guest_starts_with_the_default_and_keeps_their_own_change_to_the_call(db, lane, sysop, alice):
    from netbbs.net.menu_description_preference import set_menu_description_level
    from netbbs.user_preferences import session_scoped_preferences

    set_guest_user(db, alice)
    session = FakeSession(keys=[*_GUEST_DESCRIPTIONS, "b"])
    asyncio.run(_edit_profile(session, lane, alice, actor=sysop))
    default = menu_description_level(db, alice)
    [entry] = _profile_edits(db, alice)
    assert entry.detail == f"Menu descriptions: {default}"

    with session_scoped_preferences(alice):
        # A guest's call starts from the stored default...
        assert menu_description_level(db, alice) == default
        mine = "off" if default != "off" else "detailed"
        set_menu_description_level(db, alice, mine)
        assert menu_description_level(db, alice) == mine
    # ...and their own change never becomes the next guest's.
    assert menu_description_level(db, alice) == default


def test_your_own_character_set_still_applies_to_your_session(db, lane, alice):
    session = FakeSession(keys=[*_ANIMATIONS, "b"])
    session.animations_enabled = True
    asyncio.run(_edit_profile(session, lane, alice))
    assert session.animations_enabled is False
    assert _profile_edits(db, alice) == []


def test_a_bio_edit_is_recorded_without_its_text(db, lane, sysop, alice):
    # The text, then the blank line that ends it.
    session = FakeSession(keys=[*_BIO, "b"], lines=["Retro synths and modems.", ""])
    asyncio.run(_edit_profile(session, lane, alice, actor=sysop))
    assert get_bio(db, alice) == "Retro synths and modems."
    [entry] = _profile_edits(db, alice)
    assert entry.detail == "Bio changed"
    assert "synths" not in entry.detail


def test_a_block_is_recorded_without_the_name(db, lane, sysop, alice):
    create_user(db, "mallory", password="hunter2", user_level=10)
    session = FakeSession(keys=[*_BLOCKED, "a", "b", "b"], lines=["mallory"])
    asyncio.run(_edit_profile(session, lane, alice, actor=sysop))
    assert len(list_mail_blocks(db, alice)) == 1
    assert list_mail_blocks(db, sysop) == []
    [entry] = _profile_edits(db, alice)
    assert entry.detail == "Blocked someone"


def test_a_manager_cannot_reach_ssh_keys_from_a_profile(db, lane, sysop, alice):
    manager = _manager(db, sysop)
    session = FakeSession(keys=[*_SSH_KEYS, "b"])
    asyncio.run(_edit_profile(session, lane, alice, actor=manager))
    assert "only a SysOp can change an account's SSH keys" in session.visible_output


def test_a_permission_taken_away_mid_screen_refuses_the_next_change(db, lane, sysop, alice):
    manager = _manager(db, sysop)
    before = menu_description_level(db, alice)
    session = FakeSession(keys=[*_DESCRIPTIONS, "b"])
    # Taken away after the screen opened, before the key is pressed.
    set_staff_permissions(db, manager, 0, changed_by=sysop)
    asyncio.run(_edit_profile(session, lane, alice, actor=manager))
    assert menu_description_level(db, alice) == before
    assert "Not changed: only a SysOp can do that." in session.visible_output
    # The redraw shows the value the account still has.
    assert f"Menu descriptions: {before}" in squeezed(session.visible_output)


def test_the_screen_says_whose_profile_it_is(db, lane, sysop, alice):
    session = FakeSession(keys=["b"])
    asyncio.run(_edit_profile(session, lane, alice, actor=sysop))
    text = session.visible_output
    assert "alice's profile. Every change is logged." in text
    assert "Transport report" not in text


# -- Name & details ----------------------------------------------------------------


def test_name_and_details_uses_the_staff_path_and_keeps_location_private(db, lane, sysop, alice):
    set_location(db, alice, "Lyon")
    # 01 display name, 03 location, 05 birthdate.
    session = FakeSession(
        keys=["0", "3", "0", "5", "b"],
        lines=["Lille", "1990-04-01"],
    )
    asyncio.run(_identity_details_screen(session, lane, alice, actor=sysop))
    assert get_location(db, alice) == "Lille"
    assert get_birthdate(db, alice) == date(1990, 4, 1)
    details = [entry.detail for entry in list_actions_for_target_user(db, alice.id)]
    assert "Location changed" in details
    assert all("Lille" not in (detail or "") for detail in details)
    # The birthdate goes through `change_birthdate`, which never logs the date.
    assert all("1990" not in (detail or "") for detail in details)


def test_link_sharing_stays_the_members_choice(db, lane, sysop, alice):
    # 08 is "Share verified age over Link".
    session = FakeSession(keys=["0", "8", "b"])
    asyncio.run(_identity_details_screen(session, lane, alice, actor=sysop))
    assert "only alice can choose to share a verification over Link" in session.visible_output
