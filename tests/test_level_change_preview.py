"""
A level change shows what it opens and closes for the account before it is
made (design doc §5.7, issue #1006): `access_map.account_level_change` works
it out per account, and the user screen's `[L]evel` shows it with `[A]pply`
and `[B]ack`.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from netbbs.access_map import GateKind, account_level_change
from netbbs.attestation import attest_age
from netbbs.auth.users import (
    SYSOP_LEVEL,
    AuthError,
    UserManagementError,
    check_user_level_change,
    create_user,
    get_user_by_username,
    set_user_level,
)
from netbbs.boards.boards import create_board
from netbbs.chat.channels import create_channel
from netbbs.chat.membership import add_member
from netbbs.doors.registry import create_door
from netbbs.moderation.roles import BoardPermission, grant_permissions
from netbbs.net.admin_flow import admin_menu
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_detail_view import ScriptedSession, _Exhausted


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    user = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_redraw_in_place_enabled(db, user, True)
    return user


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def _names(gates) -> set[tuple[str, str]]:
    return {(gate.kind.value, gate.name) for gate in gates}


# -- what changes for one account


def test_a_promotion_lists_what_it_opens(db, sysop):
    create_board(db, "lounge", min_read_level=10, min_write_level=20, creator=sysop)
    create_door(db, "blacksite", "blacksite.py", min_play_level=20, creator=sysop)
    alice = create_user(db, "alice", password="hunter2")

    change = account_level_change(db, alice, 20)

    assert _names(change.gained) == {("board_read", "lounge"), ("board_write", "lounge"), ("door", "blacksite")}
    assert change.lost == () and change.blocked == ()
    assert _names(account_level_change(db, alice, 10).gained) == {("board_read", "lounge")}  # posting needs 20


def test_a_demotion_lists_what_it_closes(db, sysop):
    create_board(db, "lounge", min_read_level=10, creator=sysop)
    alice = create_user(db, "alice", password="hunter2", user_level=10)

    change = account_level_change(db, alice, 0)

    # Posting there needs reading it, so it goes too.
    assert _names(change.lost) == {("board_read", "lounge"), ("board_write", "lounge")}
    assert change.gained == ()


def test_a_grant_keeps_what_the_level_would_close(db, sysop):
    news = create_board(db, "news", min_read_level=0, min_write_level=SYSOP_LEVEL, creator=sysop)
    helper = create_user(db, "helper", password="hunter2", user_level=50)
    grant_permissions(
        db, helper, object_type="board", object_id=news.id, permissions=BoardPermission.WRITE, granted_by=sysop
    )
    create_board(db, "members", min_read_level=50, creator=sysop)

    change = account_level_change(db, helper, 0)

    assert _names(change.lost) == {("board_read", "members"), ("board_write", "members")}


def test_another_gate_still_blocking_is_shown_apart_from_the_gains(db, sysop):
    create_board(db, "adults", min_read_level=10, min_age=18, creator=sysop)
    create_channel(db, "inner circle", min_level=10, members_only=True, creator=sysop)
    minor = create_user(db, "minor", password="hunter2")
    attest_age(db, minor, date(2015, 1, 1), verifier=sysop)

    change = account_level_change(db, minor, 10)

    assert change.gained == ()
    assert {(gate.name, fails) for gate, fails in change.blocked} == {
        ("adults", ("age 18+",)), ("inner circle", ("members only",)),
    }


def test_a_member_of_a_members_only_channel_gains_it(db, sysop):
    channel = create_channel(db, "inner circle", min_level=10, members_only=True, creator=sysop)
    alice = create_user(db, "alice", password="hunter2")
    add_member(db, channel, alice, granted_by=sysop)

    assert _names(account_level_change(db, alice, 10).gained) == {("channel", "inner circle")}


def test_what_was_kept_out_anyway_is_not_a_loss(db, sysop):
    create_board(db, "adults", min_read_level=10, min_age=18, creator=sysop)
    minor = create_user(db, "minor", password="hunter2", user_level=10)
    attest_age(db, minor, date(2015, 1, 1), verifier=sysop)

    assert account_level_change(db, minor, 0).changes_nothing


def test_a_demoted_sysop_loses_the_boards_above_the_new_level(db, sysop):
    """A SysOp passes every grant check through has_permission's SysOp
    bypass. The preview asks it of the account at the new level, or a
    demotion would seem to keep every board (review of PR #1017)."""
    create_board(db, "staff room", min_read_level=100, creator=sysop)
    second = create_user(db, "second", password="hunter2", user_level=SYSOP_LEVEL)

    lost = _names(account_level_change(db, second, 10).lost)

    assert {("board_read", "staff room"), ("board_write", "staff room"), ("sysop", "SysOp console")} <= lost


def test_the_guest_account_promoted_to_sysop_gains_mail(db, sysop):
    """The guest login stops treating an account as the guest at 255, so
    mail opens for it there (review of PR #1017)."""
    from netbbs.config import set_mail_min_level
    from netbbs.guest import set_guest_user

    guest = create_user(db, "guest", password="hunter2")
    set_guest_user(db, guest)
    set_mail_min_level(db, 10)

    change = account_level_change(db, guest, SYSOP_LEVEL)

    assert ("mail", "Mail") in _names(change.gained)
    assert all(gate.name != "Mail" for gate, _ in change.blocked)
    assert [fails for gate, fails in account_level_change(db, guest, 10).blocked if gate.name == "Mail"] == [
        ("the guest account",)
    ]


def test_promotion_to_sysop_gains_the_console(db, sysop):
    alice = create_user(db, "alice", password="hunter2")

    assert (GateKind.SYSOP, "SysOp console") in {
        (gate.kind, gate.name) for gate in account_level_change(db, alice, SYSOP_LEVEL).gained
    }


# -- the range and the dry run


@pytest.mark.parametrize("level", [-1, SYSOP_LEVEL + 1])
def test_a_level_outside_the_range_is_refused(db, sysop, level):
    alice = create_user(db, "alice", password="hunter2")

    with pytest.raises(UserManagementError, match="0 to 255"):
        set_user_level(db, alice, level, changed_by=sysop)
    with pytest.raises(UserManagementError, match="0 to 255"):
        check_user_level_change(db, alice, level, changed_by=sysop)
    with pytest.raises(AuthError, match="0 to 255"):
        create_user(db, "bob", password="hunter2", user_level=level)


def test_the_dry_run_refuses_what_the_change_would_and_writes_nothing(db, sysop):
    alice = create_user(db, "alice", password="hunter2")

    with pytest.raises(UserManagementError, match="only active SysOp"):
        check_user_level_change(db, sysop, 10, changed_by=sysop)
    check_user_level_change(db, alice, 10, changed_by=sysop)
    assert get_user_by_username(db, "alice").user_level == 0


# -- the console


def _screen(lane, sysop, keys) -> list[str]:
    session = ScriptedSession(keys)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    return session.on_terminal()


def _flat(rows: list[str]) -> str:
    return "\n".join(" ".join(row.split()) for row in rows)


def test_the_level_prompt_shows_the_preview_before_changing_anything(db, lane, sysop):
    create_board(db, "lounge", min_read_level=10, creator=sysop)
    create_user(db, "alice", password="hunter2")

    rows = _screen(lane, sysop, ["u", "u", "/", "alice", "l", "10"])

    text = _flat(rows)
    assert "Level 0 → 10" in text
    assert "read lounge board" in text
    assert "pply" in text
    assert get_user_by_username(db, "alice").user_level == 0


def test_apply_makes_the_change(db, lane, sysop):
    create_board(db, "lounge", min_read_level=10, creator=sysop)
    create_user(db, "alice", password="hunter2")

    rows = _screen(lane, sysop, ["u", "u", "/", "alice", "l", "10", "a"])

    assert get_user_by_username(db, "alice").user_level == 10
    assert "✓ 'alice' is now level 10." in rows


def test_back_leaves_the_level_alone(db, lane, sysop):
    create_board(db, "lounge", min_read_level=10, creator=sysop)
    create_user(db, "alice", password="hunter2")

    rows = _screen(lane, sysop, ["u", "u", "/", "alice", "l", "10", "b"])

    assert get_user_by_username(db, "alice").user_level == 0
    assert "'alice' stays at level 0." in rows


def test_a_change_that_opens_nothing_is_applied_without_a_preview(db, lane, sysop):
    create_user(db, "alice", password="hunter2")

    rows = _screen(lane, sysop, ["u", "u", "/", "alice", "l", "10"])

    assert get_user_by_username(db, "alice").user_level == 10
    assert "✓ 'alice' is now level 10. That opens and closes nothing for them." in rows


def test_a_refused_change_is_refused_before_any_preview(db, lane, sysop):
    rows = _screen(lane, sysop, ["u", "u", "/", "sysop", "l", "10"])

    assert "Level 255" not in _flat(rows)
    assert any("only active SysOp-level account" in row for row in rows)
    assert get_user_by_username(db, "sysop").user_level == SYSOP_LEVEL
