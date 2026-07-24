"""
Unit tests for `netbbs.link.store` — the
`link_peers`/`link_events` persistence functions `netbbs.link.
transport` dispatches through a `DatabaseLane`. These call the plain
`db`-first functions directly (no lane, no event loop, no real
transport) — `tests/test_link_transport.py` already proves the same
functions survive being called from a real `LinkServer`/`dial_hello`
over an actual socket; this file proves the storage logic itself in
isolation.
"""

from __future__ import annotations

import json

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, edit_post, tombstone_post
from netbbs.link.boards import (
    close_board_if_linked,
    link_board,
    queue_board_post_edit_if_linked,
    queue_board_post_if_linked,
    queue_board_post_moderator_edit_if_linked,
    queue_board_post_tombstone_if_linked,
)
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
from netbbs.moderation.roles import BoardPermission, grant_permissions
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
    backstop, not the primary mechanism."""
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


# -- purge_expired_key_transitions (design doc §8.9, issue #86) -------------


def _backdate_link_event(db: Database, content_id: str, *, received_at: str) -> None:
    db.connection.execute(
        "UPDATE link_events SET received_at = ? WHERE content_id = ?", (received_at, content_id)
    )
    db.connection.commit()


def test_purge_expired_key_transitions_deletes_only_rows_older_than_the_retention_window(tmp_path):
    """Rows inserted directly via SQL, bypassing `save_event`'s own
    inline purge-on-write -- this test wants to prove `purge_expired_
    key_transitions` itself, in isolation from that wiring (covered
    separately by `test_save_event_purges_expired_key_transitions_
    inline` below)."""
    import json as json_module

    from netbbs.link.store import purge_expired_key_transitions

    db = Database(tmp_path / "node.db")
    sender_identity = bootstrap_node_identity("bob")
    old_transition = sender_identity.transitions[0]
    recent_identity = bootstrap_node_identity("carol")
    recent_transition = recent_identity.transitions[0]
    db.connection.execute(
        "INSERT INTO link_events (content_id, sender_fingerprint, object_type, envelope_json, received_at) "
        "VALUES (?, ?, 'key_transition', ?, ?)",
        (
            old_transition.content_id, sender_identity.fingerprint,
            json_module.dumps(old_transition.to_dict()), "2020-01-01T00:00:00Z",
        ),
    )
    db.connection.execute(
        "INSERT INTO link_events (content_id, sender_fingerprint, object_type, envelope_json, received_at) "
        "VALUES (?, ?, 'key_transition', ?, ?)",
        (
            recent_transition.content_id, recent_identity.fingerprint,
            json_module.dumps(recent_transition.to_dict()), "2025-12-01T00:00:00Z",
        ),
    )
    db.connection.commit()

    deleted = purge_expired_key_transitions(db, now_iso="2026-01-01T00:00:00Z")

    assert deleted == 1
    remaining_ids = {
        row["content_id"] for row in db.connection.execute("SELECT content_id FROM link_events")
    }
    assert remaining_ids == {recent_transition.content_id}
    db.close()


def test_purge_expired_key_transitions_leaves_every_other_object_type_alone_regardless_of_age(tmp_path):
    """Design doc §8.9's own per-type trace: only key_transition is
    provably redundant with a separately-durable source -- every
    board-scoped type must survive purging regardless of how old it is,
    since restart reconstruction and issue #85's inventory diff both
    still depend on the row."""
    from netbbs.link.boards import materialize_carried_board
    from netbbs.link.events import build_board_genesis
    from netbbs.link.store import purge_expired_key_transitions

    db = Database(tmp_path / "node.db")
    remote_identity = bootstrap_node_identity("elsewhere")
    genesis = build_board_genesis(
        signing_identity=remote_identity.signing_key,
        origin_fingerprint=remote_identity.fingerprint,
        board_id="remote-board-id",
        name="Remote Discussion",
        created_at="2020-01-01T00:00:00Z",
    )
    materialize_carried_board(db, genesis)
    save_event(
        db, sender_fingerprint=remote_identity.fingerprint, content_id=genesis.content_id,
        object_type="board_genesis", envelope=genesis.to_dict(),
    )
    _backdate_link_event(db, genesis.content_id, received_at="2020-01-01T00:00:00Z")

    deleted = purge_expired_key_transitions(db, now_iso="2026-01-01T00:00:00Z")

    assert deleted == 0
    row = db.connection.execute(
        "SELECT 1 FROM link_events WHERE content_id = ?", (genesis.content_id,)
    ).fetchone()
    assert row is not None
    db.close()


def test_save_event_purges_expired_key_transitions_inline(tmp_path):
    """The actual wiring (design doc §8.9): a new key_transition write
    triggers the purge itself, the same 'purge on write, same table'
    shape `LinkDiagnosticLogHandler.emit` already established -- no
    separate scheduled task exists or is needed."""
    db = Database(tmp_path / "node.db")
    old_identity = bootstrap_node_identity("bob")
    old_transition = old_identity.transitions[0]
    save_event(
        db, sender_fingerprint=old_identity.fingerprint, content_id=old_transition.content_id,
        object_type="key_transition", envelope=old_transition.to_dict(),
    )
    _backdate_link_event(db, old_transition.content_id, received_at="2020-01-01T00:00:00Z")

    new_identity = bootstrap_node_identity("carol")
    new_transition = new_identity.transitions[0]
    save_event(
        db, sender_fingerprint=new_identity.fingerprint, content_id=new_transition.content_id,
        object_type="key_transition", envelope=new_transition.to_dict(),
    )

    remaining_ids = {
        row["content_id"] for row in db.connection.execute("SELECT content_id FROM link_events")
    }
    assert old_transition.content_id not in remaining_ids
    assert new_transition.content_id in remaining_ids
    db.close()


def test_save_event_populates_board_id_for_board_genesis(tmp_path):
    """Design doc §8.8, issue #85: `board_id` is populated directly from
    the envelope at insert time for the board-scoped object types
    `save_event` still handles (board_post/board_post_edit bypass this
    function entirely -- see `netbbs.link.boards.materialize_carried_
    post`/`_edit`, tested separately)."""
    db = Database(tmp_path / "node.db")
    remote_identity = bootstrap_node_identity("elsewhere")
    genesis = build_board_genesis(
        signing_identity=remote_identity.signing_key,
        origin_fingerprint=remote_identity.fingerprint,
        board_id="remote-board-id",
        name="Remote Discussion",
        created_at="2026-01-01T00:00:00Z",
    )

    save_event(
        db,
        sender_fingerprint=remote_identity.fingerprint,
        content_id=genesis.content_id,
        object_type="board_genesis",
        envelope=genesis.to_dict(),
    )

    row = db.connection.execute(
        "SELECT board_id FROM link_events WHERE content_id = ?", (genesis.content_id,)
    ).fetchone()
    assert row["board_id"] == "remote-board-id"
    db.close()


def test_save_event_leaves_board_id_null_for_a_non_board_event(tmp_path):
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

    row = db.connection.execute(
        "SELECT board_id FROM link_events WHERE content_id = ?", (transition.content_id,)
    ).fetchone()
    assert row["board_id"] is None
    db.close()


def test_migration_backfills_board_id_for_a_pre_existing_link_events_row(tmp_path, monkeypatch):
    """A `link_events` row written before this migration existed has no
    `board_id` of its own -- the migration must compute it from the
    envelope it already stores, the same "preserve existing data,
    never reset it" discipline issue #72's own arrival_id backfill
    test already established for a different table."""
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    db_path = tmp_path / "node.db"
    remote_identity = bootstrap_node_identity("elsewhere")
    genesis = build_board_genesis(
        signing_identity=remote_identity.signing_key,
        origin_fingerprint=remote_identity.fingerprint,
        board_id="remote-board-id",
        name="Remote Discussion",
        created_at="2026-01-01T00:00:00Z",
    )

    # Apply every migration before the one that added board_id, matching
    # the schema shape a real pre-upgrade database would have on disk --
    # link_events exists, but with no board_id column yet. A carried
    # board's genesis also always lands on the boards row itself
    # (`materialize_carried_board`'s own real-world behavior) --
    # reproduced here with a raw INSERT so `board_event_diff` below
    # recognizes this board as carried the same way it would for a
    # database that actually went through that function.
    #
    # Found by description rather than MIGRATIONS[:-1] -- this migration
    # is no longer guaranteed to be the last one in the list (issue #87's
    # own migration was appended after it).
    board_id_migration_index = next(
        i for i, m in enumerate(MIGRATIONS) if "idx_link_events_board_id" in m.sql
    )
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:board_id_migration_index])
    db = Database(db_path)
    db.connection.execute(
        "INSERT INTO link_events (content_id, sender_fingerprint, object_type, envelope_json, received_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            genesis.content_id, remote_identity.fingerprint, "board_genesis",
            json.dumps(genesis.to_dict()), "2026-01-01T00:00:00Z",
        ),
    )
    db.connection.execute(
        "INSERT INTO boards "
        "(board_id, name, description, min_read_level, min_write_level, category_id, pinned, "
        " created_at, moderated, max_post_age_days, min_age, name_requirement, community_id, link_genesis_json) "
        "VALUES (?, ?, NULL, 0, 0, NULL, 0, ?, 0, NULL, NULL, NULL, NULL, ?)",
        ("remote-board-id", "Remote Discussion", "2026-01-01T00:00:00Z", json.dumps(genesis.to_dict())),
    )
    db.connection.commit()
    db.close()
    monkeypatch.undo()

    # Reopen with the real, full migration list -- only the new one runs.
    db = Database(db_path)
    try:
        row = db.connection.execute(
            "SELECT board_id FROM link_events WHERE content_id = ?", (genesis.content_id,)
        ).fetchone()
        assert row["board_id"] == "remote-board-id"

        # And the diff query works immediately using the backfilled value.
        from netbbs.link.store import board_event_diff

        events, _more_available = board_event_diff(db, {"remote-board-id": []}, limit=200)
        assert len(events) == 1
    finally:
        db.close()


def test_save_board_genesis_event_then_load_link_node_reconstructs_boards(tmp_path):
    """`LinkServer._handle_events` already persists any accepted event
    generically, board_genesis included -- proves the
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
    """A board this node itself originated (`netbbs.link.
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
    """A self-authored edit (`netbbs.link.boards.queue_
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


def test_load_link_node_reconstructs_self_originated_board_post_moderator_edit(tmp_path):
    """Design doc §9.5, issue #88: a self-originated moderator edit
    (`queue_board_post_moderator_edit_if_linked`) never goes through
    `handle_events` either -- lives on the same `posts.link_event_json`
    column a self-authored edit does. Proves `load_link_node`
    reconstructs `node.post_edits` from it, mirroring the self-authored
    case above."""
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    author = create_user(db, "alice", password="hunter2", user_level=10)
    moderator = create_user(db, "modmin", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=author)
    grant_permissions(
        db, moderator, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=author
    )
    link_board(db, board, node_identity=own_identity)
    post = create_post(db, board, author, "hello", "world")
    board_post = queue_board_post_if_linked(db, post, board, node_identity=own_identity)
    edited = edit_post(db, post, board, subject="hello (moderator edit)", body="redacted", edited_by=moderator)
    mod_edit = queue_board_post_moderator_edit_if_linked(
        db, edited, board, node_identity=own_identity, edited_by=moderator
    )

    node = load_link_node(db, own_identity)

    assert board_post.content_id in node.post_edits
    assert [e.content_id for e in node.post_edits[board_post.content_id]] == [mod_edit.content_id]
    db.close()


def test_load_link_node_reconstructs_self_originated_board_post_tombstone(tmp_path):
    """Design doc §9.5, issue #88: same restart reconstruction as the
    moderator-edit case above, for a self-originated tombstone."""
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    author = create_user(db, "alice", password="hunter2", user_level=10)
    moderator = create_user(db, "modmin", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=author)
    grant_permissions(
        db, moderator, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=author
    )
    link_board(db, board, node_identity=own_identity)
    post = create_post(db, board, author, "hello", "world")
    board_post = queue_board_post_if_linked(db, post, board, node_identity=own_identity)
    tombstoned = tombstone_post(db, post, board, tombstoned_by=moderator)
    tombstone = queue_board_post_tombstone_if_linked(db, tombstoned, board, node_identity=own_identity)

    node = load_link_node(db, own_identity)

    assert board_post.content_id in node.post_edits
    assert [e.content_id for e in node.post_edits[board_post.content_id]] == [tombstone.content_id]
    db.close()


def test_load_link_node_reconstructs_self_originated_board_closure(tmp_path):
    """Design doc §9.5, issue #88: a self-originated board_closure
    lives on `boards.link_lifecycle_json`, the same column an
    origin-transfer offer/acceptance does. Proves `load_link_node`
    reconstructs `node.board_closures`/`board_lifecycle_head` from it."""
    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    creator = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=creator)
    link_board(db, board, node_identity=own_identity)
    closure = close_board_if_linked(db, board, node_identity=own_identity)

    node = load_link_node(db, own_identity)

    assert board.board_id in node.board_closures
    assert node.board_closures[board.board_id].content_id == closure.content_id
    assert node.board_lifecycle_head[board.board_id] == closure.content_id
    db.close()


def test_load_link_node_reconstructs_self_originated_file_area_genesis(tmp_path):
    """Design doc §11, issue #89: a self-originated file_area_genesis
    lives only on file_areas.link_genesis_json -- mirrors the board_
    genesis restart-reconstruction test above exactly."""
    from netbbs.files.areas import create_file_area
    from netbbs.link.files import link_file_area

    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    creator = create_user(db, "alice", password="hunter2", user_level=10)
    area = create_file_area(db, "files", creator=creator)
    genesis = link_file_area(db, area, node_identity=own_identity)

    node = load_link_node(db, own_identity)

    assert area.area_id in node.file_areas
    assert node.file_areas[area.area_id].content_id == genesis.content_id
    db.close()


def test_load_link_node_reconstructs_peer_received_file_area_genesis_and_file_descriptor(tmp_path):
    """A peer-received file_area_genesis/file_descriptor both go through
    the ordinary link_events restart path -- file_area_genesis needs its
    own node.file_areas branch (no chain to reconstruct otherwise);
    file_descriptor needs none beyond the generic known_event_ids/events
    restoration every link_events row already gets."""
    from netbbs.link.events import build_file_area_genesis, build_file_descriptor

    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    sender_identity = bootstrap_node_identity("bob")

    genesis = build_file_area_genesis(
        signing_identity=sender_identity.signing_key,
        origin_fingerprint=sender_identity.fingerprint,
        area_id="remote-area-id",
        name="Remote Files",
        created_at="2026-01-01T00:00:00Z",
    )
    save_event(
        db, sender_fingerprint=sender_identity.fingerprint, content_id=genesis.content_id,
        object_type=genesis.envelope["object_type"], envelope=genesis.to_dict(),
    )
    descriptor = build_file_descriptor(
        signing_identity=sender_identity.signing_key,
        area_id="remote-area-id",
        file_id="some-file-content-id",
        filename="game.zip",
        size_bytes=1000,
        sha256="a" * 64,
        created_at="2026-01-01T00:00:00Z",
    )
    save_event(
        db, sender_fingerprint=sender_identity.fingerprint, content_id=descriptor.content_id,
        object_type=descriptor.envelope["object_type"], envelope=descriptor.to_dict(),
    )

    node = load_link_node(db, own_identity)

    assert "remote-area-id" in node.file_areas
    assert node.file_areas["remote-area-id"].content_id == genesis.content_id
    assert descriptor.content_id in node.known_event_ids
    assert descriptor.content_id in node.events
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
    """Peer-received board_post_edit rows must reconstruct in the
    order they were originally accepted, or the chain's own
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


# -- peer-list candidates (design doc §8.3) ------------------------------


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
    cleanup -- a fingerprint that becomes a real verified
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


# -- carried_board_ids/build_inventory_request/board_event_diff (design doc §8.8, issue #85) --


def _remote_genesis_for_store_tests(remote_identity, *, board_id="remote-board-id"):
    return build_board_genesis(
        signing_identity=remote_identity.signing_key,
        origin_fingerprint=remote_identity.fingerprint,
        board_id=board_id,
        name="Remote Discussion",
        created_at="2026-01-01T00:00:00Z",
    )


def _remote_post_for_store_tests(remote_identity, *, board_id="remote-board-id", **kwargs):
    return build_board_post(
        signing_identity=remote_identity.signing_key,
        home_node_fingerprint=remote_identity.fingerprint,
        local_user_id="wanderer",
        board_id=board_id,
        subject=kwargs.pop("subject", "hello"),
        body=kwargs.pop("body", "first post"),
        created_at=kwargs.pop("created_at", "2026-01-01T00:00:00Z"),
        **kwargs,
    )


def test_carried_board_ids_includes_both_self_originated_and_carried_boards(tmp_path):
    from netbbs.link.boards import materialize_carried_board
    from netbbs.link.store import carried_board_ids

    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    creator = create_user(db, "alice", password="hunter2", user_level=10)
    own_board = create_board(db, "general", creator=creator)
    link_board(db, own_board, node_identity=own_identity)

    remote_identity = bootstrap_node_identity("elsewhere")
    materialize_carried_board(db, _remote_genesis_for_store_tests(remote_identity, board_id="carried-board"))

    assert set(carried_board_ids(db)) == {own_board.board_id, "carried-board"}
    db.close()


def test_carried_board_ids_excludes_an_unlinked_board(tmp_path):
    from netbbs.link.store import carried_board_ids

    db = Database(tmp_path / "node.db")
    creator = create_user(db, "alice", password="hunter2", user_level=10)
    create_board(db, "not-linked", creator=creator)

    assert carried_board_ids(db) == []
    db.close()


def test_build_inventory_request_includes_self_originated_genesis_and_post(tmp_path):
    from netbbs.link.store import build_inventory_request

    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    creator = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=creator)
    genesis = link_board(db, board, node_identity=own_identity)
    post = create_post(db, board, creator, "hello", "world")
    board_post = queue_board_post_if_linked(db, post, board, node_identity=own_identity)

    request = build_inventory_request(db)

    assert board.board_id in request.boards
    assert set(request.boards[board.board_id]) == {genesis.content_id, board_post.content_id}
    db.close()


def test_build_inventory_request_includes_carried_content(tmp_path):
    from netbbs.link.boards import materialize_carried_board, materialize_carried_post
    from netbbs.link.store import build_inventory_request

    db = Database(tmp_path / "node.db")
    own_identity = bootstrap_node_identity("alice")
    remote_identity = bootstrap_node_identity("elsewhere")
    genesis = _remote_genesis_for_store_tests(remote_identity)
    materialize_carried_board(db, genesis)
    post = _remote_post_for_store_tests(remote_identity)
    materialize_carried_post(db, post, sender_fingerprint=remote_identity.fingerprint)

    request = build_inventory_request(db)

    assert set(request.boards["remote-board-id"]) == {genesis.content_id, post.content_id}
    db.close()


def test_board_event_diff_returns_only_events_missing_from_the_requesters_known_ids(tmp_path):
    from netbbs.link.boards import materialize_carried_board, materialize_carried_post
    from netbbs.link.store import board_event_diff

    db = Database(tmp_path / "node.db")
    remote_identity = bootstrap_node_identity("elsewhere")
    genesis = _remote_genesis_for_store_tests(remote_identity)
    materialize_carried_board(db, genesis)
    post = _remote_post_for_store_tests(remote_identity)
    materialize_carried_post(db, post, sender_fingerprint=remote_identity.fingerprint)

    events, more_available = board_event_diff(db, {"remote-board-id": [genesis.content_id]}, limit=200)

    returned_ids = {_content_id_of(e) for e in events}
    assert post.content_id in returned_ids
    assert genesis.content_id not in returned_ids
    assert more_available is False
    db.close()


def _content_id_of(raw_envelope: dict) -> str:
    from netbbs.link.events import event_content_id

    return event_content_id(raw_envelope["envelope"])


def test_board_event_diff_silently_skips_a_board_id_this_node_does_not_carry(tmp_path):
    from netbbs.link.store import board_event_diff

    db = Database(tmp_path / "node.db")

    events, more_available = board_event_diff(db, {"unknown-board-id": []}, limit=200)

    assert events == []
    assert more_available is False
    db.close()


def test_board_event_diff_respects_the_limit_and_reports_more_available(tmp_path):
    from netbbs.link.boards import materialize_carried_board, materialize_carried_post
    from netbbs.link.store import board_event_diff

    db = Database(tmp_path / "node.db")
    remote_identity = bootstrap_node_identity("elsewhere")
    genesis = _remote_genesis_for_store_tests(remote_identity)
    materialize_carried_board(db, genesis)
    for i in range(3):
        post = _remote_post_for_store_tests(
            remote_identity, subject=f"post {i}", nonce=f"nonce-{i}", created_at="2026-01-01T00:00:00Z"
        )
        materialize_carried_post(db, post, sender_fingerprint=remote_identity.fingerprint)

    events, more_available = board_event_diff(db, {"remote-board-id": []}, limit=2)

    # 1 genesis + 3 posts = 4 total events on file; limit=2 must return
    # exactly 2 and flag that more remain, never silently truncate
    # without saying so.
    assert len(events) == 2
    assert more_available is True
    db.close()


def test_board_event_diff_is_genuinely_multi_hop_for_a_board_this_node_never_originated(tmp_path):
    """The actual multi-hop proof at the store layer (design doc §8.8,
    issue #85): a node that only *carries* board X, never originated
    it, must still be able to answer for it -- `board_event_diff` reads
    what this node has on file, not who authored it."""
    from netbbs.link.boards import materialize_carried_board, materialize_carried_post
    from netbbs.link.store import board_event_diff

    db = Database(tmp_path / "node.db")
    origin_identity = bootstrap_node_identity("origin-node")
    genesis = _remote_genesis_for_store_tests(origin_identity)
    materialize_carried_board(db, genesis)
    post = _remote_post_for_store_tests(origin_identity)
    materialize_carried_post(db, post, sender_fingerprint=origin_identity.fingerprint)

    events, _more_available = board_event_diff(db, {"remote-board-id": []}, limit=200)

    returned_ids = {_content_id_of(e) for e in events}
    assert {genesis.content_id, post.content_id} == returned_ids
    db.close()
    db.close()
