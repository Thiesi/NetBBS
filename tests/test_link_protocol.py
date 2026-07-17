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
    build_board_post,
    build_board_post_edit,
    build_endpoint_descriptor,
    build_key_transition,
)
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
