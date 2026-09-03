from __future__ import annotations

import pytest

from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.node_profiles import (
    MAX_IDENTITY_OBSERVATIONS_PER_PEER,
    identity_for_peer,
    list_identity_observations,
    record_peer_identity_observation,
    resolve_peer_reference,
    resolve_stored_peer_reference,
    own_canonical_dns_name,
    is_node_fingerprint,
)
from netbbs.managed_dns.state import (
    RegistrationStatus, set_previous_name, set_previous_status, set_registered_name,
    set_registration_status,
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


def test_resolver_preserves_terminal_periods_in_friendly_names(db, tmp_path):
    dotted = _peer(tmp_path, "dotted", "The Anchor.", "dotted.example.org")
    plain = _peer(tmp_path, "plain", "The Anchor", "plain.example.org")
    save_peer(db, dotted)
    save_peer(db, plain)

    assert resolve_peer_reference([dotted, plain], "The Anchor.") is dotted
    assert resolve_stored_peer_reference(db, "The Anchor.") == dotted.fingerprint


def test_friendly_name_reserves_label_delimiter_and_invisible_controls():
    from netbbs.link.node_profiles import normalize_friendly_name

    assert normalize_friendly_name("Trusted Node · honest.example.org") is None
    assert normalize_friendly_name("Anchor\x9b31m") is None
    assert normalize_friendly_name("Anchor\u202eevil") is None
    assert normalize_friendly_name('The "Rusty" Anchor') is None


def test_unseen_technical_identity_requires_a_complete_node_fingerprint():
    assert is_node_fingerprint("abcdefghijklmnopqrstuvwxyz234567")
    assert is_node_fingerprint("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert not is_node_fingerprint("node-name-typo")
    assert not is_node_fingerprint("abcd")


def test_exact_fingerprint_cannot_be_shadowed_by_a_friendly_name(db, tmp_path):
    alice = _peer(tmp_path, "alice", "Alice", "alice.example.org")
    impostor = _peer(tmp_path, "impostor", alice.fingerprint, "impostor.example.org")
    save_peer(db, alice)
    save_peer(db, impostor)

    assert resolve_peer_reference([alice, impostor], alice.fingerprint) is alice
    assert resolve_stored_peer_reference(db, alice.fingerprint) == alice.fingerprint


def test_blank_reference_never_resolves_to_the_only_peer(db, tmp_path):
    alice = _peer(tmp_path, "alice", "Alice", "alice.example.org")
    save_peer(db, alice)

    assert resolve_peer_reference([alice], "  ") == []
    assert resolve_stored_peer_reference(db, "  ") == []


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


def test_dns_claim_colliding_with_another_peers_friendly_name_is_security_notice(db, tmp_path):
    first = _peer(tmp_path, "alice", "anchor.example.org", "alice.example.org")
    second = _peer(tmp_path, "mallory", "Other", "anchor.example.org")
    save_peer(db, first)
    save_peer(db, second)

    notice = list_identity_observations(db)[0]
    assert notice.kind == "cryptographic_identity_changed"
    assert notice.previous_fingerprint == first.fingerprint


def test_pending_initial_managed_name_is_not_advertised_before_publication(db):
    set_registered_name(db, "reserved-name")
    set_registration_status(db, RegistrationStatus.PENDING)

    assert own_canonical_dns_name(db, "currently-live.example.org") == "currently-live.example.org"


def test_pending_rename_does_not_advertise_an_unpublished_previous_name(db):
    set_registered_name(db, "replacement")
    set_registration_status(db, RegistrationStatus.PENDING)
    set_previous_name(db, "also-pending")
    set_previous_status(db, RegistrationStatus.PENDING)

    assert own_canonical_dns_name(db, "currently-live.example.org") == "currently-live.example.org"


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


def test_new_peer_adopting_a_retained_previous_name_is_security_notice(db, tmp_path):
    alice_identity = bootstrap_node_identity("alice")
    alice_node = LinkNode(identity=alice_identity)
    old_alice = alice_node.handle_hello(alice_node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-03T12:00:00+00:00",
        friendly_name="Old Anchor", canonical_dns_name="alice.example.org",
    ))
    save_peer(db, old_alice)
    renamed_alice = alice_node.handle_hello(alice_node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-03T13:00:00+00:00",
        friendly_name="New Anchor", canonical_dns_name="alice.example.org",
    ))
    save_peer(db, renamed_alice)

    impostor = _peer(tmp_path, "impostor", "Old Anchor", "impostor.example.org")
    save_peer(db, impostor)

    notice = list_identity_observations(db)[0]
    assert notice.kind == "cryptographic_identity_changed"
    assert notice.severity == "security"
    assert notice.previous_fingerprint == alice_identity.fingerprint
    assert notice.previous_friendly_name == "Old Anchor"


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
