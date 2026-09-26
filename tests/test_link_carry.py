"""
The Link carry model's recorded states (design doc §9.3; issue #683), against
a real SQLite database: intake past the cap is an offer, not a permanent
decline; accepting and excluding are single transactions; what is recorded is
what the node declares as not carried.
"""

from __future__ import annotations

import json

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board, delete_board, get_board_by_name
from netbbs.link import boards as link_boards_module
from netbbs.link.boards import board_origin_fingerprint, link_board
from netbbs.link.carry import (
    EXCLUDED,
    OFFERED,
    CarryDecisionError,
    accept_genesis,
    accept_offer,
    carry_decision_counts,
    carry_decision_state,
    exclude_offer,
    list_carry_decisions,
)
from netbbs.link.events import (
    build_board_closure,
    build_board_genesis,
    build_board_origin_transfer_accepted,
    build_board_origin_transfer_offer,
    build_channel_genesis,
    build_file_area_genesis,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.store import (
    clear_deletion_record,
    retain_linked_genesis,
    save_event,
    uncarried_resource_ids,
)
from netbbs.storage.database import Database

BOARD_ID = "b" * 64


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=255)


@pytest.fixture(scope="module")
def remote():
    return bootstrap_node_identity("carry-remote")


@pytest.fixture(scope="module")
def own():
    return bootstrap_node_identity("carry-own")


def _board_genesis(remote, board_id=BOARD_ID, name="Remote Discussion"):
    return build_board_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        board_id=board_id, name=name, created_at="2026-01-01T00:00:00Z",
    )


def _accept(db, genesis, own, *, kind="boards", cap=500):
    return accept_genesis(
        db, kind=kind, envelope=genesis.to_dict(), sender_fingerprint=genesis.payload["origin_fingerprint"],
        content_id=genesis.content_id, own_fingerprint=own.fingerprint, cap=cap,
    )


def _events(db):
    return db.connection.execute("SELECT COUNT(*) FROM link_events").fetchone()[0]


def test_a_genesis_under_the_cap_is_carried_and_leaves_no_decision(db, remote, own):
    assert _accept(db, _board_genesis(remote), own) == "carried"
    assert get_board_by_name(db, "Remote Discussion").board_id == BOARD_ID
    assert carry_decision_state(db, "boards", BOARD_ID) is None
    assert _events(db) == 1


@pytest.mark.parametrize("cap", [0, 1])
def test_a_genesis_past_the_cap_is_offered_not_declined(db, remote, own, cap):
    """A cap of 0 is a curated node: everything new is offered."""
    if cap:
        _accept(db, _board_genesis(remote, board_id="a" * 64, name="First"), own, cap=cap)

    assert _accept(db, _board_genesis(remote), own, cap=cap) == "cap"

    assert carry_decision_state(db, "boards", BOARD_ID) == OFFERED
    assert db.connection.execute("SELECT 1 FROM boards WHERE board_id = ?", (BOARD_ID,)).fetchone() is None
    assert uncarried_resource_ids(db) == {"boards": (BOARD_ID,)}


@pytest.mark.parametrize("kind, build, id_field", [
    ("channels", build_channel_genesis, "channel_id"),
    ("file_areas", build_file_area_genesis, "area_id"),
])
def test_channels_and_file_areas_are_offered_the_same_way(db, remote, own, kind, build, id_field):
    genesis = build(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        name="elsewhere", created_at="2026-01-01T00:00:00Z", **{id_field: "c" * 64},
    )
    assert _accept(db, genesis, own, kind=kind, cap=0) == "cap"
    assert carry_decision_counts(db) == {(kind, OFFERED): 1}
    accept_offer(db, kind, "c" * 64, actor_user_id=None)
    assert carry_decision_counts(db) == {}


def test_the_genesis_and_its_outcome_are_one_transaction(db, remote, own, monkeypatch):
    """The old separate save and materialize left an accepted genesis with no
    local row and no repair path when anything failed between them."""
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("netbbs.link.carry.materialize_carried_board", boom)
    with pytest.raises(RuntimeError):
        _accept(db, _board_genesis(remote), own)

    assert _events(db) == 0
    assert carry_decision_counts(db) == {}


def test_accepting_an_offer_carries_it_uncapped_and_clears_the_offer(db, remote, own, alice):
    _accept(db, _board_genesis(remote), own, cap=0)

    accept_offer(db, "boards", BOARD_ID, actor_user_id=alice.id)

    assert get_board_by_name(db, "Remote Discussion").board_id == BOARD_ID
    assert carry_decision_state(db, "boards", BOARD_ID) is None
    assert uncarried_resource_ids(db) == {}


def test_accepting_applies_the_lifecycle_accepted_while_it_was_offered(db, remote, own):
    """A transfer and a closure saved while there was no local row were silent
    no-ops then; the accepted copy must not come back with its genesis origin,
    open."""
    new_origin = bootstrap_node_identity("carry-new-origin")
    genesis = _board_genesis(remote)
    _accept(db, genesis, own, cap=0)
    offer = build_board_origin_transfer_offer(
        signing_identity=remote.signing_key, board_id=BOARD_ID, previous_event_id=genesis.content_id,
        old_origin_fingerprint=remote.fingerprint,
        new_origin_fingerprint=new_origin.fingerprint, created_at="2026-01-02T00:00:00Z",
    )
    accepted = build_board_origin_transfer_accepted(
        signing_identity=new_origin.signing_key, board_id=BOARD_ID, previous_event_id=offer.content_id,
        new_origin_fingerprint=new_origin.fingerprint, created_at="2026-01-03T00:00:00Z",
    )
    closure = build_board_closure(
        signing_identity=new_origin.signing_key, board_id=BOARD_ID, previous_event_id=accepted.content_id,
        reason=None, created_at="2026-01-04T00:00:00Z",
    )
    for event, object_type in ((offer, "board_origin_transfer_offer"),
                               (accepted, "board_origin_transfer_accepted"),
                               (closure, "board_closure")):
        save_event(db, sender_fingerprint=remote.fingerprint, content_id=event.content_id,
                   object_type=object_type, envelope=event.to_dict())

    accept_offer(db, "boards", BOARD_ID, actor_user_id=None)

    board = get_board_by_name(db, "Remote Discussion")
    assert board_origin_fingerprint(db, board) == new_origin.fingerprint
    assert link_boards_module.is_board_closed(db, board)


def test_an_accepted_offer_whose_name_is_taken_gets_a_free_one(db, remote, own, alice):
    create_board(db, "Remote Discussion", creator=alice)
    _accept(db, _board_genesis(remote), own, cap=0)

    accept_offer(db, "boards", BOARD_ID, actor_user_id=alice.id)

    assert get_board_by_name(db, f"Remote Discussion-{BOARD_ID[:8]}").board_id == BOARD_ID


def test_excluding_an_offer_keeps_it_out_and_it_can_no_longer_be_accepted(db, remote, own, alice):
    _accept(db, _board_genesis(remote), own, cap=0)

    exclude_offer(db, "boards", BOARD_ID, actor_user_id=alice.id)

    assert carry_decision_state(db, "boards", BOARD_ID) == EXCLUDED
    [decision] = list_carry_decisions(db, EXCLUDED)
    assert (decision.name, decision.reason, decision.actor_user_id) == ("Remote Discussion", "sysop", alice.id)
    assert uncarried_resource_ids(db) == {"boards": (BOARD_ID,)}
    with pytest.raises(CarryDecisionError):
        accept_offer(db, "boards", BOARD_ID, actor_user_id=alice.id)


def test_listing_shows_what_the_stored_genesis_says(db, remote, own):
    _accept(db, _board_genesis(remote), own, cap=0)
    [decision] = list_carry_decisions(db, OFFERED)
    assert (decision.kind, decision.name, decision.reason, decision.origin_fingerprint) == (
        "boards", "Remote Discussion", "cap", remote.fingerprint,
    )


def test_a_stale_decision_never_hides_a_resource_this_node_carries(db, remote, own):
    _accept(db, _board_genesis(remote), own)
    db.connection.execute(
        "INSERT INTO link_carry_decisions (kind, resource_id, state, reason, decided_at) "
        "VALUES ('boards', ?, 'excluded', 'deleted', '2026-01-01T00:00:00+00:00')",
        (BOARD_ID,),
    )
    db.connection.commit()

    assert uncarried_resource_ids(db) == {}


def test_deleting_a_linked_board_records_it_excluded_by_its_deleter(db, alice, own):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=own)

    assert retain_linked_genesis(db, "boards", board.board_id, actor_user_id=alice.id) is True
    delete_board(db, board, deleted_by=alice)

    [decision] = list_carry_decisions(db, EXCLUDED)
    assert (decision.resource_id, decision.reason, decision.actor_user_id) == (board.board_id, "deleted", alice.id)


def test_a_deletion_that_did_not_happen_leaves_no_exclusion(db, alice, own):
    board = create_board(db, "general", creator=alice)
    link_board(db, board, node_identity=own)
    retain_linked_genesis(db, "boards", board.board_id, actor_user_id=alice.id)

    clear_deletion_record(db, "boards", board.board_id)

    assert carry_decision_counts(db) == {}
    assert uncarried_resource_ids(db) == {}


def test_the_migration_offers_every_genesis_held_without_a_local_copy(tmp_path, monkeypatch, remote, own):
    """Before #683 a cap refusal, a deletion and the crash window all looked
    alike; offered is the state that loses nothing and forces nothing."""
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "link_carry_decisions" in m.sql)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    old = Database(tmp_path / "node.db")
    carried, orphan = _board_genesis(remote, board_id="a" * 64, name="Carried"), _board_genesis(remote)
    for genesis in (carried, orphan):
        old.connection.execute(
            "INSERT INTO link_events (content_id, sender_fingerprint, object_type, envelope_json, received_at, board_id) "
            "VALUES (?, ?, 'board_genesis', ?, '2026-01-01T00:00:00+00:00', ?)",
            (genesis.content_id, remote.fingerprint, json.dumps(genesis.to_dict()), genesis.payload["board_id"]),
        )
    old.connection.commit()
    link_boards_module.materialize_carried_board(old, carried)
    old.close()
    monkeypatch.undo()

    migrated = Database(tmp_path / "node.db")
    try:
        [decision] = list_carry_decisions(migrated, OFFERED)
        assert (decision.resource_id, decision.reason) == (BOARD_ID, "migrated")
        assert carry_decision_state(migrated, "boards", "a" * 64) is None
    finally:
        migrated.close()
