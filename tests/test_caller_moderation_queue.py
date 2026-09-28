"""
A caller granted APPROVE on a board or file area decides on its held posts
and uploads from the board's or area's own page (issue #678), not only a
SysOp in the console.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, list_posts_page
from netbbs.files.areas import create_file_area
from netbbs.files.entries import list_files_page, upload_file
from netbbs.moderation import BoardPermission, grant_permissions
from netbbs.net.board_flow import _show_board
from netbbs.net.file_flow import _show_area
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def mod(db):
    return create_user(db, "mod", password="hunter2", user_level=10)


class _FakeSession:
    def __init__(self, keys):
        self._keys = list(keys)
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        if not self._keys:
            raise AssertionError("ran out of scripted keys")
        return self._keys.pop(0)

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        if not self._keys:
            raise AssertionError("ran out of scripted input")
        return self._keys.pop(0)

    @property
    def text(self) -> str:
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", "".join(self.written))


def _approver_on_board(db, sysop, mod, board):
    grant_permissions(
        db, mod, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=sysop,
    )


def test_an_approver_approves_from_the_board_page(db, sysop, alice, mod):
    board = create_board(db, "general", creator=sysop, moderated=True)
    _approver_on_board(db, sysop, mod, board)
    create_post(db, board, alice, "Held one", "text")

    # q: the queue; 01: the post; a: approve; b: out of the queue; b: out of the board.
    session = _FakeSession(["q", "0", "1", "a", "b", "b"])
    asyncio.run(_show_board(session, db, board, mod))

    assert "[Q]ueue (1)" in session.text
    assert [post.subject for post in list_posts_page(db, board, mod).posts] == ["Held one"]


def test_an_approver_is_not_shown_the_sysops_status_or_the_edit_keys(db, sysop, alice, mod):
    board = create_board(db, "general", creator=sysop, moderated=True)
    _approver_on_board(db, sysop, mod, board)
    create_post(db, board, alice, "Held one", "text")

    session = _FakeSession(["q", "0", "1", "b", "b", "b"])
    asyncio.run(_show_board(session, db, board, mod))
    screen = session.text[session.text.index("PENDING POST"):]

    assert "Backup:" not in screen
    assert "[A]pprove" in screen and "[R]eject" in screen
    assert "Pin toggle" not in screen and "empt toggle" not in screen


def test_a_caller_who_may_not_approve_has_no_queue(db, sysop, alice):
    board = create_board(db, "general", creator=sysop, moderated=True)
    create_post(db, board, alice, "Held one", "text")
    bob = create_user(db, "bob", password="hunter2", user_level=10)

    session = _FakeSession(["q", "b"])
    asyncio.run(_show_board(session, db, board, bob))

    assert "Queue" not in session.text


def test_an_approver_rejects_on_an_empty_board(db, sysop, alice, mod):
    board = create_board(db, "general", creator=sysop, moderated=True)
    _approver_on_board(db, sysop, mod, board)
    create_post(db, board, alice, "Held one", "text")

    # r: reject, then the optional reason line.
    session = _FakeSession(["q", "0", "1", "r", "spam", "b", "b"])
    asyncio.run(_show_board(session, db, board, mod))

    assert "[Q]ueue (1)" in session.text
    assert db.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0


def test_an_approver_approves_from_the_file_area_page(db, sysop, alice, mod):
    area = create_file_area(db, "uploads", creator=sysop, moderated=True)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    upload_file(db, area, alice, "notes.txt", b"data")
    lane = DatabaseLane(db.path)
    try:
        session = _FakeSession(["q", "0", "1", "a", "b", "b", "b"])
        asyncio.run(_show_area(session, lane, area, mod))
    finally:
        lane.close()

    assert "[Q]ueue (1)" in session.text
    assert [entry.filename for entry in list_files_page(db, area, mod).entries] == ["notes.txt"]


def test_an_approver_may_reject_a_held_post_but_not_delete_a_published_one(db, sysop, alice, mod):
    from netbbs.boards.posts import PostError, approve_post, delete_post

    board = create_board(db, "general", creator=sysop, moderated=True)
    _approver_on_board(db, sysop, mod, board)
    held = create_post(db, board, alice, "Held", "x")
    published = approve_post(db, create_post(db, board, alice, "Published", "x"), approved_by=sysop)

    delete_post(db, held, deleted_by=mod, reason="no")
    with pytest.raises(PostError, match="DELETE"):
        delete_post(db, published, deleted_by=mod)


def test_an_approver_may_reject_a_held_upload_but_not_delete_a_published_one(db, sysop, alice, mod):
    from netbbs.files.entries import FileEntryError, approve_file, delete_file

    area = create_file_area(db, "uploads", creator=sysop, moderated=True)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    held = upload_file(db, area, alice, "held.txt", b"a")
    published = approve_file(db, upload_file(db, area, alice, "published.txt", b"b"), approved_by=sysop)

    delete_file(db, held, deleted_by=mod)
    with pytest.raises(FileEntryError, match="DELETE"):
        delete_file(db, published, deleted_by=mod)


# -- Codex review on #796 ----------------------------------------------


def test_a_stale_rejection_never_deletes_a_post_approved_meanwhile(db, sysop, alice, mod):
    from netbbs.boards.posts import PostError, approve_post, delete_post

    board = create_board(db, "general", creator=sysop, moderated=True)
    _approver_on_board(db, sysop, mod, board)
    stale = create_post(db, board, alice, "Held", "x")
    approve_post(db, stale, approved_by=sysop)

    with pytest.raises(PostError, match="already decided"):
        delete_post(db, stale, deleted_by=mod)
    assert [post.subject for post in list_posts_page(db, board, mod).posts] == ["Held"]
    assert db.connection.execute("SELECT COUNT(*) FROM post_rejections").fetchone()[0] == 0


def test_a_stale_rejection_never_deletes_an_upload_approved_meanwhile(db, sysop, alice, mod):
    from netbbs.files.entries import FileEntryError, approve_file, delete_file

    area = create_file_area(db, "uploads", creator=sysop, moderated=True)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    stale = upload_file(db, area, alice, "notes.txt", b"data")
    approve_file(db, stale, approved_by=sysop)

    with pytest.raises(FileEntryError, match="already decided"):
        delete_file(db, stale, deleted_by=mod)
    assert [entry.filename for entry in list_files_page(db, area, mod).entries] == ["notes.txt"]


def test_leaving_the_queue_keeps_the_board_page_the_caller_was_on(db, sysop, alice, mod, monkeypatch):
    from netbbs.boards import posts as posts_module
    from netbbs.boards.posts import approve_post

    seconds = iter(range(1000))
    monkeypatch.setattr(
        posts_module, "utc_now_iso", lambda: f"2026-01-01T00:{(s := next(seconds)) // 60:02d}:{s % 60:02d}.000000Z"
    )
    board = create_board(db, "general", creator=sysop, moderated=True)
    _approver_on_board(db, sysop, mod, board)
    for i in range(30):
        approve_post(db, create_post(db, board, alice, f"Subject {i:02d}", "x"), approved_by=sysop)
    create_post(db, board, alice, "Held", "x")

    # o: an older page; q, then b out of the unchanged queue; b out of the board.
    session = _FakeSession(["o", "q", "b", "b"])
    asyncio.run(_show_board(session, db, board, mod))
    after_queue = session.text.rsplit("Pending posts in", 1)[1]

    # Still an older page: the newest has nothing newer to offer.
    assert "[N]ewer" in after_queue


def test_backing_out_of_the_queue_on_an_empty_area_stays_on_one_screen(db, sysop, alice, mod):
    """Codex review on #796: [Q]ueue then [B]ack, again and again, must not
    pile up screens."""
    area = create_file_area(db, "uploads", creator=sysop, moderated=True)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    upload_file(db, area, alice, "notes.txt", b"data")
    rounds = 5
    lane = DatabaseLane(db.path)
    try:
        session = _FakeSession(["q", "b"] * rounds + ["b"])
        asyncio.run(_show_area(session, lane, area, mod))
    finally:
        lane.close()

    # The area's screen was drawn once; each round redrew only its key bar.
    assert session.text.count("This file area has no files yet") == 1
    assert session.text.count("[Q]ueue (1)") == rounds + 1


def test_approving_from_the_queue_counts_the_post_as_unread_for_the_board(db, sysop, alice, mod):
    """Codex review on #796: the page's new-post count follows an approval."""
    from netbbs.activity import unread_post_count

    board = create_board(db, "general", creator=sysop, moderated=True)
    _approver_on_board(db, sysop, mod, board)
    create_post(db, board, alice, "Held one", "text")

    session = _FakeSession(["q", "0", "1", "a", "b", "b"])
    asyncio.run(_show_board(session, db, board, mod))

    assert unread_post_count(db, mod, board) == 1
    assert "[M]ark all read" in session.text.rsplit("Pending posts in", 1)[1]
