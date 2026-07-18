"""
Tests for `netbbs.link.protocol` — the first real NetBBS Link
handshake/gossip protocol code (design doc §11/§12, round 116). Driven
entirely against `tests/link_harness.py`'s `ScriptedTransport`, proving
the protocol logic is genuinely transport-agnostic: nothing here opens
a socket or makes an HTTP call.
"""

from __future__ import annotations

import pytest

from netbbs.link.events import (
    build_board_genesis,
    build_board_origin_transfer_accepted,
    build_board_origin_transfer_offer,
    build_board_post,
    build_board_post_edit,
    build_endpoint_descriptor,
    build_key_transition,
    build_link_message,
    build_link_message_accepted,
    build_link_message_bounced,
)
from netbbs.link.node_identity import rotate_operational_key
from netbbs.link.protocol import HelloMessage, LinkNode, LinkProtocolError, PeerListMessage
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


def test_build_hello_omits_relays_when_none_are_serving(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    node = LinkNode(identity=alice.identity)

    hello = _hello_bytes(node, clock=clock)

    assert "relays" not in hello.descriptor.payload

    alice.close()


def test_build_hello_includes_relays_serving_me(tmp_path, clock):
    # Round 95/issue #58: build_hello reads relays_serving_me directly
    # rather than taking it as a caller-supplied parameter -- see that
    # method's own docstring.
    alice = spawn_node(tmp_path, "alice")
    node = LinkNode(identity=alice.identity)
    node.relays_serving_me["bobs-fingerprint"] = "2026-01-01T00:00:00Z"

    hello = _hello_bytes(node, clock=clock)

    assert hello.descriptor.payload["relays"] == ["bobs-fingerprint"]

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



# -- events: gossiping board_genesis/board_post (design doc round 124/125) --


def test_handle_events_accepts_a_valid_board_genesis(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    genesis = build_board_genesis(
        signing_identity=alice.identity.signing_key,
        origin_fingerprint=alice.fingerprint,
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at=clock.now_iso(),
    )

    accepted = bob_node.handle_events(alice.fingerprint, [genesis.to_dict()])

    assert accepted == [genesis.content_id]
    assert bob_node.boards["existing-local-board-id"].content_id == genesis.content_id

    alice.close()


def test_handle_events_rejects_board_genesis_with_mismatched_origin(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    mallory = spawn_node(tmp_path, "mallory")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    # mallory's own valid genesis, relayed as if it came from alice.
    forged = build_board_genesis(
        signing_identity=mallory.identity.signing_key,
        origin_fingerprint=mallory.fingerprint,
        board_id="existing-local-board-id",
        name="Mallory's Board",
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [forged.to_dict()])

    alice.close()
    mallory.close()


def test_handle_events_rejects_conflicting_board_genesis_for_same_board_id(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    first = build_board_genesis(
        signing_identity=alice.identity.signing_key,
        origin_fingerprint=alice.fingerprint,
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [first.to_dict()])

    # A different genesis (different name -> different content_id) for
    # the exact same board_id -- must be rejected, not silently replace
    # what's on file.
    clock.advance(hours=1)
    conflicting = build_board_genesis(
        signing_identity=alice.identity.signing_key,
        origin_fingerprint=alice.fingerprint,
        board_id="existing-local-board-id",
        name="A Different Name",
        created_at=clock.now_iso(),
    )
    assert conflicting.content_id != first.content_id

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [conflicting.to_dict()])

    alice.close()


def test_handle_events_board_genesis_is_idempotent_for_already_seen(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    genesis = build_board_genesis(
        signing_identity=alice.identity.signing_key,
        origin_fingerprint=alice.fingerprint,
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at=clock.now_iso(),
    )

    first = bob_node.handle_events(alice.fingerprint, [genesis.to_dict()])
    second = bob_node.handle_events(alice.fingerprint, [genesis.to_dict()])

    assert first == [genesis.content_id]
    assert second == []  # already seen -- silently skipped

    alice.close()


def test_handle_events_accepts_a_valid_board_post_after_genesis(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    genesis = build_board_genesis(
        signing_identity=alice.identity.signing_key,
        origin_fingerprint=alice.fingerprint,
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [genesis.to_dict()])

    post = build_board_post(
        signing_identity=alice.identity.signing_key,
        home_node_fingerprint=alice.fingerprint,
        local_user_id="wanderer",
        board_id="existing-local-board-id",
        subject="hello",
        body="first post",
        created_at=clock.now_iso(),
    )

    accepted = bob_node.handle_events(alice.fingerprint, [post.to_dict()])

    assert accepted == [post.content_id]
    assert bob_node.events[post.content_id] == post.to_dict()

    alice.close()


def test_handle_events_rejects_board_post_for_unknown_board_id(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    # No board_genesis for this board_id was ever received.
    post = build_board_post(
        signing_identity=alice.identity.signing_key,
        home_node_fingerprint=alice.fingerprint,
        local_user_id="wanderer",
        board_id="never-announced-board-id",
        subject="hello",
        body="first post",
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [post.to_dict()])

    alice.close()


def test_handle_events_rejects_board_post_with_unsupported_author_kind(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    genesis = build_board_genesis(
        signing_identity=alice.identity.signing_key,
        origin_fingerprint=alice.fingerprint,
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [genesis.to_dict()])

    post = build_board_post(
        signing_identity=alice.identity.signing_key,
        home_node_fingerprint=alice.fingerprint,
        local_user_id="wanderer",
        board_id="existing-local-board-id",
        subject="hello",
        body="first post",
        created_at=clock.now_iso(),
    )
    # user_key/node aren't built this round (design doc round 124) --
    # splice in an unsupported author kind and confirm it's refused
    # rather than silently accepted or crashing.
    raw = post.to_dict()
    raw["envelope"]["payload"]["author"] = {"kind": "user_key", "fingerprint": "some-user-fingerprint"}

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [raw])

    alice.close()


def test_handle_events_rejects_board_post_vouching_for_a_different_home_node(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    mallory = spawn_node(tmp_path, "mallory")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    genesis = build_board_genesis(
        signing_identity=alice.identity.signing_key,
        origin_fingerprint=alice.fingerprint,
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [genesis.to_dict()])

    # mallory's own valid post, relayed as if it came from alice --
    # mallory has no completed hello with bob at all, so this must be
    # refused even though alice (the actual sender) does.
    forged = build_board_post(
        signing_identity=mallory.identity.signing_key,
        home_node_fingerprint=mallory.fingerprint,
        local_user_id="wanderer",
        board_id="existing-local-board-id",
        subject="hello",
        body="first post",
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [forged.to_dict()])

    alice.close()
    mallory.close()


def test_handle_events_board_post_is_idempotent_for_already_seen(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    genesis = build_board_genesis(
        signing_identity=alice.identity.signing_key,
        origin_fingerprint=alice.fingerprint,
        board_id="existing-local-board-id",
        name="Vintage Computing",
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [genesis.to_dict()])

    post = build_board_post(
        signing_identity=alice.identity.signing_key,
        home_node_fingerprint=alice.fingerprint,
        local_user_id="wanderer",
        board_id="existing-local-board-id",
        subject="hello",
        body="first post",
        created_at=clock.now_iso(),
    )

    first = bob_node.handle_events(alice.fingerprint, [post.to_dict()])
    second = bob_node.handle_events(alice.fingerprint, [post.to_dict()])

    assert first == [post.content_id]
    assert second == []  # already seen -- silently skipped

    alice.close()



# -- events: gossiping board_post_edit (design doc round 129/130) -----------


def _linked_board_with_post(alice, bob_node, clock, *, board_id="existing-local-board-id"):
    """Sets up bob_node with alice's board_genesis and one board_post
    already accepted -- the common setup every board_post_edit test
    needs."""
    genesis = build_board_genesis(
        signing_identity=alice.identity.signing_key,
        origin_fingerprint=alice.fingerprint,
        board_id=board_id,
        name="Vintage Computing",
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [genesis.to_dict()])

    post = build_board_post(
        signing_identity=alice.identity.signing_key,
        home_node_fingerprint=alice.fingerprint,
        local_user_id="wanderer",
        board_id=board_id,
        subject="hello",
        body="first post",
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [post.to_dict()])
    return post


def test_handle_events_accepts_a_valid_board_post_edit(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)
    post = _linked_board_with_post(alice, bob_node, clock)

    edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=post.content_id,
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )

    accepted = bob_node.handle_events(alice.fingerprint, [edit.to_dict()])

    assert accepted == [edit.content_id]
    assert bob_node.post_edits[post.content_id][-1].content_id == edit.content_id

    alice.close()


def test_handle_events_accepts_a_second_chained_board_post_edit(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)
    post = _linked_board_with_post(alice, bob_node, clock)

    first_edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=post.content_id,
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [first_edit.to_dict()])

    second_edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=first_edit.content_id,
        subject="hello (edited again)",
        body="first post, edited again",
        created_at=clock.now_iso(),
    )
    accepted = bob_node.handle_events(alice.fingerprint, [second_edit.to_dict()])

    assert accepted == [second_edit.content_id]
    assert [e.content_id for e in bob_node.post_edits[post.content_id]] == [
        first_edit.content_id,
        second_edit.content_id,
    ]

    alice.close()


def test_handle_events_rejects_board_post_edit_for_unknown_root_post(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author={"kind": "node_vouched_user", "home_node_fingerprint": alice.fingerprint, "local_user_id": "wanderer"},
        board_id="existing-local-board-id",
        root_post_id="never-accepted-root-content-id",
        previous_event_id="never-accepted-root-content-id",
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [edit.to_dict()])

    alice.close()


def test_handle_events_rejects_board_post_edit_with_mismatched_author(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    mallory = spawn_node(tmp_path, "mallory")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)
    post = _linked_board_with_post(alice, bob_node, clock)

    # Same node relaying the edit, but claiming a different author than
    # the root post's own -- the mechanical expression of "moderator
    # edits aren't supported this round" (design doc round 129).
    edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author={"kind": "node_vouched_user", "home_node_fingerprint": alice.fingerprint, "local_user_id": "someone-else"},
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=post.content_id,
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [edit.to_dict()])

    alice.close()
    mallory.close()


def test_handle_events_rejects_board_post_edit_vouching_for_a_different_home_node(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    mallory = spawn_node(tmp_path, "mallory")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)
    post = _linked_board_with_post(alice, bob_node, clock)

    # Author matches the root post's *fingerprint* claim, but that
    # claimed fingerprint isn't the actual sender (mallory relaying as
    # if from alice) -- refused the same way a forged board_post is.
    forged = build_board_post_edit(
        signing_identity=mallory.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=post.content_id,
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(mallory.fingerprint, [forged.to_dict()])

    alice.close()
    mallory.close()


def test_handle_events_rejects_out_of_order_board_post_edit(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)
    post = _linked_board_with_post(alice, bob_node, clock)

    # previous_event_id points at a made-up content_id, not the actual
    # current head (post.content_id) -- refused outright, not queued
    # waiting for the "missing" predecessor to arrive later (round 129:
    # same push-and-retry model as key_transition, round 122).
    edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id="some-other-edit-that-was-never-sent",
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [edit.to_dict()])

    alice.close()


def test_handle_events_board_post_edit_is_idempotent_for_already_seen(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)
    post = _linked_board_with_post(alice, bob_node, clock)

    edit = build_board_post_edit(
        signing_identity=alice.identity.signing_key,
        author=post.payload["author"],
        board_id="existing-local-board-id",
        root_post_id=post.content_id,
        previous_event_id=post.content_id,
        subject="hello (edited)",
        body="first post, edited",
        created_at=clock.now_iso(),
    )

    first = bob_node.handle_events(alice.fingerprint, [edit.to_dict()])
    second = bob_node.handle_events(alice.fingerprint, [edit.to_dict()])

    assert first == [edit.content_id]
    assert second == []  # already seen -- silently skipped, not re-applied or errored

    alice.close()


def test_handle_events_rejects_an_unrecognized_object_type(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    # board_post is now a recognized type (design doc round 124/125) --
    # use a genuinely bogus one instead.
    fake_event = {
        "envelope": {"netbbs_protocol": 1, "object_type": "not_a_real_object_type", "payload": {}},
        "signature": "",
    }
    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [fake_event])

    alice.close()


# -- events: gossiping board_origin_transfer_offer/accepted (design doc round 94/#53) --


def _linked_board(alice, bob_node, clock, *, board_id="existing-local-board-id"):
    """Sets up bob_node with alice's board_genesis already accepted --
    the common setup every origin-transfer test needs, alice starting
    as the board's own origin."""
    genesis = build_board_genesis(
        signing_identity=alice.identity.signing_key,
        origin_fingerprint=alice.fingerprint,
        board_id=board_id,
        name="Vintage Computing",
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [genesis.to_dict()])
    return genesis


def test_handle_events_accepts_a_valid_origin_transfer_offer(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    carol = spawn_node(tmp_path, "carol")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))
    genesis = _linked_board(alice, bob_node, clock)

    offer = build_board_origin_transfer_offer(
        signing_identity=alice.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=genesis.content_id,
        old_origin_fingerprint=alice.fingerprint,
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    accepted = bob_node.handle_events(alice.fingerprint, [offer.to_dict()])

    assert accepted == [offer.content_id]
    # The offer alone changes nothing -- the old origin is still trusted.
    assert bob_node.current_board_origin("existing-local-board-id") == alice.fingerprint
    assert bob_node.pending_origin_transfers["existing-local-board-id"].content_id == offer.content_id

    alice.close()
    carol.close()


def test_handle_events_accepts_a_valid_origin_transfer_completing_the_handoff(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    carol = spawn_node(tmp_path, "carol")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=carol.identity), clock=clock))
    genesis = _linked_board(alice, bob_node, clock)

    offer = build_board_origin_transfer_offer(
        signing_identity=alice.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=genesis.content_id,
        old_origin_fingerprint=alice.fingerprint,
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [offer.to_dict()])

    accepted_event = build_board_origin_transfer_accepted(
        signing_identity=carol.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=offer.content_id,
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    accepted = bob_node.handle_events(carol.fingerprint, [accepted_event.to_dict()])

    assert accepted == [accepted_event.content_id]
    assert bob_node.current_board_origin("existing-local-board-id") == carol.fingerprint
    assert "existing-local-board-id" not in bob_node.pending_origin_transfers

    alice.close()
    carol.close()


def test_handle_events_rejects_an_offer_not_from_the_current_origin(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    mallory = spawn_node(tmp_path, "mallory")
    carol = spawn_node(tmp_path, "carol")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=mallory.identity), clock=clock))
    genesis = _linked_board(alice, bob_node, clock)

    # mallory (not the origin) tries to offer alice's board to carol.
    forged_offer = build_board_origin_transfer_offer(
        signing_identity=mallory.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=genesis.content_id,
        old_origin_fingerprint=mallory.fingerprint,
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(mallory.fingerprint, [forged_offer.to_dict()])

    assert bob_node.current_board_origin("existing-local-board-id") == alice.fingerprint

    alice.close()
    mallory.close()
    carol.close()


def test_handle_events_rejects_an_offer_with_a_mismatched_old_origin_claim(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    carol = spawn_node(tmp_path, "carol")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))
    genesis = _linked_board(alice, bob_node, clock)

    offer = build_board_origin_transfer_offer(
        signing_identity=alice.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=genesis.content_id,
        old_origin_fingerprint="not-actually-the-current-origin",
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [offer.to_dict()])

    alice.close()
    carol.close()


def test_handle_events_rejects_a_second_offer_while_one_is_outstanding(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    carol = spawn_node(tmp_path, "carol")
    dave = spawn_node(tmp_path, "dave")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))
    genesis = _linked_board(alice, bob_node, clock)

    first_offer = build_board_origin_transfer_offer(
        signing_identity=alice.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=genesis.content_id,
        old_origin_fingerprint=alice.fingerprint,
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [first_offer.to_dict()])

    second_offer = build_board_origin_transfer_offer(
        signing_identity=alice.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=genesis.content_id,
        old_origin_fingerprint=alice.fingerprint,
        new_origin_fingerprint=dave.fingerprint,
        created_at=clock.now_iso(),
    )
    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [second_offer.to_dict()])

    alice.close()
    carol.close()
    dave.close()


def test_handle_events_rejects_an_offer_for_an_unknown_board(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    carol = spawn_node(tmp_path, "carol")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))

    offer = build_board_origin_transfer_offer(
        signing_identity=alice.identity.signing_key,
        board_id="never-linked-board-id",
        previous_event_id="some-content-id",
        old_origin_fingerprint=alice.fingerprint,
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [offer.to_dict()])

    alice.close()
    carol.close()


def test_handle_events_rejects_an_acceptance_with_no_outstanding_offer(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    carol = spawn_node(tmp_path, "carol")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=carol.identity), clock=clock))
    _linked_board(alice, bob_node, clock)

    accepted_event = build_board_origin_transfer_accepted(
        signing_identity=carol.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id="some-offer-that-was-never-made",
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(carol.fingerprint, [accepted_event.to_dict()])

    alice.close()
    carol.close()


def test_handle_events_rejects_an_acceptance_not_from_the_offers_named_new_origin(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    carol = spawn_node(tmp_path, "carol")
    mallory = spawn_node(tmp_path, "mallory")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=mallory.identity), clock=clock))
    genesis = _linked_board(alice, bob_node, clock)

    offer = build_board_origin_transfer_offer(
        signing_identity=alice.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=genesis.content_id,
        old_origin_fingerprint=alice.fingerprint,
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    bob_node.handle_events(alice.fingerprint, [offer.to_dict()])

    # mallory (not the named new origin) tries to accept carol's offer.
    forged_accept = build_board_origin_transfer_accepted(
        signing_identity=mallory.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=offer.content_id,
        new_origin_fingerprint=mallory.fingerprint,
        created_at=clock.now_iso(),
    )
    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(mallory.fingerprint, [forged_accept.to_dict()])

    assert bob_node.current_board_origin("existing-local-board-id") == alice.fingerprint

    alice.close()
    carol.close()
    mallory.close()


def test_handle_events_is_idempotent_for_a_resent_origin_transfer_offer(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    carol = spawn_node(tmp_path, "carol")
    bob_node = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob_node.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))
    genesis = _linked_board(alice, bob_node, clock)

    offer = build_board_origin_transfer_offer(
        signing_identity=alice.identity.signing_key,
        board_id="existing-local-board-id",
        previous_event_id=genesis.content_id,
        old_origin_fingerprint=alice.fingerprint,
        new_origin_fingerprint=carol.fingerprint,
        created_at=clock.now_iso(),
    )
    first = bob_node.handle_events(alice.fingerprint, [offer.to_dict()])
    second = bob_node.handle_events(alice.fingerprint, [offer.to_dict()])

    assert first == [offer.content_id]
    assert second == []  # already seen -- silently skipped, not re-applied or errored

    alice.close()
    carol.close()


# -- events: gossiping link_message (design doc round 93) -------------------


def _link_message(alice, bob, clock, *, tier="tier1_home_node_key"):
    return build_link_message(
        signing_identity=alice.identity.signing_key,
        home_node_fingerprint=alice.fingerprint,
        local_user_id="wanderer",
        recipient_home_node_fingerprint=bob.fingerprint,
        recipient_local_user_id="bob",
        confidentiality_tier=tier,
        ciphertext=b"opaque sealed bytes",
        created_at=clock.now_iso(),
    )


def test_handle_events_accepts_a_valid_link_message_addressed_to_this_node(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    bob_node = LinkNode(identity=bob.identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    message = _link_message(alice, bob, clock)
    accepted = bob_node.handle_events(alice.fingerprint, [message.to_dict()])

    assert accepted == [message.content_id]
    assert bob_node.events[message.content_id] == message.to_dict()

    alice.close()
    bob.close()


def test_handle_events_rejects_link_message_addressed_to_a_different_node(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    bob_node = LinkNode(identity=bob.identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    # addressed to carol, delivered to bob -- bob is not the recipient
    # and must refuse it outright, not store it on carol's behalf.
    message = _link_message(alice, carol, clock)

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [message.to_dict()])

    alice.close()
    bob.close()
    carol.close()


def test_handle_events_rejects_link_message_with_unsupported_sender_kind(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    bob_node = LinkNode(identity=bob.identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    message = _link_message(alice, bob, clock)
    raw = message.to_dict()
    raw["envelope"]["payload"]["sender"] = {"kind": "user_key", "fingerprint": "some-user-fingerprint"}

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [raw])

    alice.close()
    bob.close()


def test_handle_events_rejects_link_message_vouching_for_a_different_home_node(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    mallory = spawn_node(tmp_path, "mallory")
    bob_node = LinkNode(identity=bob.identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    # mallory's own valid message, relayed as if it came from alice --
    # mallory has no completed hello with bob at all, so this must be
    # refused even though alice (the actual sender) does.
    forged = build_link_message(
        signing_identity=mallory.identity.signing_key,
        home_node_fingerprint=mallory.fingerprint,
        local_user_id="wanderer",
        recipient_home_node_fingerprint=bob.fingerprint,
        recipient_local_user_id="bob",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque sealed bytes",
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_events(alice.fingerprint, [forged.to_dict()])

    alice.close()
    bob.close()
    mallory.close()


def test_handle_events_link_message_is_idempotent_for_already_seen(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    bob_node = LinkNode(identity=bob.identity)

    alice_hello = _hello_bytes(LinkNode(identity=alice.identity), clock=clock)
    bob_node.handle_hello(alice_hello)

    message = _link_message(alice, bob, clock)
    first = bob_node.handle_events(alice.fingerprint, [message.to_dict()])
    second = bob_node.handle_events(alice.fingerprint, [message.to_dict()])

    assert first == [message.content_id]
    assert second == []  # already seen -- silently skipped

    alice.close()
    bob.close()


# -- events: gossiping link_message_accepted/link_message_bounced (round 93) --


def _seed_own_link_message(alice_node, alice, bob, clock):
    """A message alice_node itself originated and already knows about --
    self-originated events never pass through handle_events (same as
    board_genesis/board_post local origination, round 128), so a test
    exercising the *acknowledgement* path seeds this directly rather
    than round-tripping it through some other node's handle_events
    first."""
    message = _link_message(alice, bob, clock)
    alice_node.known_event_ids.add(message.content_id)
    alice_node.events[message.content_id] = message.to_dict()
    return message


def test_handle_events_accepts_a_valid_link_message_accepted(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)
    message = _seed_own_link_message(alice_node, alice, bob, clock)

    ack = build_link_message_accepted(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint=bob.fingerprint,
        message_content_id=message.content_id,
        created_at=clock.now_iso(),
    )
    accepted = alice_node.handle_events(bob.fingerprint, [ack.to_dict()])

    assert accepted == [ack.content_id]
    assert alice_node.events[ack.content_id] == ack.to_dict()

    alice.close()
    bob.close()


def test_handle_events_rejects_link_message_accepted_for_unknown_message(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)

    # No link_message with this content_id was ever originated by alice_node.
    ack = build_link_message_accepted(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint=bob.fingerprint,
        message_content_id="never-sent-content-id",
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        alice_node.handle_events(bob.fingerprint, [ack.to_dict()])

    alice.close()
    bob.close()


def test_handle_events_rejects_link_message_accepted_for_a_message_this_node_did_not_send(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)

    # A message carol sent (not alice) happens to be known to alice_node
    # (e.g. it observed it some other way) -- alice_node must refuse an
    # acknowledgement about a message it didn't itself originate.
    carols_message = build_link_message(
        signing_identity=carol.identity.signing_key,
        home_node_fingerprint=carol.fingerprint,
        local_user_id="carol",
        recipient_home_node_fingerprint=bob.fingerprint,
        recipient_local_user_id="bob",
        confidentiality_tier="tier1_home_node_key",
        ciphertext=b"opaque sealed bytes",
        created_at=clock.now_iso(),
    )
    alice_node.known_event_ids.add(carols_message.content_id)
    alice_node.events[carols_message.content_id] = carols_message.to_dict()

    ack = build_link_message_accepted(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint=bob.fingerprint,
        message_content_id=carols_message.content_id,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        alice_node.handle_events(bob.fingerprint, [ack.to_dict()])

    alice.close()
    bob.close()
    carol.close()


def test_handle_events_rejects_link_message_accepted_vouching_for_a_different_recipient_node(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)
    message = _seed_own_link_message(alice_node, alice, bob, clock)

    # bob signs an ack claiming a different recipient_node_fingerprint
    # than himself -- the field-level vouching check must catch this
    # even though bob is genuinely the one who sent it.
    ack = build_link_message_accepted(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint="some-other-node-fingerprint",
        message_content_id=message.content_id,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        alice_node.handle_events(bob.fingerprint, [ack.to_dict()])

    alice.close()
    bob.close()


def test_handle_events_rejects_link_message_accepted_from_a_node_the_message_was_not_addressed_to(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    mallory = spawn_node(tmp_path, "mallory")
    alice_node = LinkNode(identity=alice.identity)

    mallory_hello = _hello_bytes(LinkNode(identity=mallory.identity), clock=clock)
    alice_node.handle_hello(mallory_hello)
    # the message was addressed to bob, not mallory
    message = _seed_own_link_message(alice_node, alice, bob, clock)

    # mallory legitimately signs this herself, honestly naming herself
    # as the (wrong) recipient -- still refused, since the original
    # message was never addressed to her.
    ack = build_link_message_accepted(
        signing_identity=mallory.identity.signing_key,
        recipient_node_fingerprint=mallory.fingerprint,
        message_content_id=message.content_id,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        alice_node.handle_events(mallory.fingerprint, [ack.to_dict()])

    alice.close()
    bob.close()
    mallory.close()


def test_handle_events_link_message_accepted_is_idempotent_for_already_seen(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)
    message = _seed_own_link_message(alice_node, alice, bob, clock)

    ack = build_link_message_accepted(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint=bob.fingerprint,
        message_content_id=message.content_id,
        created_at=clock.now_iso(),
    )
    first = alice_node.handle_events(bob.fingerprint, [ack.to_dict()])
    second = alice_node.handle_events(bob.fingerprint, [ack.to_dict()])

    assert first == [ack.content_id]
    assert second == []  # already seen -- silently skipped

    alice.close()
    bob.close()


def test_handle_events_accepts_a_valid_link_message_bounced(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)
    message = _seed_own_link_message(alice_node, alice, bob, clock)

    bounced = build_link_message_bounced(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint=bob.fingerprint,
        message_content_id=message.content_id,
        reason="mailbox_full",
        created_at=clock.now_iso(),
    )
    accepted = alice_node.handle_events(bob.fingerprint, [bounced.to_dict()])

    assert accepted == [bounced.content_id]
    assert alice_node.events[bounced.content_id] == bounced.to_dict()

    alice.close()
    bob.close()


def test_handle_events_rejects_link_message_bounced_for_unknown_message(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)

    bounced = build_link_message_bounced(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint=bob.fingerprint,
        message_content_id="never-sent-content-id",
        reason="unknown_recipient",
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        alice_node.handle_events(bob.fingerprint, [bounced.to_dict()])

    alice.close()
    bob.close()


def test_handle_events_link_message_bounced_is_idempotent_for_already_seen(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)
    message = _seed_own_link_message(alice_node, alice, bob, clock)

    bounced = build_link_message_bounced(
        signing_identity=bob.identity.signing_key,
        recipient_node_fingerprint=bob.fingerprint,
        message_content_id=message.content_id,
        reason="blocked_sender",
        created_at=clock.now_iso(),
    )
    first = alice_node.handle_events(bob.fingerprint, [bounced.to_dict()])
    second = alice_node.handle_events(bob.fingerprint, [bounced.to_dict()])

    assert first == [bounced.content_id]
    assert second == []  # already seen -- silently skipped

    alice.close()
    bob.close()


# -- peer-list exchange (design doc round 95) --------------------------------


def _descriptor_for(node, clock, *, created_at=None, outgoing_only=False):
    return build_endpoint_descriptor(
        signing_identity=node.identity.signing_key,
        subject_fingerprint=node.fingerprint,
        addresses=None if outgoing_only else [{"protocol": "http", "address": "203.0.113.1", "port": 7862}],
        outgoing_only=outgoing_only,
        created_at=created_at or clock.now_iso(),
    )


def test_build_peer_list_returns_own_verified_peers_descriptors(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)

    peer_list = alice_node.build_peer_list()

    assert [d.payload["subject_fingerprint"] for d in peer_list.descriptors] == [bob.fingerprint]

    alice.close()
    bob.close()


def test_handle_peer_list_records_new_candidates(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)

    carol_descriptor = _descriptor_for(carol, clock)
    message = PeerListMessage(descriptors=(carol_descriptor,))

    recorded = alice_node.handle_peer_list(bob.fingerprint, message)

    assert recorded == [carol.fingerprint]
    assert alice_node.candidate_descriptors[carol.fingerprint].content_id == carol_descriptor.content_id
    # not promoted to a real peer just from a secondhand claim
    assert carol.fingerprint not in alice_node.peers

    alice.close()
    bob.close()
    carol.close()


def test_handle_peer_list_rejects_from_a_peer_with_no_completed_hello(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    alice_node = LinkNode(identity=alice.identity)

    message = PeerListMessage(descriptors=(_descriptor_for(carol, clock),))

    with pytest.raises(LinkProtocolError):
        alice_node.handle_peer_list(bob.fingerprint, message)

    alice.close()
    bob.close()
    carol.close()


def test_handle_peer_list_skips_this_nodes_own_fingerprint(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)

    # bob (harmlessly, or maliciously) hands alice's own descriptor back to her
    own_descriptor = _descriptor_for(alice, clock)
    recorded = alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=(own_descriptor,)))

    assert recorded == []
    assert alice_node.candidate_descriptors == {}

    alice.close()
    bob.close()


def test_handle_peer_list_skips_a_fingerprint_already_a_verified_peer(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)
    carol_hello = _hello_bytes(LinkNode(identity=carol.identity), clock=clock)
    alice_node.handle_hello(carol_hello)

    # bob shares a descriptor for carol, whom alice already directly knows
    recorded = alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=(_descriptor_for(carol, clock),)))

    assert recorded == []
    assert carol.fingerprint not in alice_node.candidate_descriptors

    alice.close()
    bob.close()
    carol.close()


def test_handle_hello_clears_a_matching_candidate_entry(tmp_path, clock):
    """Once a real hello with a fingerprint completes, an earlier
    secondhand candidate entry for it is superseded, not left sitting
    alongside the real thing."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)
    alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=(_descriptor_for(carol, clock),)))
    assert carol.fingerprint in alice_node.candidate_descriptors

    carol_hello = _hello_bytes(LinkNode(identity=carol.identity), clock=clock)
    alice_node.handle_hello(carol_hello)

    assert carol.fingerprint not in alice_node.candidate_descriptors
    assert carol.fingerprint in alice_node.peers

    alice.close()
    bob.close()
    carol.close()


def test_handle_peer_list_skips_a_stale_descriptor_for_an_existing_candidate(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)

    newer = _descriptor_for(carol, clock, created_at="2026-01-02T00:00:00+00:00")
    alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=(newer,)))

    older = _descriptor_for(carol, clock, created_at="2026-01-01T00:00:00+00:00")
    recorded = alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=(older,)))

    assert recorded == []
    assert alice_node.candidate_descriptors[carol.fingerprint].content_id == newer.content_id

    alice.close()
    bob.close()
    carol.close()


def test_handle_peer_list_refreshes_a_candidate_with_a_newer_descriptor(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)

    older = _descriptor_for(carol, clock, created_at="2026-01-01T00:00:00+00:00")
    alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=(older,)))

    newer = _descriptor_for(carol, clock, created_at="2026-01-02T00:00:00+00:00")
    recorded = alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=(newer,)))

    assert recorded == [carol.fingerprint]
    assert alice_node.candidate_descriptors[carol.fingerprint].content_id == newer.content_id

    alice.close()
    bob.close()
    carol.close()


def test_handle_peer_list_rejects_an_oversized_request(tmp_path, clock):
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)

    too_many = [_descriptor_for(spawn_node(tmp_path, f"stranger-{i}"), clock) for i in range(101)]

    with pytest.raises(LinkProtocolError):
        alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=tuple(too_many)))

    alice.close()
    bob.close()


def test_handle_peer_list_stops_accepting_brand_new_candidates_once_at_cap(tmp_path, clock, monkeypatch):
    import netbbs.link.protocol as protocol_module

    monkeypatch.setattr(protocol_module, "_MAX_CANDIDATE_DESCRIPTORS", 1)

    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    carol = spawn_node(tmp_path, "carol")
    dave = spawn_node(tmp_path, "dave")
    alice_node = LinkNode(identity=alice.identity)

    bob_hello = _hello_bytes(LinkNode(identity=bob.identity), clock=clock)
    alice_node.handle_hello(bob_hello)

    first = alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=(_descriptor_for(carol, clock),)))
    assert first == [carol.fingerprint]

    # at cap now -- a brand new fingerprint (dave) is refused...
    second = alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=(_descriptor_for(dave, clock),)))
    assert second == []
    assert dave.fingerprint not in alice_node.candidate_descriptors

    # ...but refreshing the existing candidate (carol) still works.
    refreshed = _descriptor_for(carol, clock, created_at="2026-01-02T00:00:00+00:00")
    third = alice_node.handle_peer_list(bob.fingerprint, PeerListMessage(descriptors=(refreshed,)))
    assert third == [carol.fingerprint]

    alice.close()
    bob.close()
    carol.close()
    dave.close()
