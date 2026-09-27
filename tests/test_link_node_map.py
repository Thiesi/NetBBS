"""
The node map's domain query (design doc §8.12, issue #777):
`netbbs.link.node_map.build_node_map`, the descriptor first-stored time it
caps `created_at` with (`netbbs.link.store`), the migration that added it,
and real-time sessions counting as contact (`LinkRealtimeSessionRegistry`).

Real SQLite throughout. Timestamps are set explicitly wherever an ordering
is asserted: consecutive `utc_now_iso()` calls can be equal on Windows.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from netbbs.auth.users import create_user
from netbbs.link.boards import materialize_carried_board
from netbbs.link.channels import materialize_carried_channel
from netbbs.link.enforcement import ensure_node_subject
from netbbs.link.events import (
    build_board_genesis,
    build_channel_genesis,
    build_endpoint_descriptor,
    build_file_area_genesis,
)
from netbbs.link.files import materialize_carried_file_area
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.node_map import (
    CANDIDATE,
    INTRODUCED,
    MET,
    ORIGIN,
    build_node_map,
    last_heard,
    node_numbers,
    relative_time,
    unknown_node_label,
)
from netbbs.link.protocol import PeerRecord
from netbbs.link.store import (
    record_direct_contact,
    save_candidate_descriptor,
    save_introduced_identity,
    save_peer,
)
from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, set_trust_override
from netbbs.net.node_map_flow import openable_carried_names
from netbbs.storage.database import Database

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _record(identity, *, created_at: str = "2026-09-01T00:00:00+00:00", name: str | None = None,
            dns: str | None = None, addresses=None) -> PeerRecord:
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key,
        subject_fingerprint=identity.fingerprint,
        addresses=addresses,
        outgoing_only=addresses is None,
        created_at=created_at,
        friendly_name=name,
        canonical_dns_name=dns,
    )
    return PeerRecord(
        fingerprint=identity.fingerprint,
        root_public_key=bytes(identity.root.verify_key),
        transitions=identity.transitions,
        descriptor=descriptor,
    )


def _set(db, table: str, fingerprint: str, **columns) -> None:
    assignments = ", ".join(f"{column} = ?" for column in columns)
    db.connection.execute(
        f"UPDATE {table} SET {assignments} WHERE fingerprint = ?", (*columns.values(), fingerprint)
    )
    db.connection.commit()


def _block(db, fingerprint: str, dimension: TrustDimension, state=TrustState.BLOCKED) -> None:
    ensure_node_subject(db, fingerprint)
    set_trust_override(db, TrustSubject.node(fingerprint), dimension, state, reason="test")


def _carry_board(db, origin, own, *, name: str, board_id: str, min_read: int | None = None,
                 min_age: int | None = None):
    genesis = build_board_genesis(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        board_id=board_id, name=name, created_at="2026-09-01T00:00:00+00:00",
        default_min_read_level=min_read, default_min_age=min_age,
    )
    return materialize_carried_board(db, genesis, own_fingerprint=own.fingerprint)


def _by_fp(entries):
    return {entry.fingerprint: entry for entry in entries}


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def own():
    return bootstrap_node_identity("own")


# -- what is listed, once, from its best source --------------------------


def test_each_node_is_listed_once_from_its_best_source(db, own):
    met = bootstrap_node_identity("met")
    introduced = bootstrap_node_identity("introduced")
    candidate = bootstrap_node_identity("candidate")
    origin_only = bootstrap_node_identity("origin")

    save_peer(db, _record(met, name="Met Board"))
    save_introduced_identity(db, _record(introduced, name="Introduced Board"), introduced_by=met.fingerprint)
    # The introduced node is also a candidate: introduction wins over a
    # peer list's unverified word.
    save_candidate_descriptor(db, introduced.fingerprint, _record(introduced, name="Spoofed").descriptor)
    save_candidate_descriptor(db, candidate.fingerprint, _record(candidate, name="Candidate Board").descriptor)
    # The met node is also an origin: met wins.
    _carry_board(db, met, own, name="From met", board_id="b-met")
    _carry_board(db, origin_only, own, name="From origin", board_id="b-origin")

    entries = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW))

    assert len(entries) == 4
    assert entries[met.fingerprint].source == MET and entries[met.fingerprint].is_origin
    assert entries[met.fingerprint].relationship == "direct"
    assert entries[introduced.fingerprint].source == INTRODUCED
    assert entries[introduced.fingerprint].friendly_name == "Introduced Board"
    assert entries[introduced.fingerprint].relationship == "via Met Board"
    assert entries[candidate.fingerprint].source == CANDIDATE
    assert entries[candidate.fingerprint].relationship == "unverified"
    assert entries[origin_only.fingerprint].source == ORIGIN


def test_this_node_is_never_listed(db, own):
    save_peer(db, _record(own, name="Me"))
    _carry_board(db, own, own, name="Mine", board_id="b-own")

    assert build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW) == []


def test_has_known_nodes_matches_the_sysop_map(db, own):
    from netbbs.link.node_map import has_known_nodes

    assert not has_known_nodes(db, own_fingerprint=own.fingerprint)
    _carry_board(db, own, own, name="Mine", board_id="b-own")
    assert not has_known_nodes(db, own_fingerprint=own.fingerprint)
    origin = bootstrap_node_identity("origin")
    _carry_board(db, origin, own, name="Theirs", board_id="b-theirs")
    assert has_known_nodes(db, own_fingerprint=own.fingerprint)


def test_callers_never_see_candidates(db, own):
    candidate = bootstrap_node_identity("candidate")
    save_candidate_descriptor(db, candidate.fingerprint, _record(candidate, name="Candidate").descriptor)

    assert build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW) == []
    assert [e.source for e in build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW)] == [
        CANDIDATE
    ]


def test_a_candidate_that_is_also_an_origin_is_an_origin_row_for_callers(db, own):
    """The peer list's unverified name never reaches a caller, but what this
    node carries from the node still does."""
    node = bootstrap_node_identity("both")
    save_candidate_descriptor(db, node.fingerprint, _record(node, name="Unverified Name").descriptor)
    _carry_board(db, node, own, name="Carried", board_id="b-both")

    [entry] = build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)

    assert entry.source == ORIGIN
    assert entry.friendly_name == unknown_node_label(node.fingerprint)
    [sysop_entry] = build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW)
    assert sysop_entry.source == CANDIDATE and sysop_entry.is_origin


def test_origin_only_nodes_of_every_carried_kind_read_unknown(db, own):
    board_origin = bootstrap_node_identity("board-origin")
    area_origin = bootstrap_node_identity("area-origin")
    channel_origin = bootstrap_node_identity("channel-origin")
    _carry_board(db, board_origin, own, name="Carried board", board_id="b-1")
    materialize_carried_file_area(db, build_file_area_genesis(
        signing_identity=area_origin.signing_key, origin_fingerprint=area_origin.fingerprint,
        area_id="a-1", name="Carried area", created_at="2026-09-01T00:00:00+00:00",
    ), own_fingerprint=own.fingerprint)
    materialize_carried_channel(db, build_channel_genesis(
        signing_identity=channel_origin.signing_key, origin_fingerprint=channel_origin.fingerprint,
        channel_id="c-1", name="carried-channel", created_at="2026-09-01T00:00:00+00:00",
    ), own_fingerprint=own.fingerprint)

    entries = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW))

    assert set(entries) == {board_origin.fingerprint, area_origin.fingerprint, channel_origin.fingerprint}
    for entry in entries.values():
        assert entry.source == ORIGIN
        assert entry.friendly_name == unknown_node_label(entry.fingerprint)
        assert entry.dns_name is None
        assert entry.relationship == "unknown"
        assert entry.last_heard is None and not entry.stale
        assert relative_time(entry.last_heard, now=NOW) == "unknown"


def test_a_hidden_carried_resource_does_not_make_its_origin_a_node(db, own):
    origin = bootstrap_node_identity("origin")
    board = _carry_board(db, origin, own, name="Excluded", board_id="b-x")
    db.connection.execute("UPDATE boards SET link_hidden_at = ? WHERE id = ?", ("2026-09-01T00:00:00Z", board.id))
    db.connection.commit()

    assert build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW) == []


def test_a_transferred_board_counts_for_its_current_origin(db, own):
    first = bootstrap_node_identity("first")
    second = bootstrap_node_identity("second")
    board = _carry_board(db, first, own, name="Moved", board_id="b-moved")
    db.connection.execute(
        "UPDATE boards SET link_origin_fingerprint = ? WHERE id = ?", (second.fingerprint, board.id)
    )
    db.connection.commit()

    assert [e.fingerprint for e in build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)] == [
        second.fingerprint
    ]


def test_unknown_nodes_are_told_apart(db, own):
    first = bootstrap_node_identity("first-unknown")
    second = bootstrap_node_identity("second-unknown")
    _carry_board(db, first, own, name="One", board_id="b-one")
    _carry_board(db, second, own, name="Two", board_id="b-two")

    names = {e.friendly_name for e in build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)}

    assert names == {f"Unknown node {first.fingerprint[:6]}", f"Unknown node {second.fingerprint[:6]}"}


def test_an_introduction_names_its_carrier_without_the_dns_name(db, own):
    carrier = bootstrap_node_identity("carrier")
    far = bootstrap_node_identity("far")
    save_peer(db, _record(carrier, name="Carrier Board", dns="carrier.example.org"))
    save_introduced_identity(db, _record(far, name="Far Board"), introduced_by=carrier.fingerprint)

    for sysop in (False, True):
        entry = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=sysop, now=NOW))[far.fingerprint]
        assert entry.relationship == "via Carrier Board"


# -- permanent node numbers --------------------------------------------------


def test_node_numbers_are_small_permanent_and_shared_by_every_viewer(tmp_path, own):
    db = Database(tmp_path / "node.db")
    alpha = bootstrap_node_identity("alpha")
    beta = bootstrap_node_identity("beta")
    save_peer(db, _record(alpha, name="Alpha"))
    save_peer(db, _record(beta, name="Beta"))
    caller = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW))
    assert (caller[alpha.fingerprint].number, caller[beta.fingerprint].number) == (1, 2)
    sysop = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW))
    assert (sysop[alpha.fingerprint].number, sysop[beta.fingerprint].number) == (1, 2)
    db.close()

    # Across a restart.
    db = Database(tmp_path / "node.db")
    reopened = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW))
    assert (reopened[alpha.fingerprint].number, reopened[beta.fingerprint].number) == (1, 2)

    # A node that truly leaves the map loses its number; one that returns
    # gets a new one, and nobody ever gets a number handed out before.
    db.connection.execute("DELETE FROM link_peers WHERE fingerprint = ?", (alpha.fingerprint,))
    db.connection.commit()
    assert [e.number for e in build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)] == [2]
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_node_numbers WHERE fingerprint = ?", (alpha.fingerprint,)
    ).fetchone()[0] == 0
    gamma = bootstrap_node_identity("gamma")
    save_peer(db, _record(gamma, name="Gamma"))
    save_peer(db, _record(alpha, name="Alpha"))
    back = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW))
    assert back[beta.fingerprint].number == 2
    assert {back[alpha.fingerprint].number, back[gamma.fingerprint].number} == {3, 4}
    db.close()


def test_a_callers_map_never_drops_a_hidden_nodes_number(db, own):
    hidden = bootstrap_node_identity("hidden")
    candidate = bootstrap_node_identity("candidate")
    save_peer(db, _record(hidden, name="Hidden"))
    save_candidate_descriptor(db, candidate.fingerprint, _record(candidate, name="Candidate").descriptor)
    sysop = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW))
    _block(db, hidden.fingerprint, TrustDimension.IDENTITY_INTEGRITY)

    assert build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW) == []

    again = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW))
    assert again[hidden.fingerprint].number == sysop[hidden.fingerprint].number
    assert again[candidate.fingerprint].number == sysop[candidate.fingerprint].number


def test_churned_introductions_leave_the_numbers_bounded_and_never_reused(db, own, monkeypatch):
    """A carrier minting identities through the bounded introduction store
    must not grow the numbers table without limit (AGENTS.md: bound remotely
    influenced resources)."""
    from netbbs.link import store as store_module

    monkeypatch.setattr(store_module, "MAX_INTRODUCED_IDENTITIES", 5)
    carrier = bootstrap_node_identity("carrier")
    save_peer(db, _record(carrier, name="Carrier"))
    given: dict[int, str] = {}
    for index in range(25):
        minted = bootstrap_node_identity(f"minted-{index}")
        save_introduced_identity(
            db, _record(minted, name=f"Minted {index}", created_at=f"2026-09-01T00:00:{index:02d}+00:00"),
            introduced_by=carrier.fingerprint,
        )
        for entry in build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW):
            assert given.setdefault(entry.number, entry.fingerprint) == entry.fingerprint  # never reused

    live = db.connection.execute("SELECT COUNT(*) FROM link_introduced_identities").fetchone()[0]
    assert live == 5
    stored = db.connection.execute("SELECT COUNT(*) FROM link_node_numbers").fetchone()[0]
    assert stored == live + 1  # the introductions still on file, and the carrier
    assert len(given) == 26  # every node ever listed got its own number


def test_node_numbers_never_renumber(db):
    assert node_numbers(db, ["fp-b", "fp-a"]) == {"fp-b": 1, "fp-a": 2}
    assert node_numbers(db, ["fp-a", "fp-c", "fp-b"]) == {"fp-a": 2, "fp-c": 3, "fp-b": 1}


# -- who callers do not see ------------------------------------------------


@pytest.mark.parametrize("dimension", list(TrustDimension))
@pytest.mark.parametrize("state", [TrustState.QUARANTINED, TrustState.BLOCKED])
def test_callers_do_not_see_a_node_quarantined_or_blocked_in_any_dimension(db, own, dimension, state):
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer, name="Peer"))
    _block(db, peer.fingerprint, dimension, state)

    assert build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW) == []
    [entry] = build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW)
    assert entry.trust[dimension.value] == state.value
    assert entry.hidden_from_callers


def test_probation_and_establishment_hide_nothing(db, own):
    probation = bootstrap_node_identity("probation")
    established = bootstrap_node_identity("established")
    save_peer(db, _record(probation, name="New"))
    save_peer(db, _record(established, name="Old"))
    ensure_node_subject(db, probation.fingerprint)
    for dimension in TrustDimension:
        _block(db, established.fingerprint, dimension, TrustState.ESTABLISHED)

    assert len(build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)) == 2


def test_relay_counts_are_unknown_without_a_descriptor(db, own):
    origin = bootstrap_node_identity("origin")
    peer = bootstrap_node_identity("peer")
    _carry_board(db, origin, own, name="Carried", board_id="b-1")
    save_peer(db, _record(peer, name="Peer"))

    entries = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW))

    assert entries[origin.fingerprint].published_relays is None
    assert entries[origin.fingerprint].live_relays is None
    assert entries[peer.fingerprint].published_relays == 0
    assert entries[peer.fingerprint].live_relays == 0


def test_poor_reachability_hides_nothing(db, own):
    from netbbs.link.reliability import record_dial_outcome

    peer = bootstrap_node_identity("flaky")
    save_peer(db, _record(peer, name="Flaky"))
    for _ in range(5):
        record_dial_outcome(db, peer.fingerprint, succeeded=False)

    [entry] = build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)
    assert entry.fingerprint == peer.fingerprint
    assert entry.reliability is None  # never carried on a caller's map


def test_a_hidden_origin_only_node_is_left_off_too(db, own):
    origin = bootstrap_node_identity("origin")
    _carry_board(db, origin, own, name="Carried", board_id="b-1")
    _block(db, origin.fingerprint, TrustDimension.CONTENT_CONDUCT)

    assert build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW) == []


def test_the_carrier_label_reads_another_node_when_the_carrier_is_hidden(db, own):
    carrier = bootstrap_node_identity("carrier")
    introduced = bootstrap_node_identity("introduced")
    save_peer(db, _record(carrier, name="Carrier Board"))
    save_introduced_identity(db, _record(introduced, name="Far Board"), introduced_by=carrier.fingerprint)

    [before] = [e for e in build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)
                if e.fingerprint == introduced.fingerprint]
    assert before.relationship == "via Carrier Board"

    _block(db, carrier.fingerprint, TrustDimension.RESOURCE_BEHAVIOR, TrustState.QUARANTINED)
    [after] = build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)
    assert after.fingerprint == introduced.fingerprint
    assert after.relationship == "via another node"
    # The SysOp still sees who carried it.
    sysop = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW))
    assert sysop[introduced.fingerprint].relationship == "via Carrier Board"


def test_callers_never_get_link_addresses_relay_roles_or_reliability(db, own):
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer, name="Peer", addresses=[{"protocol": "tcp", "address": "203.0.113.9", "port": 7862}]))
    db.connection.execute(
        "INSERT INTO link_relay_consents (fingerprint, role, accepted_at) VALUES (?, 'i_relay_for', ?)",
        (peer.fingerprint, "2026-09-01T00:00:00Z"),
    )
    db.connection.commit()

    [caller] = build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)
    [sysop] = build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW)

    assert caller.addresses == () and caller.reliability is None and not caller.we_relay_for_it
    assert sysop.addresses == ("tcp://203.0.113.9:7862",)
    assert sysop.we_relay_for_it and not sysop.it_relays_for_us
    assert sysop.reliability == pytest.approx(0.5)
    assert sysop.outgoing_only is False


# -- last heard --------------------------------------------------------------


def test_last_heard_is_the_later_of_direct_contact_and_the_capped_descriptor_time(db, own):
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer, created_at="2026-09-20T00:00:00+00:00"))
    _set(db, "link_peers", peer.fingerprint,
         last_direct_contact_at="2026-09-10T00:00:00.000000Z",
         descriptor_first_stored_at="2026-09-21T00:00:00.000000Z")

    [entry] = build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)
    assert entry.last_heard == datetime(2026, 9, 20, tzinfo=timezone.utc)

    _set(db, "link_peers", peer.fingerprint, last_direct_contact_at="2026-09-25T00:00:00.000000Z")
    [entry] = build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW)
    assert entry.last_heard == datetime(2026, 9, 25, tzinfo=timezone.utc)


def test_a_future_dated_descriptor_is_capped_at_when_it_was_first_stored(db, own):
    introduced = bootstrap_node_identity("liar")
    carrier = bootstrap_node_identity("carrier")
    save_peer(db, _record(carrier))
    save_introduced_identity(
        db, _record(introduced, created_at="2036-01-01T00:00:00+00:00"), introduced_by=carrier.fingerprint
    )
    _set(db, "link_introduced_identities", introduced.fingerprint,
         descriptor_first_stored_at="2026-07-01T00:00:00.000000Z")

    entry = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW))[
        introduced.fingerprint
    ]

    assert entry.last_heard == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert entry.stale  # ten years ahead did not keep it fresh


def test_re_storing_the_same_descriptor_keeps_its_first_stored_time(db, own):
    carrier = bootstrap_node_identity("carrier")
    introduced = bootstrap_node_identity("introduced")
    record = _record(introduced, created_at="2036-01-01T00:00:00+00:00")
    save_introduced_identity(db, record, introduced_by=carrier.fingerprint)
    _set(db, "link_introduced_identities", introduced.fingerprint,
         descriptor_first_stored_at="2026-07-01T00:00:00.000000Z")

    # The same signed descriptor again, re-serialized with its keys in
    # another order: still the same descriptor.
    reordered = PeerRecord(
        fingerprint=record.fingerprint, root_public_key=record.root_public_key,
        transitions=record.transitions,
        descriptor=type(record.descriptor).from_dict(
            json.loads(json.dumps(record.descriptor.to_dict(), sort_keys=True))
        ),
    )
    save_introduced_identity(db, reordered, introduced_by=carrier.fingerprint)

    row = db.connection.execute(
        "SELECT descriptor_first_stored_at FROM link_introduced_identities WHERE fingerprint = ?",
        (introduced.fingerprint,),
    ).fetchone()
    assert row["descriptor_first_stored_at"] == "2026-07-01T00:00:00.000000Z"

    # A new descriptor is new content, first stored now.
    save_introduced_identity(
        db, _record(introduced, created_at="2036-02-01T00:00:00+00:00"), introduced_by=carrier.fingerprint
    )
    row = db.connection.execute(
        "SELECT descriptor_first_stored_at FROM link_introduced_identities WHERE fingerprint = ?",
        (introduced.fingerprint,),
    ).fetchone()
    assert row["descriptor_first_stored_at"] > "2026-07-01T00:00:00.000000Z"


def test_peer_first_stored_time_moves_only_with_the_descriptor(db):
    peer = bootstrap_node_identity("peer")
    record = _record(peer)
    save_peer(db, record)
    _set(db, "link_peers", peer.fingerprint, descriptor_first_stored_at="2026-01-01T00:00:00.000000Z")

    save_peer(db, record, direct_contact=False)
    first = db.connection.execute(
        "SELECT descriptor_first_stored_at FROM link_peers WHERE fingerprint = ?", (peer.fingerprint,)
    ).fetchone()[0]
    assert first == "2026-01-01T00:00:00.000000Z"

    save_peer(db, _record(peer, created_at="2026-09-02T00:00:00+00:00"))
    moved = db.connection.execute(
        "SELECT descriptor_first_stored_at FROM link_peers WHERE fingerprint = ?", (peer.fingerprint,)
    ).fetchone()[0]
    assert moved > "2026-01-01T00:00:00.000000Z"


def test_a_candidate_is_never_heard_from_but_keeps_when_it_was_first_named(db, own):
    candidate = bootstrap_node_identity("candidate")
    save_candidate_descriptor(db, candidate.fingerprint, _record(candidate).descriptor)
    _set(db, "link_peer_candidates", candidate.fingerprint, first_named_at="2026-08-01T00:00:00.000000Z")
    # A later peer list refreshing it does not move when it was first named.
    save_candidate_descriptor(
        db, candidate.fingerprint, _record(candidate, created_at="2026-09-05T00:00:00+00:00").descriptor
    )

    [entry] = build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW)

    assert entry.last_heard is None and not entry.stale
    assert entry.first_named == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_stale_after_thirty_days(db, own):
    fresh = bootstrap_node_identity("fresh")
    old = bootstrap_node_identity("old")
    for identity, when in ((fresh, NOW - timedelta(days=29)), (old, NOW - timedelta(days=31))):
        save_peer(db, _record(identity, created_at="2020-01-01T00:00:00+00:00"))
        _set(db, "link_peers", identity.fingerprint, last_direct_contact_at=_iso(when))

    entries = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=False, now=NOW))

    assert not entries[fresh.fingerprint].stale
    assert entries[old.fingerprint].stale
    assert relative_time(entries[old.fingerprint].last_heard, now=NOW) == "31 days ago"


def test_unparsable_times_read_unknown():
    assert last_heard(
        last_direct_contact_at="garbage", descriptor_created_at="also garbage",
        descriptor_first_stored_at="2026-01-01T00:00:00Z",
    ) is None
    # Without a first-stored time to cap it, a descriptor's own date is not used.
    assert last_heard(
        last_direct_contact_at=None, descriptor_created_at="2026-01-01T00:00:00Z",
        descriptor_first_stored_at=None,
    ) is None
    assert last_heard(
        last_direct_contact_at=None, descriptor_created_at=12345,
        descriptor_first_stored_at="2026-01-01T00:00:00Z",
    ) is None


def test_record_direct_contact_advances_but_never_rewinds(db):
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer))
    _set(db, "link_peers", peer.fingerprint, last_direct_contact_at="2026-09-01T00:00:00.000000Z")

    record_direct_contact(db, peer.fingerprint, "2026-09-10T00:00:00.000000Z")
    record_direct_contact(db, peer.fingerprint, "2026-09-05T00:00:00.000000Z")
    record_direct_contact(db, "not-a-peer", "2026-09-10T00:00:00.000000Z")

    rows = db.connection.execute("SELECT fingerprint, last_direct_contact_at FROM link_peers").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(peer.fingerprint, "2026-09-10T00:00:00.000000Z")]


# -- read gates on what a caller is told this node carries ------------------


def test_a_caller_is_told_only_about_carried_resources_they_could_open(db, own):
    origin = bootstrap_node_identity("origin")
    other = bootstrap_node_identity("other")
    _carry_board(db, origin, own, name="Open board", board_id="b-open")
    _carry_board(db, origin, own, name="High board", board_id="b-high", min_read=50)
    _carry_board(db, origin, own, name="Adult board", board_id="b-adult", min_age=18)
    _carry_board(db, other, own, name="Someone else's", board_id="b-other")
    materialize_carried_file_area(db, build_file_area_genesis(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        area_id="a-open", name="Open files", created_at="2026-09-01T00:00:00+00:00",
    ), own_fingerprint=own.fingerprint)
    materialize_carried_file_area(db, build_file_area_genesis(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        area_id="a-high", name="High files", created_at="2026-09-01T00:00:00+00:00",
        default_min_read_level=50,
    ), own_fingerprint=own.fingerprint)
    materialize_carried_channel(db, build_channel_genesis(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        channel_id="c-open", name="open-chat", created_at="2026-09-01T00:00:00+00:00",
    ), own_fingerprint=own.fingerprint)
    materialize_carried_channel(db, build_channel_genesis(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        channel_id="c-high", name="high-chat", created_at="2026-09-01T00:00:00+00:00",
        default_min_level=50,
    ), own_fingerprint=own.fingerprint)

    caller = create_user(db, "caller", password="pw-long-enough", user_level=10)
    names = openable_carried_names(db, caller, origin.fingerprint)

    assert names.boards == ("Open board",)
    assert names.file_areas == ("Open files",)
    assert names.channels == ("open-chat",)

    senior = create_user(db, "senior", password="pw-long-enough", user_level=60)
    senior_names = openable_carried_names(db, senior, origin.fingerprint)
    assert senior_names.boards == ("High board", "Open board")  # still no age on file
    assert senior_names.file_areas == ("High files", "Open files")
    assert senior_names.channels == ("high-chat", "open-chat")


# -- the migration -----------------------------------------------------------


def test_first_stored_migration_backfills_from_updated_at(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "descriptor_first_stored_at" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    db = Database(tmp_path / "node.db")
    own = bootstrap_node_identity("own")
    peer = bootstrap_node_identity("peer")
    introduced = bootstrap_node_identity("introduced")
    candidate = bootstrap_node_identity("candidate")
    origin = bootstrap_node_identity("origin")
    record = _record(peer, created_at="2026-03-01T00:00:00+00:00")
    db.connection.execute(
        """INSERT INTO link_peers (fingerprint, root_public_key, transitions_json, descriptor_json, updated_at,
               last_direct_contact_at) VALUES (?, 'AA==', '[]', ?, ?, ?)""",
        (peer.fingerprint, json.dumps(record.descriptor.to_dict()), "2026-03-04T05:06:07+00:00",
         "2026-03-02T00:00:00+00:00"),
    )
    db.connection.execute(
        """INSERT INTO link_introduced_identities (fingerprint, root_public_key, transitions_json,
               descriptor_json, introduced_by, updated_at) VALUES (?, 'AA==', '[]', ?, ?, ?)""",
        (introduced.fingerprint, json.dumps(_record(introduced, created_at="2030-01-01T00:00:00+00:00")
                                            .descriptor.to_dict()), peer.fingerprint,
         "2026-04-01T00:00:00+00:00"),
    )
    db.connection.execute(
        "INSERT INTO link_peer_candidates (fingerprint, descriptor_json, updated_at) VALUES (?, ?, ?)",
        (candidate.fingerprint, json.dumps(_record(candidate).descriptor.to_dict()), "2026-05-01T00:00:00+00:00"),
    )
    db.connection.commit()
    _carry_board(db, origin, own, name="Carried before", board_id="b-before")
    db.close()

    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)
    db = Database(tmp_path / "node.db")
    try:
        assert db.connection.execute(
            "SELECT descriptor_first_stored_at FROM link_peers"
        ).fetchone()[0] == "2026-03-04T05:06:07+00:00"
        assert db.connection.execute(
            "SELECT descriptor_first_stored_at FROM link_introduced_identities"
        ).fetchone()[0] == "2026-04-01T00:00:00+00:00"
        assert tuple(db.connection.execute(
            "SELECT descriptor_first_stored_at, first_named_at FROM link_peer_candidates"
        ).fetchone()) == ("2026-05-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00")
        # The view over the two identity tables still reads.
        assert db.connection.execute("SELECT COUNT(*) FROM link_known_identities").fetchone()[0] == 2

        entries = _by_fp(build_node_map(db, own_fingerprint=own.fingerprint, sysop=True, now=NOW))
        assert entries[peer.fingerprint].last_heard == datetime(2026, 3, 2, tzinfo=timezone.utc)
        # The future-dated introduced descriptor is capped at the backfill.
        assert entries[introduced.fingerprint].last_heard == datetime(2026, 4, 1, tzinfo=timezone.utc)
        assert entries[candidate.fingerprint].first_named == datetime(2026, 5, 1, tzinfo=timezone.utc)
        assert entries[origin.fingerprint].source == ORIGIN
    finally:
        db.close()


# -- real-time sessions count as contact ------------------------------------


def test_an_open_realtime_session_records_contact_at_start_while_open_and_at_close():
    from netbbs.link.transport import LinkRealtimeSessionRegistry

    calls: list[tuple[str, str]] = []

    class _Session:
        remote_fingerprint = "peer-fp"
        is_initiator = True
        local_transport_key = None

        def __init__(self) -> None:
            self.closed = asyncio.Event()

        def seconds_since_activity(self) -> float:
            return 0.0

        async def close(self, *, reason: str, send_close_frame: bool = False) -> None:
            self.closed.set()

    async def scenario() -> None:
        async def _on_contact(fingerprint: str, at: str) -> None:
            calls.append((fingerprint, at))

        registry = LinkRealtimeSessionRegistry(
            own_fingerprint="me", on_contact=_on_contact, contact_interval_seconds=0.05,
        )
        session = _Session()
        assert await registry.admit(session)
        await asyncio.sleep(0)
        assert len(calls) == 1  # at the start, before any interval has passed
        # And while it stays open: waited for, not slept for, since a loaded
        # machine may run the interval late.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while len(calls) < 3 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert len(calls) >= 3
        open_calls = len(calls)
        await registry.close_all(reason="test")
        assert len(calls) == open_calls + 1  # and once at close
        assert registry.get("peer-fp") is None
        await asyncio.sleep(0.12)
        assert len(calls) == open_calls + 1  # nothing after it closed

    asyncio.run(scenario())
    assert {fingerprint for fingerprint, _ in calls} == {"peer-fp"}
    parsed = [datetime.strptime(at, "%Y-%m-%dT%H:%M:%S.%fZ") for _, at in calls]
    assert parsed == sorted(parsed)


def test_a_failing_contact_recorder_does_not_disturb_the_session():
    from netbbs.link.transport import LinkRealtimeSessionRegistry

    class _Session:
        remote_fingerprint = "peer-fp"
        is_initiator = True
        local_transport_key = None

        def __init__(self) -> None:
            self.closed = asyncio.Event()

        def seconds_since_activity(self) -> float:
            return 0.0

        async def close(self, *, reason: str, send_close_frame: bool = False) -> None:
            self.closed.set()

    async def scenario() -> None:
        async def _broken(fingerprint: str, at: str) -> None:
            raise RuntimeError("database is gone")

        registry = LinkRealtimeSessionRegistry(own_fingerprint="me", on_contact=_broken, contact_interval_seconds=0.02)
        session = _Session()
        assert await registry.admit(session)
        await asyncio.sleep(0.06)
        assert registry.get("peer-fp") is session
        await registry.close_all(reason="test")
        assert registry.get("peer-fp") is None

    asyncio.run(scenario())


def test_a_real_loopback_realtime_session_advances_the_peers_last_contact(tmp_path):
    """End to end over a real Noise session on a loopback socket: the
    listener's registry records contact with the dialing peer through the
    database lane, as a running node wires it."""
    from netbbs.link.transport import LinkRealtimeServer, LinkRealtimeSessionRegistry, dial_realtime_session
    from netbbs.storage.execution import DatabaseLane

    listener_db = Database(tmp_path / "listener.db")
    listener_lane = DatabaseLane(listener_db.path)
    listener = bootstrap_node_identity("listener")
    dialer = bootstrap_node_identity("dialer")
    save_peer(listener_db, _record(dialer))
    _set(listener_db, "link_peers", dialer.fingerprint, last_direct_contact_at="2020-01-01T00:00:00.000000Z")

    async def _record_contact(fingerprint: str, at: str) -> None:
        await listener_lane.run(record_direct_contact, fingerprint, at)

    async def _ignore(session, frame) -> None:
        return None

    def _last_contact() -> str:
        return listener_db.connection.execute(
            "SELECT last_direct_contact_at FROM link_peers WHERE fingerprint = ?", (dialer.fingerprint,)
        ).fetchone()[0]

    async def scenario() -> None:
        listener_registry = LinkRealtimeSessionRegistry(
            own_fingerprint=listener.fingerprint, on_contact=_record_contact,
        )
        dialer_registry = LinkRealtimeSessionRegistry(own_fingerprint=dialer.fingerprint)
        server = LinkRealtimeServer(
            host="127.0.0.1", port=0, identity=listener, registry=listener_registry, on_frame=_ignore,
        )
        await server.start()
        try:
            await dial_realtime_session(
                "127.0.0.1", server.port, dialer, on_frame=_ignore, registry=dialer_registry,
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            while _last_contact() == "2020-01-01T00:00:00.000000Z" and loop.time() < deadline:
                await asyncio.sleep(0.02)
            assert _last_contact() > "2026-01-01"
        finally:
            await dialer_registry.close_all(reason="test_done")
            await listener_registry.close_all(reason="test_done")
            await server.stop()

    try:
        asyncio.run(scenario())
    finally:
        listener_lane.close()
        listener_db.close()
