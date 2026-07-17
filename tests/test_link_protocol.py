"""
Tests for `netbbs.link.protocol` — the first real NetBBS Link
handshake/gossip protocol code (design doc §11/§12, round 116). Driven
entirely against `tests/link_harness.py`'s `ScriptedTransport`, proving
the protocol logic is genuinely transport-agnostic: nothing here opens
a socket or makes an HTTP call.
"""

from __future__ import annotations

import pytest

from netbbs.link.events import build_endpoint_descriptor, build_key_transition
from netbbs.link.node_identity import rotate_operational_key
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError
from tests.link_harness import FakeClock, ScriptedTransport, spawn_node


@pytest.fixture
def clock():
    return FakeClock()


def _hello_bytes(node: LinkNode, *, clock: FakeClock, outgoing_only: bool = True) -> HelloMessage:
    return node.build_hello(addresses=None, outgoing_only=outgoing_only, created_at=clock.now_iso())


# -- hello: build + verify -------------------------------------------------


def test_build_hello_includes_root_key_and_own_signing_transitions(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    node = LinkNode(identity=alice.identity)

    hello = _hello_bytes(node, clock=clock)

    assert hello.root_public_key == bytes(alice.identity.root.verify_key)
    assert len(hello.transitions) == 1  # the initial bootstrap "authorize" transition
    assert hello.transitions[0].payload["purpose"] == "signing"
    assert hello.descriptor.payload["subject_fingerprint"] == alice.fingerprint

    alice.close()


def test_handle_hello_accepts_a_valid_bundle_and_records_the_peer(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    bob_node = LinkNode(identity=bob.identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    record = bob_node.handle_hello(alice_hello)

    assert record.fingerprint == alice.fingerprint
    assert bob_node.peers[alice.fingerprint] is record

    alice.close()
    bob.close()


def test_handle_hello_roundtrips_through_to_dict_from_dict(tmp_path, clock):
    # Proves the wire-serializable form (what a real transport would
    # actually send as JSON) carries everything handle_hello needs --
    # not just the in-memory dataclass.
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    bob_node = LinkNode(identity=bob.identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    wire_form = HelloMessage.from_dict(alice_hello.to_dict())
    record = bob_node.handle_hello(wire_form)

    assert record.fingerprint == alice.fingerprint

    alice.close()
    bob.close()


def test_handle_hello_rejects_a_descriptor_signed_by_the_wrong_key(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    mallory = spawn_node(tmp_path, "mallory")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    # Splice in a descriptor signed by a different node's signing key,
    # while still claiming to be alice -- must not verify.
    forged_descriptor = build_endpoint_descriptor(
        signing_identity=mallory.identity.signing_key,
        subject_fingerprint=alice.fingerprint,
        addresses=None,
        outgoing_only=True,
        created_at=clock.now_iso(),
    )
    forged_hello = HelloMessage(
        root_public_key=alice_hello.root_public_key,
        transitions=alice_hello.transitions,
        descriptor=forged_descriptor,
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_hello(forged_hello)

    alice.close()
    mallory.close()


def test_handle_hello_rejects_a_descriptor_for_a_different_subject(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    mismatched_descriptor = build_endpoint_descriptor(
        signing_identity=alice.identity.signing_key,
        subject_fingerprint="some-other-fingerprint",
        addresses=None,
        outgoing_only=True,
        created_at=clock.now_iso(),
    )
    forged_hello = HelloMessage(
        root_public_key=alice_hello.root_public_key,
        transitions=alice_hello.transitions,
        descriptor=mismatched_descriptor,
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_hello(forged_hello)

    alice.close()


def test_handle_hello_ignores_a_stale_repeat_hello(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    alice_node = LinkNode(identity=alice.identity)

    first = _hello_bytes(alice_node, clock=clock)
    bob_node.handle_hello(first)

    clock.advance(hours=1)
    second = _hello_bytes(alice_node, clock=clock)
    bob_node.handle_hello(second)
    assert bob_node.peers[alice.fingerprint].descriptor.payload["created_at"] == second.descriptor.payload["created_at"]

    # A stale, older-timestamped hello arriving after the fact (e.g. a
    # reordered/replayed message) must not regress the recorded state.
    bob_node.handle_hello(first)
    assert bob_node.peers[alice.fingerprint].descriptor.payload["created_at"] == second.descriptor.payload["created_at"]

    alice.close()


# -- events: gossiping a key_transition -------------------------------------


def test_handle_events_refuses_events_from_a_peer_with_no_completed_hello(tmp_path):
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    with pytest.raises(LinkProtocolError):
        bob_node.handle_events("some-stranger-fingerprint", [])


def test_two_nodes_complete_handshake_and_gossip_a_key_rotation(tmp_path, clock):
    """The full round-116 slice, end to end: alice and bob say hello,
    alice then rotates her signing key, and the resulting key_transition
    gossips to bob and verifies against her *now-extended* chain -- all
    routed through ScriptedTransport, never a direct method call between
    the two LinkNodes, proving this works as genuinely separate parties
    exchanging serialized messages, not just shared Python objects."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    transport = ScriptedTransport()
    transport.register(alice)
    transport.register(bob)

    alice_node = LinkNode(identity=alice.identity)
    bob_node = LinkNode(identity=bob.identity)

    # -- handshake, both directions --
    import json

    alice_hello = _hello_bytes(alice_node, clock=clock)
    transport.send(alice, bob, json.dumps(alice_hello.to_dict()).encode())
    bob_hello = _hello_bytes(bob_node, clock=clock)
    transport.send(bob, alice, json.dumps(bob_hello.to_dict()).encode())
    transport.deliver_all()

    [to_bob] = transport.inbox(bob)
    bob_node.handle_hello(HelloMessage.from_dict(json.loads(to_bob.payload)))
    [to_alice] = transport.inbox(alice)
    alice_node.handle_hello(HelloMessage.from_dict(json.loads(to_alice.payload)))

    assert bob.fingerprint in alice_node.peers
    assert alice.fingerprint in bob_node.peers

    # -- alice rotates her signing key; the resulting key_transitions
    # gossip to bob. rotate_operational_key produces *two* new
    # transitions (revoke old, then authorize new, design doc round
    # 89) -- both must be sent, in order: the new "authorize"'s own
    # previous_transition_id points at the "revoke", so bob can't
    # connect it to what he already has without that middle link too.
    rotated_identity = rotate_operational_key(alice.identity, purpose="signing")
    alice.identity = rotated_identity
    alice_node.identity = rotated_identity
    revoke_transition, authorize_transition = rotated_identity.transitions[-2:]
    assert revoke_transition.payload["action"] == "revoke"
    assert authorize_transition.payload["action"] == "authorize"

    transport.send(
        alice, bob,
        json.dumps([revoke_transition.to_dict(), authorize_transition.to_dict()]).encode(),
    )
    transport.deliver_all()
    [event_message] = [m for m in transport.inbox(bob) if m.payload != to_bob.payload]
    accepted = bob_node.handle_events(alice.fingerprint, json.loads(event_message.payload))

    assert accepted == [revoke_transition.content_id, authorize_transition.content_id]
    assert bob_node.peers[alice.fingerprint].transitions[-1].content_id == authorize_transition.content_id

    alice.close()
    bob.close()


def test_handle_events_rejects_an_event_claiming_the_wrong_subject(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    mallory = spawn_node(tmp_path, "mallory")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    # mallory's own valid transition, relayed as if it came from alice.
    rotated_mallory = rotate_operational_key(mallory.identity, purpose="signing")
    forged = rotated_mallory.transitions[-1]

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [forged.to_dict()])

    alice.close()
    mallory.close()


def test_handle_events_is_idempotent_for_an_already_seen_event(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    # Both halves of the rotation (see the end-to-end test's own note on
    # why revoke must precede authorize) sent, then the identical pair
    # resent -- the second round must be a pure no-op.
    rotated = rotate_operational_key(alice.identity, purpose="signing")
    revoke_transition, authorize_transition = rotated.transitions[-2:]
    pair = [revoke_transition.to_dict(), authorize_transition.to_dict()]

    first = bob_node.handle_events(alice.fingerprint, pair)
    second = bob_node.handle_events(alice.fingerprint, pair)

    assert first == [revoke_transition.content_id, authorize_transition.content_id]
    assert second == []  # already seen -- silently skipped, not re-applied or errored

    alice.close()


def test_handle_events_recovers_idempotency_from_the_chain_when_the_dedup_entry_is_gone(tmp_path, clock):
    """Round 121: simulates what a future purge of known_event_ids
    would look like today -- clear the dedup entry by hand (nothing
    currently purges it, round 120), but leave sender.transitions
    alone (that state is permanent, never purged). A resend of the
    exact same, already-integrated transition must still be recognized
    as a safe no-op via chain membership, not rejected as a forged
    fork, and must re-populate known_event_ids/events from the
    authoritative chain state."""
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    rotated = rotate_operational_key(alice.identity, purpose="signing")
    revoke_transition, authorize_transition = rotated.transitions[-2:]
    pair = [revoke_transition.to_dict(), authorize_transition.to_dict()]

    first = bob_node.handle_events(alice.fingerprint, pair)
    assert first == [revoke_transition.content_id, authorize_transition.content_id]

    # Simulate a purged dedup table: the entries are gone, but the
    # peer's own chain (permanent state) still has both transitions.
    bob_node.known_event_ids.discard(revoke_transition.content_id)
    bob_node.known_event_ids.discard(authorize_transition.content_id)
    del bob_node.events[revoke_transition.content_id]
    del bob_node.events[authorize_transition.content_id]

    second = bob_node.handle_events(alice.fingerprint, pair)
    assert second == []  # still a no-op, not a rejection

    # Self-healed: a third resend takes the known_event_ids fast path.
    assert revoke_transition.content_id in bob_node.known_event_ids
    assert authorize_transition.content_id in bob_node.known_event_ids
    assert bob_node.events[authorize_transition.content_id] == authorize_transition.to_dict()

    # The chain itself was never touched twice -- still exactly the
    # transitions from the one real acceptance above.
    assert bob_node.peers[alice.fingerprint].transitions[-1].content_id == authorize_transition.content_id

    alice.close()


def test_handle_events_still_rejects_a_genuine_fork_when_dedup_is_gone(tmp_path, clock):
    """The other half of round 121's proof: clearing known_event_ids
    must not turn a *real* fork attempt into a false no-op. A fork
    carries a different transition (different content_id) claiming the
    same previous_transition_id as one already applied -- must still
    be rejected."""
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    rotated = rotate_operational_key(alice.identity, purpose="signing")
    revoke_transition, authorize_transition = rotated.transitions[-2:]
    accepted = bob_node.handle_events(
        alice.fingerprint, [revoke_transition.to_dict(), authorize_transition.to_dict()]
    )
    assert accepted == [revoke_transition.content_id, authorize_transition.content_id]

    # A second, *different* transition extending the same revoke --
    # same previous_transition_id as authorize_transition, different
    # content_id. Even with known_event_ids cleared (as if purged), the
    # chain-membership check must not mistake this for the same event.
    bob_node.known_event_ids.discard(revoke_transition.content_id)
    bob_node.known_event_ids.discard(authorize_transition.content_id)

    forked = build_key_transition(
        root=alice.identity.root,
        purpose="signing",
        action="authorize",
        operational_key=rotate_operational_key(alice.identity, purpose="transport").transport_key.verify_key,
        previous_transition_id=revoke_transition.content_id,
        created_at=clock.now_iso(),
    )
    assert forked.content_id != authorize_transition.content_id

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [forked.to_dict()])

    alice.close()


def test_handle_events_rejects_an_unrecognized_object_type(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    fake_event = {
        "envelope": {"netbbs_protocol": 1, "object_type": "board_post", "payload": {}},
        "signature": "",
    }
    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [fake_event])

    alice.close()
