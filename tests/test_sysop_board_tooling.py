"""
SysOp tooling for boards and file areas (issue #681): counts the dashboard
and lists can draw without writing, the lists' post/file columns, and a
detail screen's [B]ack that comes back to the list.
"""

from __future__ import annotations

import datetime

from netbbs.boards.boards import create_board
from netbbs.boards.posts import count_listed_posts, count_visible_posts, create_post
from netbbs.files.areas import create_file_area
from netbbs.files.entries import count_listed_files, count_visible_files, upload_file
from netbbs.auth.users import create_user
from tests.test_admin_flow import FakeSession, _run, _visible, _written_text, db, lane, sysop  # noqa: F401 -- fixtures


def _days_ago(days: int) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_listed_posts_leave_out_the_aged_ones_without_writing(db, sysop):
    board = create_board(db, "general", creator=sysop, max_post_age_days=30)
    old = create_post(db, board, sysop, "Old", "x")
    create_post(db, board, sysop, "New", "x")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", (_days_ago(40), old.id))
    db.connection.commit()

    listed, _newest = count_listed_posts(db, board)

    assert listed == 1
    # Read only: the aged post is still 'approved' until a sweep runs.
    assert db.connection.execute("SELECT status FROM posts WHERE id = ?", (old.id,)).fetchone()[0] == "approved"
    # And the sweeping count agrees with it.
    assert count_visible_posts(db, board)[0] == 1


def test_an_exempt_post_is_listed_past_the_age_limit(db, sysop):
    board = create_board(db, "general", creator=sysop, max_post_age_days=30)
    kept = create_post(db, board, sysop, "Kept", "x")
    db.connection.execute(
        "UPDATE posts SET created_at = ?, exempt_from_expiry = 1 WHERE id = ?", (_days_ago(40), kept.id)
    )
    db.connection.commit()

    assert count_listed_posts(db, board)[0] == 1


def test_listed_files_leave_out_the_aged_ones_without_writing(db, sysop):
    area = create_file_area(db, "uploads", creator=sysop, max_file_age_days=30)
    old = upload_file(db, area, sysop, "old.txt", b"a")
    upload_file(db, area, sysop, "new.txt", b"b")
    db.connection.execute("UPDATE files SET created_at = ? WHERE id = ?", (_days_ago(40), old.id))
    db.connection.commit()

    assert count_listed_files(db, area)[0] == 1
    assert db.connection.execute("SELECT status FROM files WHERE id = ?", (old.id,)).fetchone()[0] == "approved"
    assert count_visible_files(db, area)[0] == 1


def test_drawing_the_dashboard_does_not_write(db, lane, sysop):
    board = create_board(db, "general", creator=sysop, max_post_age_days=30)
    old = create_post(db, board, sysop, "Old", "x")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", (_days_ago(40), old.id))
    db.connection.commit()

    # The landing dashboard, then the Content menu, then out.
    _run(FakeSession(["c", "b", "b"]), lane, sysop)

    assert db.connection.execute("SELECT status FROM posts WHERE id = ?", (old.id,)).fetchone()[0] == "approved"


def test_the_board_list_shows_posts_and_what_waits(db, lane, sysop):
    board = create_board(db, "general", creator=sysop, moderated=True)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    from netbbs.boards.posts import approve_post

    approve_post(db, create_post(db, board, alice, "Published", "x"), approved_by=sysop)
    create_post(db, board, alice, "Held", "x")

    session = FakeSession(["c", "m", "l", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))

    assert "POSTS" in text.upper() and "1 +1" in text


def test_back_from_a_board_returns_to_the_list(db, lane, sysop):
    create_board(db, "general", creator=sysop)

    # Content, Message boards, List, board 01, Back: the list again.
    session = FakeSession(["c", "m", "l", "0", "1", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    after_detail = _visible(_written_text(session)).rsplit("Return to the list", 1)[1]

    assert "page 1/1" in after_detail  # the picker, not the Message boards menu
