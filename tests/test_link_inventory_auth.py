"""
Tests for `netbbs.link.protocol.LinkNode.handle_inventory_request` (issue
#106): verification-only, mirroring `tests/test_link_relay_consent.py`'s
own scope and structure for `handle_relay_consent_request` exactly, since
both methods share the identical three-part shape (completed-peer check,
claimed-identity cross-check, signature verification) and neither mutates
any state -- these tests check verification outcomes directly, never a
real HTTP round trip (see `tests/test_link_transport.py` for the real-
socket proof that a refusal here actually becomes a 403 and that a valid
request still lets a peer discover carried content).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from netbbs.link.events import sign_inventory_request
from netbbs.link.protocol import InventoryRequest, LinkNode, LinkProtocolError
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


def _signed_empty_request(
    *,
    signing_identity,
    requester_fingerprint,
    responder_fingerprint,
    created_at,
    nonce="0123456789abcdef0123456789abcdef",
) -> InventoryRequest:
    signature = sign_inventory_request(
        signing_identity=signing_identity,
        requester_fingerprint=requester_fingerprint,
        responder_fingerprint=responder_fingerprint,
        created_at=created_at,
        nonce=nonce,
        boards={}, channels={}, file_areas={},
    )
    return InventoryRequest(
        requester_fingerprint=requester_fingerprint,
        responder_fingerprint=responder_fingerprint,
        created_at=created_at,
        nonce=nonce,
        signature=signature,
        boards={}, channels={}, file_areas={},
    )


def test_handle_inventory_request_accepts_a_valid_request_from_a_completed_peer(tmp_path, clock):
    """The bootstrap-discovery case (issue #94) remains reachable: a
    completed peer's genuinely empty, correctly-signed inventory request
    passes verification -- nothing here refuses it just for asking about
    nothing."""
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)

    request = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )

    bob_node.handle_inventory_request(
        alice.fingerprint, request, now_iso=clock.now_iso()
    )  # does not raise

    alice.close()
    bob.close()


def test_handle_inventory_request_refuses_a_stranger(tmp_path, clock):
    """The exact case issue #106 exists for: before any resource
    enumeration is even considered, the caller must already be a
    completed peer -- a validly self-signed request from someone bob has
    never said hello to is still refused."""
    alice = spawn_node(tmp_path, "alice")
    bob = spawn_node(tmp_path, "bob")
    bob_node = LinkNode(identity=bob.identity)  # bob never completed a hello with alice

    request = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_inventory_request(alice.fingerprint, request)

    alice.close()
    bob.close()


def test_handle_inventory_request_rejects_a_mismatched_requester_claim(tmp_path, clock):
    """A completed peer cannot enumerate on some other identity's
    behalf: alice sends the request (and the URL names her as sender),
    but the signed payload itself claims mallory's fingerprint."""
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    mallory = spawn_node(tmp_path, "mallory")

    request = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=mallory.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_inventory_request(alice.fingerprint, request)

    alice.close()
    bob.close()
    mallory.close()


def test_handle_inventory_request_rejects_a_forged_signature(tmp_path, clock):
    """Merely claiming a known, completed peer's fingerprint is not
    enough -- the request must actually be signed by that peer's own
    current key, not an arbitrary stranger's. This is the crux of issue
    #106: fingerprints are discoverable (e.g. via `/peers`), so the
    check that actually matters is proof of key possession, not the
    claim alone."""
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    mallory = spawn_node(tmp_path, "mallory")

    # Signed by mallory, but claiming to be from alice (a real completed peer).
    request = _signed_empty_request(
        signing_identity=mallory.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError):
        bob_node.handle_inventory_request(alice.fingerprint, request)

    alice.close()
    bob.close()
    mallory.close()


def test_handle_inventory_request_rejects_a_request_signed_for_another_responder(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    mallory = spawn_node(tmp_path, "mallory")
    request = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=mallory.fingerprint,
        created_at=clock.now_iso(),
    )

    with pytest.raises(LinkProtocolError, match="addressed to"):
        bob_node.handle_inventory_request(alice.fingerprint, request, now_iso=clock.now_iso())

    alice.close()
    bob.close()
    mallory.close()


def test_handle_inventory_request_rejects_stale_and_future_requests(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    stale = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
        nonce="11111111111111111111111111111111",
    )
    clock.advance(seconds=301)

    with pytest.raises(LinkProtocolError, match="freshness window"):
        bob_node.handle_inventory_request(alice.fingerprint, stale, now_iso=clock.now_iso())

    future_at = (clock.now() + timedelta(seconds=301)).isoformat()
    future = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=future_at,
        nonce="22222222222222222222222222222222",
    )
    with pytest.raises(LinkProtocolError, match="freshness window"):
        bob_node.handle_inventory_request(alice.fingerprint, future, now_iso=clock.now_iso())

    alice.close()
    bob.close()


def test_handle_inventory_request_rejects_an_exact_replay(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    request = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )
    bob_node.handle_inventory_request(alice.fingerprint, request, now_iso=clock.now_iso())

    with pytest.raises(LinkProtocolError, match="reuses a recent nonce"):
        bob_node.handle_inventory_request(alice.fingerprint, request, now_iso=clock.now_iso())

    alice.close()
    bob.close()


def test_inventory_security_fields_are_covered_by_the_signature(tmp_path, clock):
    alice, bob, alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    request = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )
    request.created_at = (clock.now() + timedelta(seconds=1)).isoformat()

    with pytest.raises(LinkProtocolError, match="current signing key"):
        bob_node.handle_inventory_request(alice.fingerprint, request, now_iso=clock.now_iso())

    alice.close()
    bob.close()


# -- Issue #669: `not_carried` ---------------------------------------------------


def _signed_request_with_not_carried(alice, bob, clock, not_carried):
    from netbbs.link.events import sign_inventory_request as sign

    nonce = "fedcba9876543210fedcba9876543210"
    signature = sign(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
        nonce=nonce,
        boards={}, channels={}, file_areas={},
        not_carried=not_carried,
    )
    return InventoryRequest(
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
        nonce=nonce,
        signature=signature,
        boards={}, channels={}, file_areas={},
        not_carried=not_carried,
    )


def test_a_request_naming_nothing_not_carried_signs_exactly_as_before(tmp_path, clock):
    """An empty `not_carried` stays out of the signed payload and the wire
    form, so an older requester and a newer responder still agree on what
    was signed."""
    alice, bob, _alice_node, _bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    old = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
        nonce="fedcba9876543210fedcba9876543210",
    )
    new = _signed_request_with_not_carried(alice, bob, clock, {"boards": ()})
    assert new.signature == old.signature
    assert "not_carried" not in new.to_dict()
    alice.close()
    bob.close()


def test_not_carried_is_signed_and_verifies_round_trip(tmp_path, clock):
    alice, bob, _alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    request = _signed_request_with_not_carried(alice, bob, clock, {"boards": ("b1",), "file_areas": ("f1",)})

    wire = InventoryRequest.from_dict(request.to_dict())
    assert wire.not_carried == {"boards": ("b1",), "file_areas": ("f1",)}
    bob_node.handle_inventory_request(alice.fingerprint, wire, now_iso=clock.now_iso())  # does not raise

    # A responder that does not know the field verifies without it and
    # refuses -- which is why it is sent only where it is advertised.
    from netbbs.link.events import verify_inventory_request

    assert not verify_inventory_request(
        requester_fingerprint=wire.requester_fingerprint,
        responder_fingerprint=wire.responder_fingerprint,
        created_at=wire.created_at,
        nonce=wire.nonce,
        boards={}, channels={}, file_areas={},
        signature=wire.signature,
        signing_verify_key=alice.identity.signing_key.verify_key,
    )
    alice.close()
    bob.close()


def test_not_carried_cannot_be_added_after_signing(tmp_path, clock):
    """It steers what the responder leaves out, so a third party must not be
    able to suppress content by inserting it into someone else's request."""
    alice, bob, _alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    request = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )
    data = request.to_dict()
    data["not_carried"] = {"boards": ["b1"]}
    with pytest.raises(LinkProtocolError):
        bob_node.handle_inventory_request(alice.fingerprint, InventoryRequest.from_dict(data), now_iso=clock.now_iso())
    alice.close()
    bob.close()


@pytest.mark.parametrize(
    "not_carried",
    [["b1"], {"posts": ["b1"]}, {"boards": "b1"}, {"boards": [1]}],
    ids=["not-an-object", "unknown-kind", "not-a-list", "not-strings"],
)
def test_a_malformed_not_carried_is_a_malformed_request(tmp_path, clock, not_carried):
    alice, bob, _alice_node, _bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    data = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    ).to_dict()
    data["not_carried"] = not_carried
    with pytest.raises(ValueError):
        InventoryRequest.from_dict(data)
    alice.close()
    bob.close()


def test_the_not_carried_declaration_is_bounded_and_sampled_fresh():
    """A peer can keep sending geneses to a node past its carry cap; declaring
    every one would grow each request until the responder refused it (413)."""
    from netbbs.link.store import bound_not_carried

    many = {"boards": tuple(f"b{i:05d}" for i in range(30)), "channels": tuple(f"c{i:05d}" for i in range(30))}
    small = {"boards": ("b1",)}
    assert bound_not_carried(small, limit=10) is small

    samples = [bound_not_carried(many, limit=10) for _ in range(20)]
    for sample in samples:
        assert sum(len(ids) for ids in sample.values()) == 10
        assert set(sample) <= {"boards", "channels"}
        for kind, ids in sample.items():
            assert set(ids) <= set(many[kind])
    # A fresh sample each time, so no fixed subset is left out on every pass.
    assert len({tuple(sorted((k, i) for k, ids in s.items() for i in ids)) for s in samples}) > 1


# -- Issue #685: a declaration split into pages -------------------------------


def _signed_paged_request(alice, bob, clock, page):
    signature = sign_inventory_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
        nonce="0123456789abcdef0123456789abcdef",
        boards={"b1": ("c1",)}, channels={}, file_areas={},
        page=page,
    )
    return InventoryRequest(
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
        nonce="0123456789abcdef0123456789abcdef",
        signature=signature,
        boards={"b1": ("c1",)},
        page=page,
    )


def test_a_paged_request_round_trips_and_verifies(tmp_path, clock):
    alice, bob, _alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    request = _signed_paged_request(alice, bob, clock, (1, 3))
    data = request.to_dict()
    assert data["page"] == {"index": 1, "count": 3}
    parsed = InventoryRequest.from_dict(data)
    assert parsed.page == (1, 3)
    bob_node.handle_inventory_request(alice.fingerprint, parsed, now_iso=clock.now_iso())
    alice.close()
    bob.close()


def test_the_page_is_signed(tmp_path, clock):
    """It steers what the responder leaves out, so a relay must not be able to
    move a request to another page."""
    alice, bob, _alice_node, bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    data = _signed_paged_request(alice, bob, clock, (1, 3)).to_dict()
    data["page"] = {"index": 2, "count": 3}
    with pytest.raises(LinkProtocolError):
        bob_node.handle_inventory_request(alice.fingerprint, InventoryRequest.from_dict(data), now_iso=clock.now_iso())
    alice.close()
    bob.close()


def test_a_whole_declaration_omits_the_page(tmp_path, clock):
    """A one-page request is the pre-#685 shape byte for byte, so it still
    verifies at a responder that has never heard of pages."""
    alice, bob, _alice_node, _bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    request = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    )
    assert "page" not in request.to_dict()
    alice.close()
    bob.close()


@pytest.mark.parametrize(
    "page",
    [[0, 2], {"index": 0}, {"index": 0, "count": 2, "x": 1}, {"index": "0", "count": 2},
     {"index": True, "count": 2}, {"index": 2, "count": 2}, {"index": -1, "count": 2},
     {"index": 0, "count": 1}, {"index": 0, "count": 4097}],
    ids=["not-an-object", "no-count", "extra-key", "string", "bool", "index-past-end",
         "negative", "one-page", "too-many-pages"],
)
def test_a_malformed_page_is_a_malformed_request(tmp_path, clock, page):
    alice, bob, _alice_node, _bob_node = _two_nodes_with_completed_hello(tmp_path, clock)
    data = _signed_empty_request(
        signing_identity=alice.identity.signing_key,
        requester_fingerprint=alice.fingerprint,
        responder_fingerprint=bob.fingerprint,
        created_at=clock.now_iso(),
    ).to_dict()
    data["page"] = page
    with pytest.raises(ValueError):
        InventoryRequest.from_dict(data)
    alice.close()
    bob.close()


def test_inventory_page_is_stable_spreads_evenly_and_changes_with_the_salt():
    """Both sides must compute the same split for one request; and a new
    request, with a new nonce, must split differently, or a peer could craft
    IDs that share one page and push it past the body limit forever."""
    from netbbs.link.protocol import inventory_page

    ids = [f"{i:064x}" for i in range(4000)]
    pages = [inventory_page(i, 4, "salt-a") for i in ids]
    assert pages == [inventory_page(i, 4, "salt-a") for i in ids]
    for page in range(4):
        assert 800 < pages.count(page) < 1200
    crafted = [i for i, page in zip(ids, pages) if page == 0]
    # IDs that all shared page 0 under one salt spread out again under another.
    still_together = sum(inventory_page(i, 4, "salt-b") == 0 for i in crafted)
    assert len(crafted) // 8 < still_together < len(crafted) * 3 // 8


def test_a_paged_request_declares_its_whole_share_of_not_carried(tmp_path, monkeypatch):
    """#669 capped `not_carried` with a random sample, so a refused set larger
    than the cap was never suppressed in full. Paged, each request declares
    every declined resource that falls on its page -- and the responder skips
    undeclared resources off the page -- so none is ever resent."""
    from netbbs.link import store as store_module
    from netbbs.link.protocol import inventory_page

    alice = spawn_node(tmp_path, "alice")
    declined = {"boards": tuple(f"b{i:04d}" for i in range(600)), "channels": tuple(f"c{i:04d}" for i in range(400))}
    monkeypatch.setattr(store_module, "uncarried_resource_ids", lambda db: declined)
    monkeypatch.setattr(store_module, "MAX_NOT_CARRIED_DECLARED", 200)

    def build(cursor, *, paged=True):
        return store_module.build_inventory_request(
            alice.db, signing_identity=alice.identity.signing_key,
            requester_fingerprint=alice.fingerprint, responder_fingerprint="responder",
            declare_not_carried=True, paged=paged, page_cursor=cursor,
        )

    first = build(0)
    index, count = first.page
    assert count >= 10
    for cursor in range(count):
        request = build(cursor)
        index = request.page[0]
        on_page = {
            (kind, r) for kind, ids in declined.items() for r in ids
            if inventory_page(r, count, request.nonce) == index
        }
        assert on_page
        assert {(kind, r) for kind, ids in request.not_carried.items() for r in ids} == on_page
    # Unpaged, the old sample: bounded, but never the whole set at once.
    assert sum(len(ids) for ids in build(0, paged=False).not_carried.values()) == 200
    alice.close()
