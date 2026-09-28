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
from netbbs.net import admin_flow
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

    items, more = _load_pending_items(db, sysop)

    assert not more
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

    items, _more = _load_pending_items(db, sysop, boards=[board])

    assert [item.kind for item in items] == ["post", "reply", "edit"]


def test_a_held_edit_is_shown_against_the_current_text(db, lane, sysop, alice):
    board, _area = _held_everything(db, sysop, alice)
    edit = next(item.post for item in _load_pending_items(db, sysop)[0] if item.kind == "edit")

    session = FakeSession(["b"])
    asyncio.run(_post_action_screen(session, lane, sysop, edit, board))
    text = _visible(_written_text(session))

    assert "PENDING EDIT" in text
    assert "PROPOSED TEXT" in text and "new text" in text
    assert "CURRENT TEXT" in text and "old text" in text
    assert "Current subject" in text and "Original" in text


def test_a_held_reply_names_what_it_answers(db, lane, sysop, alice):
    board, _area = _held_everything(db, sysop, alice)
    reply = next(item.post for item in _load_pending_items(db, sysop)[0] if item.kind == "reply")

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


# -- Codex review on #795 ----------------------------------------------


def test_a_queue_lists_the_oldest_and_says_more_waits(db, lane, sysop, alice, monkeypatch):
    monkeypatch.setattr(admin_flow, "MAX_QUEUE_ITEMS", 2)
    board = create_board(db, "general", creator=sysop, moderated=True)
    for i in range(3):
        create_post(db, board, alice, f"Post {i}", "x")

    items, more = _load_pending_items(db, sysop)
    assert more and [item.title for item in items] == ["Post 0", "Post 1"]

    session = FakeSession(["c", "p", "b", "b", "b"])
    _run(session, lane, sysop)
    assert "The oldest 2 are listed" in _visible(_written_text(session))


def test_the_queue_is_oldest_first_by_instant_not_by_text(db, sysop, alice):
    board = create_board(db, "general", creator=sysop, moderated=True)
    later = create_post(db, board, alice, "Later", "x")
    earlier = create_post(db, board, alice, "Earlier", "x")
    # 01:00+02:00 is 23:00Z the day before: earlier, though it sorts after as text.
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:00.000000Z", later.id))
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", ("2026-01-01T01:00:00+02:00", earlier.id))
    db.connection.commit()

    items, _more = _load_pending_items(db, sysop)

    assert [item.title for item in items] == ["Earlier", "Later"]


def test_an_unshowable_time_does_not_keep_the_queue_shut(db, lane, sysop, alice):
    board = create_board(db, "general", creator=sysop, moderated=True)
    held = create_post(db, board, alice, "Odd", "x")
    create_post(db, board, alice, "Normal", "x")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", ("not a time", held.id))
    db.connection.commit()

    items, _more = _load_pending_items(db, sysop)
    assert [(item.title, item.when) for item in items][-1] == ("Odd", "not a time")

    odd = next(item.post for item in items if item.title == "Odd")
    session = FakeSession(["b"])
    asyncio.run(_post_action_screen(session, lane, sysop, odd, board))
    assert "not a time" in _visible(_written_text(session))


def test_an_edit_older_than_the_approved_one_says_it_is_superseded(db, lane, sysop, alice):
    board = create_board(db, "general", creator=sysop, moderated=True)
    original = approve_post(db, create_post(db, board, alice, "Hello", "v1"), approved_by=sysop)
    older = edit_post(db, original, board, subject="Hello", body="v2", edited_by=alice)
    newer = edit_post(db, original, board, subject="Hello", body="v3", edited_by=alice)
    approve_post(db, newer, approved_by=sysop)

    session = FakeSession(["b"])
    asyncio.run(_post_action_screen(session, lane, sysop, older, board))
    text = _visible(_written_text(session))

    assert "a later edit is already approved" in text


def test_the_edit_readers_would_get_is_not_called_superseded(db, lane, sysop, alice):
    board, _area = _held_everything(db, sysop, alice)
    edit = next(item.post for item in _load_pending_items(db, sysop)[0] if item.kind == "edit")

    session = FakeSession(["b"])
    asyncio.run(_post_action_screen(session, lane, sysop, edit, board))

    assert "Superseded" not in _visible(_written_text(session))


def test_the_node_wide_cap_is_one_cap_across_every_board(db, sysop, alice, monkeypatch):
    """Codex review on #795: one bounded query for the node, not one per board."""
    monkeypatch.setattr(admin_flow, "MAX_QUEUE_ITEMS", 2)
    first = create_board(db, "first", creator=sysop, moderated=True)
    second = create_board(db, "second", creator=sysop, moderated=True)
    create_post(db, first, alice, "A", "x")
    create_post(db, second, alice, "B", "x")
    create_post(db, first, alice, "C", "x")
    create_post(db, second, alice, "D", "x")

    items, more = _load_pending_items(db, sysop)

    assert more and [(item.where, item.title) for item in items] == [("first", "A"), ("second", "B")]


def test_a_capped_board_queue_keeps_the_earliest_instant(db, sysop, alice):
    """Codex review on #795: the cap picks by instant, before the screen sorts."""
    from netbbs.boards.posts import list_pending_posts

    board = create_board(db, "general", creator=sysop, moderated=True)
    later = create_post(db, board, alice, "Later", "x")
    earlier = create_post(db, board, alice, "Earlier", "x")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", ("2026-01-01T00:00:00.000000Z", later.id))
    db.connection.execute("UPDATE posts SET created_at = ? WHERE id = ?", ("2026-01-01T03:00:00+05:00", earlier.id))
    db.connection.commit()

    assert [post.subject for post in list_pending_posts(db, board, requesting_user=sysop, limit=1)] == ["Earlier"]


def test_only_a_sysop_reads_the_node_wide_queue(db, alice):
    from netbbs.boards.posts import list_node_pending_posts
    from netbbs.files.entries import list_node_pending_files
    from netbbs.permissions.levels import InsufficientLevelError

    with pytest.raises(InsufficientLevelError):
        list_node_pending_posts(db, requesting_user=alice, limit=10)
    with pytest.raises(InsufficientLevelError):
        list_node_pending_files(db, requesting_user=alice, limit=10)
