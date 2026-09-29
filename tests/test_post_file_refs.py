"""
A board post that points at a file in a file area (issue #842, F086).

Built on mail's file references (issue #830, `netbbs.file_refs`): each post
revision has its own rows in `post_file_refs`, written with it, removed with
it by every hard delete, and named in text -- never as a reference -- in the
post's Link event. The screens run through the board reader's `FakeSession`.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board, delete_board
from netbbs.boards.posts import (
    PostError,
    create_post,
    delete_post,
    edit_post,
    shown_post_refs,
    sweep_expired_posts,
    withdraw_post,
)
from netbbs.file_refs import (
    AVAILABLE,
    MAX_FILE_REFS,
    open_ref,
    post_refs,
    ref_for_entry,
    refs_some_readers_cannot_open,
)
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.link.boards import (
    link_board,
    queue_board_post_edit_if_linked,
    queue_board_post_if_linked,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.net import board_flow
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.storage.database import Database
from tests.test_board_list_and_reader import FakeSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _user(db, name, **kwargs):
    kwargs.setdefault("user_level", 10)
    user = create_user(db, name, password="hunter2pw", **kwargs)
    set_redraw_in_place_enabled(db, user, True)
    return user


def _file(db, owner, *, area_name="Practice pages", filename="page.png", data=b"payload", **area_kwargs):
    area = create_file_area(db, area_name, creator=owner, **area_kwargs)
    entry = upload_file(db, area, owner, filename, data)
    return area, entry, ref_for_entry(entry, area)


def _set_area(db, area, **columns):
    for column, value in columns.items():
        db.connection.execute(f"UPDATE file_areas SET {column} = ? WHERE id = ?", (value, area.id))
    db.connection.commit()


def _ref_rows(db):
    return db.connection.execute("SELECT * FROM post_file_refs ORDER BY post_id, position").fetchall()


# -- the post and its revisions -----------------------------------------------


def test_a_post_points_at_a_file_and_its_reader_can_open_it(db):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    board = create_board(db, "Critique", creator=alice)
    _area, _entry, ref = _file(db, alice)

    post = create_post(db, board, alice, "My page", "What do you think?", files=[ref])

    assert post_refs(db, post.post_id) == [ref]
    assert shown_post_refs(db, post) == [ref]
    assert open_ref(db, bob, ref).state == AVAILABLE
    # The body stays what the author wrote.
    assert post.body == "What do you think?"


def test_the_author_cannot_point_at_a_file_they_cannot_open(db):
    root, alice = _user(db, "root", user_level=255), _user(db, "alice")
    board = create_board(db, "Critique", creator=root)
    _area, _entry, ref = _file(db, root, area_name="Staff", min_read_level=50)

    with pytest.raises(PostError, match="no longer available to you.*from the post, then publish it"):
        create_post(db, board, alice, "Look", "x", files=[ref])
    assert _ref_rows(db) == []


def test_more_files_than_the_limit_are_refused(db):
    alice = _user(db, "alice")
    board = create_board(db, "Critique", creator=alice)
    area = create_file_area(db, "Pages", creator=alice)
    refs = [
        ref_for_entry(upload_file(db, area, alice, f"p{i}.png", f"data{i}".encode()), area)
        for i in range(MAX_FILE_REFS + 1)
    ]

    with pytest.raises(PostError, match=f"A post can point at {MAX_FILE_REFS} files at most"):
        create_post(db, board, alice, "Look", "x", files=refs)


def test_an_edit_keeps_the_files_unless_it_changes_them(db):
    alice = _user(db, "alice")
    moderator = _user(db, "mod", user_level=255)
    board = create_board(db, "Critique", creator=alice)
    area, _entry, ref = _file(db, alice)
    other = ref_for_entry(upload_file(db, area, alice, "second.png", b"other"), area)
    post = create_post(db, board, alice, "My page", "v1", files=[ref])

    # A moderator's edit, which deals in no files, keeps them.
    moderated = edit_post(db, post, board, subject="My page", body="v2", edited_by=moderator)
    assert post_refs(db, moderated.post_id) == [ref]
    assert shown_post_refs(db, post) == [ref]

    # The author changes only the files: still a new revision.
    changed = edit_post(db, post, board, subject="My page", body="v2", edited_by=alice, files=[other])
    assert changed.post_id != moderated.post_id
    assert shown_post_refs(db, post) == [other]
    # Earlier revisions keep their own.
    assert post_refs(db, post.post_id) == [ref]


def test_an_edit_keeps_a_file_gone_since_but_refuses_a_new_one_the_author_cannot_open(db):
    root, alice = _user(db, "root", user_level=255), _user(db, "alice")
    board = create_board(db, "Critique", creator=alice)
    area, _entry, ref = _file(db, alice)
    post = create_post(db, board, alice, "My page", "v1", files=[ref])
    _set_area(db, area, min_read_level=50)  # closed to alice since

    kept = edit_post(db, post, board, subject="My page", body="v2", edited_by=alice, files=[ref])
    assert post_refs(db, kept.post_id) == [ref]

    _staff, _entry2, staff_ref = _file(db, root, area_name="Staff", filename="s.png", min_read_level=50)
    with pytest.raises(PostError, match="no longer available to you"):
        edit_post(db, post, board, subject="My page", body="v3", edited_by=alice, files=[ref, staff_ref])


def test_a_withdrawn_post_points_at_nothing(db):
    alice = _user(db, "alice")
    board = create_board(db, "Critique", creator=alice)
    _area, _entry, ref = _file(db, alice)
    post = create_post(db, board, alice, "My page", "v1", files=[ref])

    withdrawn = withdraw_post(db, post, board, withdrawn_by=alice)

    assert post_refs(db, withdrawn.post_id) == []
    assert shown_post_refs(db, post) == []


def test_references_go_with_a_rejected_post_an_expired_one_and_a_deleted_board(db, monkeypatch):
    alice, mod = _user(db, "alice"), _user(db, "mod", user_level=255)
    moderated = create_board(db, "Held", creator=mod, moderated=True)
    _area, _entry, ref = _file(db, alice)

    held = create_post(db, moderated, alice, "Look", "x", files=[ref])
    assert len(_ref_rows(db)) == 1
    delete_post(db, held, deleted_by=mod)
    assert _ref_rows(db) == []

    expiring = create_board(db, "Short", creator=mod, max_post_age_days=1)
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: "2020-01-01T00:00:00.000000Z")
    create_post(db, expiring, alice, "Old", "x", files=[ref])
    monkeypatch.undo()
    assert len(_ref_rows(db)) == 1
    sweep_expired_posts(db, expiring)  # expired and past its grace: deleted
    assert db.connection.execute("SELECT COUNT(*) FROM posts WHERE board_id = ?", (expiring.id,)).fetchone()[0] == 0
    assert _ref_rows(db) == []

    board = create_board(db, "Gone", creator=mod)
    create_post(db, board, alice, "Look", "x", files=[ref])
    assert _ref_rows(db)
    delete_board(db, board, deleted_by=mod)
    assert _ref_rows(db) == []


def test_files_in_a_stricter_area_are_named_for_the_review_screen(db):
    alice = _user(db, "alice", user_level=50)
    board = create_board(db, "Critique", creator=alice)
    _open_area, _entry, open_ref_ = _file(db, alice)
    _staff, _entry2, staff_ref = _file(db, alice, area_name="Members", filename="m.png", min_read_level=20)

    assert refs_some_readers_cannot_open(db, [open_ref_, staff_ref], board) == [staff_ref]


# -- over Link: text, never a reference ---------------------------------------


def test_a_linked_post_and_its_edit_name_their_files_in_text(db):
    identity = bootstrap_node_identity("roanoke")
    alice = _user(db, "alice")
    board = create_board(db, "Critique", creator=alice)
    link_board(db, board, node_identity=identity)
    _area, _entry, ref = _file(db, alice)

    post = create_post(db, board, alice, "My page", "What do you think?", files=[ref])
    event = queue_board_post_if_linked(db, post, board, node_identity=identity)

    line = 'File: page.png (7 B) in file area "Practice pages" on NetBBS'
    assert event.payload["body"] == f"What do you think?\n\n{line}"
    # Locally the body is what the author wrote; the file is a reference.
    assert db.connection.execute("SELECT body FROM posts WHERE post_id = ?", (post.post_id,)).fetchone()[0] == (
        "What do you think?"
    )

    edited = edit_post(db, post, board, subject="My page", body="Second try.", edited_by=alice)
    edit = queue_board_post_edit_if_linked(db, edited, board, node_identity=identity, edited_by=alice)
    assert edit.payload["body"] == f"Second try.\n\n{line}"

    removed = edit_post(db, post, board, subject="My page", body="No file now.", edited_by=alice, files=[])
    edit = queue_board_post_edit_if_linked(db, removed, board, node_identity=identity, edited_by=alice)
    assert edit.payload["body"] == "No file now."


# -- the screens --------------------------------------------------------------


def _screens(session):
    return "\n".join(session.screens())


def test_the_reader_lists_the_files_and_get_file_downloads_one(db, monkeypatch):
    alice, bob = _user(db, "alice"), _user(db, "bob")
    board = create_board(db, "Critique", creator=alice)
    _area, entry, ref = _file(db, alice)
    create_post(db, board, alice, "My page", "What do you think?", files=[ref])
    downloads = []

    async def fake_send(session_, lane_, area, entry_, user, **kwargs):
        downloads.append((area.name, entry_.file_id, user.username))
        return False

    monkeypatch.setattr("netbbs.net.file_ref_view.send_file_to_caller", fake_send)
    session = FakeSession(["1", "g", "b", "b"], width=120)

    asyncio.run(board_flow._show_board(session, db, board, bob))

    text = _screens(session)
    assert "Files:" in text and "page.png  7 B in Practice pages" in text
    assert "[G]et file" in text
    assert downloads == [("Practice pages", entry.file_id, "bob")]


def test_a_reader_who_cannot_open_the_area_is_not_told_the_files_name(db):
    root, alice, bob = _user(db, "root", user_level=255), _user(db, "alice", user_level=50), _user(db, "bob")
    board = create_board(db, "Critique", creator=alice)
    _area, _entry, ref = _file(db, root, area_name="Members lounge", filename="secret-plans.png", min_read_level=50)
    create_post(db, board, alice, "Look", "x", files=[ref])

    session = FakeSession(["1", "g", "b", "b"], width=120)
    asyncio.run(board_flow._show_board(session, db, board, bob))

    text = _screens(session)
    assert "A file in a file area you can't open" in text
    assert "secret-plans" not in text and "Members lounge" not in text
    assert "None of the files in this post is available to you." in "".join(session.written)


def test_attach_a_file_on_review_and_publish_it(db):
    alice = _user(db, "alice")
    board = create_board(db, "Critique", creator=alice)
    _area, _entry, ref = _file(db, alice)
    # [P]ost, subject, one line, /done; on review [A]ttach, area 01, file 01; [P]ost; Back.
    session = FakeSession(
        ["p", "My page", "What do you think?", "/done", "a", "0", "1", "0", "1", "p", "b"], width=120,
    )

    asyncio.run(board_flow._show_board(session, db, board, alice))

    assert "Attached page.png." in "".join(session.written)
    post_id = db.connection.execute("SELECT post_id FROM posts").fetchone()[0]
    assert post_refs(db, post_id) == [ref]


def test_a_held_post_shows_its_files_to_the_moderator(db, tmp_path):
    from netbbs.net.admin_flow import _post_action_screen
    from netbbs.storage.execution import DatabaseLane

    alice, mod = _user(db, "alice"), _user(db, "mod", user_level=255)
    board = create_board(db, "Held", creator=mod, moderated=True)
    _area, _entry, ref = _file(db, alice)
    held = create_post(db, board, alice, "Look", "x", files=[ref])

    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["b"], width=120)
        asyncio.run(_post_action_screen(session, lane, mod, held, board))
    finally:
        lane.close()

    assert "page.png  7 B in Practice pages" in _screens(session)
