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

from netbbs.link.events import build_endpoint_descriptor, build_key_transition
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import PeerRecord
from netbbs.link.store import load_link_node, save_event, save_peer
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
