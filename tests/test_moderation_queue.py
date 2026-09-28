"""
The moderator's side of the queue (issue #678): rows that say what each held
item is, an edit judged against the text it would replace, and one
node-wide screen of everything awaiting review.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import approve_post, create_post, edit_post
from netbbs.files import entries as entries_module
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.net.admin_flow import _load_pending_items, _post_action_screen
from tests.test_admin_flow import FakeSession, _run, _visible, _written_text, db, lane, sysop  # noqa: F401 -- fixtures


@pytest.fixture(autouse=True)
def _one_second_apart(monkeypatch):
    # Items made in the same instant would sort by chance.
    seconds = itertools.count()

    def now() -> str:
        second = next(seconds)
        return f"2026-01-01T00:{second // 60:02d}:{second % 60:02d}.000000Z"

    monkeypatch.setattr(posts_module, "utc_now_iso", now)
    monkeypatch.setattr(entries_module, "utc_now_iso", now, raising=False)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _held_everything(db, sysop, alice):
    board = create_board(db, "general", creator=sysop, moderated=True)
    area = create_file_area(db, "uploads", creator=sysop, moderated=True)
    original = approve_post(db, create_post(db, board, alice, "Original", "old text"), approved_by=sysop)
    create_post(db, board, alice, "Fresh", "a new post")
    create_post(db, board, alice, "Re: Original", "an answer", parent_post_id=original.post_id)
    edit_post(db, original, board, subject="Original, amended", body="new text", edited_by=alice)
    upload_file(db, area, alice, "notes.txt", b"data")
    return board, area


def test_the_node_wide_queue_names_each_kind_oldest_first(db, sysop, alice):
    _held_everything(db, sysop, alice)

    items = _load_pending_items(db, sysop)

    assert [(item.kind, item.where, item.title) for item in items] == [
        ("post", "general", "Fresh"),
        ("reply", "general", "Re: Original"),
        ("edit", "general", "Original, amended"),
        ("file", "uploads", "notes.txt"),
    ]
    assert all(item.author == "alice" for item in items)
    # A post and a file may share an id: the node-wide queue numbers by place.
    assert [item.stable_id for item in items] == [1, 2, 3, 4]


def test_a_board_queue_lists_only_that_board(db, sysop, alice):
    board, _area = _held_everything(db, sysop, alice)

    items = _load_pending_items(db, sysop, boards=[board])

    assert [item.kind for item in items] == ["post", "reply", "edit"]


def test_a_held_edit_is_shown_against_the_current_text(db, lane, sysop, alice):
    board, _area = _held_everything(db, sysop, alice)
    edit = next(item.post for item in _load_pending_items(db, sysop) if item.kind == "edit")

    session = FakeSession(["b"])
    asyncio.run(_post_action_screen(session, lane, sysop, edit, board))
    text = _visible(_written_text(session))

    assert "PENDING EDIT" in text
    assert "PROPOSED TEXT" in text and "new text" in text
    assert "CURRENT TEXT" in text and "old text" in text
    assert "Current subject" in text and "Original" in text


def test_a_held_reply_names_what_it_answers(db, lane, sysop, alice):
    board, _area = _held_everything(db, sysop, alice)
    reply = next(item.post for item in _load_pending_items(db, sysop) if item.kind == "reply")

    session = FakeSession(["b"])
    asyncio.run(_post_action_screen(session, lane, sysop, reply, board))
    text = _visible(_written_text(session))

    assert "PENDING REPLY" in text and "Reply to" in text
    assert "CURRENT TEXT" not in text


def test_the_content_menu_opens_the_node_wide_queue(db, lane, sysop, alice):
    _held_everything(db, sysop, alice)

    session = FakeSession(["c", "p", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))

    assert "Pending review" in text
    for title in ("Fresh", "Re: Original", "Original, amended", "notes.txt"):
        assert title in text
    assert "uploads" in text and "file" in text
