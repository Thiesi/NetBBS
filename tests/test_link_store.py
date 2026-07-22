"""
Unit tests for `netbbs.link.store` (design doc round 120) — the
`link_peers`/`link_events` persistence functions `netbbs.link.
transport` dispatches through a `DatabaseLane`. These call the plain
`db`-first functions directly (no lane, no event loop, no real
transport) — `tests/test_link_transport.py` already proves the same
functions survive being called from a real `LinkServer`/`dial_hello`
over an actual socket; this file proves the storage logic itself in
isolation.
"""

from __future__ import annotations

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, edit_post
from netbbs.link.boards import link_board, queue_board_post_edit_if_linked, queue_board_post_if_linked
from netbbs.link.events import (
    build_board_genesis,
    build_board_post,
    build_board_post_edit,
    build_endpoint_descriptor,
    build_key_transition,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import PeerRecord
from netbbs.link.store import load_link_node, load_peer_last_contact, save_candidate_descriptor, save_event, save_peer
from netbbs.storage.database import Database


def _peer_record_for(identity, *, created_at: str = "2026-01-01T00:00:00+00:00") -> PeerRecord:
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key,
        subject_fingerprint=identity.fingerprint,
        addresses=None,
        outgoing_only=True,
        created_at=created_at,
    )
    return PeerRecord(
        fingerprint=identity.fingerprint,
        root_public_key=bytes(identity.root.verify_key),
        transitions=identity.transitions,
        descriptor=descriptor,
    )


def test_load_link_node_with_empty_tables_returns_an_empty_node(tmp_path):
    db = Database(tmp_path / "node.db")
    identity = bootstrap_node_identity("alice")

    node = load_link_node(db, identity)

    assert node.identity is identity
    assert node.peers == {}
    assert node.known_event_ids == set()
    assert node.events == {}
    assert node.boards == {}
    db.close()


def test_save_peer_then_load_link_node_reconstructs_it(tmp_path):
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    peer_identity = bootstrap_node_identity("bob")
    peer = _peer_record_for(peer_identity)

    save_peer(db, peer)
    node = load_link_node(db, own_identity)

    assert peer_identity.fingerprint in node.peers
    loaded = node.peers[peer_identity.fingerprint]
    assert loaded.root_public_key == peer.root_public_key
    assert [t.content_id for t in loaded.transitions] == [t.content_id for t in peer.transitions]
    assert loaded.descriptor.content_id == peer.descriptor.content_id
    db.close()


def test_load_peer_last_contact_reflects_saved_peers_only(tmp_path):
    db = Database(tmp_path / "node.db")
    peer_identity = bootstrap_node_identity("bob")
    peer = _peer_record_for(peer_identity)

    save_peer(db, peer)
    last_contact = load_peer_last_contact(db)

    assert peer_identity.fingerprint in last_contact
    assert last_contact[peer_identity.fingerprint]  # non-empty ISO timestamp string
    assert "never-saved-fingerprint" not in last_contact
    db.close()


def test_save_peer_upserts_the_latest_transitions_on_conflict(tmp_path):
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    peer_identity = bootstrap_node_identity("bob")
    peer = _peer_record_for(peer_identity)

    save_peer(db, peer)

    extra = build_key_transition(
        root=peer_identity.root,
        purpose="signing",
        action="revoke",
        operational_key=peer_identity.signing_key.verify_key,
        previous_transition_id=peer.transitions[0].content_id,
        created_at="2026-01-02T00:00:00+00:00",
    )
    updated_peer = PeerRecord(
        fingerprint=peer.fingerprint,
        root_public_key=peer.root_public_key,
        transitions=peer.transitions + (extra,),
        descriptor=peer.descriptor,
    )
    save_peer(db, updated_peer)

    node = load_link_node(db, own_identity)
    loaded = node.peers[peer_identity.fingerprint]
    assert len(loaded.transitions) == len(peer.transitions) + 1
    assert loaded.transitions[-1].content_id == extra.content_id

    rows = db.connection.execute("SELECT COUNT(*) AS n FROM link_peers").fetchone()
    assert rows["n"] == 1  # upsert, not a second row
    db.close()


def test_save_event_then_load_link_node_reconstructs_dedup_and_body(tmp_path):
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    sender_identity = bootstrap_node_identity("bob")
    transition = sender_identity.transitions[0]

    save_event(
        db,
        sender_fingerprint=sender_identity.fingerprint,
        content_id=transition.content_id,
        object_type="key_transition",
        envelope=transition.to_dict(),
    )

    node = load_link_node(db, own_identity)
    assert transition.content_id in node.known_event_ids
    assert node.events[transition.content_id] == transition.to_dict()
    db.close()


def test_save_event_on_conflict_does_nothing(tmp_path):
    """A resend of an already-stored event must never overwrite the
    original row -- matches `handle_events`' own dedup guard, which
    means `save_event` is only ever called once per content_id in
    practice; `ON CONFLICT ... DO NOTHING` here is a defensive
    backstop, not the primary mechanism (round 120)."""
    db = Database(tmp_path / "node.db")
    sender_identity = bootstrap_node_identity("bob")
    transition = sender_identity.transitions[0]

    save_event(
        db,
        sender_fingerprint=sender_identity.fingerprint,
        content_id=transition.content_id,
        object_type="key_transition",
        envelope=transition.to_dict(),
    )
    save_event(
        db,
        sender_fingerprint=sender_identity.fingerprint,
        content_id=transition.content_id,
        object_type="key_transition",
        envelope={"tampered": True},
    )

    row = db.connection.execute(
        "SELECT envelope_json FROM link_events WHERE content_id = ?", (transition.content_id,)
    ).fetchone()
    assert "tampered" not in row["envelope_json"]
    db.close()


def test_save_board_genesis_event_then_load_link_node_reconstructs_boards(tmp_path):
    """Round 126: `LinkServer._handle_events` already persists any
    accepted event generically, board_genesis included -- proves the
    other half actually works: a restarted node reconstructs `node.
    boards` from those rows too, not just `known_event_ids`/`events`,
    so a resent board_post for that board_id isn't wrongly rejected
    after a restart as having no verified genesis on file."""
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    origin_identity = bootstrap_node_identity("bob")

    genesis = build_board_genesis(
        signing_identity=origin_identity.signing_key,
        origin_fingerprint=origin_identity.fingerprint,
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at="2026-01-01T00:00:00+00:00",
    )
    save_event(
        db,
        sender_fingerprint=origin_identity.fingerprint,
        content_id=genesis.content_id,
        object_type="board_genesis",
        envelope=genesis.to_dict(),
    )

    node = load_link_node(db, own_identity)

    assert genesis.content_id in node.known_event_ids
    assert "existing-local-board-id" in node.boards
    assert node.boards["existing-local-board-id"].content_id == genesis.content_id
    db.close()


def test_save_board_post_event_then_load_link_node_does_not_populate_boards(tmp_path):
    """A board_post is content, not a board announcement -- only
    board_genesis rows should ever populate `node.boards`."""
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    author_identity = bootstrap_node_identity("bob")

    post = build_board_post(
        signing_identity=author_identity.signing_key,
        home_node_fingerprint=author_identity.fingerprint,
        local_user_id="wanderer",
        board_id="existing-local-board-id",
        subject="hello",
        body="world",
        created_at="2026-01-01T00:00:00+00:00",
    )
    save_event(
        db,
        sender_fingerprint=author_identity.fingerprint,
        content_id=post.content_id,
        object_type="board_post",
        envelope=post.to_dict(),
    )

    node = load_link_node(db, own_identity)

    assert post.content_id in node.known_event_ids
    assert node.boards == {}
    db.close()


def test_load_link_node_reconstructs_self_originated_board_genesis(tmp_path):
    """Round 128: a board this node itself originated (`netbbs.link.
    boards.link_board`) never goes through `handle_events`/`link_
    events` at all -- its genesis lives only on the local `boards`
    row's own `link_genesis_json` column. Proves `load_link_node`
    reconstructs `node.boards` from *that* source too, not just from
    peer-received `link_events` rows (the other half tested above) --
    without this, a restarted node would forget its own Linked boards
    and wrongly reject a remote user's legitimate board_post on one."""
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    creator = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=creator)
    genesis = link_board(db, board, node_identity=own_identity)

    node = load_link_node(db, own_identity)

    assert board.board_id in node.boards
    assert node.boards[board.board_id].content_id == genesis.content_id
    db.close()


def test_load_link_node_reconstructs_self_originated_board_post_edit(tmp_path):
    """Round 130: a self-authored edit (`netbbs.link.boards.queue_
    board_post_edit_if_linked`) also never goes through `handle_events`
    -- it lives only on the edited revision's own `posts.link_event_
    json` column. Proves `load_link_node` reconstructs `node.post_
    edits` from that source too."""
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    creator = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=creator)
    link_board(db, board, node_identity=own_identity)
    post = create_post(db, board, creator, "hello", "world")
    board_post = queue_board_post_if_linked(db, post, board, node_identity=own_identity)
    edited = edit_post(db, post, board, subject="hello (edited)", body="world, edited", edited_by=creator)
    edit = queue_board_post_edit_if_linked(db, edited, board, node_identity=own_identity, edited_by=creator)

    node = load_link_node(db, own_identity)

    assert board_post.content_id in node.post_edits
    assert [e.content_id for e in node.post_edits[board_post.content_id]] == [edit.content_id]
    db.close()


def test_load_link_node_reconstructs_self_originated_edit_chain_when_created_at_ties(tmp_path, monkeypatch):
    """Issue #11's "deterministic ordering when timestamps tie" question,
    applied to restart reconstruction: two real successive `edit_post`
    calls *have* landed on the identical microsecond before (see
    `test_list_posts_page_returns_all_in_order`'s own comment in
    tests/test_boards.py), so this isn't a hypothetical. Unlike the
    peer-received loop above (already ordered by the locally-assigned
    `received_at`), the self-originated loop sorted only by the
    payload's own `created_at` -- a tie there left SQLite's row order
    unspecified, risking `node.post_edits` reconstructing in the wrong
    causal order after a restart. Forces the tie via `monkeypatch` (real
    wall-clock timing can't be relied on to reproduce it on demand) and
    confirms the chain still reconstructs in true creation order."""
    frozen = "2026-01-01T00:00:00.000000+00:00"
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: frozen)

    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    creator = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=creator)
    link_board(db, board, node_identity=own_identity)
    post = create_post(db, board, creator, "hello", "world")
    board_post = queue_board_post_if_linked(db, post, board, node_identity=own_identity)

    first_edited = edit_post(db, post, board, subject="v2", body="v2", edited_by=creator)
    first_edit = queue_board_post_edit_if_linked(
        db, first_edited, board, node_identity=own_identity, edited_by=creator
    )
    second_edited = edit_post(db, first_edited, board, subject="v3", body="v3", edited_by=creator)
    second_edit = queue_board_post_edit_if_linked(
        db, second_edited, board, node_identity=own_identity, edited_by=creator
    )

    node = load_link_node(db, own_identity)

    assert [e.content_id for e in node.post_edits[board_post.content_id]] == [
        first_edit.content_id,
        second_edit.content_id,
    ]
    db.close()


def test_load_link_node_reconstructs_peer_received_board_post_edit_chain_in_order(tmp_path):
    """Round 130: peer-received board_post_edit rows must reconstruct
    in the order they were originally accepted, or the chain's own
    'does previous_event_id match the current head' invariant would be
    violated the moment it's used again."""
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    sender_identity = bootstrap_node_identity("bob")
    post = build_board_post(
        signing_identity=sender_identity.signing_key,
        home_node_fingerprint=sender_identity.fingerprint,
        local_user_id="wanderer",
        board_id="existing-local-board-id",
        subject="hello",
        body="world",
        created_at="2026-01-01T00:00:00+00:00",
    )
    save_event(
        db, sender_fingerprint=sender_identity.fingerprint, content_id=post.content_id,
        object_type="board_post", envelope=post.to_dict(),
    )

    first_edit = build_board_post_edit(
        signing_identity=sender_identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=post.content_id,
        subject="hello (v2)",
        body="world v2",
        created_at="2026-01-01T00:01:00+00:00",
    )
    save_event(
        db, sender_fingerprint=sender_identity.fingerprint, content_id=first_edit.content_id,
        object_type="board_post_edit", envelope=first_edit.to_dict(),
    )
    second_edit = build_board_post_edit(
        signing_identity=sender_identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=first_edit.content_id,
        subject="hello (v3)",
        body="world v3",
        created_at="2026-01-01T00:02:00+00:00",
    )
    save_event(
        db, sender_fingerprint=sender_identity.fingerprint, content_id=second_edit.content_id,
        object_type="board_post_edit", envelope=second_edit.to_dict(),
    )

    node = load_link_node(db, own_identity)

    assert [e.content_id for e in node.post_edits[post.content_id]] == [
        first_edit.content_id,
        second_edit.content_id,
    ]
    db.close()


# -- peer-list candidates (design doc round 95) ------------------------------


def test_save_candidate_descriptor_then_load_link_node_reconstructs_it(tmp_path):
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    candidate_identity = bootstrap_node_identity("carol")
    descriptor = build_endpoint_descriptor(
        signing_identity=candidate_identity.signing_key,
        subject_fingerprint=candidate_identity.fingerprint,
        addresses=[{"protocol": "http", "address": "203.0.113.1", "port": 7862}],
        outgoing_only=False,
        created_at="2026-01-01T00:00:00+00:00",
    )

    save_candidate_descriptor(db, candidate_identity.fingerprint, descriptor)
    node = load_link_node(db, own_identity)

    assert node.candidate_descriptors[candidate_identity.fingerprint].content_id == descriptor.content_id
    db.close()


def test_save_candidate_descriptor_upserts_on_conflict(tmp_path):
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    candidate_identity = bootstrap_node_identity("carol")
    first = build_endpoint_descriptor(
        signing_identity=candidate_identity.signing_key,
        subject_fingerprint=candidate_identity.fingerprint,
        addresses=[{"protocol": "http", "address": "203.0.113.1", "port": 7862}],
        outgoing_only=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    second = build_endpoint_descriptor(
        signing_identity=candidate_identity.signing_key,
        subject_fingerprint=candidate_identity.fingerprint,
        addresses=[{"protocol": "http", "address": "203.0.113.2", "port": 7862}],
        outgoing_only=False,
        created_at="2026-01-02T00:00:00+00:00",
    )

    save_candidate_descriptor(db, candidate_identity.fingerprint, first)
    save_candidate_descriptor(db, candidate_identity.fingerprint, second)
    node = load_link_node(db, own_identity)

    assert node.candidate_descriptors[candidate_identity.fingerprint].content_id == second.content_id
    assert db.connection.execute("SELECT COUNT(*) FROM link_peer_candidates").fetchone()[0] == 1
    db.close()


def test_save_peer_clears_a_matching_on_disk_candidate(tmp_path):
    """Mirrors `LinkNode.handle_hello`'s own in-memory candidate
    cleanup (round 95) -- a fingerprint that becomes a real verified
    peer must not also resurrect as a stale candidate after a
    restart."""
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    peer_identity = bootstrap_node_identity("bob")
    descriptor = build_endpoint_descriptor(
        signing_identity=peer_identity.signing_key,
        subject_fingerprint=peer_identity.fingerprint,
        addresses=[{"protocol": "http", "address": "203.0.113.1", "port": 7862}],
        outgoing_only=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    save_candidate_descriptor(db, peer_identity.fingerprint, descriptor)

    peer = PeerRecord(
        fingerprint=peer_identity.fingerprint,
        root_public_key=bytes(peer_identity.root.verify_key),
        transitions=peer_identity.transitions,
        descriptor=descriptor,
    )
    save_peer(db, peer)

    node = load_link_node(db, own_identity)
    assert peer_identity.fingerprint in node.peers
    assert peer_identity.fingerprint not in node.candidate_descriptors
    assert db.connection.execute("SELECT COUNT(*) FROM link_peer_candidates").fetchone()[0] == 0
    db.close()
