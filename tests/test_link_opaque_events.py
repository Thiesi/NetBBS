"""
Link events of a type this node does not understand (design doc §7.5, issue
#1022): kept and relayed opaquely, never projected, bounded per sending peer
and by age, and judged properly at startup once the node understands them.
"""

from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path

import pytest

from netbbs.link import store as store_module
from netbbs.link.boards import board_posting_mode, materialize_carried_board
from netbbs.link.events import build_board_genesis, build_board_post, build_board_posting, build_envelope
from netbbs.link.protocol import (
    KNOWN_EVENT_OBJECT_TYPES,
    OPAQUE_EVENT_MAX_BYTES,
    LinkNode,
    LinkProtocolError,
)
from netbbs.link.store import (
    _all_board_events,
    load_link_node,
    purge_expired_opaque_events,
    store_opaque_event,
)
from netbbs.link.transport import persist_accepted_events, rejudge_opaque_events
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.link_harness import spawn_node
from tests.test_link_protocol import _hello_bytes, _linked_board, clock  # noqa: F401 -- clock is a fixture

BOARD = "existing-local-board-id"


def _future(signer, *, object_type="board_frobnicate", board_id=BOARD, **payload):
    """An event of a type no build of this node understands yet, signed the
    way any event is."""
    from netbbs.link.events import canonical_bytes

    envelope = build_envelope(object_type, {"board_id": board_id, "created_at": "2026-02-01T00:00:00Z", **payload})
    import base64

    return {"envelope": envelope, "signature": base64.b64encode(signer.identity.signing_key.sign(
        canonical_bytes(envelope))).decode("ascii")}


def _setup(tmp_path, clock):  # noqa: F811
    alice = spawn_node(tmp_path, "alice")
    bob = LinkNode(identity=spawn_node(tmp_path, "bob").identity)
    bob.handle_hello(_hello_bytes(LinkNode(identity=alice.identity), clock=clock))
    genesis = _linked_board(alice, bob, clock, board_id=BOARD)
    return alice, bob, genesis


def _post(alice, clock, subject):  # noqa: F811
    return build_board_post(
        signing_identity=alice.identity.signing_key, home_node_fingerprint=alice.fingerprint,
        local_user_id="wanderer", board_id=BOARD, subject=subject, body="b", created_at=clock.now_iso(),
    )


# -- receiving


def test_an_unknown_type_is_accepted_with_the_rest_of_its_batch(tmp_path, clock):  # noqa: F811
    alice, bob, _ = _setup(tmp_path, clock)
    first, last = _post(alice, clock, "one"), _post(alice, clock, "two")
    future = _future(alice)

    accepted = bob.handle_events(alice.fingerprint, [first.to_dict(), future, last.to_dict()])

    assert len(accepted) == 3 and first.content_id in accepted and last.content_id in accepted
    assert bob.handle_events(alice.fingerprint, [future]) == []  # known now
    alice.close()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda raw: raw["envelope"].__setitem__("object_type", "Bad Type!"), "malformed event object_type"),
        (lambda raw: raw["envelope"].__setitem__("payload", "not a dict"), "malformed board_frobnicate"),
        (lambda raw: raw.__setitem__("signature", 42), "malformed board_frobnicate"),
        (lambda raw: raw["envelope"]["payload"].__setitem__("blob", "x" * OPAQUE_EVENT_MAX_BYTES), "larger than"),
    ],
)
def test_a_malformed_or_oversized_unknown_event_is_refused(tmp_path, clock, change, message):  # noqa: F811
    alice, bob, _ = _setup(tmp_path, clock)
    raw = copy.deepcopy(_future(alice))
    change(raw)

    with pytest.raises(LinkProtocolError, match=message):
        bob.handle_events(alice.fingerprint, [raw])
    alice.close()


# -- keeping


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _persist(db, node, accepted, sender):
    lane = DatabaseLane(db.path)
    try:
        asyncio.run(persist_accepted_events(lane, node, accepted, sender_fingerprint=sender, max_carried_boards=None))
    finally:
        lane.close()


def test_kept_opaquely_never_projected_and_served_with_its_board(tmp_path, db, clock):  # noqa: F811
    alice, bob, genesis = _setup(tmp_path, clock)
    materialize_carried_board(db, genesis)
    future = _future(alice)
    accepted = bob.handle_events(alice.fingerprint, [future])

    _persist(db, bob, accepted, alice.fingerprint)

    row = db.connection.execute("SELECT object_type, board_id FROM opaque_events").fetchone()
    assert (row["object_type"], row["board_id"]) == ("board_frobnicate", BOARD)
    assert db.connection.execute("SELECT COUNT(*) FROM link_events").fetchone()[0] == 0
    assert accepted[0] in _all_board_events(db, BOARD)
    alice.close()


def test_each_peer_keeps_at_most_its_bound_oldest_going_first(db, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_OPAQUE_EVENTS_PER_PEER", 3)
    stamps = iter(f"2026-03-01T00:00:0{i}.000000Z" for i in range(9))
    monkeypatch.setattr(store_module, "utc_now_iso", lambda: next(stamps))
    raw = {"envelope": {"object_type": "x", "payload": {}}, "signature": ""}

    kept = [store_opaque_event(db, sender_fingerprint="noisy", content_id=f"c{i}", object_type="x", envelope=raw)
            for i in range(5)]
    store_opaque_event(db, sender_fingerprint="quiet", content_id="q0", object_type="x", envelope=raw)

    assert kept == [True] * 5
    ids = {row[0] for row in db.connection.execute("SELECT content_id FROM opaque_events")}
    assert ids == {"c2", "c3", "c4", "q0"}


def test_a_dropped_event_is_forgotten_so_it_can_come_again(tmp_path, db, clock, monkeypatch):  # noqa: F811
    monkeypatch.setattr(store_module, "MAX_OPAQUE_EVENTS_PER_PEER", 0)
    alice, bob, _ = _setup(tmp_path, clock)
    future = _future(alice)
    accepted = bob.handle_events(alice.fingerprint, [future])

    _persist(db, bob, accepted, alice.fingerprint)

    assert accepted[0] not in bob.known_event_ids
    alice.close()


def test_kept_events_expire(db):
    raw = {"envelope": {"object_type": "x", "payload": {}}, "signature": ""}
    store_opaque_event(db, sender_fingerprint="p", content_id="old", object_type="x", envelope=raw)
    db.connection.execute("UPDATE opaque_events SET received_at = '2025-01-01T00:00:00.000000Z'")
    db.connection.commit()

    assert purge_expired_opaque_events(db, now_iso="2026-01-01T00:00:00.000000Z") == 1


# -- after an upgrade


def test_at_startup_a_kept_event_of_a_now_known_type_is_judged_and_applied(tmp_path, db, clock):  # noqa: F811
    """Kept by an older build that did not know `board_posting`; this build
    does, so it goes through the real checks and takes effect."""
    alice, bob, genesis = _setup(tmp_path, clock)
    board = materialize_carried_board(db, genesis)
    setting = build_board_posting(
        signing_identity=alice.identity.signing_key, origin_fingerprint=alice.fingerprint,
        board_id=BOARD, posting="origin_only", created_at=clock.now_iso(),
    )
    forged = copy.deepcopy(setting.to_dict())
    forged["envelope"]["payload"]["posting"] = "anyone"  # signature no longer matches
    for raw in (setting.to_dict(), forged):
        from netbbs.link.events import event_content_id

        store_opaque_event(db, sender_fingerprint=alice.fingerprint, content_id=event_content_id(raw["envelope"]),
                           object_type="board_posting", envelope=raw)

    lane = DatabaseLane(db.path)
    try:
        taken = asyncio.run(rejudge_opaque_events(lane, bob, max_carried_boards=None))
    finally:
        lane.close()

    assert taken == 1
    assert board_posting_mode(db, board) == "origin_only"
    assert db.connection.execute("SELECT COUNT(*) FROM opaque_events").fetchone()[0] == 0
    alice.close()


def test_at_startup_only_still_unknown_kept_events_count_as_known(tmp_path, db, monkeypatch):
    node_identity = spawn_node(tmp_path, "self").identity
    raw = {"envelope": {"object_type": "board_frobnicate", "payload": {}}, "signature": ""}
    store_opaque_event(db, sender_fingerprint="p", content_id="future", object_type="board_frobnicate", envelope=raw)
    store_opaque_event(db, sender_fingerprint="p", content_id="known", object_type="board_posting", envelope=raw)

    node = load_link_node(db, node_identity)

    assert "future" in node.known_event_ids and "known" not in node.known_event_ids


# -- the list of understood types is the list handle_events handles


def test_known_types_are_exactly_the_types_handle_events_handles():
    source = Path(__file__).resolve().parents[1] / "src/netbbs/link/protocol.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "handle_events"
    )
    handled = {
        comparator.id
        for node in ast.walk(method) if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name) and node.left.id == "object_type"
        for comparator in node.comparators if isinstance(comparator, ast.Name)
    }
    import netbbs.link.protocol as protocol

    assert {getattr(protocol, name) for name in handled} == set(KNOWN_EVENT_OBJECT_TYPES)
