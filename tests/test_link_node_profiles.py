from __future__ import annotations

import pytest

from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.node_profiles import (
    MAX_IDENTITY_OBSERVATIONS_PER_PEER,
    identity_for_fingerprint,
    identity_for_peer,
    normalize_friendly_name,
    present_link_author_label,
    profile_claims_are_canonical,
    latest_identity_observation,
    list_identity_observations,
    record_peer_identity_observation,
    remember_own_identity_claims,
    resolve_peer_reference,
    resolve_stored_peer_reference,
    own_canonical_dns_name,
    is_node_fingerprint,
)
from netbbs.managed_dns.state import (
    RegistrationStatus, set_previous_name, set_previous_published, set_previous_status,
    set_node_fingerprint, set_published, set_registered_name, set_registration_status,
)
from netbbs.config import set_node_display_name
from netbbs.link.protocol import LinkNode, LinkProtocolError
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


def test_transport_endpoint_host_is_not_a_presentation_identity_claim(db):
    peers = []
    for label, port in (("first-endpoint", 7862), ("second-endpoint", 7863)):
        identity = bootstrap_node_identity(label)
        node = LinkNode(identity=identity)
        peer = node.handle_hello(node.build_hello(
            addresses=[{"protocol": "http", "address": "shared.example.org", "port": port}],
            outgoing_only=False,
            created_at="2026-09-04T00:00:00+00:00",
        ))
        save_peer(db, peer)
        peers.append(peer)

    assert all(identity_for_peer(peer).dns_name is None for peer in peers)
    assert resolve_peer_reference(peers, "shared.example.org") == []
    assert all(identity_for_fingerprint(db, peer.fingerprint).dns_name is None for peer in peers)
    assert resolve_stored_peer_reference(db, "shared.example.org") == []
    assert not any(item.severity == "security" for item in list_identity_observations(db))


def test_resolver_prefers_dns_and_refuses_ambiguous_friendly_names(tmp_path):
    alice = _peer(tmp_path, "alice", "The Anchor", "one.example.org")
    bob = _peer(tmp_path, "bob", "The Anchor", "two.example.org")
    assert resolve_peer_reference([alice, bob], "two.example.org") is bob
    assert resolve_peer_reference([alice, bob], "The Anchor") == [alice, bob]
    assert resolve_peer_reference([alice, bob], alice.fingerprint[:12]) is alice


def test_resolver_refuses_a_reference_shared_across_dns_and_friendly_name(db, tmp_path):
    friendly = _peer(tmp_path, "friendly", "shared.example.org", "friendly.example.org")
    dns = _peer(tmp_path, "dns", "DNS owner", "shared.example.org")
    save_peer(db, friendly)
    save_peer(db, dns)

    assert resolve_peer_reference([friendly, dns], "shared.example.org") == [friendly, dns]
    assert resolve_stored_peer_reference(db, "shared.example.org") == [
        friendly.fingerprint,
        dns.fingerprint,
    ]


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
    assert normalize_friendly_name("Unnamed linked node") is None
    assert normalize_friendly_name("UNNAMED LINKED NODE") is None
    assert normalize_friendly_name("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") is None


def test_unseen_technical_identity_requires_a_complete_node_fingerprint():
    assert is_node_fingerprint("abcdefghijklmnopqrstuvwxyz234567")
    assert is_node_fingerprint("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert not is_node_fingerprint("node-name-typo")
    assert not is_node_fingerprint("abcd")


def test_exact_fingerprint_cannot_be_advertised_as_a_friendly_name(db, tmp_path):
    alice = _peer(tmp_path, "alice", "Alice", "alice.example.org")
    assert normalize_friendly_name(alice.fingerprint) is None


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


def test_matured_managed_name_is_advertised_only_once_the_service_confirms_a_record(db):
    """The service matures a registration before its first provider
    upsert and leaves it matured if that fails -- so `matured` alone
    must not replace a working configured hostname with a name that has
    no DNS record yet."""
    set_registered_name(db, "myboard")
    set_registration_status(db, RegistrationStatus.MATURED)

    assert own_canonical_dns_name(db, "currently-live.example.org") == "currently-live.example.org"

    set_published(db, True)
    assert own_canonical_dns_name(db, "currently-live.example.org") == "myboard.netbbs.org"


def test_pending_rename_advertises_the_previous_name_only_once_it_was_published(db):
    set_registered_name(db, "replacement")
    set_registration_status(db, RegistrationStatus.PENDING)
    set_previous_name(db, "old-name")
    set_previous_status(db, RegistrationStatus.MATURED)

    assert own_canonical_dns_name(db, "currently-live.example.org") == "currently-live.example.org"

    set_previous_published(db, True)
    assert own_canonical_dns_name(db, "currently-live.example.org") == "old-name.netbbs.org"


def test_abandoned_replacement_keeps_advertising_a_published_previous_name(db):
    set_registered_name(db, "replacement")
    set_registration_status(db, RegistrationStatus.ABANDONED)
    set_previous_name(db, "old-name")
    set_previous_status(db, RegistrationStatus.MATURED)
    set_previous_published(db, True)

    assert own_canonical_dns_name(db, "currently-live.example.org") == "old-name.netbbs.org"


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


def test_latest_observation_prefers_undismissed_security_over_newer_benign_change(db, tmp_path):
    anchor = _peer(tmp_path, "anchor", "Anchor", "anchor.example.org")
    impostor_identity = bootstrap_node_identity("impostor")
    impostor_node = LinkNode(identity=impostor_identity)
    initial = impostor_node.handle_hello(impostor_node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-03T12:00:00+00:00",
        friendly_name="Other", canonical_dns_name="other.example.org",
    ))
    save_peer(db, anchor)
    save_peer(db, initial)
    save_peer(db, impostor_node.handle_hello(impostor_node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-03T13:00:00+00:00",
        friendly_name="Anchor", canonical_dns_name="other.example.org",
    )))
    for index in range(MAX_IDENTITY_OBSERVATIONS_PER_PEER + 5):
        save_peer(db, impostor_node.handle_hello(impostor_node.build_hello(
            addresses=None, outgoing_only=True,
            created_at=f"2026-09-04T12:{index:02d}:00+00:00",
            friendly_name=f"Recovered {index}", canonical_dns_name="other.example.org",
        )))

    latest = latest_identity_observation(db, impostor_identity.fingerprint)
    assert latest is not None
    assert latest.kind == "cryptographic_identity_changed"
    assert latest.severity == "security"
    count = db.connection.execute(
        "SELECT COUNT(*) FROM link_node_identity_observations WHERE node_fingerprint = ?",
        (impostor_identity.fingerprint,),
    ).fetchone()[0]
    assert count == MAX_IDENTITY_OBSERVATIONS_PER_PEER


@pytest.mark.parametrize(
    ("friendly_name", "dns_name"),
    [("Local Anchor", "other.example.org"), ("Other", "local.example.org")],
)
def test_peer_adopting_the_local_nodes_claim_is_security_notice(
    db, tmp_path, friendly_name, dns_name,
):
    set_node_display_name(db, "Local Anchor")
    set_node_fingerprint(db, "abcdefghijklmnopqrstuvwxyz234567")
    remember_own_identity_claims(db, canonical_dns_name="local.example.org")

    save_peer(db, _peer(tmp_path, "impostor", friendly_name, dns_name))

    notice = list_identity_observations(db)[0]
    assert notice.kind == "cryptographic_identity_changed"
    assert notice.severity == "security"
    assert notice.previous_fingerprint == "abcdefghijklmnopqrstuvwxyz234567"


@pytest.mark.parametrize(
    ("friendly_name", "dns_name"),
    [("Old Local", "other.example.org"), ("Other", "old-local.example.org")],
)
def test_peer_adopting_a_retained_previous_local_claim_is_security_notice(
    db, tmp_path, friendly_name, dns_name,
):
    set_node_display_name(db, "Old Local")
    set_node_fingerprint(db, "abcdefghijklmnopqrstuvwxyz234567")
    remember_own_identity_claims(db, canonical_dns_name="old-local.example.org")
    set_node_display_name(db, "New Local")
    remember_own_identity_claims(db, canonical_dns_name="new-local.example.org")

    save_peer(db, _peer(tmp_path, "impostor", friendly_name, dns_name))

    notice = list_identity_observations(db)[0]
    assert notice.kind == "cryptographic_identity_changed"
    assert notice.severity == "security"
    assert notice.previous_fingerprint == "abcdefghijklmnopqrstuvwxyz234567"


def test_peer_adopting_the_last_advertised_local_name_before_the_next_hello_is_security_notice(
    db, tmp_path,
):
    """The last advertised claim bridges a local rename until the own-hello
    provider has moved that claim into bounded history."""
    set_node_display_name(db, "Old Local")
    set_node_fingerprint(db, "abcdefghijklmnopqrstuvwxyz234567")
    remember_own_identity_claims(db, canonical_dns_name="local.example.org")
    set_node_display_name(db, "New Local")

    save_peer(db, _peer(tmp_path, "impostor", "Old Local", "other.example.org"))

    notice = list_identity_observations(db)[0]
    assert notice.kind == "cryptographic_identity_changed"
    assert notice.severity == "security"
    assert notice.previous_fingerprint == "abcdefghijklmnopqrstuvwxyz234567"


def test_unchanged_peer_is_rechecked_after_local_friendly_name_changes(db, tmp_path):
    peer = _peer(tmp_path, "peer", "Familiar", "peer.example.org")
    set_node_display_name(db, "Original Local")
    set_node_fingerprint(db, "abcdefghijklmnopqrstuvwxyz234567")
    remember_own_identity_claims(db, canonical_dns_name="local.example.org")
    save_peer(db, peer)

    set_node_display_name(db, "Familiar")
    save_peer(db, peer)
    save_peer(db, peer)

    notices = [
        item for item in list_identity_observations(db)
        if item.kind == "cryptographic_identity_changed"
    ]
    assert len(notices) == 1
    assert notices[0].node_fingerprint == peer.fingerprint
    assert notices[0].previous_fingerprint == "abcdefghijklmnopqrstuvwxyz234567"


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


def test_global_observation_bound_preserves_an_undismissed_security_warning(
    db, tmp_path, monkeypatch,
):
    monkeypatch.setattr("netbbs.link.node_profiles.MAX_IDENTITY_OBSERVATIONS_TOTAL", 3)
    anchor = _peer(tmp_path, "global-anchor", "Anchor", "anchor.example.org")
    impostor_identity = bootstrap_node_identity("global-impostor")
    impostor_node = LinkNode(identity=impostor_identity)
    save_peer(db, anchor)
    save_peer(db, impostor_node.handle_hello(impostor_node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-03T12:00:00+00:00",
        friendly_name="Other", canonical_dns_name="other.example.org",
    )))
    save_peer(db, impostor_node.handle_hello(impostor_node.build_hello(
        addresses=None, outgoing_only=True, created_at="2026-09-03T13:00:00+00:00",
        friendly_name="Anchor", canonical_dns_name="other.example.org",
    )))
    for index in range(4):
        save_peer(db, _peer(
            tmp_path, f"global-peer-{index}", f"Peer {index}", f"peer-{index}.example.org",
        ))

    latest = latest_identity_observation(db, impostor_identity.fingerprint)
    assert latest is not None
    assert latest.severity == "security"
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_node_identity_observations"
    ).fetchone()[0] == 3


NFC_NAME = "Caf\u00e9 Anchor"
NFD_NAME = "Cafe\u0301 Anchor"


def test_unicode_equivalent_friendly_names_are_one_claim(db, tmp_path):
    """Precomposed and combining-accent spellings render identically, so
    they must be one name everywhere: canonicalized on normalization,
    refused as a non-canonical claim in a hello, resolved by either
    spelling, and caught as a collision against retained history."""
    assert normalize_friendly_name(NFD_NAME) == NFC_NAME
    assert not profile_claims_are_canonical({"friendly_name": NFD_NAME})
    assert profile_claims_are_canonical({"friendly_name": NFC_NAME})

    with pytest.raises(LinkProtocolError, match="invalid profile claims"):
        _peer(tmp_path, "impostor", NFD_NAME, "impostor.example.org")

    alice = _peer(tmp_path, "alice", NFC_NAME, "alice.example.org")
    save_peer(db, alice)
    assert resolve_peer_reference([alice], NFD_NAME) is alice
    assert resolve_stored_peer_reference(db, NFD_NAME) == alice.fingerprint

    # A retained observation from before canonicalization still counts.
    db.connection.execute(
        """
        INSERT INTO link_node_identity_observations
            (node_fingerprint, previous_fingerprint, friendly_name, previous_friendly_name,
             canonical_dns_name, previous_dns_name, severity, kind, observed_at)
        VALUES (?, NULL, ?, NULL, ?, NULL, 'info', 'first_seen', '2026-09-01T00:00:00+00:00')
        """,
        ("legacy" * 5 + "ab", NFD_NAME, "legacy.example.org"),
    )
    bob = _peer(tmp_path, "bob", NFC_NAME, "bob.example.org")
    db.connection.execute("DELETE FROM link_peers")
    record_peer_identity_observation(db, bob)
    latest = list_identity_observations(db)[0]
    assert latest.kind == "cryptographic_identity_changed"
    assert latest.severity == "security"


def test_unseen_fingerprint_is_presented_by_its_technical_identity(db):
    """An administratively configured node this node has never admitted
    has only its fingerprint -- two such nodes must not both read as the
    same placeholder in a trust picker."""
    fingerprint = "abcdefghijklmnopqrstuvwxyz234567"
    identity = identity_for_fingerprint(db, fingerprint)
    assert identity.friendly_name == "Unknown linked node"
    assert identity.label == fingerprint


def test_persisted_link_labels_are_presented_by_the_home_nodes_current_identity(db, tmp_path):
    alice = _peer(tmp_path, "alice", "The Anchor", "anchor.example.org")
    save_peer(db, alice)
    unseen = "abcdefghijklmnopqrstuvwxyz234567"

    assert present_link_author_label(db, f"bob@{alice.fingerprint}") == "bob@The Anchor · anchor.example.org"
    assert present_link_author_label(db, f"remote@{unseen}") == f"remote@{unseen}"
    assert present_link_author_label(db, "bob") == "bob"
    assert present_link_author_label(db, "bob@not-a-node-fingerprint") == "bob@not-a-node-fingerprint"
