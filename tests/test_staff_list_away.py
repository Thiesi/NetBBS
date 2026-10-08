"""
The Staff list and the away notice (design doc §5.6, issue #836).
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

from netbbs.auth.users import (
    CO_SYSOP_PRESET,
    SYSOP_LEVEL,
    StaffPermission,
    UserManagementError,
    create_user,
    set_staff_permissions,
    set_user_disabled,
)
from netbbs.boards import create_board
from netbbs.chat.mailbox import MessageMailbox
from netbbs.guest import set_guest_user
from netbbs.moderation.roles import BoardPermission, grant_permissions
from netbbs.net.admin_flow import admin_menu, staff_list_screen, staff_menu
from netbbs.net.main_menu import _draw_main_menu
from netbbs.net.signup_text import pending_approval_notice
from netbbs.staff import (
    MAX_AWAY_MESSAGE_CHARS,
    approvers_away_line,
    away_notice,
    away_problem,
    end_away,
    list_staff,
    node_today,
    set_away,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text

TODAY = datetime.date(2026, 9, 29)


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
    return create_user(db, "InkWell", password="hunter2", user_level=SYSOP_LEVEL)


def _staff(db, sysop, name, permissions):
    return set_staff_permissions(db, create_user(db, name, password="hunter2"), permissions, changed_by=sysop)


# -- the away notice -------------------------------------------------------------


def test_a_sysop_marks_themselves_away_and_ends_it(db, sysop):
    until = node_today(db) + datetime.timedelta(days=10)
    notice = set_away(db, sysop, "  At a pen show  ", until)
    assert notice.message == "At a pen show" and notice.until == until
    assert away_notice(db, sysop) == notice
    end_away(db, sysop)
    assert away_notice(db, sysop) is None


def test_a_notice_with_a_date_ends_by_itself_after_that_day(db, sysop):
    until = node_today(db)
    set_away(db, sysop, "Back tomorrow", until)
    assert away_notice(db, sysop, today=until) is not None
    assert away_notice(db, sysop, today=until + datetime.timedelta(days=1)) is None


def test_a_notice_without_a_date_stays(db, sysop):
    set_away(db, sysop, "Travelling", None)
    assert away_notice(db, sysop, today=node_today(db) + datetime.timedelta(days=400)) is not None


def test_a_staff_member_may_be_away_and_a_plain_caller_may_not(db, sysop):
    helper = _staff(db, sysop, "helper", StaffPermission.APPROVE_ACCOUNTS)
    set_away(db, helper, "Exams", None)
    carol = create_user(db, "carol", password="hunter2")
    with pytest.raises(UserManagementError):
        set_away(db, carol, "Holiday", None)


@pytest.mark.parametrize(
    ("message", "until", "fragment"),
    [
        ("", None, "empty"),
        ("x" * (MAX_AWAY_MESSAGE_CHARS + 1), None, "characters"),
        ("line one\nline two", None, "one line"),
        ("Back |07soon", None, "pipe code"),
        ("Back soon", TODAY - datetime.timedelta(days=1), "passed"),
    ],
)
def test_away_messages_are_one_short_plain_line(message, until, fragment):
    assert fragment in away_problem(message, until, TODAY)


def test_a_plain_bar_is_fine(db, sysop):
    assert away_problem("Pens | inks", None, TODAY) is None


# -- the Staff list ---------------------------------------------------------------


def test_the_staff_list_names_sysops_staff_and_moderators_in_that_order(db, sysop):
    helper = _staff(db, sysop, "Copperplate", StaffPermission.APPROVE_ACCOUNTS | StaffPermission.MANAGE_ACCOUNTS)
    mod = create_user(db, "OldNib", password="hunter2")
    board = create_board(db, "Trading Post", creator=sysop, moderated=True)
    grant_permissions(
        db, mod, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=sysop
    )
    create_user(db, "lena_h", password="hunter2")
    gone = _staff(db, sysop, "gone", StaffPermission.APPROVE_ACCOUNTS)
    set_user_disabled(db, gone, True, changed_by=sysop)
    entries = list_staff(db)
    assert [(e.user.username, e.role) for e in entries] == [
        ("InkWell", "SysOp"), ("Copperplate", "Staff"), ("OldNib", "Moderator"),
    ]
    assert entries[1].looks_after == "approve accounts, manage accounts"
    assert [grant.object_id for grant in entries[2].grants] == [board.id]
    assert helper  # used above


def test_a_read_or_post_grant_alone_does_not_make_a_moderator(db, sysop):
    board = create_board(db, "Announcements", creator=sysop)
    poster = create_user(db, "Quill", password="hunter2")
    reader = create_user(db, "Blot", password="hunter2")
    mixed = create_user(db, "Serif", password="hunter2")
    grant_permissions(
        db, poster, object_type="board", object_id=board.id,
        permissions=BoardPermission.READ | BoardPermission.WRITE, granted_by=sysop,
    )
    grant_permissions(
        db, reader, object_type="file_area", object_id=None, permissions=BoardPermission.READ, granted_by=sysop
    )
    grant_permissions(
        db, mixed, object_type="board", object_id=board.id,
        permissions=BoardPermission.READ | BoardPermission.WRITE, granted_by=sysop,
    )
    grant_permissions(
        db, mixed, object_type="board", object_id=None, permissions=BoardPermission.APPROVE, granted_by=sysop
    )
    entries = list_staff(db)
    assert [(e.user.username, e.role) for e in entries] == [("InkWell", "SysOp"), ("Serif", "Moderator")]
    # Only the grant that moderates is described.
    assert [(grant.object_id, grant.permissions) for grant in entries[1].grants] == [
        (None, int(BoardPermission.APPROVE))
    ]


def test_access_bits_merged_into_a_moderator_grant_are_not_listed(db, sysop):
    board = create_board(db, "Announcements", creator=sysop)
    mod = create_user(db, "Serif", password="hunter2")
    # Both land in one grant row for the board.
    grant_permissions(
        db, mod, object_type="board", object_id=board.id,
        permissions=BoardPermission.READ | BoardPermission.WRITE, granted_by=sysop,
    )
    grant_permissions(
        db, mod, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=sysop
    )
    [entry] = [e for e in list_staff(db) if e.role == "Moderator"]
    assert [grant.permissions for grant in entry.grants] == [int(BoardPermission.APPROVE)]


def test_the_staff_list_screen_shows_last_session_dates_and_who_is_away(db, lane, sysop):
    set_away(db, sysop, "At a pen show", node_today(db) + datetime.timedelta(days=3))
    carol = create_user(db, "carol", password="hunter2")
    session = FakeSession(["b"])
    asyncio.run(staff_list_screen(session, lane, carol))
    text = _visible(_written_text(session))
    assert "InkWell" in text and "runs the node" in text
    assert "never" in text  # no session yet
    assert "InkWell: away, back" in text and "At a pen show" in text


def test_every_member_but_the_guest_is_offered_the_staff_list(db, sysop):
    carol = create_user(db, "carol", password="hunter2")
    guest = create_user(db, "guest", password="hunter2")
    set_guest_user(db, guest)

    def menu(user):
        session = FakeSession()
        asyncio.run(_draw_main_menu(session, db, MessageMailbox(), user))
        return _visible(_written_text(session))

    assert "S[t]aff list" in menu(carol)
    assert "S[t]aff list" not in menu(guest)


# -- pending callers --------------------------------------------------------------


def test_pending_callers_hear_nothing_while_one_approver_is_in(db, sysop):
    helper = _staff(db, sysop, "helper", StaffPermission.APPROVE_ACCOUNTS)
    set_away(db, sysop, "Holiday", None)
    assert approvers_away_line(db) is None
    assert helper


def test_pending_callers_are_told_who_is_back_first_when_everyone_is_away(db, sysop):
    helper = _staff(db, sysop, "helper", StaffPermission.APPROVE_ACCOUNTS)
    today = node_today(db)
    set_away(db, sysop, "At a pen show", today + datetime.timedelta(days=9))
    set_away(db, helper, "Exams", today + datetime.timedelta(days=4))
    line = approvers_away_line(db)
    assert "helper expects to be back on" in line
    assert (today + datetime.timedelta(days=4)).isoformat() in line and "Exams" in line
    assert line in pending_approval_notice("newbie", line)


def test_an_undated_absence_says_since_when(db, sysop):
    set_away(db, sysop, "Travelling", None)
    assert "InkWell has been away since" in approvers_away_line(db)


def test_a_manager_without_approve_is_not_an_approver(db, sysop):
    _staff(db, sysop, "manager", StaffPermission.MANAGE_ACCOUNTS)
    set_away(db, sysop, "Holiday", None)
    assert approvers_away_line(db) is not None


def test_the_sysops_own_words_are_sanitized_for_callers():
    assert "\x1b" not in pending_approval_notice("newbie", "InkWell is away: \x1b[31mred")


# -- the consoles ------------------------------------------------------------------


def test_the_sysop_sets_an_away_notice_from_the_console_and_is_reminded(db, lane, sysop):
    until = (node_today(db) + datetime.timedelta(days=5)).isoformat()
    # t: Time away; s: set; message; date; (back on the landing) b: leave.
    session = FakeSession(["t", "s", "At a pen show", until, "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert away_notice(db, sysop).message == "At a pen show"
    landing = _visible(_written_text(session)).rsplit("SysOp operations console", 1)[-1]
    assert f"You are marked away, back {until} -- At a pen show." in " ".join(landing.split())


def test_a_staff_member_ends_their_notice_from_the_staff_console(db, lane, sysop):
    helper = _staff(db, sysop, "helper", CO_SYSOP_PRESET)
    set_away(db, helper, "Exams", None)
    session = FakeSession(["t", "e", "b"])
    asyncio.run(staff_menu(session, lane, helper))
    assert away_notice(db, helper) is None


def test_a_bad_date_is_refused_without_setting_anything(db, lane, sysop):
    session = FakeSession(["t", "s", "Away", "12.10.2026", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert away_notice(db, sysop) is None
    assert "not a date like" in _visible(_written_text(session))


def test_the_staff_list_names_nothing_a_member_cannot_see(db, lane, sysop):
    # Review on #870: a moderator's grants name boards and Communities.
    from netbbs.communities import create_community

    mod = create_user(db, "OldNib", password="hunter2")
    secret = create_board(db, "Inner circle", creator=sysop, min_read_level=200)
    hidden = create_community(db, "Back room", creator=sysop, hidden=True)
    open_board = create_board(db, "Trading Post", creator=sysop)
    for object_id, community_id in ((secret.id, None), (None, hidden.id), (open_board.id, None)):
        grant_permissions(
            db, mod, object_type="board", object_id=object_id, community_id=community_id,
            permissions=BoardPermission.APPROVE, granted_by=sysop,
        )
    carol = create_user(db, "carol", password="hunter2")
    session = FakeSession(["b"])
    asyncio.run(staff_list_screen(session, lane, carol))
    text = " ".join(_visible(_written_text(session)).split())
    assert "Trading Post" in text
    # Table cells wrap, so look for the first word of each name.
    assert "Inner" not in text and '"Back' not in text

    session = FakeSession(["b"])
    asyncio.run(staff_list_screen(session, lane, sysop))
    text = _visible(_written_text(session))
    assert "Inner" in text and '"Back' in text
