"""Carrying the trust objects of a node nobody can dial (issue #627, design doc §12.7).

Trust objects are pulled from their issuer, and an outgoing-only node cannot be
dialed, so nothing it signed could reach anyone. It deposits them at the nodes
that relay for it, which keep them apart from anything they act on themselves.

The cast: R relays, A issues and cannot be dialed.
"""

from __future__ import annotations

import pytest

from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode, LinkProtocolError
from netbbs.link.store import build_inventory_request
from netbbs.link.trust import TrustSubject
from netbbs.link.trust_carriage import (
    TrustCarriageFull,
    carries_trust_objects_for,
    clear_trust_deposit_position,
    load_own_trust_objects_to_deposit,
    load_trust_page_for_pull,
    save_trust_deposit_position,
    store_deposited_trust_objects,
)
from netbbs.link.trust_wire import (
    UnknownTrustPullCursor,
    build_trust_revocation,
    build_trust_vouch,
    store_issued_trust_object,
)
from netbbs.storage.database import Database

SUBJECT = TrustSubject.node("a-third-node-fingerprint")
ISSUED = "2026-09-18T12:00:00+00:00"
EXPIRES = "2026-12-17T12:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "relay.db")
    yield database
    database.close()


@pytest.fixture
def cast():
    r = LinkNode(identity=bootstrap_node_identity("R"))
    a = LinkNode(identity=bootstrap_node_identity("A"))
    for one, other in ((r, a), (a, r)):
        one.handle_hello(other.build_hello(
            addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00",
        ))
    r.relaying_for[a.identity.fingerprint] = ISSUED
    return {"R": r, "A": a}


def vouch(node: LinkNode, vouch_id: str = "v1", *, identity=None, expires_at: str = EXPIRES):
    return build_trust_vouch(
        signing_identity=(identity or node.identity).signing_key,
        issuer_fingerprint=node.identity.fingerprint, vouch_id=vouch_id, subject=SUBJECT,
        issued_at=ISSUED, expires_at=expires_at, explanation="known operator",
    )


def revocation(node: LinkNode, target):
    return build_trust_revocation(
        signing_identity=node.identity.signing_key, issuer_fingerprint=node.identity.fingerprint,
        revocation_id="r1", revoked_content_id=target.content_id, issued_at=ISSUED, vouch=True,
    )


def authorization(db, sender: LinkNode, responder: LinkNode):
    return build_inventory_request(
        db, signing_identity=sender.identity.signing_key,
        requester_fingerprint=sender.identity.fingerprint,
        responder_fingerprint=responder.identity.fingerprint, include_inventory=False,
    )


# -- who may deposit, and what --------------------------------------------------------------------


def test_a_relay_takes_the_objects_of_a_node_it_relays_for(db, cast):
    r, a = cast["R"], cast["A"]
    signed = vouch(a)

    verified, unverifiable = r.handle_trust_deposit(
        a.identity.fingerprint, authorization(db, a, r), [signed.to_dict()]
    )

    assert [obj.content_id for obj in verified] == [signed.content_id] and unverifiable == 0


def test_a_node_this_one_does_not_relay_for_is_refused(db, cast):
    """Relay consent is the existing opt-in, and the existing cap, on whom this
    node holds things for."""
    r, a = cast["R"], cast["A"]
    r.relaying_for.clear()

    with pytest.raises(LinkProtocolError, match="does not relay for"):
        r.handle_trust_deposit(a.identity.fingerprint, authorization(db, a, r), [vouch(a).to_dict()])


def test_only_the_issuer_may_deposit_because_only_the_issuer_may_order(db, cast):
    """The objects would verify whoever sent them. The order they are stored in
    is the order subscribers read them in, and a third party replaying a
    withdrawn vouch ahead of its revocation would bring it back to life."""
    r, a = cast["R"], cast["A"]
    other = LinkNode(identity=bootstrap_node_identity("other"))
    r.handle_hello(other.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00"))
    r.relaying_for[other.identity.fingerprint] = ISSUED

    # Signed by A, deposited by another node R relays for: A's key does not
    # verify under the depositor's, so it is left out...
    verified, unverifiable = r.handle_trust_deposit(
        other.identity.fingerprint, authorization(db, other, r), [vouch(a).to_dict()]
    )
    assert verified == [] and unverifiable == 1
    # ...and an object the depositor signed itself in another node's name is refused outright.
    forged = build_trust_vouch(
        signing_identity=other.identity.signing_key, issuer_fingerprint=a.identity.fingerprint,
        vouch_id="v1", subject=SUBJECT, issued_at=ISSUED, expires_at=EXPIRES,
    )
    with pytest.raises(LinkProtocolError, match="issued itself"):
        r.handle_trust_deposit(other.identity.fingerprint, authorization(db, other, r), [forged.to_dict()])


def test_the_authorization_cannot_be_replayed_or_carry_an_inventory(db, cast):
    r, a = cast["R"], cast["A"]
    once = authorization(db, a, r)
    r.handle_trust_deposit(a.identity.fingerprint, once, [])

    with pytest.raises(LinkProtocolError, match="nonce"):
        r.handle_trust_deposit(a.identity.fingerprint, once, [])
    with pytest.raises(LinkProtocolError, match="list of at most"):
        r.handle_trust_deposit(a.identity.fingerprint, authorization(db, a, r), [{}] * 101)


def test_what_an_earlier_key_signed_is_left_out_and_the_rest_is_taken(db, cast):
    """An issuer's own store keeps what its earlier keys signed. Nobody holding
    its current key can use those, and it re-signs what still matters."""
    r, a = cast["R"], cast["A"]
    current = vouch(a, "current")
    stale = vouch(a, "stale", identity=bootstrap_node_identity("not-a's-key"))

    verified, unverifiable = r.handle_trust_deposit(
        a.identity.fingerprint, authorization(db, a, r), [stale.to_dict(), current.to_dict()]
    )

    assert [obj.content_id for obj in verified] == [current.content_id] and unverifiable == 1


def test_an_authentic_object_this_release_does_not_understand_is_carried_all_the_same(db, cast):
    """A newer issuer's object type. A carrier has no use for the payload, and
    refusing would stop that issuer's deposits at this object for as long as
    the relay stays on its version."""
    import base64

    from netbbs.link.events import build_envelope, canonical_bytes

    r, a = cast["R"], cast["A"]
    envelope = build_envelope("trust_something_newer", {"issuer_fingerprint": a.identity.fingerprint, "x": 1})
    novel = {
        "envelope": envelope,
        "signature": base64.b64encode(a.identity.signing_key.sign(canonical_bytes(envelope))).decode("ascii"),
    }

    verified, unverifiable = r.handle_trust_deposit(
        a.identity.fingerprint, authorization(db, a, r), [novel, vouch(a).to_dict()]
    )
    stored, _held = store_deposited_trust_objects(db, a.identity.fingerprint, verified)

    assert unverifiable == 0 and len(stored) == 2
    page, _more = load_trust_page_for_pull(
        db, own_fingerprint=r.identity.fingerprint, issuer_fingerprint=a.identity.fingerprint,
    )
    assert page[0] == novel


# -- the carried store ----------------------------------------------------------------------------------


def test_what_is_carried_is_served_in_the_order_it_was_deposited_and_never_admitted(db, cast):
    r, a = cast["R"], cast["A"]
    first = vouch(a)
    second = revocation(a, first)

    stored, held = store_deposited_trust_objects(db, a.identity.fingerprint, [first, second])
    again = store_deposited_trust_objects(db, a.identity.fingerprint, [first])

    assert stored == [first.content_id, second.content_id] and held == []
    assert again == ([], [first.content_id])
    page, more = load_trust_page_for_pull(
        db, own_fingerprint=r.identity.fingerprint, issuer_fingerprint=a.identity.fingerprint,
    )
    assert [item["envelope"]["object_type"] for item in page] == ["trust_vouch", "trust_vouch_revocation"]
    assert not more
    # Carriage grants nothing: this node acts on none of it.
    for table in ("link_trust_wire_objects", "link_trust_vouches", "link_trust_subjects"):
        assert db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_a_cursor_from_the_other_store_is_unknown_so_the_subscriber_starts_over(db, cast):
    r, a = cast["R"], cast["A"]
    store_deposited_trust_objects(db, a.identity.fingerprint, [vouch(a)])

    with pytest.raises(UnknownTrustPullCursor):
        load_trust_page_for_pull(
            db, own_fingerprint=r.identity.fingerprint, issuer_fingerprint=a.identity.fingerprint,
            after_content_id="c" * 64,
        )


def test_a_nodes_own_objects_are_still_served_from_what_it_issued(db, cast):
    r = cast["R"]
    own = vouch(r)
    store_issued_trust_object(db, own, issued_at=ISSUED)

    page, _more = load_trust_page_for_pull(
        db, own_fingerprint=r.identity.fingerprint, issuer_fingerprint=r.identity.fingerprint,
    )

    assert [item["envelope"]["payload"]["vouch_id"] for item in page] == ["v1"]


def test_carriage_is_bounded_per_depositor_and_a_deposit_is_all_or_nothing(db, cast, monkeypatch):
    from netbbs.link import trust_carriage

    monkeypatch.setattr(trust_carriage, "MAX_CARRIED_TRUST_OBJECTS_PER_ISSUER", 2)
    a = cast["A"]
    store_deposited_trust_objects(db, a.identity.fingerprint, [vouch(a, "v1")])

    with pytest.raises(TrustCarriageFull):
        store_deposited_trust_objects(db, a.identity.fingerprint, [vouch(a, "v2"), vouch(a, "v3")])

    assert db.connection.execute("SELECT COUNT(*) FROM link_trust_carried_objects").fetchone()[0] == 1


def test_an_expired_object_stops_being_carried(db, cast):
    a = cast["A"]
    store_deposited_trust_objects(
        db, a.identity.fingerprint, [vouch(a, "old", expires_at="2026-09-19T00:00:00+00:00")],
        now_iso="2026-09-18T12:00:00+00:00",
    )

    store_deposited_trust_objects(
        db, a.identity.fingerprint, [vouch(a, "new")], now_iso="2026-09-20T00:00:00+00:00",
    )

    assert carries_trust_objects_for(db, a.identity.fingerprint)
    assert db.connection.execute("SELECT COUNT(*) FROM link_trust_carried_objects").fetchone()[0] == 1


# -- the depositor's position at each relay ----------------------------------------------------------------


def test_each_relay_is_brought_up_to_date_from_its_own_position(db, cast):
    r, a = cast["R"], cast["A"]
    first, second = vouch(a, "v1"), vouch(a, "v2")
    store_issued_trust_object(db, first, issued_at=ISSUED)

    objects, position = load_own_trust_objects_to_deposit(
        db, own_fingerprint=a.identity.fingerprint, relay_fingerprint=r.identity.fingerprint,
    )
    assert [o["envelope"]["payload"]["vouch_id"] for o in objects] == ["v1"]
    save_trust_deposit_position(db, r.identity.fingerprint, position)
    store_issued_trust_object(db, second, issued_at=ISSUED)

    objects, position = load_own_trust_objects_to_deposit(
        db, own_fingerprint=a.identity.fingerprint, relay_fingerprint=r.identity.fingerprint,
    )
    assert [o["envelope"]["payload"]["vouch_id"] for o in objects] == ["v2"]
    # Another relay, or this one selected afresh, starts from the beginning.
    clear_trust_deposit_position(db, r.identity.fingerprint)
    objects, _position = load_own_trust_objects_to_deposit(
        db, own_fingerprint=a.identity.fingerprint, relay_fingerprint=r.identity.fingerprint,
    )
    assert len(objects) == 2
    save_trust_deposit_position(db, r.identity.fingerprint, position)
    assert load_own_trust_objects_to_deposit(
        db, own_fingerprint=a.identity.fingerprint, relay_fingerprint=r.identity.fingerprint,
    ) == ([], None)


# -- verification against an identity known only by introduction -----------------------------------------------


def test_a_trust_object_verifies_against_an_introduced_issuer_and_a_wire_peer_still_has_to_be_met(cast):
    a = cast["A"]
    b = LinkNode(identity=bootstrap_node_identity("B"))
    b.handle_introduction(a.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00"))

    assert b.resolve_known_signing_key(a.identity.fingerprint) is not None
    assert b.resolve_known_superseded_signing_keys(a.identity.fingerprint) == []
    with pytest.raises(LinkProtocolError, match="unknown issuer"):
        b.resolve_peer_signing_key(a.identity.fingerprint)


def test_a_carrier_says_who_a_node_it_relays_for_is(cast):
    """A subscriber to what that node deposits has to be able to learn who it
    is, and the node publishes this relay in its own descriptor anyway."""
    r, a = cast["R"], cast["A"]

    assert len(r.build_identity_response((a.identity.fingerprint,))) == 1
    r.relaying_for.clear()
    assert r.build_identity_response((a.identity.fingerprint,)) == []
