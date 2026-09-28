"""
What an author is told when a moderator decides on their held post (issue
#678): a one-time notice at the main menu, and for a rejection a mail with
the reason and their text.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.moderation_notices import take_moderation_notices
from netbbs.boards.posts import approve_post, create_post, delete_post, edit_post
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.mail import list_inbox
from netbbs.net.char_input import InputHistory
from netbbs.net.main_menu import _main_menu
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
def board(db, sysop):
    return create_board(db, "general", creator=sysop, moderated=True)


def test_an_approval_is_told_once(db, sysop, alice, board):
    held = create_post(db, board, alice, "Hello there", "text")

    approve_post(db, held, approved_by=sysop)

    assert take_moderation_notices(db, alice) == [("approved", 'Your post "Hello there" on general was approved.')]
    assert take_moderation_notices(db, alice) == []


def test_a_rejection_is_told_with_its_reason_and_mailed_with_the_text(db, sysop, alice, board):
    held = create_post(db, board, alice, "Hello there", "what I wrote")

    delete_post(db, held, deleted_by=sysop, reason="off topic")

    assert take_moderation_notices(db, alice) == [
        ("rejected", 'Your post "Hello there" on general was rejected: off topic')
    ]
    [mail] = list_inbox(db, alice)
    assert mail.sender_user_id == sysop.id
    assert "Reason: off topic" in mail.body and "what I wrote" in mail.body


def test_a_rejection_without_a_reason_says_so(db, sysop, alice, board):
    delete_post(db, create_post(db, board, alice, "Hello", "x"), deleted_by=sysop)

    assert take_moderation_notices(db, alice)[0][1].endswith("was rejected.")
    assert "No reason was given." in list_inbox(db, alice)[0].body


def test_an_edit_is_told_as_an_edit(db, sysop, alice, board):
    post = approve_post(db, create_post(db, board, alice, "Hello", "x"), approved_by=sysop)
    take_moderation_notices(db, alice)
    edit = edit_post(db, post, board, subject="Hello", body="y", edited_by=alice)
    assert edit.status == "pending"

    delete_post(db, edit, deleted_by=sysop)

    assert take_moderation_notices(db, alice)[0][1].startswith('Your edit of "Hello"')


def test_a_moderator_deciding_on_their_own_post_is_not_told(db, sysop, board):
    held = create_post(db, board, sysop, "Mine", "x")
    assert held.status == "pending"

    approve_post(db, held, approved_by=sysop)

    assert take_moderation_notices(db, sysop) == []


def test_the_main_menu_tells_it_once(db, sysop, alice, board):
    delete_post(db, create_post(db, board, alice, "Hello", "x"), deleted_by=sysop, reason="spam")
    lane = DatabaseLane(db.path)
    try:
        texts = []
        for _ in range(2):
            session = _FakeSession(["l", "y"])
            asyncio.run(_main_menu(
                session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice, lane=lane,
            ))
            texts.append("".join(session.written))
    finally:
        lane.close()

    assert 'Your post "Hello" on general was rejected: spam' in texts[0]
    assert "was rejected" not in texts[1]


def test_rejecting_from_the_pending_screen_asks_for_a_reason(db, sysop, alice, board):
    from netbbs.net.admin_flow import _post_action_screen

    held = create_post(db, board, alice, "Hello", "x")
    lane = DatabaseLane(db.path)
    try:
        session = _FakeSession(["r", "not here"])
        asyncio.run(_post_action_screen(session, lane, sysop, held, board))
    finally:
        lane.close()

    reason = db.connection.execute(
        "SELECT reason FROM post_rejections WHERE post_id = ?", (held.post_id,)
    ).fetchone()[0]
    assert reason == "not here"


class _FakeSession:
    def __init__(self, inputs):
        self._inputs = list(inputs)
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

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_key)")
        return self._inputs.pop(0)
