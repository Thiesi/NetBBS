"""
The Staff console, `[A]pprovals (n)`, the pending-accounts notice and the
grant-everywhere action (design doc §5.2, §5.6; issues #836, #835 F071).
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import (
    CO_SYSOP_PRESET,
    SYSOP_LEVEL,
    StaffPermission,
    create_user,
    get_user_by_id,
    get_user_by_username,
    set_staff_permissions,
)
from netbbs.boards import create_board
from netbbs.boards.posts import create_post, list_node_pending_posts
from netbbs.chat.mailbox import MessageMailbox
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.moderation.roles import (
    BoardPermission,
    ChannelPermission,
    grant_everywhere,
    grant_permissions,
    list_grants_for_user,
)
from netbbs.net.admin_flow import _grant_moderator_screen, moderation_queue, staff_menu
from netbbs.net.main_menu import _draw_main_menu
from netbbs.net.notices import pending_notices
from netbbs.permissions import InsufficientLevelError
from netbbs.staff import count_moderation_items, has_moderation_scope, told_of_pending_accounts
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
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _staff(db, sysop, name, permissions, level=10):
    user = create_user(db, name, password="hunter2", user_level=level)
    return set_staff_permissions(db, user, permissions, changed_by=sysop)


def _menu(db, user) -> str:
    session = FakeSession()
    asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))
    return _visible(_written_text(session))


# -- the main menu -------------------------------------------------------------


def test_a_staff_member_gets_the_staff_console_not_the_sysops(db, sysop):
    helper = _staff(db, sysop, "helper", APPROVE)
    text = _menu(db, helper)
    assert "[S]taff" in text
    assert "[S]ysOp" not in text


def test_a_plain_caller_gets_neither_console(db):
    carol = create_user(db, "carol", password="hunter2")
    text = _menu(db, carol)
    assert "[S]taff" not in text and "[S]ysOp" not in text
    assert "[A]pprovals" not in text


def test_a_moderator_is_told_what_waits_for_them(db, sysop):
    mod = create_user(db, "mod", password="hunter2")
    author = create_user(db, "author", password="hunter2")
    board = create_board(db, "Trading Post", creator=sysop, moderated=True)
    other = create_board(db, "Elsewhere", creator=sysop, moderated=True)
    grant_permissions(
        db, mod, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=sysop
    )
    create_post(db, board, author, "For sale", "a pen")
    create_post(db, other, author, "Not theirs", "text")
    assert count_moderation_items(db, mod) == 1
    assert "[A]pprovals (1)" in _menu(db, mod)


def test_an_edit_only_grant_does_not_make_a_moderation_queue(db, sysop):
    mod = create_user(db, "mod", password="hunter2")
    board = create_board(db, "News", creator=sysop)
    grant_permissions(
        db, mod, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=sysop
    )
    assert has_moderation_scope(db, mod) is False
    assert "[A]pprovals" not in _menu(db, mod)


def test_moderate_everything_counts_every_held_post(db, sysop):
    helper = _staff(db, sysop, "helper", MODERATE)
    author = create_user(db, "author", password="hunter2")
    for name in ("One", "Two"):
        board = create_board(db, name, creator=sysop, moderated=True)
        create_post(db, board, author, f"Held in {name}", "text")
    assert "[A]pprovals (2)" in _menu(db, helper)
    assert len(list_node_pending_posts(db, requesting_user=helper, limit=10)) == 2


def test_the_node_wide_queue_stays_closed_to_a_plain_account(db):
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(InsufficientLevelError):
        list_node_pending_posts(db, requesting_user=carol, limit=10)


@pytest.mark.parametrize(
    ("permissions", "told"),
    [(APPROVE, True), (MANAGE, False), (MODERATE, False), (0, False)],
)
def test_pending_accounts_are_announced_to_approvers_only(db, sysop, permissions, told):
    create_user(db, "newbie", password="hunter2pw", pending_approval=True)
    create_user(db, "another", password="hunter2pw", pending_approval=True)
    user = _staff(db, sysop, "helper", permissions) if permissions else create_user(db, "helper", password="x1")
    assert told_of_pending_accounts(user) is told
    text = _menu(db, user)
    assert ("2 accounts awaiting approval" in text) is told
    if told:
        assert "Staff -> Accounts waiting" in text or "Staff → Accounts waiting" in text


def test_the_sysop_is_told_where_to_approve(db, sysop):
    create_user(db, "newbie", password="hunter2pw", pending_approval=True)
    text = _menu(db, sysop)
    assert "1 account awaiting approval: SysOp" in text


def test_nobody_is_told_when_nothing_waits(db, sysop):
    assert "awaiting approval" not in _menu(db, sysop)


# -- the Staff console ---------------------------------------------------------


def _staff_console(session, lane, user):
    asyncio.run(staff_menu(session, lane, user))


def test_the_console_offers_only_what_the_permissions_reach(db, lane, sysop):
    helper = _staff(db, sysop, "helper", APPROVE)
    session = FakeSession(["b"])
    _staff_console(session, lane, helper)
    text = _visible(_written_text(session))
    assert "Staff console" in text
    assert "[A]ccounts waiting" in text
    assert "[U]sers" not in text and "[M]oderation" not in text
    for sysop_only in ("Settings", "Operations", "Link status", "DNS", "Backup"):
        assert sysop_only not in text


def test_an_approver_approves_a_signup_from_the_console(db, lane, sysop):
    helper = _staff(db, sysop, "helper", APPROVE)
    carol = create_user(db, "carol", password="hunter2pw", pending_approval=True)
    # a: waiting accounts; 01: carol; a: approve; y: confirm; b: detail; b: console.
    session = FakeSession(["a", "0", "1", "a", "y", "b", "b"])
    _staff_console(session, lane, helper)
    assert get_user_by_id(db, carol.id).pending_approval is False
    assert list_actions_for_target_user(db, carol.id)[-1].actor_user_id == helper.id


def test_the_waiting_list_holds_only_pending_accounts(db, lane, sysop):
    helper = _staff(db, sysop, "helper", APPROVE)
    create_user(db, "carol", password="hunter2pw", pending_approval=True)
    session = FakeSession(["a", "b", "b"])
    _staff_console(session, lane, helper)
    text = _visible(_written_text(session))
    listing = text[text.index("Waiting for approval"):]
    assert "carol" in listing
    assert "sysop" not in listing.split("Choice")[0]


def test_a_manager_sets_a_level_from_the_console(db, lane, sysop):
    helper = _staff(db, sysop, "helper", MANAGE)
    carol = create_user(db, "carol", password="hunter2pw")
    # Sorted: carol, helper, sysop -- carol is 01.
    session = FakeSession(["u", "0", "1", "l", "20", "b", "b", "b"])
    _staff_console(session, lane, helper)
    assert get_user_by_id(db, carol.id).user_level == 20


def test_a_manager_cannot_raise_to_255_from_the_console(db, lane, sysop):
    helper = _staff(db, sysop, "helper", MANAGE)
    carol = create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["u", "0", "1", "l", "255", "b", "b", "b"])
    _staff_console(session, lane, helper)
    assert get_user_by_id(db, carol.id).user_level != SYSOP_LEVEL
    assert "only a SysOp can do that" in _visible(_written_text(session))


def test_the_sysops_account_is_view_only_for_staff(db, lane, sysop):
    helper = _staff(db, sysop, "helper", CO_SYSOP_PRESET)
    # Sorted: helper, sysop -- sysop is 02. "l" is refused with a bell.
    session = FakeSession(["u", "0", "2", "l", "t", "d", "b", "b", "b"])
    _staff_console(session, lane, helper)
    detail = _visible(_written_text(session)).split("RECORD", 1)[-1].split("Choice:", 1)[0]
    assert "[L]evel" not in detail and "[D]elete" not in detail and "oggle enable" not in detail
    assert "Backup:" not in detail
    fresh = get_user_by_id(db, sysop.id)
    assert fresh.user_level == SYSOP_LEVEL and fresh.disabled_at is None


def test_staff_never_see_keys_blocklist_staff_or_delete(db, lane, sysop):
    helper = _staff(db, sysop, "helper", CO_SYSOP_PRESET)
    create_user(db, "carol", password="hunter2pw")
    session = FakeSession(["u", "0", "1", "b", "b", "b"])
    _staff_console(session, lane, helper)
    detail = _visible(_written_text(session)).split("RECORD", 1)[-1].split("Choice:", 1)[0]
    assert "[L]evel" in detail and "[P]assword" in detail
    for hidden in ("[K]ey", "estrict login", "[S]taff", "dentity verification", "[D]elete"):
        assert hidden not in detail


def test_the_console_closes_once_the_permissions_are_gone(db, lane, sysop):
    helper = _staff(db, sysop, "helper", APPROVE)
    set_staff_permissions(db, helper, 0, changed_by=sysop)
    session = FakeSession(["a"])
    _staff_console(session, lane, helper)
    assert "no longer has staff access" in _visible(_written_text(session))


# -- Approvals (n) ---------------------------------------------------------------


def test_a_moderators_queue_holds_only_what_their_grants_cover(db, lane, sysop):
    mod = create_user(db, "mod", password="hunter2")
    author = create_user(db, "author", password="hunter2")
    board = create_board(db, "Trading Post", creator=sysop, moderated=True)
    other = create_board(db, "Elsewhere", creator=sysop, moderated=True)
    grant_permissions(
        db, mod, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=sysop
    )
    create_post(db, board, author, "For sale", "a pen")
    create_post(db, other, author, "Not theirs", "text")
    session = FakeSession(["b"])
    asyncio.run(moderation_queue(session, lane, mod))
    text = _visible(_written_text(session))
    assert "For sale" in text
    assert "Not theirs" not in text


# -- grant everywhere ------------------------------------------------------------


def test_grant_everywhere_writes_three_blanket_grants(db, sysop):
    carol = create_user(db, "carol", password="hunter2")
    grants = grant_everywhere(
        db, carol, board_permissions=BoardPermission.APPROVE,
        channel_permissions=ChannelPermission.MODERATE, granted_by=sysop,
    )
    assert sorted(grant.object_type for grant in grants) == ["board", "channel", "file_area"]
    assert all(grant.object_id is None and grant.community_id is None for grant in grants)
    assert [entry.action for entry in list_actions_for_target_user(db, carol.id)] == ["grant"] * 3


def test_the_grant_screen_offers_everything_at_once(db, lane, sysop):
    create_user(db, "carol", password="hunter2")
    # The editor directly: user carol (01), scope everything, save.
    session = FakeSession(["u", "0", "1", "o", "e", "s"])
    asyncio.run(_grant_moderator_screen(session, lane, sysop))
    carol = get_user_by_username(db, "carol")
    assert sorted(g.object_type for g in list_grants_for_user(db, carol)) == ["board", "channel", "file_area"]
    assert "Members see moderators on the Staff list." in _visible("".join(pending_notices(session)))


@pytest.mark.parametrize(("preset_steps", "moderates"), [(0, True), (2, False), (3, False)])
def test_only_a_moderating_grant_mentions_the_staff_list(db, lane, sysop, monkeypatch, preset_steps, moderates):
    board = create_board(db, "Announcements", creator=sysop)
    create_user(db, "carol", password="hunter2")

    async def _one_board(*_args, **_kwargs):
        return "board", board.id, "board 'Announcements'"

    monkeypatch.setattr("netbbs.net.admin_flow._pick_moderator_scope", _one_board)
    # User carol (01), the one board, the preset cycled from full, save.
    session = FakeSession(["u", "0", "1", "o", *["p"] * preset_steps, "s"])
    asyncio.run(_grant_moderator_screen(session, lane, sysop))
    # The outcome is queued for the next screen, not written here.
    text = _visible("".join(pending_notices(session)))
    assert "Granted" in text
    assert ("Members see moderators on the Staff list." in text) is moderates
