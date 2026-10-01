"""
Who may post on a Linked board (design doc §9.3, issue #993): the origin's
signed `board_posting` setting, binding on every node that carries the board;
and a closed board refusing carried posts too (§9.5, issue #1021).
"""

from __future__ import annotations

import json

import pytest

from netbbs.access_map import GateKind, list_gates
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.link.boards import (
    LinkBoardsError,
    board_posting_mode,
    link_board,
    load_own_board_events,
    materialize_carried_board,
    materialize_carried_board_closure,
    materialize_carried_board_posting,
    materialize_carried_post,
    posting_here,
    posting_refusal,
    rebuild_carried_post_materialization,
    record_board_origin_change,
    set_board_posting,
)
from netbbs.link.events import (
    BoardPosting,
    build_board_closure,
    build_board_genesis,
    build_board_post,
    build_board_posting,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode, LinkProtocolError
from netbbs.storage.database import Database
from tests.test_link_protocol import _hello_bytes, _linked_board, clock  # noqa: F401 -- clock is a fixture
from tests.link_harness import spawn_node


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2pw", user_level=SYSOP_LEVEL)


@pytest.fixture
def here():
    return bootstrap_node_identity("here")


@pytest.fixture
def origin():
    return bootstrap_node_identity("origin")


@pytest.fixture
def third():
    return bootstrap_node_identity("third")


def _carried(db, origin, board_id="announce-id"):
    genesis = build_board_genesis(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        board_id=board_id, name="Announcements", created_at="2026-01-01T00:00:00Z",
    )
    return materialize_carried_board(db, genesis)


def _post(author, board_id="announce-id", **kwargs):
    return build_board_post(
        signing_identity=author.signing_key, home_node_fingerprint=author.fingerprint,
        local_user_id="someone", board_id=board_id, subject=kwargs.pop("subject", "news"),
        body="body", created_at=kwargs.pop("created_at", "2026-01-02T00:00:00Z"), **kwargs,
    )


def _setting(origin, posting, created_at, board_id="announce-id"):
    return build_board_posting(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        board_id=board_id, posting=posting, created_at=created_at,
    )


def _shown(db, post) -> bool:
    return db.connection.execute("SELECT 1 FROM posts WHERE post_id = ?", (post.content_id,)).fetchone() is not None


# -- the origin sets it


def test_the_origin_sets_who_posts_and_re_pushes_it(db, alice, here):
    board = create_board(db, "news", creator=alice)
    link_board(db, board, node_identity=here)

    event = set_board_posting(db, board, "origin_only", node_identity=here)

    assert board_posting_mode(db, board) == "origin_only"
    own = [e for e in load_own_board_events(db, here.fingerprint) if isinstance(e, BoardPosting)]
    assert [e.content_id for e in own] == [event.content_id]
    # The origin's own callers still post.
    assert posting_here(db, board, own_fingerprint=here.fingerprint) == "all"


def test_only_the_current_origin_sets_it(db, alice, here, origin):
    board = create_board(db, "news", creator=alice)
    with pytest.raises(LinkBoardsError, match="not Linked"):
        set_board_posting(db, board, "origin_only", node_identity=here)
    link_board(db, board, node_identity=here)
    with pytest.raises(LinkBoardsError, match="unknown posting mode"):
        set_board_posting(db, board, "nobody", node_identity=here)
    record_board_origin_change(db, board.board_id, origin.fingerprint)
    with pytest.raises(LinkBoardsError, match="not board 'news'"):
        set_board_posting(db, board, "origin_only", node_identity=here)


# -- a carrying node applies it


def test_the_latest_setting_wins(db, origin):
    board = _carried(db, origin)

    materialize_carried_board_posting(db, _setting(origin, "origin_only", "2026-01-03T00:00:00Z"))
    materialize_carried_board_posting(db, _setting(origin, "anyone", "2026-01-02T00:00:00Z"))  # older

    assert board_posting_mode(db, board) == "origin_only"
    materialize_carried_board_posting(db, _setting(origin, "origin_threads", "2026-01-04T00:00:00Z"))
    assert board_posting_mode(db, board) == "origin_threads"


def test_origin_only_keeps_other_nodes_posts_and_replies_out(db, origin, third):
    _carried(db, origin)
    materialize_carried_board_posting(db, _setting(origin, "origin_only", "2026-01-01T12:00:00Z"))
    root = _post(origin)
    materialize_carried_post(db, root, sender_fingerprint=origin.fingerprint)
    stranger = _post(third, subject="spam")
    reply = _post(third, subject="re", parent_post_id=root.content_id)

    assert materialize_carried_post(db, stranger, sender_fingerprint=origin.fingerprint) is None
    assert materialize_carried_post(db, reply, sender_fingerprint=origin.fingerprint) is None
    assert _shown(db, root) and not _shown(db, stranger) and not _shown(db, reply)


def test_origin_threads_lets_replies_in_but_not_new_threads(db, origin, third):
    _carried(db, origin)
    materialize_carried_board_posting(db, _setting(origin, "origin_threads", "2026-01-01T12:00:00Z"))
    root = _post(origin)
    materialize_carried_post(db, root, sender_fingerprint=origin.fingerprint)
    reply = _post(third, subject="re", parent_post_id=root.content_id)
    thread = _post(third, subject="my own thread")

    assert materialize_carried_post(db, reply, sender_fingerprint=third.fingerprint) is not None
    assert materialize_carried_post(db, thread, sender_fingerprint=third.fingerprint) is None


def test_the_rule_follows_the_current_origin(db, origin, third):
    _carried(db, origin)
    materialize_carried_board_posting(db, _setting(origin, "origin_only", "2026-01-01T12:00:00Z"))
    record_board_origin_change(db, "announce-id", third.fingerprint)

    assert materialize_carried_post(db, _post(third), sender_fingerprint=third.fingerprint) is not None
    assert materialize_carried_post(db, _post(origin, subject="late"), sender_fingerprint=origin.fingerprint) is None


def test_a_refused_post_is_kept_and_a_rebuild_does_not_bring_it_back(db, origin, third):
    _carried(db, origin)
    materialize_carried_board_posting(db, _setting(origin, "origin_only", "2026-01-01T12:00:00Z"))
    stranger = _post(third)
    materialize_carried_post(db, stranger, sender_fingerprint=third.fingerprint)

    kept = db.connection.execute("SELECT 1 FROM link_events WHERE content_id = ?", (stranger.content_id,)).fetchone()
    assert kept is not None
    rebuild_carried_post_materialization(db)
    assert not _shown(db, stranger)


def test_a_closed_board_refuses_carried_posts(db, origin, third):
    """Issue #1021: closure stopped only this node's own callers."""
    board = _carried(db, origin)
    genesis_id = json.loads(db.connection.execute(
        "SELECT link_genesis_json FROM boards WHERE id = ?", (board.id,)
    ).fetchone()[0])
    from netbbs.link.events import event_content_id

    closure = build_board_closure(
        signing_identity=origin.signing_key, board_id="announce-id",
        previous_event_id=event_content_id(genesis_id["envelope"]), reason=None,
        created_at="2026-01-03T00:00:00Z",
    )
    materialize_carried_board_closure(db, closure)

    assert materialize_carried_post(db, _post(third), sender_fingerprint=third.fingerprint) is None
    assert materialize_carried_post(db, _post(origin), sender_fingerprint=origin.fingerprint) is None


# -- this node's own callers


def test_local_callers_on_a_carrying_node_are_told_before_writing(db, here, origin):
    board = _carried(db, origin)
    materialize_carried_board_posting(db, _setting(origin, "origin_threads", "2026-01-01T12:00:00Z"))

    assert posting_here(db, board, own_fingerprint=here.fingerprint) == "replies"
    assert posting_refusal(db, board, own_fingerprint=here.fingerprint, is_reply=True) is None
    assert "starts threads" in posting_refusal(db, board, own_fingerprint=here.fingerprint, is_reply=False)

    materialize_carried_board_posting(db, _setting(origin, "origin_only", "2026-01-02T12:00:00Z"))
    assert "Only the board's origin node posts" in posting_refusal(
        db, board, own_fingerprint=here.fingerprint, is_reply=True,
    )


def test_the_access_map_names_the_posting_setting(db, origin):
    _carried(db, origin)
    write = next(g for g in list_gates(db) if g.kind is GateKind.BOARD_WRITE)
    assert write.note == "local callers only"

    materialize_carried_board_posting(db, _setting(origin, "origin_only", "2026-01-02T12:00:00Z"))
    write = next(g for g in list_gates(db) if g.kind is GateKind.BOARD_WRITE)
    assert write.note == "origin's callers only"


# -- the protocol


def test_handle_events_accepts_the_origins_setting_and_refuses_anyone_elses(tmp_path, clock):  # noqa: F811
    alice = spawn_node(tmp_path, "alice")
    mallory = spawn_node(tmp_path, "mallory")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=mallory.identity), clock=clock))
    _linked_board(alice, bob_node, clock)

    good = build_board_posting(
        signing_identity=alice.identity.signing_key, origin_fingerprint=alice.fingerprint,
        board_id="existing-local-board-id", posting="origin_only", created_at=clock.now_iso(),
    )
    assert bob_node.handle_events(alice.fingerprint, [good.to_dict()]) == [good.content_id]

    as_herself = build_board_posting(
        signing_identity=mallory.identity.signing_key, origin_fingerprint=mallory.fingerprint,
        board_id="existing-local-board-id", posting="anyone", created_at=clock.now_iso(),
    )
    with pytest.raises(LinkProtocolError, match="not from its current origin"):
        bob_node.handle_events(mallory.fingerprint, [as_herself.to_dict()])

    forged = build_board_posting(
        signing_identity=mallory.identity.signing_key, origin_fingerprint=alice.fingerprint,
        board_id="existing-local-board-id", posting="anyone", created_at=clock.now_iso(),
    )
    with pytest.raises(LinkProtocolError, match="does not verify"):
        bob_node.handle_events(mallory.fingerprint, [forged.to_dict()])

    unknown = good.to_dict()
    unknown["envelope"] = json.loads(json.dumps(unknown["envelope"]))
    unknown["envelope"]["payload"]["posting"] = "nobody"
    with pytest.raises(LinkProtocolError, match="unknown posting mode"):
        bob_node.handle_events(alice.fingerprint, [unknown])

    alice.close()
    mallory.close()


def test_the_board_screen_offers_no_post_key_and_says_why(db, origin):
    import asyncio

    from netbbs.net import board_flow
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled
    from tests.test_board_list_and_reader import FakeSession

    board = _carried(db, origin)
    materialize_carried_board_posting(db, _setting(origin, "origin_only", "2026-01-01T12:00:00Z"))
    reader = create_user(db, "reader", password="hunter2pw", user_level=10)
    set_redraw_in_place_enabled(db, reader, True)
    session = FakeSession(["b"])

    asyncio.run(board_flow._show_board(session, db, board, reader))

    screen = session.screens()[0]
    assert "Only the board's origin node posts on this board." in " ".join(screen.split())
    assert "[P]ost" not in screen
