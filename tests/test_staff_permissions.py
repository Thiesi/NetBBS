"""
Staff permissions (design doc §5.6, issue #836): what a SysOp can hand to an
account below 255, and the guards that keep a staff member away from the
SysOp, other staff, and their own account.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import (
    CO_SYSOP_PRESET,
    SYSOP_LEVEL,
    StaffPermission,
    User,
    UserManagementError,
    approve_pending_user,
    create_user,
    decline_pending_user,
    delete_user,
    describe_staff_permissions,
    get_user_by_id,
    get_user_by_username,
    set_can_verify_identity,
    set_password,
    set_staff_permissions,
    set_user_disabled,
    set_user_level,
)
from netbbs.boards import create_board
from netbbs.chat.channels import create_channel
from netbbs.files import create_file_area
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.moderation.roles import (
    BoardPermission,
    ChannelPermission,
    describe_grant,
    grant_permissions,
    has_permission,
    list_grants_for_user,
)
from netbbs.net.login_flow import _apply_access_change
from netbbs.net.main_menu import _access_change_notice
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text

APPROVE = StaffPermission.APPROVE_ACCOUNTS
MANAGE = StaffPermission.MANAGE_ACCOUNTS
MODERATE = StaffPermission.MODERATE_ALL


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _staff(db, sysop, name, permissions, level=10):
    user = create_user(db, name, password="hunter2", user_level=level)
    return set_staff_permissions(db, user, permissions, changed_by=sysop)


# -- granting -----------------------------------------------------------------


def test_a_new_account_holds_no_staff_permissions(db):
    user = create_user(db, "carol", password="hunter2")
    assert user.staff_permissions == 0
    assert describe_staff_permissions(user.staff_permissions) == "none"


def test_a_sysop_applies_the_co_sysop_preset_and_it_is_audited(db, sysop):
    carol = create_user(db, "carol", password="hunter2", user_level=20)
    carol = set_staff_permissions(db, carol, CO_SYSOP_PRESET, changed_by=sysop)
    assert carol.has_staff(APPROVE) and carol.has_staff(MANAGE) and carol.has_staff(MODERATE)
    # Its level is untouched: staff is independent of level.
    assert carol.user_level == 20
    entry = list_actions_for_target_user(db, carol.id)[-1]
    assert entry.action == "set_staff_permissions"
    assert entry.detail == "none -> approve accounts, manage accounts, moderate everything"


def test_staff_permissions_can_be_removed_one_at_a_time(db, sysop):
    carol = _staff(db, sysop, "carol", CO_SYSOP_PRESET)
    carol = set_staff_permissions(db, carol, CO_SYSOP_PRESET & ~MANAGE, changed_by=sysop)
    assert describe_staff_permissions(carol.staff_permissions) == "approve accounts, moderate everything"


@pytest.mark.parametrize("actor_permissions", [0, CO_SYSOP_PRESET])
def test_only_a_sysop_grants_staff_permissions(db, sysop, actor_permissions):
    actor = create_user(db, "helper", password="hunter2", user_level=200)
    if actor_permissions:
        actor = set_staff_permissions(db, actor, actor_permissions, changed_by=sysop)
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(UserManagementError):
        set_staff_permissions(db, carol, APPROVE, changed_by=actor)
    assert get_user_by_id(db, carol.id).staff_permissions == 0


def test_a_staff_member_cannot_widen_their_own_permissions(db, sysop):
    helper = _staff(db, sysop, "helper", APPROVE)
    with pytest.raises(UserManagementError):
        set_staff_permissions(db, helper, CO_SYSOP_PRESET, changed_by=helper)


def test_staff_permissions_are_for_accounts_below_255(db, sysop):
    boss = create_user(db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    with pytest.raises(UserManagementError, match="SysOp already"):
        set_staff_permissions(db, boss, APPROVE, changed_by=sysop)


def test_a_pending_account_is_approved_before_it_becomes_staff(db, sysop):
    carol = create_user(db, "carol", password="hunter2pw", pending_approval=True)
    with pytest.raises(UserManagementError, match="approve"):
        set_staff_permissions(db, carol, APPROVE, changed_by=sysop)


def test_unknown_staff_bits_are_refused(db, sysop):
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(ValueError):
        set_staff_permissions(db, carol, 1 << 10, changed_by=sysop)


def test_only_a_sysop_grants_the_verify_identity_permission(db, sysop):
    helper = _staff(db, sysop, "helper", CO_SYSOP_PRESET)
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(UserManagementError):
        set_can_verify_identity(db, carol, True, changed_by=helper)


# -- manage accounts ---------------------------------------------------------


def test_a_manager_sets_a_members_level_up_to_254(db, sysop):
    helper = _staff(db, sysop, "helper", MANAGE)
    carol = create_user(db, "carol", password="hunter2")
    carol = set_user_level(db, carol, 254, changed_by=helper)
    assert carol.user_level == 254
    assert list_actions_for_target_user(db, carol.id)[-1].actor_user_id == helper.id


def test_a_manager_never_raises_anyone_to_255(db, sysop):
    helper = _staff(db, sysop, "helper", MANAGE)
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(UserManagementError):
        set_user_level(db, carol, SYSOP_LEVEL, changed_by=helper)
    assert get_user_by_id(db, carol.id).user_level != SYSOP_LEVEL


@pytest.mark.parametrize("action", ["demote", "disable", "password"])
def test_a_manager_never_reaches_the_sysop(db, sysop, action):
    helper = _staff(db, sysop, "helper", CO_SYSOP_PRESET)
    boss = create_user(db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    with pytest.raises(UserManagementError):
        if action == "demote":
            set_user_level(db, boss, 10, changed_by=helper)
        elif action == "disable":
            set_user_disabled(db, boss, True, changed_by=helper)
        else:
            set_password(db, boss, "taken-over", changed_by=helper)
    fresh = get_user_by_id(db, boss.id)
    assert fresh.user_level == SYSOP_LEVEL and fresh.disabled_at is None


@pytest.mark.parametrize("action", ["level", "disable", "password"])
def test_a_manager_never_reaches_another_staff_member(db, sysop, action):
    helper = _staff(db, sysop, "helper", CO_SYSOP_PRESET)
    other = _staff(db, sysop, "other", APPROVE)
    with pytest.raises(UserManagementError, match="staff"):
        if action == "level":
            set_user_level(db, other, 5, changed_by=helper)
        elif action == "disable":
            set_user_disabled(db, other, True, changed_by=helper)
        else:
            set_password(db, other, "taken-over", changed_by=helper)


@pytest.mark.parametrize("action", ["level", "disable"])
def test_a_manager_never_acts_on_their_own_account(db, sysop, action):
    helper = _staff(db, sysop, "helper", MANAGE, level=10)
    with pytest.raises(UserManagementError):
        if action == "level":
            set_user_level(db, helper, 254, changed_by=helper)
        else:
            set_user_disabled(db, helper, True, changed_by=helper)
    assert get_user_by_id(db, helper.id).user_level == 10


def test_a_staff_member_still_changes_their_own_password(db, sysop):
    # The self-service change, which proved the current password first.
    helper = _staff(db, sysop, "helper", APPROVE)
    set_password(db, helper, "new-secret", changed_by=helper)


def test_a_manager_disables_enables_and_resets_a_member(db, sysop):
    helper = _staff(db, sysop, "helper", MANAGE)
    carol = create_user(db, "carol", password="hunter2")
    carol = set_user_disabled(db, carol, True, changed_by=helper)
    assert carol.disabled_at is not None
    carol = set_user_disabled(db, carol, False, changed_by=helper)
    assert carol.disabled_at is None
    set_password(db, carol, "fresh-start", changed_by=helper)


def test_a_moderator_only_account_is_within_reach(db, sysop):
    helper = _staff(db, sysop, "helper", MANAGE)
    carol = create_user(db, "carol", password="hunter2")
    board = create_board(db, "News", creator=sysop)
    grant_permissions(
        db, carol, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=sysop
    )
    assert set_user_disabled(db, carol, True, changed_by=helper).disabled_at is not None


def test_approve_accounts_alone_does_not_manage_accounts(db, sysop):
    helper = _staff(db, sysop, "helper", APPROVE)
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(UserManagementError):
        set_user_disabled(db, carol, True, changed_by=helper)


def test_a_permission_revoked_meanwhile_stops_the_next_action(db, sysop):
    helper = _staff(db, sysop, "helper", MANAGE)
    stale_helper = helper
    set_staff_permissions(db, helper, 0, changed_by=sysop)
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(UserManagementError):
        set_user_disabled(db, carol, True, changed_by=stale_helper)


def test_a_disabled_staff_member_can_do_nothing(db, sysop):
    helper = _staff(db, sysop, "helper", MANAGE)
    set_user_disabled(db, helper, True, changed_by=sysop)
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(UserManagementError):
        set_user_level(db, carol, 20, changed_by=helper)


def test_a_plain_account_can_change_no_other_account(db):
    alice = create_user(db, "alice", password="hunter2", user_level=200)
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(UserManagementError, match="only a SysOp"):
        set_user_level(db, carol, 20, changed_by=alice)


def test_staff_never_delete_accounts(db, sysop):
    helper = _staff(db, sysop, "helper", CO_SYSOP_PRESET)
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(UserManagementError):
        delete_user(db, carol, deleted_by=helper)
    assert get_user_by_id(db, carol.id) is not None


# -- approve accounts ---------------------------------------------------------


def test_an_approver_approves_and_declines_signups(db, sysop):
    helper = _staff(db, sysop, "helper", APPROVE)
    carol = create_user(db, "carol", password="hunter2pw", pending_approval=True)
    dave = create_user(db, "dave", password="hunter2pw", pending_approval=True)
    assert approve_pending_user(db, carol, approved_by=helper).pending_approval is False
    decline_pending_user(db, dave, declined_by=helper)
    assert get_user_by_id(db, dave.id) is None


def test_manage_accounts_alone_does_not_approve(db, sysop):
    helper = _staff(db, sysop, "helper", MANAGE)
    carol = create_user(db, "carol", password="hunter2pw", pending_approval=True)
    with pytest.raises(UserManagementError):
        approve_pending_user(db, carol, approved_by=helper)
    with pytest.raises(UserManagementError):
        decline_pending_user(db, carol, declined_by=helper)
    assert get_user_by_id(db, carol.id).pending_approval is True


# -- moderate everything ------------------------------------------------------


def test_moderate_everything_is_every_moderator_permission_everywhere(db, sysop):
    helper = _staff(db, sysop, "helper", MODERATE)
    plain = create_user(db, "plain", password="hunter2")
    board = create_board(db, "News", creator=sysop)
    channel = create_channel(db, "lobby", creator=sysop)
    area = create_file_area(db, "Uploads", creator=sysop)
    for user, expected in ((helper, True), (plain, False)):
        assert has_permission(
            db, user, object_type="board", object_id=board.id, permission=BoardPermission.APPROVE
        ) is expected
        assert has_permission(
            db, user, object_type="file_area", object_id=area.id, permission=BoardPermission.DELETE
        ) is expected
        assert has_permission(
            db, user, object_type="channel", object_id=channel.id, permission=ChannelPermission.MODERATE
        ) is expected


def test_grant_summaries_name_the_scope_and_the_bits(db, sysop):
    carol = create_user(db, "carol", password="hunter2")
    board = create_board(db, "News", creator=sysop)
    grant_permissions(
        db, carol, object_type="board", object_id=board.id,
        permissions=BoardPermission.APPROVE | BoardPermission.DELETE, granted_by=sysop,
    )
    grant_permissions(
        db, carol, object_type="file_area", object_id=None, permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    summaries = sorted(describe_grant(db, grant) for grant in list_grants_for_user(db, carol))
    assert summaries == ['board "News": delete, approve', "every file area: approve"]


# -- live sessions ------------------------------------------------------------


def test_losing_a_staff_permission_unwinds_the_session_and_gaining_one_does_not():
    base = User(id=1, username="helper", user_level=10, fingerprint=None, created_at="x", last_login_at=None)
    approver = User(**{**base.__dict__, "staff_permissions": int(APPROVE)})
    both = User(**{**base.__dict__, "staff_permissions": int(APPROVE | MANAGE)})

    async def scenario():
        registry = ActiveSessionRegistry()
        session = object()
        registry.enter(session)
        registry.record_account(session, user_level=10, can_verify_identity=False, staff_permissions=int(APPROVE))
        unwinds: list[object] = []
        registry.request_level_unwind = lambda s: unwinds.append(s) or True
        _apply_access_change(session, both, registry)
        assert registry.account_baseline(session) == (10, False, int(APPROVE | MANAGE))
        assert unwinds == []
        _apply_access_change(session, approver, registry)
        assert unwinds == [session]
        assert registry.account_baseline(session) == (10, False, int(APPROVE))

    asyncio.run(scenario())


def test_the_menu_says_which_staff_permissions_changed(db):
    base = User(id=1, username="helper", user_level=10, fingerprint=None, created_at="x", last_login_at=None)
    after = User(**{**base.__dict__, "staff_permissions": int(APPROVE)})
    assert "Staff permissions granted: approve accounts." in _visible(_access_change_notice(base, after))
    assert "Staff permissions removed: approve accounts." in _visible(_access_change_notice(after, base))


# -- the account detail -------------------------------------------------------


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def _run(session, lane, user):
    from netbbs.net.admin_flow import admin_menu

    asyncio.run(admin_menu(session, lane, user))


def test_the_account_detail_shows_staff_and_moderator_grants(db, lane, sysop):
    carol = create_user(db, "carol", password="hunter2pw")
    board = create_board(db, "News", creator=sysop)
    grant_permissions(
        db, carol, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=sysop
    )
    session = FakeSession(["u", "u", "0", "1", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    assert "Staff" in text and "none" in text
    assert "Moderator grants" in text
    assert 'board "News": approve' in text


def test_the_co_sysop_preset_is_one_confirmed_step(db, lane, sysop):
    create_user(db, "carol", password="hunter2pw")
    # carol sorts before sysop -- item 01.
    # "n": the follow-up verify question (issue #1115) is declined.
    session = FakeSession(["u", "u", "0", "1", "s", "c", "y", "n", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    carol = get_user_by_username(db, "carol")
    assert carol.staff_permissions == int(CO_SYSOP_PRESET)
    assert carol.can_verify_identity is False
    assert "approve accounts, manage accounts, moderate everything" in _visible(_written_text(session))


def test_a_declined_confirmation_changes_nothing(db, lane, sysop):
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["u", "u", "0", "1", "s", "a", "n", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    assert get_user_by_id(db, carol.id).staff_permissions == 0


# -- a refusal on screen is a message, never a crash (review on #863) ------------


def test_an_approval_refused_meanwhile_is_reported_not_raised(db, lane, sysop):
    from netbbs.net.admin_flow import _user_detail_screen

    helper = _staff(db, sysop, "helper", APPROVE)
    stale_helper = helper
    set_staff_permissions(db, helper, 0, changed_by=sysop)
    carol = create_user(db, "carol", password="hunter2pw", pending_approval=True)
    session = FakeSession(["a", "y", "b"])
    asyncio.run(_user_detail_screen(session, lane, stale_helper, carol, None))
    assert "only a SysOp can do that" in _visible(_written_text(session))
    assert get_user_by_id(db, carol.id).pending_approval is True


def test_an_identity_grant_by_a_demoted_sysop_is_reported_not_raised(db, lane, sysop):
    from netbbs.net.admin_flow import _user_detail_screen

    boss = create_user(db, "boss", password="hunter2", user_level=SYSOP_LEVEL)
    stale_boss = boss
    set_user_level(db, boss, 10, changed_by=sysop)
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["i", "y", "b"])
    asyncio.run(_user_detail_screen(session, lane, stale_boss, carol, None))
    assert "only a SysOp can do that" in _visible(_written_text(session))
    assert get_user_by_id(db, carol.id).can_verify_identity is False


def test_a_password_reset_refused_meanwhile_is_reported_not_raised(db, lane, sysop):
    from netbbs.net.password_screen import manage_password_screen

    helper = _staff(db, sysop, "helper", MANAGE)
    stale_helper = helper
    set_staff_permissions(db, helper, 0, changed_by=sysop)
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["c", "new-secret", "new-secret", "b"])
    asyncio.run(manage_password_screen(session, lane, carol, changed_by=stale_helper))
    assert "only a SysOp can do that" in _visible(_written_text(session))
