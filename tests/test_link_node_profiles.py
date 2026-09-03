from __future__ import annotations

import pytest

from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.node_profiles import (
    MAX_IDENTITY_OBSERVATIONS_PER_PEER,
    identity_for_peer,
    list_identity_observations,
    record_peer_identity_observation,
    resolve_peer_reference,
)
from netbbs.link.protocol import LinkNode
from netbbs.link.store import save_peer
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _peer(tmp_path, label: str, friendly_name: str, dns_name: str):
    identity = bootstrap_node_identity(label)
    node = LinkNode(identity=identity)
    return node.handle_hello(
        node.build_hello(
            addresses=None, outgoing_only=True, created_at="2026-09-03T12:00:00+00:00",
            friendly_name=friendly_name, canonical_dns_name=dns_name,
        )
    )


def test_signed_profile_is_presented_as_friendly_name_and_dns(tmp_path):
    peer = _peer(tmp_path, "alice", "The Rusty Anchor", "rusty.netbbs.org")
    identity = identity_for_peer(peer)
    assert identity.label == "The Rusty Anchor · rusty.netbbs.org"
    assert peer.descriptor.payload["friendly_name"] == "The Rusty Anchor"


def test_resolver_prefers_dns_and_refuses_ambiguous_friendly_names(tmp_path):
    alice = _peer(tmp_path, "alice", "The Anchor", "one.example.org")
    bob = _peer(tmp_path, "bob", "The Anchor", "two.example.org")
    assert resolve_peer_reference([alice, bob], "two.example.org") is bob
    assert resolve_peer_reference([alice, bob], "The Anchor") == [alice, bob]
    assert resolve_peer_reference([alice, bob], alice.fingerprint[:12]) is alice


def test_same_fingerprint_rename_is_informational(db, tmp_path):
    identity = bootstrap_node_identity("alice")
    node = LinkNode(identity=identity)
    first = node.handle_hello(node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-03T12:00:00+00:00",
        friendly_name="Old Name", canonical_dns_name="same.example.org",
    ))
    save_peer(db, first)
    changed = node.handle_hello(node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-03T13:00:00+00:00",
        friendly_name="New Name", canonical_dns_name="same.example.org",
    ))
    save_peer(db, changed)
    notices = list_identity_observations(db)
    assert [(item.kind, item.severity) for item in notices] == [("friendly_name_changed", "info")]


def test_same_dns_with_new_fingerprint_is_security_notice(db, tmp_path):
    first = _peer(tmp_path, "alice", "Anchor", "same.example.org")
    second = _peer(tmp_path, "mallory", "Anchor", "same.example.org")
    save_peer(db, first)
    save_peer(db, second)
    notices = list_identity_observations(db)
    assert notices[0].kind == "cryptographic_identity_changed"
    assert notices[0].severity == "security"
    assert notices[0].previous_fingerprint == first.fingerprint


def test_same_friendly_name_with_new_fingerprint_is_security_notice(db, tmp_path):
    first = _peer(tmp_path, "alice", "Anchor", "old.example.org")
    second = _peer(tmp_path, "mallory", "Anchor", "new.example.org")
    save_peer(db, first)
    save_peer(db, second)
    notices = list_identity_observations(db)
    assert notices[0].kind == "cryptographic_identity_changed"
    assert notices[0].severity == "security"


def test_existing_peer_adopting_another_peers_name_is_security_notice(db, tmp_path):
    first = _peer(tmp_path, "alice", "Anchor", "anchor.example.org")
    bob_identity = bootstrap_node_identity("bob")
    bob_node = LinkNode(identity=bob_identity)
    second = bob_node.handle_hello(bob_node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-03T12:00:00+00:00",
        friendly_name="Other", canonical_dns_name="other.example.org",
    ))
    save_peer(db, first)
    save_peer(db, second)
    changed = bob_node.handle_hello(bob_node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-03T13:00:00+00:00",
        friendly_name="Anchor", canonical_dns_name="other.example.org",
    ))
    save_peer(db, changed)
    notice = list_identity_observations(db)[0]
    assert notice.kind == "cryptographic_identity_changed"
    assert notice.previous_fingerprint == first.fingerprint


def test_identity_observation_history_is_bounded_per_peer(db, tmp_path):
    identity = bootstrap_node_identity("flapping-peer")
    node = LinkNode(identity=identity)
    for index in range(MAX_IDENTITY_OBSERVATIONS_PER_PEER + 5):
        peer = node.handle_hello(node.build_hello(
            addresses=None, outgoing_only=True,
            created_at=f"2026-09-03T12:{index:02d}:00+00:00",
            friendly_name=f"Name {index}", canonical_dns_name="same.example.org",
        ))
        save_peer(db, peer)
    count = db.connection.execute(
        "SELECT COUNT(*) FROM link_node_identity_observations WHERE node_fingerprint = ?",
        (identity.fingerprint,),
    ).fetchone()[0]
    assert count == MAX_IDENTITY_OBSERVATIONS_PER_PEER
