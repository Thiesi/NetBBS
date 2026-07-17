"""
Tests for `netbbs.link.boards` — the local-origination bridge (design
doc round 124, round 128 wiring) turning an existing local board/post
into a signed `board_genesis`/`board_post` event.
"""

from __future__ import annotations

import json

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import approve_post, create_post, edit_post
from netbbs.link.boards import (
    LinkBoardsError,
    is_board_linked,
    link_board,
    load_own_board_events,
    queue_board_post_edit_if_linked,
    queue_board_post_if_linked,
)
from netbbs.link.events import BoardGenesis, BoardPostEdit
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.moderation.roles import BoardPermission, grant_permissions
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def node_identity():
    return bootstrap_node_identity("roanoke")


# -- link_board ---------------------------------------------------------------


def test_link_board_references_existing_board_id_not_a_new_one(db, alice, node_identity):
    board = create_board(db, "general", description="General discussion", creator=alice)

    genesis = link_board(db, board, node_identity=node_identity)

    assert genesis.payload["board_id"] == board.board_id
    assert genesis.payload["origin_fingerprint"] == node_identity.fingerprint


def test_link_board_persists_genesis_on_the_board_row(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    assert not is_board_linked(db, board)

    genesis = link_board(db, board, node_identity=node_identity)

    assert is_board_linked(db, board)
    row = db.connection.execute(
        "SELECT link_genesis_json FROM boards WHERE id = ?", (board.id,)
    ).fetchone()
    assert row["link_genesis_json"] is not None
    assert BoardGenesis.from_dict(json.loads(row["link_genesis_json"])).content_id == genesis.content_id


def test_link_board_refuses_to_relink_an_already_linked_board(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=node_identity)

    with pytest.raises(LinkBoardsError):
        link_board(db, board, node_identity=node_identity)


def test_link_board_carries_cascading_scalar_defaults(db, alice, node_identity):
    board = create_board(db, "general", creator=alice, min_read_level=0, min_write_level=1, moderated=True)

    genesis = link_board(
        db,
        board,
        node_identity=node_identity,
        default_min_read_level=0,
        default_min_write_level=1,
        default_moderated=True,
        default_max_post_age_days=90,
    )

    assert genesis.payload["default_min_read_level"] == 0
    assert genesis.payload["default_min_write_level"] == 1
    assert genesis.payload["default_moderated"] is True
    assert genesis.payload["default_max_post_age_days"] == 90


def test_link_board_defaults_description_to_the_boards_own(db, alice, node_identity):
    board = create_board(db, "general", description="General discussion", creator=alice)

    genesis = link_board(db, board, node_identity=node_identity)

    assert genesis.payload["description"] == "General discussion"


# -- queue_board_post_if_linked -------------------------------------------------


def test_queue_board_post_is_a_noop_when_board_is_not_linked(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "hello", "world")

    result = queue_board_post_if_linked(db, post, board, node_identity=node_identity)

    assert result is None
    row = db.connection.execute(
        "SELECT link_event_json FROM posts WHERE post_id = ?", (post.post_id,)
    ).fetchone()
    assert row["link_event_json"] is None


def test_queue_board_post_is_a_noop_for_a_still_pending_post(db, alice, node_identity):
    board = create_board(db, "general", creator=alice, moderated=True)
    link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")
    assert post.status == "pending"

    result = queue_board_post_if_linked(db, post, board, node_identity=node_identity)

    assert result is None


def test_queue_board_post_builds_and_persists_for_an_approved_post_on_a_linked_board(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")
    assert post.status == "approved"

    board_post = queue_board_post_if_linked(db, post, board, node_identity=node_identity)

    assert board_post is not None
    assert board_post.payload["board_id"] == board.board_id
    assert board_post.payload["author"] == {
        "kind": "node_vouched_user",
        "home_node_fingerprint": node_identity.fingerprint,
        "local_user_id": "alice",
    }
    row = db.connection.execute(
        "SELECT link_event_json FROM posts WHERE post_id = ?", (post.post_id,)
    ).fetchone()
    assert row["link_event_json"] is not None


def test_queue_board_post_after_moderated_approval(db, alice, node_identity):
    moderator = create_user(db, "modmin", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=alice, moderated=True)
    grant_permissions(
        db, moderator, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=alice
    )
    link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")
    approved = approve_post(db, post, approved_by=moderator)

    board_post = queue_board_post_if_linked(db, approved, board, node_identity=node_identity)

    assert board_post is not None


def test_queue_board_post_is_idempotent(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")

    first = queue_board_post_if_linked(db, post, board, node_identity=node_identity)
    second = queue_board_post_if_linked(db, post, board, node_identity=node_identity)

    assert first.content_id == second.content_id


def test_queue_board_post_links_parent_when_parent_is_itself_linked(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=node_identity)
    root = create_post(db, board, alice, "hello", "world")
    root_board_post = queue_board_post_if_linked(db, root, board, node_identity=node_identity)

    reply = create_post(db, board, alice, "re: hello", "reply body", parent_post_id=root.post_id)
    reply_board_post = queue_board_post_if_linked(db, reply, board, node_identity=node_identity)

    assert reply_board_post.payload["parent_post_id"] == root_board_post.content_id


def test_queue_board_post_omits_parent_when_parent_predates_linking(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    # root created *before* the board goes Linked -- no board_post of
    # its own, per round 124's "no backfill" decision.
    root = create_post(db, board, alice, "hello", "world")
    link_board(db, board, node_identity=node_identity)
    reply = create_post(db, board, alice, "re: hello", "reply body", parent_post_id=root.post_id)

    reply_board_post = queue_board_post_if_linked(db, reply, board, node_identity=node_identity)

    assert "parent_post_id" not in reply_board_post.payload


# -- queue_board_post_edit_if_linked (design doc round 129/130) ----------------


def test_queue_board_post_edit_builds_and_persists(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")
    board_post = queue_board_post_if_linked(db, post, board, node_identity=node_identity)

    edited = edit_post(db, post, board, subject="hello (edited)", body="world, edited", edited_by=alice)
    edit = queue_board_post_edit_if_linked(db, edited, board, node_identity=node_identity, edited_by=alice)

    assert edit is not None
    # root_post_id is the *Link event's* own content_id, not the local
    # post_id -- the two hash schemes are deliberately different (round
    # 124: full-envelope hash vs. the old flat-dict local scheme).
    assert edit.payload["root_post_id"] == board_post.content_id
    assert edit.payload["previous_event_id"] == board_post.content_id
    assert edit.payload["subject"] == "hello (edited)"
    row = db.connection.execute(
        "SELECT link_event_json FROM posts WHERE post_id = ?", (edited.post_id,)
    ).fetchone()
    assert row["link_event_json"] is not None


def test_queue_board_post_edit_chains_a_second_edit(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")
    board_post = queue_board_post_if_linked(db, post, board, node_identity=node_identity)

    first_edit = edit_post(db, post, board, subject="hello (v2)", body="world v2", edited_by=alice)
    first = queue_board_post_edit_if_linked(db, first_edit, board, node_identity=node_identity, edited_by=alice)

    second_edit = edit_post(db, first_edit, board, subject="hello (v3)", body="world v3", edited_by=alice)
    second = queue_board_post_edit_if_linked(db, second_edit, board, node_identity=node_identity, edited_by=alice)

    assert second.payload["previous_event_id"] == first.content_id
    assert second.payload["root_post_id"] == first.payload["root_post_id"] == board_post.content_id


def test_queue_board_post_edit_is_a_noop_for_a_moderator_edit(db, alice, node_identity):
    moderator = create_user(db, "modmin", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=alice)
    grant_permissions(
        db, moderator, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=alice
    )
    link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")
    queue_board_post_if_linked(db, post, board, node_identity=node_identity)

    edited = edit_post(db, post, board, subject="moderator changed this", body="world", edited_by=moderator)
    result = queue_board_post_edit_if_linked(db, edited, board, node_identity=node_identity, edited_by=moderator)

    assert result is None
    row = db.connection.execute(
        "SELECT link_event_json FROM posts WHERE post_id = ?", (edited.post_id,)
    ).fetchone()
    assert row["link_event_json"] is None


def test_queue_board_post_edit_is_a_noop_when_root_predates_linking(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "hello", "world")  # created before Linking
    link_board(db, board, node_identity=node_identity)

    edited = edit_post(db, post, board, subject="hello (edited)", body="world, edited", edited_by=alice)
    result = queue_board_post_edit_if_linked(db, edited, board, node_identity=node_identity, edited_by=alice)

    assert result is None


def test_queue_board_post_edit_is_idempotent(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")
    queue_board_post_if_linked(db, post, board, node_identity=node_identity)
    edited = edit_post(db, post, board, subject="hello (edited)", body="world, edited", edited_by=alice)

    first = queue_board_post_edit_if_linked(db, edited, board, node_identity=node_identity, edited_by=alice)
    second = queue_board_post_edit_if_linked(db, edited, board, node_identity=node_identity, edited_by=alice)

    assert first.content_id == second.content_id


def test_queue_board_post_edit_author_matches_root_post_exactly(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")
    board_post = queue_board_post_if_linked(db, post, board, node_identity=node_identity)
    edited = edit_post(db, post, board, subject="hello (edited)", body="world, edited", edited_by=alice)

    edit = queue_board_post_edit_if_linked(db, edited, board, node_identity=node_identity, edited_by=alice)

    assert edit.payload["author"] == board_post.payload["author"]


# -- load_own_board_events -------------------------------------------------------


def test_load_own_board_events_returns_genesis_and_posts(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    genesis = link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")
    board_post = queue_board_post_if_linked(db, post, board, node_identity=node_identity)

    events = load_own_board_events(db)

    content_ids = {e.content_id for e in events}
    assert genesis.content_id in content_ids
    assert board_post.content_id in content_ids


def test_load_own_board_events_includes_edits_and_distinguishes_them_by_type(db, alice, node_identity):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=node_identity)
    post = create_post(db, board, alice, "hello", "world")
    board_post = queue_board_post_if_linked(db, post, board, node_identity=node_identity)
    edited = edit_post(db, post, board, subject="hello (edited)", body="world, edited", edited_by=alice)
    edit = queue_board_post_edit_if_linked(db, edited, board, node_identity=node_identity, edited_by=alice)

    events = load_own_board_events(db)

    by_content_id = {e.content_id: e for e in events}
    assert isinstance(by_content_id[board_post.content_id], type(board_post))
    assert isinstance(by_content_id[edit.content_id], BoardPostEdit)


def test_load_own_board_events_empty_when_nothing_linked(db, alice):
    create_board(db, "general", creator=alice)

    assert load_own_board_events(db) == []
