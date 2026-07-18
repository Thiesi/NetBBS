"""
Tests for `netbbs.link.protocol`'s relay-consent verification methods
(design doc §12, round 95/issue #58): `handle_relay_consent_request`/
`handle_relay_consent_response`. Both are verify-only -- neither method
mutates `relaying_for`/`relays_serving_me`, matching the module's own
"verify here, policy/mutation elsewhere" split (see each method's own
docstring) -- so these tests check verification outcomes and the
in-memory bookkeeping fields directly, the same way `test_link_protocol.
py` tests the mutual-consent board-origin-transfer pair.
"""

from __future__ import annotations

import pytest

from netbbs.link.events import (
    build_relay_consent_request,
    build_relay_consent_response,
)
from netbbs.link.protocol import LinkNode, LinkProtocolError
from tests.link_harness import FakeClock, spawn_node


@pytest.fixture
def clock():
    return FakeClock()


def _two_nodes_with_completed_hello(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)

    alice_hello = alice_node.build_hello(addresses=None, outgoing_only=True, created_at=clock.now_iso())
    bob_hello = bob_node.build_hello(
        addresses=[{"protocol": "http", "address": "198.51.100.7", "port": 7862}],
        outgoing_only=False,
        created_at=clock.now_iso(),
    )
    bob_node.handle_hello(alice_hello)
    alice_node.handle_hello(bob_hello)

    return alice, bob, alice_node, bob_node


def test_handle_relay_consent_request_accepts_a_valid_request(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)

    request = build_relay_consent_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        relay_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )

    bob_node.handle_relay_consent_request(alice.fingerprint, request)  # does not raise

    # Verification alone never mutates relaying_for -- that's the caller's job.
    assert bob_node.relaying_for == {}

    alice.close()
    bob.close()


def test_handle_relay_consent_request_refuses_a_stranger(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    bob_node = LinkNode(identity=bob.identity)  # bob never completed a hello with alice

    request = build_relay_consent_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        relay_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_relay_consent_request(alice.fingerprint, request)

    alice.close()
    bob.close()


def test_handle_relay_consent_request_rejects_a_mismatched_requester_claim(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    mallory = spawn_node(tmp_path, "mallory")

    request = build_relay_consent_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=mallory.fingerprint,  # doesn't match who actually sent it
        relay_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_relay_consent_request(alice.fingerprint, request)

    alice.close()
    bob.close()
    mallory.close()


def test_handle_relay_consent_request_rejects_a_request_addressed_to_someone_else(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    mallory = spawn_node(tmp_path, "mallory")

    request = build_relay_consent_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        relay_fingerprint=mallory.fingerprint,  # not bob
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_relay_consent_request(alice.fingerprint, request)

    alice.close()
    bob.close()
    mallory.close()


def test_handle_relay_consent_request_rejects_a_forged_signature(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    mallory = spawn_node(tmp_path, "mallory")

    # Signed by mallory, but claiming to be from alice.
    request = build_relay_consent_request(
        signing_identity=mallory.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        relay_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_relay_consent_request(alice.fingerprint, request)

    alice.close()
    bob.close()
    mallory.close()


def test_handle_relay_consent_response_accepts_a_valid_response(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)

    request = build_relay_consent_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        relay_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )
    alice_node.pending_own_relay_requests[bob.fingerprint] = request

    response = build_relay_consent_response(
        signing_identity=bob.identity.signing_key,
        request_content_id=request.content_id,
        relay_fingerprint=bob.fingerprint,
        requester_fingerprint=alice.fingerprint,
        accepted=True,
        created_at=clock.now_iso(),
    )

    alice_node.handle_relay_consent_response(bob.fingerprint, response, original_request=request)  # does not raise

    # Verification alone never mutates relays_serving_me -- that's the caller's job.
    assert alice_node.relays_serving_me == {}

    alice.close()
    bob.close()


def test_handle_relay_consent_response_rejects_an_answer_to_a_different_request(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)

    real_request = build_relay_consent_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        relay_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )
    other_request = build_relay_consent_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        relay_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
        nonce="a-different-nonce",
    )

    response = build_relay_consent_response(
        signing_identity=bob.identity.signing_key,
        request_content_id=other_request.content_id,  # answers the wrong one
        relay_fingerprint=bob.fingerprint,
        requester_fingerprint=alice.fingerprint,
        accepted=True,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        alice_node.handle_relay_consent_response(bob.fingerprint, response, original_request=real_request)

    alice.close()
    bob.close()


def test_handle_relay_consent_response_rejects_a_relay_impersonating_another(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    mallory = spawn_node(tmp_path, "mallory")
    mallory_node = LinkNode(identity=mallory.identity)
    mallory_hello = mallory_node.build_hello(
        addresses=[{"protocol": "http", "address": "198.51.100.99", "port": 7862}],
        outgoing_only=False,
        created_at=clock.now_iso(),
    )
    alice_node.handle_hello(mallory_hello)
    alice_hello = alice_node.build_hello(addresses=None, outgoing_only=True, created_at=clock.now_iso())
    mallory_node.handle_hello(alice_hello)

    request = build_relay_consent_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        relay_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )

    # Mallory (a different, also-completed peer) answers a request that
    # was actually addressed to bob.
    forged_response = build_relay_consent_response(
        signing_identity=mallory.identity.signing_key,
        request_content_id=request.content_id,
        relay_fingerprint=mallory.fingerprint,
        requester_fingerprint=alice.fingerprint,
        accepted=True,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        alice_node.handle_relay_consent_response(mallory.fingerprint, forged_response, original_request=request)

    alice.close()
    bob.close()
    mallory.close()
