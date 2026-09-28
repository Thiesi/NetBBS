"""
A board's and a file area's detail screen say who moderates it and what
moderators did there (issue #678).
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import approve_post, create_post, delete_post
from netbbs.files.areas import create_file_area
from netbbs.files.entries import approve_file, upload_file
from netbbs.moderation import BoardPermission, grant_permissions
from netbbs.moderation.log import list_recent_actions
from netbbs.net.admin_flow import _area_detail_screen, _board_detail_screen
from tests.test_admin_flow import FakeSession, _visible, _written_text, db, lane, sysop  # noqa: F401 -- fixtures


def _moderated_board(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    mod = create_user(db, "mod", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=sysop, moderated=True)
    other = create_board(db, "other", creator=sysop, moderated=True)
    grant_permissions(
        db, mod, object_type="board", object_id=board.id,
        permissions=BoardPermission.APPROVE | BoardPermission.DELETE, granted_by=sysop,
    )
    approve_post(db, create_post(db, board, alice, "Kept", "x"), approved_by=sysop)
    delete_post(db, create_post(db, board, alice, "Dropped", "x"), deleted_by=mod, reason="no")
    approve_post(db, create_post(db, other, alice, "Elsewhere", "x"), approved_by=sysop)
    return board, other


def test_a_boards_history_is_its_own_and_bounded(db, sysop):
    board, other = _moderated_board(db, sysop)

    actions = list_recent_actions(db, object_type="board", object_id=board.id)

    assert {entry.object_id for entry in actions} == {board.id}
    assert {"approve", "reject"} <= {entry.action for entry in actions}
    assert len(list_recent_actions(db, object_type="board", object_id=board.id, limit=1)) == 1


def test_the_board_detail_names_its_moderators(db, lane, sysop):
    board, _other = _moderated_board(db, sysop)

    session = FakeSession(["b"])
    asyncio.run(_board_detail_screen(session, lane, sysop, board))
    text = _visible(_written_text(session))

    assert "Moderators:" in text and "mod (delete, approve)" in text
    assert "[H]istory" in text


def test_a_board_without_moderators_says_so(db, lane, sysop):
    board = create_board(db, "quiet", creator=sysop)

    session = FakeSession(["b"])
    asyncio.run(_board_detail_screen(session, lane, sysop, board))

    assert "none (SysOps only)" in _visible(_written_text(session))


def test_a_blanket_grant_is_named_as_one(db, lane, sysop):
    board = create_board(db, "general", creator=sysop)
    everyone = create_user(db, "everywhere", password="hunter2", user_level=10)
    grant_permissions(
        db, everyone, object_type="board", object_id=None, permissions=BoardPermission.APPROVE, granted_by=sysop,
    )

    session = FakeSession(["b"])
    asyncio.run(_board_detail_screen(session, lane, sysop, board))

    assert "everywhere (approve, all local ones)" in _visible(_written_text(session))


def test_history_lists_what_moderators_did_on_the_board(db, lane, sysop):
    board, _other = _moderated_board(db, sysop)

    session = FakeSession(["h", "b", "b"])
    asyncio.run(_board_detail_screen(session, lane, sysop, board))
    text = _visible(_written_text(session))

    assert "History of general" in text
    assert "reject" in text and "approve" in text


def test_the_file_area_detail_names_moderators_and_history(db, lane, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    mod = create_user(db, "mod", password="hunter2", user_level=10)
    area = create_file_area(db, "uploads", creator=sysop, moderated=True)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    approve_file(db, upload_file(db, area, alice, "notes.txt", b"data"), approved_by=sysop)

    session = FakeSession(["h", "b", "b"])
    asyncio.run(_area_detail_screen(session, lane, sysop, area))
    text = _visible(_written_text(session))

    assert "mod (approve)" in text
    assert "History of uploads" in text and "approve" in text
