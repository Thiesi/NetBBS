"""Tests for netbbs.link.node_identity — the node key-lifecycle model
(root key + signing/transport operational keys + signed transition
chains)."""

from __future__ import annotations

import base64
import dataclasses

import pytest

from netbbs.identity.keys import Identity, IdentityKind
from netbbs.link.events import build_key_transition
from netbbs.link.node_identity import (
    NodeIdentity,
    NodeIdentityError,
    bootstrap_node_identity,
    load_or_bootstrap_node_identity,
    resolve_current_operational_key,
    rotate_operational_key,
)


def _b64(identity: Identity) -> str:
    return base64.b64encode(bytes(identity.verify_key)).decode("ascii")


# -- bootstrap ------------------------------------------------------------


def test_bootstrap_produces_distinct_root_and_operational_keys():
    identity = bootstrap_node_identity("roanoke")
    fingerprints = {identity.root.fingerprint, identity.signing_key.fingerprint, identity.transport_key.fingerprint}
    assert len(fingerprints) == 3  # root, signing, transport are three genuinely different keys


def test_bootstrap_creates_one_authorize_transition_per_purpose():
    identity = bootstrap_node_identity("roanoke")
    assert len(identity.transitions) == 2
    purposes = {t.payload["purpose"] for t in identity.transitions}
    assert purposes == {"signing", "transport"}
    assert all(t.payload["action"] == "authorize" for t in identity.transitions)


def test_two_bootstraps_produce_different_fingerprints():
    a = bootstrap_node_identity("roanoke")
    b = bootstrap_node_identity("roanoke")
    assert a.fingerprint != b.fingerprint


def test_fingerprint_is_the_root_keys_fingerprint():
    identity = bootstrap_node_identity("roanoke")
    assert identity.fingerprint == identity.root.fingerprint


# -- resolving the current operational key --------------------------------


def test_resolve_current_operational_key_matches_bootstrapped_key():
    identity = bootstrap_node_identity("roanoke")
    resolved = resolve_current_operational_key(
        identity.transitions,
        root_verify_key=identity.root.verify_key,
        subject_fingerprint=identity.fingerprint,
        purpose="signing",
    )
    assert resolved == _b64(identity.signing_key)


def test_resolve_current_operational_key_none_when_no_transitions():
    resolved = resolve_current_operational_key(
        (), root_verify_key=Identity.generate(IdentityKind.NODE, "x").verify_key,
        subject_fingerprint="whatever", purpose="signing",
    )
    assert resolved is None


# -- rotation ---------------------------------------------------------------


def test_rotate_produces_a_new_operational_key():
    identity = bootstrap_node_identity("roanoke")
    old_signing_fingerprint = identity.signing_key.fingerprint
    rotated = rotate_operational_key(identity, purpose="signing")
    assert rotated.signing_key.fingerprint != old_signing_fingerprint


def test_rotate_adds_exactly_two_transitions_revoke_then_authorize():
    identity = bootstrap_node_identity("roanoke")
    rotated = rotate_operational_key(identity, purpose="signing")
    assert len(rotated.transitions) == len(identity.transitions) + 2
    new_pair = rotated.transitions[-2:]
    assert new_pair[0].payload["action"] == "revoke"
    assert new_pair[1].payload["action"] == "authorize"
    assert new_pair[0].payload["operational_key"] == _b64(identity.signing_key)
    assert new_pair[1].payload["operational_key"] == _b64(rotated.signing_key)


def test_rotate_resolves_to_new_key_not_old_one():
    identity = bootstrap_node_identity("roanoke")
    rotated = rotate_operational_key(identity, purpose="signing")
    resolved = resolve_current_operational_key(
        rotated.transitions,
        root_verify_key=rotated.root.verify_key,
        subject_fingerprint=rotated.fingerprint,
        purpose="signing",
    )
    assert resolved == _b64(rotated.signing_key)
    assert resolved != _b64(identity.signing_key)


def test_rotating_signing_key_does_not_affect_transport_chain():
    identity = bootstrap_node_identity("roanoke")
    rotated = rotate_operational_key(identity, purpose="signing")
    assert rotated.transport_key.fingerprint == identity.transport_key.fingerprint
    resolved_transport = resolve_current_operational_key(
        rotated.transitions,
        root_verify_key=rotated.root.verify_key,
        subject_fingerprint=rotated.fingerprint,
        purpose="transport",
    )
    assert resolved_transport == _b64(identity.transport_key)


def test_rotate_invalid_purpose_raises():
    identity = bootstrap_node_identity("roanoke")
    with pytest.raises(NodeIdentityError):
        rotate_operational_key(identity, purpose="not-a-real-purpose")


def test_double_rotation_chains_correctly():
    identity = bootstrap_node_identity("roanoke")
    once = rotate_operational_key(identity, purpose="signing")
    twice = rotate_operational_key(once, purpose="signing")
    resolved = resolve_current_operational_key(
        twice.transitions,
        root_verify_key=twice.root.verify_key,
        subject_fingerprint=twice.fingerprint,
        purpose="signing",
    )
    assert resolved == _b64(twice.signing_key)
    assert twice.signing_key.fingerprint not in {
        identity.signing_key.fingerprint, once.signing_key.fingerprint,
    }


# -- chain integrity: forks, breaks, forged signatures ----------------------


def test_forked_chain_is_rejected():
    identity = bootstrap_node_identity("roanoke")
    genesis = next(t for t in identity.transitions if t.payload["purpose"] == "signing")
    branch_a_key = Identity.generate(IdentityKind.NODE, "roanoke")
    branch_b_key = Identity.generate(IdentityKind.NODE, "roanoke")
    # Two distinct transitions both claiming genesis as their
    # predecessor -- a genuine fork, unlike a single transition
    # extending genesis (which is just a normal chain of length 2).
    branch_a = build_key_transition(
        root=identity.root, purpose="signing", action="authorize",
        operational_key=branch_a_key.verify_key,
        previous_transition_id=genesis.content_id,
        created_at="2026-01-01T00:00:01Z",
    )
    branch_b = build_key_transition(
        root=identity.root, purpose="signing", action="authorize",
        operational_key=branch_b_key.verify_key,
        previous_transition_id=genesis.content_id,
        created_at="2026-01-01T00:00:02Z",
    )
    identity_with_fork = dataclasses.replace(
        identity, transitions=identity.transitions + (branch_a, branch_b)
    )
    with pytest.raises(NodeIdentityError):
        resolve_current_operational_key(
            identity_with_fork.transitions,
            root_verify_key=identity.root.verify_key,
            subject_fingerprint=identity.fingerprint,
            purpose="signing",
        )


def test_disconnected_transition_is_rejected():
    identity = bootstrap_node_identity("roanoke")
    orphan_operational = Identity.generate(IdentityKind.NODE, "roanoke")
    orphan = build_key_transition(
        root=identity.root, purpose="signing", action="authorize",
        operational_key=orphan_operational.verify_key,
        previous_transition_id="some-id-that-does-not-exist-in-this-chain",
        created_at="2026-01-01T00:00:00Z",
    )
    identity_with_orphan = dataclasses.replace(
        identity, transitions=identity.transitions + (orphan,)
    )
    with pytest.raises(NodeIdentityError):
        resolve_current_operational_key(
            identity_with_orphan.transitions,
            root_verify_key=identity.root.verify_key,
            subject_fingerprint=identity.fingerprint,
            purpose="signing",
        )


def test_transition_signed_by_wrong_root_is_rejected():
    identity = bootstrap_node_identity("roanoke")
    impostor_root = Identity.generate(IdentityKind.NODE, "impostor")
    forged_operational = Identity.generate(IdentityKind.NODE, "roanoke")
    forged = build_key_transition(
        root=impostor_root, purpose="signing", action="authorize",
        operational_key=forged_operational.verify_key,
        previous_transition_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    # Constructed to *claim* the real node's fingerprint even though it
    # wasn't actually signed by the real root -- the signature check
    # (not the claimed subject_fingerprint) must be what catches this.
    forged_payload = dict(forged.payload)
    forged_payload["subject_fingerprint"] = identity.fingerprint
    tampered = dataclasses.replace(
        forged, envelope={**forged.envelope, "payload": forged_payload}
    )
    with pytest.raises(NodeIdentityError):
        resolve_current_operational_key(
            (tampered,),
            root_verify_key=identity.root.verify_key,
            subject_fingerprint=identity.fingerprint,
            purpose="signing",
        )


# -- persistence --------------------------------------------------------------


def test_save_and_load_roundtrip(tmp_path):
    identity = bootstrap_node_identity("roanoke")
    directory = tmp_path / "identity"
    identity.save(directory)

    loaded = NodeIdentity.load(directory)
    assert loaded.fingerprint == identity.fingerprint
    assert loaded.signing_key.fingerprint == identity.signing_key.fingerprint
    assert loaded.transport_key.fingerprint == identity.transport_key.fingerprint
    assert len(loaded.transitions) == len(identity.transitions)


def test_save_and_load_after_rotation(tmp_path):
    identity = rotate_operational_key(bootstrap_node_identity("roanoke"), purpose="transport")
    directory = tmp_path / "identity"
    identity.save(directory)

    loaded = NodeIdentity.load(directory)
    assert loaded.transport_key.fingerprint == identity.transport_key.fingerprint
    resolved = resolve_current_operational_key(
        loaded.transitions,
        root_verify_key=loaded.root.verify_key,
        subject_fingerprint=loaded.fingerprint,
        purpose="transport",
    )
    assert resolved == _b64(loaded.transport_key)


def test_load_detects_tampered_operational_key(tmp_path):
    identity = bootstrap_node_identity("roanoke")
    directory = tmp_path / "identity"
    identity.save(directory)

    # Swap in an unrelated signing key file -- the chain on disk still
    # names the original operational key as current, so the on-disk
    # private key file no longer matches what the verified chain says.
    rogue = Identity.generate(IdentityKind.NODE, "rogue")
    rogue.save(directory / "signing.identity")

    with pytest.raises(NodeIdentityError):
        NodeIdentity.load(directory)


def test_load_or_bootstrap_creates_once_then_loads(tmp_path):
    directory = tmp_path / "identity"
    first = load_or_bootstrap_node_identity(directory, label="roanoke")
    second = load_or_bootstrap_node_identity(directory, label="roanoke")
    assert first.fingerprint == second.fingerprint
    assert first.signing_key.fingerprint == second.signing_key.fingerprint


def test_load_or_bootstrap_persists_to_disk_on_first_call(tmp_path):
    directory = tmp_path / "identity"
    assert not directory.exists()
    load_or_bootstrap_node_identity(directory, label="roanoke")
    assert (directory / "root.identity").exists()
    assert (directory / "signing.identity").exists()
    assert (directory / "transport.identity").exists()
    assert (directory / "transitions.json").exists()
