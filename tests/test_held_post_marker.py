"""
A caller's own held posts in the board list (issue #678): listed in their
dated place for their author only, marked "held", read only until a
moderator decides; an approved post with an edit of theirs held is marked
too.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import approve_post, count_visible_posts, create_post, edit_post, list_posts_page
from netbbs.net.board_flow import _show_board
from netbbs.storage.database import Database


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
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def board(db, sysop):
    return create_board(db, "general", creator=sysop, moderated=True)


def test_a_held_post_is_listed_for_its_author_only(db, sysop, alice, bob, board):
    approve_post(db, create_post(db, board, bob, "Approved", "x"), approved_by=sysop)
    held = create_post(db, board, alice, "Mine", "x")

    mine = list_posts_page(db, board, alice)
    theirs = list_posts_page(db, board, bob)

    assert [p.subject for p in mine.posts] == ["Approved", "Mine"]
    assert mine.posts[1].status == "pending" and mine.posts[1].post_id == held.post_id
    assert [p.subject for p in theirs.posts] == ["Approved"]
    # Counts are what others see.
    assert count_visible_posts(db, board)[0] == 1


def test_a_held_post_pages_like_any_other(db, sysop, alice, board):
    for i in range(3):
        approve_post(db, create_post(db, board, alice, f"Old {i}", "x"), approved_by=sysop)
    create_post(db, board, alice, "Held", "x")

    newest = list_posts_page(db, board, alice, limit=2)
    older = list_posts_page(db, board, alice, before=newest.oldest_cursor, limit=2)

    assert [p.subject for p in newest.posts] == ["Old 2", "Held"]
    assert newest.has_older and [p.subject for p in older.posts] == ["Old 0", "Old 1"]
    assert older.has_newer


def test_an_edit_of_theirs_held_is_named_on_the_page(db, sysop, alice, bob, board):
    post = approve_post(db, create_post(db, board, alice, "Hello", "x"), approved_by=sysop)
    edit_post(db, post, board, subject="Hello", body="y", edited_by=alice)

    assert list_posts_page(db, board, alice).held_edits == frozenset({post.root_post_id})
    assert list_posts_page(db, board, bob).held_edits == frozenset()
    # The listed text is still the approved one.
    assert list_posts_page(db, board, alice).posts[0].body == "x"


def test_a_moderators_held_edit_is_theirs_not_the_authors(db, sysop, alice, board):
    mod = create_user(db, "mod", password="hunter2", user_level=SYSOP_LEVEL)
    post = approve_post(db, create_post(db, board, alice, "Hello", "x"), approved_by=sysop)
    edit_post(db, post, board, subject="Hello", body="y", edited_by=mod)

    assert list_posts_page(db, board, alice).held_edits == frozenset()
    assert list_posts_page(db, board, mod).held_edits == frozenset({post.root_post_id})


def test_the_list_marks_it_held_and_the_reader_offers_nothing_but_leaving(db, alice, board):
    create_post(db, board, alice, "Mine", "what I wrote")

    session = _FakeSession(keys=["1", "b", "b"])
    asyncio.run(_show_board(session, db, board, alice))
    text = session.visible_output

    assert re.search(r"Mine\s+held\s+alice", text)
    assert "[awaiting approval]" in text and "what I wrote" in text
    reader = text[text.index("[awaiting approval]"):]
    for action in ("Reply", "Edit", "Withdraw", "Remove post", "History"):
        assert action not in reader


def test_the_reader_says_an_edit_awaits_approval(db, sysop, alice, board):
    post = approve_post(db, create_post(db, board, alice, "Hello", "approved text"), approved_by=sysop)
    edit_post(db, post, board, subject="Hello", body="new text", edited_by=alice)

    session = _FakeSession(keys=["1", "b", "b"])
    asyncio.run(_show_board(session, db, board, alice))
    text = session.visible_output

    assert re.search(r"Hello\s+held\s+alice", text)
    assert "[your edit awaits approval]" in text and "approved text" in text


class _FakeSession:
    def __init__(self, keys=None, lines=None):
        self._keys = iter(keys or [])
        self._lines = iter(lines or [])
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
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("ran out of scripted keys")
        return key

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return next(self._lines, "")

    @property
    def visible_output(self) -> str:
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", "".join(self.written))
