"""
Tests for `netbbs.link.sync` (design doc §12) — the background loop
that makes a node *originate* outbound Link activity. Drives real
`LinkServer` instances (`tests/test_link_transport.py`'s own
real-server/real-client convention) rather than `ScriptedTransport`,
since the whole point is proving the loop actually reaches a peer over
a real socket, pushes real events, and tolerates a real peer being
unreachable or rejecting it.

`run_link_sync`/`dial_hello` persist through a `DatabaseLane`, so
every node here gets a real, separately-opened `Database` file too --
see `tests/test_link_transport.py`'s module docstring for why a
`Database`/`DatabaseLane` pair, not just one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date

import aiohttp
import pytest

from tests.link_sync_wait import run_sync_briefly as _run_sync_briefly

from netbbs.attestation import attest_age, set_attestation_link_visible
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, edit_post
from netbbs.link.boards import link_board, queue_board_post_edit_if_linked, queue_board_post_if_linked
from netbbs.link.events import build_endpoint_descriptor
from netbbs.link.mail import compose_link_message
from netbbs.link.node_identity import bootstrap_node_identity, rotate_operational_key
from netbbs.link.protocol import MAX_EVENTS_PER_REQUEST, HelloMessage, LinkNode, PeerRecord
from netbbs.link.onboarding import Participation, set_participation
from netbbs.link.reliable_nodes import ReliableNode, set_cached_reliable_nodes
from netbbs.link.remote_attestation import (
    configure_attestation_authority,
    configure_attestation_recipient,
    remote_meets_age,
)
from netbbs.link.sync import run_link_sync
from netbbs.link.trust import (
    EvidenceClass,
    TrustDimension,
    TrustState,
    TrustSubject,
    clear_local_observation,
    get_effective_trust_state,
    record_local_observation,
    configure_trust_domain,
    configure_trusted_reporter,
    register_subject,
)
from netbbs.link.trust_issuance import (
    reconcile_issued_vouches,
    record_vouch_intent,
    withdraw_vouch_intent,
)
from netbbs.link.transport import LinkServer, LinkTransportError
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


def _hello_for(node: LinkNode, *, created_at: str = "2026-01-01T00:00:00+00:00"):
    return node.build_hello(addresses=None, outgoing_only=True, created_at=created_at)


async def _run_server(node: LinkNode, lane: DatabaseLane, **kwargs) -> LinkServer:
    server = LinkServer(
        host="127.0.0.1", port=0, node=node, own_hello_provider=lambda: _hello_for(node), lane=lane,
        **kwargs,
    )
    await server.start()
    return server


class _NodeDb:
    """A node's paired `Database` (test assertions) and `DatabaseLane`
    (what the code under test dispatches through) against the same
    file -- see this module's docstring."""

    def __init__(self, tmp_path, name: str) -> None:
        self.db = Database(tmp_path / f"{name}.db")
        self.lane = DatabaseLane(self.db.path)

    def close(self) -> None:
        self.lane.close()
        self.db.close()


def test_sync_completes_a_hello_and_pushes_events_to_a_real_seed(tmp_path):
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            rotated = rotate_operational_key(dialer_identity, purpose="signing")
            dialer_node.identity = rotated

            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

        return seed_node

    try:
        seed_node_after = asyncio.run(scenario())
        assert dialer_identity.fingerprint in seed_node_after.peers
        peer_record = seed_node_after.peers[dialer_identity.fingerprint]
        # Both halves of the rotation (revoke + authorize, per the
        # design doc's own ordering note) reached the seed -- via the
        # hello (which already carried them, since the rotation
        # happened before the first sync pass) and via push_events
        # (pushes *all* of identity.transitions, including the
        # not-yet-seen transport-purpose transition, which lands after
        # them in the flat tuple). This assertion checks membership,
        # not tuple position, which was never a meaningful proxy for
        # "current head" once more than one purpose is interleaved in
        # the same flat PeerRecord.transitions tuple.
        peer_content_ids = {t.content_id for t in peer_record.transitions}
        for transition in dialer_node.identity.transitions:
            assert transition.content_id in peer_content_ids
    finally:
        dialer.close()
        seed.close()


def test_sync_refreshes_mutable_hello_claims_before_the_first_provider_call(tmp_path):
    node = LinkNode(identity=bootstrap_node_identity("refreshing-sync"))
    node_db = _NodeDb(tmp_path, "refreshing-sync")
    stop_event = asyncio.Event()
    events: list[str] = []

    class RefreshableHello:
        async def refresh(self, lane):
            assert lane is node_db.lane
            events.append("refresh")

        def __call__(self):
            events.append("provide")
            stop_event.set()
            return _hello_for(node)

    async def scenario():
        async with aiohttp.ClientSession() as session:
            await run_link_sync(
                node, session, [], RefreshableHello(), node_db.lane,
                interval_seconds=60.0, stop_event=stop_event,
            )

    try:
        asyncio.run(scenario())
        assert events == ["refresh", "provide"]
    finally:
        node_db.close()


def test_sync_requests_and_persists_a_seeds_peer_list(tmp_path):
    """_sync_one_seed also asks the seed who else it knows, right
    after the hello -- the seed here already has carol as a
    completed peer of its own; one sync pass should leave the dialer
    with carol as a recorded (unverified) candidate, on disk too."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    carol_identity = bootstrap_node_identity("carol")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    carol_hello = _hello_for(LinkNode(identity=carol_identity))
    seed_node.handle_hello(carol_hello)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert carol_identity.fingerprint in dialer_node.candidate_descriptors
        assert carol_identity.fingerprint not in dialer_node.peers
        row = dialer.db.connection.execute("SELECT fingerprint FROM link_peer_candidates").fetchone()
        assert row["fingerprint"] == carol_identity.fingerprint
    finally:
        dialer.close()
        seed.close()


def test_sync_dials_a_cached_reliable_node_once_participation_is_accepted(tmp_path):
    """A seed the operator never configured, but that a (simulated)
    reliable-nodes refresh already cached, still gets dialed once the
    SysOp has accepted participation (design doc §16, issue #219) --
    proves the per-pass merge actually happens, not just that the
    cache-read function exists."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")
    ports: list[int] = []

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        ports.append(seed_server.port)
        seed_url = f"http://127.0.0.1:{seed_server.port}"
        # Not passed as an operator-configured seed below -- only cached,
        # as if a prior run_scheduled_reliable_nodes_refresh pass had
        # already fetched it, with participation accepted.
        set_cached_reliable_nodes(dialer.db, [ReliableNode(name="Seed", url=seed_url)])
        set_participation(dialer.db, Participation.ACCEPTED)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [],  # no operator-configured seeds at all
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert dialer_identity.fingerprint in seed_node.peers  # the dial actually reached the seed
        # The identity observed at the roster URL is what binds "reliable
        # node" to a peer for live relay/anchoring -- recorded by the dial.
        from netbbs.link.reliable_nodes import get_observed_reliable_identities
        assert get_observed_reliable_identities(dialer.db) == {
            f"http://127.0.0.1:{ports[0]}/": seed_identity.fingerprint,
        }
    finally:
        dialer.close()
        seed.close()


# -- candidate fallback (design doc §8.3) --------------------------------


def test_sync_never_dials_a_reliable_node_while_participation_is_not_accepted(tmp_path):
    """The inverse of the test above: a cached roster is *not* dialed
    while participation is declined (or never answered) -- a node
    upgraded in place must never start dialing project infrastructure
    until its SysOp says so (design doc §16, issue #219)."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        seed_url = f"http://127.0.0.1:{seed_server.port}"
        # Not passed as an operator-configured seed below -- only cached,
        # as if a prior run_scheduled_reliable_nodes_refresh pass had
        # already fetched it, with participation accepted.
        set_cached_reliable_nodes(dialer.db, [ReliableNode(name="Seed", url=seed_url)])
        set_participation(dialer.db, Participation.DECLINED)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [],  # no operator-configured seeds at all
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert dialer_identity.fingerprint not in seed_node.peers  # never dialed
    finally:
        dialer.close()
        seed.close()


# -- candidate fallback (design doc §8.3) --------------------------------


def _seed_candidate(dialer_node: LinkNode, candidate_identity, *, port: int) -> None:
    """Directly populates a candidate descriptor for a real running
    server, as if an earlier peer-list exchange had already discovered
    it -- no protocol round trip needed to set up this test state."""
    descriptor = build_endpoint_descriptor(
        signing_identity=candidate_identity.signing_key,
        subject_fingerprint=candidate_identity.fingerprint,
        addresses=[{"protocol": "http", "address": "127.0.0.1", "port": port}],
        outgoing_only=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    dialer_node.candidate_descriptors[candidate_identity.fingerprint] = descriptor


def test_sync_falls_back_to_a_candidate_when_the_only_seed_fails(tmp_path):
    dialer_identity = bootstrap_node_identity("dialer")
    candidate_identity = bootstrap_node_identity("candidate")
    dialer_node = LinkNode(identity=dialer_identity)
    candidate_node = LinkNode(identity=candidate_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    candidate = _NodeDb(tmp_path, "candidate")

    async def scenario():
        candidate_server = await _run_server(candidate_node, candidate.lane)
        _seed_candidate(dialer_node, candidate_identity, port=candidate_server.port)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, ["http://127.0.0.1:1"],  # the only seed, dead
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task, settle=3.0)
        finally:
            await candidate_server.stop()

    try:
        asyncio.run(scenario())
        assert candidate_identity.fingerprint in dialer_node.peers  # reached via fallback
        assert candidate_identity.fingerprint not in dialer_node.candidate_descriptors  # promoted, not left behind
        assert dialer_identity.fingerprint in candidate_node.peers  # the dial really landed
    finally:
        dialer.close()
        candidate.close()


def test_sync_tries_a_candidates_second_address_when_its_first_is_dead(tmp_path):
    """Issue #58: previously only `addresses[0]` was ever attempted for
    any peer, anywhere -- a candidate's later, genuinely-reachable
    addresses were silently dead data. A real server behind the
    *second* advertised address must still be reached."""
    dialer_identity = bootstrap_node_identity("dialer")
    candidate_identity = bootstrap_node_identity("candidate")
    dialer_node = LinkNode(identity=dialer_identity)
    candidate_node = LinkNode(identity=candidate_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    candidate = _NodeDb(tmp_path, "candidate")

    async def scenario():
        candidate_server = await _run_server(candidate_node, candidate.lane)
        descriptor = build_endpoint_descriptor(
            signing_identity=candidate_identity.signing_key,
            subject_fingerprint=candidate_identity.fingerprint,
            addresses=[
                {"protocol": "http", "address": "127.0.0.1", "port": 1},  # dead
                {"protocol": "http", "address": "127.0.0.1", "port": candidate_server.port},  # real
            ],
            outgoing_only=False,
            created_at="2026-01-01T00:00:00+00:00",
        )
        dialer_node.candidate_descriptors[candidate_identity.fingerprint] = descriptor
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, ["http://127.0.0.1:1"],  # the only seed, dead too
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task, settle=8.0)
        finally:
            await candidate_server.stop()

    try:
        asyncio.run(scenario())
        assert candidate_identity.fingerprint in dialer_node.peers  # reached via the second address
        assert dialer_identity.fingerprint in candidate_node.peers  # the dial really landed
    finally:
        dialer.close()
        candidate.close()


def test_sync_falls_back_when_no_seeds_are_configured_at_all(tmp_path):
    """The brand-new-node case the design doc names as the one this
    resilience path matters most for."""
    dialer_identity = bootstrap_node_identity("dialer")
    candidate_identity = bootstrap_node_identity("candidate")
    dialer_node = LinkNode(identity=dialer_identity)
    candidate_node = LinkNode(identity=candidate_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    candidate = _NodeDb(tmp_path, "candidate")

    async def scenario():
        candidate_server = await _run_server(candidate_node, candidate.lane)
        _seed_candidate(dialer_node, candidate_identity, port=candidate_server.port)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [],  # zero seeds, participation undecided
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task, settle=3.0)
        finally:
            await candidate_server.stop()

    try:
        asyncio.run(scenario())
        assert candidate_identity.fingerprint in dialer_node.peers
    finally:
        dialer.close()
        candidate.close()


def test_sync_does_not_fall_back_when_a_seed_succeeds(tmp_path):
    """A candidate must never be dialed via *fallback* just because it's
    known -- only when every seed this pass genuinely failed. Issue
    #58's separate relay-selection mechanism also dials known
    candidates, but only for an outgoing-only node (design doc §8.5: a
    full peer never needs relays) -- this dialer is deliberately built
    as a full peer here so that mechanism stays out of this test's way,
    keeping it scoped to fallback specifically (relay selection has its
    own dedicated tests)."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    candidate_identity = bootstrap_node_identity("candidate")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    candidate_node = LinkNode(identity=candidate_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")
    candidate = _NodeDb(tmp_path, "candidate")

    def _full_peer_hello_for_dialer() -> HelloMessage:
        return dialer_node.build_hello(
            addresses=[{"protocol": "http", "address": "198.51.100.50", "port": 7862}],
            outgoing_only=False,
            created_at="2026-01-01T00:00:00+00:00",
        )

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        candidate_server = await _run_server(candidate_node, candidate.lane)
        _seed_candidate(dialer_node, candidate_identity, port=candidate_server.port)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        _full_peer_hello_for_dialer, dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()
            await candidate_server.stop()

    try:
        asyncio.run(scenario())
        assert dialer_identity.fingerprint in seed_node.peers  # the real seed was reached
        assert candidate_identity.fingerprint not in dialer_node.peers  # candidate never dialed
        assert dialer_identity.fingerprint not in candidate_node.peers
    finally:
        dialer.close()
        seed.close()
        candidate.close()


def test_sync_respects_the_fallback_attempt_cap(tmp_path, monkeypatch):
    import netbbs.link.sync as sync_module

    monkeypatch.setattr(sync_module, "_MAX_CANDIDATE_FALLBACK_ATTEMPTS", 1)

    dialer_identity = bootstrap_node_identity("dialer")
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")

    # Two candidates, both genuinely undialable (dead ports) -- with the
    # cap patched to 1, only one of the two should ever be attempted.
    # Since neither can succeed, the observable proxy here is that the
    # pass completes and returns (proven by _run_sync_briefly not
    # timing out) rather than hanging trying every candidate forever --
    # a weak assertion on its own, strengthened by counting attempts
    # via a wrapped dial.
    first_identity = bootstrap_node_identity("first")
    second_identity = bootstrap_node_identity("second")
    _seed_candidate(dialer_node, first_identity, port=1)
    _seed_candidate(dialer_node, second_identity, port=2)

    attempted_urls: list[str] = []
    original_dial_hello = sync_module.dial_hello

    async def counting_dial_hello(node, session, base_url, *args, **kwargs):
        attempted_urls.append(base_url)
        return await original_dial_hello(node, session, base_url, *args, **kwargs)

    monkeypatch.setattr(sync_module, "dial_hello", counting_dial_hello)

    # A full peer, not the shared outgoing_only=True `_hello_for` default --
    # otherwise `_maintain_relay_selection` also runs this same pass and
    # dials both candidates on its own, independently-bounded schedule
    # (`TARGET_RELAY_COUNT`), which has nothing to do with the fallback cap
    # this test targets and would make `attempted_urls` flaky depending on
    # `_try_candidate_fallback`'s own random pick. Same fix as the test
    # above: keep this scoped to fallback specifically.
    def _full_peer_hello_for_dialer() -> HelloMessage:
        return dialer_node.build_hello(
            addresses=[{"protocol": "http", "address": "198.51.100.50", "port": 7862}],
            outgoing_only=False,
            created_at="2026-01-01T00:00:00+00:00",
        )

    async def scenario():
        async with aiohttp.ClientSession() as session:
            task = asyncio.create_task(
                run_link_sync(
                    dialer_node, session, ["http://127.0.0.1:1"],  # the only "seed", also dead
                    _full_peer_hello_for_dialer, dialer.lane, interval_seconds=60.0,
                )
            )
            await _run_sync_briefly(task, settle=3.0)

    try:
        asyncio.run(scenario())
        # One call for the dead seed itself, plus at most one fallback
        # candidate attempt (the cap) -- never both candidates.
        candidate_urls = [u for u in attempted_urls if u != "http://127.0.0.1:1"]
        assert len(candidate_urls) <= 1
    finally:
        dialer.close()


def test_sync_pushes_own_linked_board_genesis_and_post_to_a_real_seed(tmp_path):
    """`_sync_one_seed` also pushes this node's own `board_
    genesis`/`board_post` events, read fresh off the `boards`/`posts`
    tables (`netbbs.link.boards.load_own_board_events`) via the same
    `lane` already used for `dial_hello`'s own persistence -- proves
    they actually reach a real peer over a real socket, not just that
    the query returns the right rows."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    genesis = link_board(dialer.db, board, node_identity=dialer_identity)
    post = create_post(dialer.db, board, creator, "hello", "world")
    board_post = queue_board_post_if_linked(dialer.db, post, board, node_identity=dialer_identity)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert genesis.content_id in seed_node.known_event_ids
        assert board.board_id in seed_node.boards
        assert board_post.content_id in seed_node.known_event_ids
    finally:
        dialer.close()
        seed.close()


def test_sync_materializes_a_received_post_into_the_seeds_own_browsable_board(tmp_path):
    """Design doc §9.3/issue #73 regression: the carried board on the
    receiving side must not just track the board_post event
    (known_event_ids) -- it must become a real, locally browsable
    `posts` row, over the exact same real-socket push path the
    sibling test above already proves reaches the peer at all."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    link_board(dialer.db, board, node_identity=dialer_identity)
    post = create_post(dialer.db, board, creator, "hello", "world")
    board_post = queue_board_post_if_linked(dialer.db, post, board, node_identity=dialer_identity)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        row = seed.db.connection.execute(
            "SELECT subject, body, author_user_id, author_label FROM posts WHERE post_id = ?",
            (board_post.content_id,),
        ).fetchone()
        assert row is not None
        assert row["subject"] == "hello"
        assert row["body"] == "world"
        assert row["author_user_id"] is None
        assert row["author_label"] == f"alice@{dialer_identity.fingerprint}"
        # Indexed for local search too, the same call every other posts
        # write path already makes.
        search_row = seed.db.connection.execute(
            "SELECT 1 FROM post_search WHERE root_post_id = ?", (board_post.content_id,)
        ).fetchone()
        assert search_row is not None
    finally:
        dialer.close()
        seed.close()


def test_sync_pushes_a_self_authored_board_post_edit_to_a_real_seed(tmp_path):
    """`load_own_board_events` also gathers this node's own
    `board_post_edit` events (stored on the edited revision's own
    `posts.link_event_json` column) -- proves one actually reaches a
    real peer and lands correctly in `seed_node.post_edits`."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    link_board(dialer.db, board, node_identity=dialer_identity)
    post = create_post(dialer.db, board, creator, "hello", "world")
    board_post = queue_board_post_if_linked(dialer.db, post, board, node_identity=dialer_identity)
    edited = edit_post(dialer.db, post, board, subject="hello (edited)", body="world, edited", edited_by=creator)
    edit = queue_board_post_edit_if_linked(dialer.db, edited, board, node_identity=dialer_identity, edited_by=creator)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert board_post.content_id in seed_node.known_event_ids
        assert edit.content_id in seed_node.known_event_ids
        assert seed_node.post_edits[board_post.content_id][-1].content_id == edit.content_id
    finally:
        dialer.close()
        seed.close()


def test_sync_materializes_a_received_edit_as_the_seeds_resolved_current_version(tmp_path):
    """Design doc §9.3/issue #73 regression: a received, self-authored
    edit must update what a reader on the carrying node actually sees
    -- not just extend `seed_node.post_edits` in memory."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    link_board(dialer.db, board, node_identity=dialer_identity)
    post = create_post(dialer.db, board, creator, "hello", "world")
    board_post = queue_board_post_if_linked(dialer.db, post, board, node_identity=dialer_identity)
    edited = edit_post(dialer.db, post, board, subject="hello (edited)", body="world, edited", edited_by=creator)
    edit = queue_board_post_edit_if_linked(dialer.db, edited, board, node_identity=dialer_identity, edited_by=creator)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        root_row = seed.db.connection.execute(
            "SELECT subject FROM posts WHERE post_id = ?", (board_post.content_id,)
        ).fetchone()
        assert root_row["subject"] == "hello"  # the root row itself is never mutated in place

        current = seed.db.connection.execute(
            """
            SELECT subject, body FROM posts
            WHERE root_post_id = ? AND status = 'approved'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (board_post.content_id,),
        ).fetchone()
        assert current["subject"] == "hello (edited)"
        assert current["body"] == "world, edited"

        edit_row = seed.db.connection.execute(
            "SELECT edit_of_post_id FROM posts WHERE post_id = ?", (edit.content_id,)
        ).fetchone()
        assert edit_row["edit_of_post_id"] == board_post.content_id
    finally:
        dialer.close()
        seed.close()


def test_sync_dials_every_configured_seed_in_one_pass(tmp_path):
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    seed_a_node = LinkNode(identity=bootstrap_node_identity("seed-a"))
    seed_b_node = LinkNode(identity=bootstrap_node_identity("seed-b"))
    dialer = _NodeDb(tmp_path, "dialer")
    seed_a = _NodeDb(tmp_path, "seed-a")
    seed_b = _NodeDb(tmp_path, "seed-b")

    async def scenario():
        seed_a_server = await _run_server(seed_a_node, seed_a.lane)
        seed_b_server = await _run_server(seed_b_node, seed_b.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session,
                        [f"http://127.0.0.1:{seed_a_server.port}", f"http://127.0.0.1:{seed_b_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_a_server.stop()
            await seed_b_server.stop()

    try:
        asyncio.run(scenario())
        assert dialer_node.identity.fingerprint in seed_a_node.peers
        assert dialer_node.identity.fingerprint in seed_b_node.peers
    finally:
        dialer.close()
        seed_a.close()
        seed_b.close()


def test_sync_skips_an_unreachable_seed_without_crashing_the_loop(tmp_path):
    """A dead seed (port 1, nothing listening) must not prevent a
    *later* reachable seed in the same pass from being dialed. A
    generous settle window -- how long a real "connection refused" to
    a privileged port takes to surface at the OS level isn't something
    this test controls, and a short one flaked here on a sandbox where
    it took longer than expected."""
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    reachable_node = LinkNode(identity=bootstrap_node_identity("reachable"))
    dialer = _NodeDb(tmp_path, "dialer")
    reachable = _NodeDb(tmp_path, "reachable")

    async def scenario():
        reachable_server = await _run_server(reachable_node, reachable.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session,
                        ["http://127.0.0.1:1", f"http://127.0.0.1:{reachable_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task, settle=3.0)
        finally:
            await reachable_server.stop()

    try:
        asyncio.run(scenario())
        assert dialer_node.identity.fingerprint in reachable_node.peers
    finally:
        dialer.close()
        reachable.close()


def test_sync_runs_a_second_pass_after_the_interval_elapses(tmp_path):
    """A short interval must produce a *second* completed hello, not
    just the immediate first-pass one -- proves the sleep-then-repeat
    shape actually repeats, not just runs once."""
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    hello_count = 0
    real_handle_hello = seed_node.handle_hello

    def _counting_handle_hello(message, **kwargs):
        nonlocal hello_count
        hello_count += 1
        return real_handle_hello(message, **kwargs)

    seed_node.handle_hello = _counting_handle_hello

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=0.05,
                    )
                )
                # Until the second hello lands, not for a fixed 0.3s: two
                # passes over a real transport can take longer than that on
                # a loaded parallel run.
                deadline = asyncio.get_running_loop().time() + 60
                while hello_count < 2 and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                await _run_sync_briefly(task, settle=0)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert hello_count >= 2
    finally:
        dialer.close()
        seed.close()


def test_sync_is_cleanly_cancellable_mid_sleep(tmp_path):
    """Cancelling during the interval sleep (not mid-dial) must still
    propagate CancelledError cleanly, the same contract netbbs.__main__
    already relies on for its other background tasks (e.g. the
    daybreak announcer)."""
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    dialer = _NodeDb(tmp_path, "dialer")

    async def scenario():
        async with aiohttp.ClientSession() as session:
            task = asyncio.create_task(
                run_link_sync(
                    dialer_node, session, [], lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0
                )
            )
            await asyncio.sleep(0.05)  # past the (empty) seed pass, into the sleep
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        asyncio.run(scenario())
    finally:
        dialer.close()


def test_sync_pushes_pending_link_mail_directly_to_its_known_recipient(tmp_path):
    """The routing decision, proved over a real socket: a pending
    `link_message` is pushed straight to its own recipient node using
    the address already on file for it (from a prior hello), not to
    whichever seeds happen to be configured. Uses the recipient itself
    as the configured "seed" for the first pass -- exactly what lets
    the dialer resolve its signing key (to compose to it) and its
    address (to reach it directly) in the first place, per this
    module's own docstring on why a target must already be a known
    peer."""
    dialer_identity = bootstrap_node_identity("dialer")
    recipient_identity = bootstrap_node_identity("recipient")
    dialer_node = LinkNode(identity=dialer_identity)
    recipient_node = LinkNode(identity=recipient_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    recipient = _NodeDb(tmp_path, "recipient")

    alice = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    create_user(recipient.db, "bob", password="hunter2", user_level=10)

    async def scenario():
        # Unlike _hello_for/_run_server's own outgoing_only=True default
        # (fine for every other test here, which only ever pushes *to* a
        # statically-configured seed URL), the recipient must advertise
        # a real, dialable address in its own hello -- that's the only
        # way the dialer's later _dialable_addresses_for_peer lookup has
        # anything to find for it.
        recipient_server = LinkServer(
            host="127.0.0.1", port=0, node=recipient_node,
            own_hello_provider=lambda: recipient_node.build_hello(
                addresses=[{"protocol": "http", "address": "127.0.0.1", "port": recipient_server.port}],
                outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
            ),
            lane=recipient.lane,
        )
        await recipient_server.start()
        seed_url = f"http://127.0.0.1:{recipient_server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                # First pass: just the hello, so the dialer learns
                # recipient's signing key/address.
                first_pass = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [seed_url], lambda: _hello_for(dialer_node),
                        dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(first_pass)

                message = compose_link_message(
                    dialer.db, alice, f"bob@{recipient_identity.fingerprint}", "hello", "world",
                    node_identity=dialer_identity,
                )

                # Second pass: the pending message should now reach
                # recipient directly.
                second_pass = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [seed_url], lambda: _hello_for(dialer_node),
                        dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(second_pass)
        finally:
            await recipient_server.stop()

        return message

    try:
        message = asyncio.run(scenario())
        assert message.content_id in recipient_node.known_event_ids
        row = recipient.db.connection.execute(
            "SELECT subject, body, link_source_event_id FROM mail_messages"
        ).fetchone()
        assert row["subject"] == "hello"
        assert row["body"] == "world"
        assert row["link_source_event_id"] == message.content_id
    finally:
        dialer.close()
        recipient.close()


def test_sync_completes_the_link_mail_acknowledgement_round_trip_back_to_the_sender(tmp_path):
    """Regression for issue #69: `compose_link_message` (`netbbs.link.
    mail`) is deliberately DB-only and never registered a composed
    message into the sender's own `LinkNode.events`, so `_resolve_own_
    link_message` (`netbbs.link.protocol`) could never recognize its own
    message once the recipient's `link_message_accepted` came back --
    the sender's own server rejected that acknowledgement with a
    `LinkProtocolError` unconditionally, every time. Proves the full
    round trip over real sockets and two real sync loops: dialer
    composes and pushes; recipient delivers and queues its own
    acknowledgement; dialer's *own* sync loop, dialing the recipient
    again, receives and accepts that acknowledgement -- which used to
    fail before `netbbs.link.sync._push_pending_link_mail` started
    registering the composed message (see that function's own
    docstring)."""
    dialer_identity = bootstrap_node_identity("dialer")
    recipient_identity = bootstrap_node_identity("recipient")
    dialer_node = LinkNode(identity=dialer_identity)
    recipient_node = LinkNode(identity=recipient_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    recipient = _NodeDb(tmp_path, "recipient")

    alice = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    create_user(recipient.db, "bob", password="hunter2", user_level=10)

    async def scenario():
        # Both sides directly dialable here, deliberately -- this test
        # is isolating issue #69's own fix (registering a composed
        # message into the sender's own LinkNode.events before the ack
        # comes back), not the relay-fallback path a not-directly-
        # dialable dialer would now take (issue #94; see
        # test_full_relay_round_trip_delivers_an_acknowledgement_back_
        # to_an_outgoing_only_sender below for that scenario).
        dialer_hello = lambda: dialer_node.build_hello(  # noqa: E731
            addresses=[{"protocol": "http", "address": "127.0.0.1", "port": dialer_server.port}],
            outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
        )
        recipient_hello = lambda: recipient_node.build_hello(  # noqa: E731
            addresses=[{"protocol": "http", "address": "127.0.0.1", "port": recipient_server.port}],
            outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
        )
        dialer_server = LinkServer(
            host="127.0.0.1", port=0, node=dialer_node, own_hello_provider=dialer_hello, lane=dialer.lane
        )
        recipient_server = LinkServer(
            host="127.0.0.1", port=0, node=recipient_node, own_hello_provider=recipient_hello, lane=recipient.lane
        )
        await dialer_server.start()
        await recipient_server.start()
        recipient_seed = f"http://127.0.0.1:{recipient_server.port}"
        dialer_seed = f"http://127.0.0.1:{dialer_server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                # Pass 1: dialer hellos recipient directly -- both sides
                # learn each other's dialable address, since neither
                # hello is outgoing_only here.
                first_pass = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [recipient_seed], dialer_hello, dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(first_pass)

                message = compose_link_message(
                    dialer.db, alice, f"bob@{recipient_identity.fingerprint}", "hello", "world",
                    node_identity=dialer_identity,
                )

                # Pass 2: dialer's sync pushes the message directly to
                # recipient -- this is also where the fix registers the
                # composed message into dialer_node.events (issue #69).
                second_pass = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [recipient_seed], dialer_hello, dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(second_pass)

                # Pass 3: recipient's own sync loop pushes its queued
                # link_message_accepted back to the dialer -- before the
                # fix, the dialer's own _handle_events rejected this
                # every time.
                recipient_pass = asyncio.create_task(
                    run_link_sync(
                        recipient_node, session, [dialer_seed], recipient_hello, recipient.lane,
                        interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(recipient_pass)
        finally:
            await dialer_server.stop()
            await recipient_server.stop()

        return message

    try:
        message = asyncio.run(scenario())
        assert message.content_id in dialer_node.known_event_ids
        row = dialer.db.connection.execute(
            "SELECT link_delivery_status FROM mail_messages WHERE link_event_content_id = ?",
            (message.content_id,),
        ).fetchone()
        assert row["link_delivery_status"] == "delivered"
    finally:
        dialer.close()
        recipient.close()


# -- relay selection, send-via-relay, and pickup (design doc §8.5/issue #58) --------


def _seed_peer(db, identity, *, created_at="2026-01-01T00:00:00+00:00"):
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key,
        subject_fingerprint=identity.fingerprint,
        addresses=None,
        outgoing_only=True,
        created_at=created_at,
    )
    peer = PeerRecord(
        fingerprint=identity.fingerprint,
        root_public_key=bytes(identity.root.verify_key),
        transitions=identity.transitions,
        descriptor=descriptor,
    )
    from netbbs.link.store import save_peer

    save_peer(db, peer)
    return peer


def test_full_relay_round_trip_delivers_a_message_to_an_outgoing_only_recipient(tmp_path):
    """
    Issue #58 end-to-end sync-loop wiring: carol is outgoing-
    only. bob is a full peer willing to relay. alice already knows
    carol (a prior direct hello, persisted -- not something this test
    is trying to prove) but has no way to dial her now. Proves the
    whole chain purely through real sync passes over real sockets:
    carol's own sync pass selects bob as a relay and gets consent;
    alice's own sync pass learns carol is reachable via bob through
    ordinary peer-list exchange, and deposits her pending message there
    since she can't reach carol directly; carol's next pass picks the
    message up from bob and delivers it into her own local mailbox.
    """
    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    carol_identity = bootstrap_node_identity("carol")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    carol_node = LinkNode(identity=carol_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")
    carol = _NodeDb(tmp_path, "carol")

    # alice already knows carol from a prior direct hello (persisted;
    # compose_link_message needs it to resolve her encryption key).
    _seed_peer(alice.db, carol_identity)
    # carol already knows alice too, the other half of that same prior
    # relationship -- needed for her own handle_events to accept the
    # picked-up message later. Set directly on the live LinkNode
    # (issue #53's own "self-origination"/pre-existing-relationship
    # pattern, applied here to "pre-existing," not self-originated).
    # Deliberately outgoing_only=True here too (alice's real, dialable
    # address is a separate matter -- her own _alice_hello below) --
    # this specific descriptor is only what carol has on file for her.
    # Giving carol a bogus *dialable* record for alice would make alice
    # a spurious relay-selection candidate (a real, if inert, hazard
    # caught while first writing this test: dialing an unroutable test
    # address stalls an entire pass for the length of the HTTP timeout).
    carol_node.peers[alice_identity.fingerprint] = PeerRecord(
        fingerprint=alice_identity.fingerprint,
        root_public_key=bytes(alice_identity.root.verify_key),
        transitions=alice_identity.transitions,
        descriptor=build_endpoint_descriptor(
            signing_identity=alice_identity.signing_key,
            subject_fingerprint=alice_identity.fingerprint,
            addresses=None,
            outgoing_only=True,
            created_at="2026-01-01T00:00:00+00:00",
        ),
    )

    alice_user = create_user(alice.db, "alice", password="hunter2", user_level=10)
    carol_user = create_user(carol.db, "carolusername", password="hunter2", user_level=10)

    compose_link_message(
        alice.db, alice_user, f"carolusername@{carol_identity.fingerprint}", "hello",
        "reachable only via bob", node_identity=alice_identity,
    )

    port_holder: dict[str, int] = {}

    def _bob_hello():
        return bob_node.build_hello(
            addresses=[{"protocol": "http", "address": "127.0.0.1", "port": port_holder["port"]}],
            outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
        )

    def _alice_hello():
        return alice_node.build_hello(
            addresses=[{"protocol": "http", "address": "198.51.100.10", "port": 7862}],
            outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
        )

    async def scenario():
        bob_server = LinkServer(
            host="127.0.0.1", port=0, node=bob_node, own_hello_provider=_bob_hello, lane=bob.lane
        )
        await bob_server.start()
        port_holder["port"] = bob_server.port
        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: carol selects bob as a relay and gets consent.
                # Her *own* hello for this pass goes out before her own
                # relay selection runs later in the same pass (see
                # _maintain_relay_selection's own docstring: "the very
                # next hello... carries the updated relays field"),
                # so bob only learns her relay-less descriptor from this
                # first pass -- a short interval lets a second pass run
                # within the settle window, whose hello already reflects
                # the relay she just selected.
                carol_task = asyncio.create_task(
                    run_link_sync(
                        carol_node, session, [f"http://127.0.0.1:{bob_server.port}"],
                        lambda: _hello_for(carol_node), carol.lane, interval_seconds=0.2,
                    )
                )
                await _run_sync_briefly(carol_task, settle=2.0)

                # Step 2: alice learns of carol's relayed reachability via
                # bob's own peer list, and deposits her pending message
                # there since she can't reach carol directly.
                alice_task = asyncio.create_task(
                    run_link_sync(
                        alice_node, session, [f"http://127.0.0.1:{bob_server.port}"],
                        _alice_hello, alice.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(alice_task, settle=1.0)

                # Step 3: carol's next pass picks the message up from bob.
                carol_task_2 = asyncio.create_task(
                    run_link_sync(
                        carol_node, session, [f"http://127.0.0.1:{bob_server.port}"],
                        lambda: _hello_for(carol_node), carol.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(carol_task_2, settle=1.0)
        finally:
            await bob_server.stop()

    try:
        asyncio.run(scenario())

        assert carol_identity.fingerprint in bob_node.relaying_for
        assert bob_identity.fingerprint in carol_node.relays_serving_me

        row = carol.db.connection.execute("SELECT * FROM mail_messages").fetchone()
        assert row is not None
        assert row["recipient_user_id"] == carol_user.id
        assert row["subject"] == "hello"
        assert row["body"] == "reachable only via bob"

        # The relay's own mailbox is empty again -- picked up and cleared.
        assert bob.db.connection.execute("SELECT * FROM link_relay_mailbox").fetchone() is None

        # Issue #874: alice's copy is not delivered on the relay's word. It
        # stays pending (carol's acceptance cannot reach alice's made-up
        # address here), with the handoff recorded for Sent and the timeout.
        sent = alice.db.connection.execute(
            "SELECT link_delivery_status, link_relay_handoff_at FROM mail_messages"
        ).fetchone()
        assert sent["link_delivery_status"] == "pending"
        assert sent["link_relay_handoff_at"] is not None
    finally:
        alice.close()
        bob.close()
        carol.close()


def test_the_sync_pass_expires_mail_left_at_a_relay_that_got_no_answer(tmp_path):
    """Issue #874: a relay deposit ends the delivery work item, so the sync
    pass itself gives up on the letter once 14 days pass with no answer,
    and leaves a letter handed over more recently alone."""
    from netbbs.link.sync import _push_pending_link_mail

    alice_identity = bootstrap_node_identity("alice")
    carol_identity = bootstrap_node_identity("carol")
    alice = _NodeDb(tmp_path, "alice")
    try:
        _seed_peer(alice.db, carol_identity)
        alice_user = create_user(alice.db, "alice", password="hunter2", user_level=10)
        old = compose_link_message(
            alice.db, alice_user, f"carol@{carol_identity.fingerprint}", "old", "b", node_identity=alice_identity,
        )
        recent = compose_link_message(
            alice.db, alice_user, f"carol@{carol_identity.fingerprint}", "recent", "b",
            node_identity=alice_identity,
        )
        # Both were deposited at a relay: their work items are done.
        alice.db.connection.execute("UPDATE link_work_items SET status = 'pushed'")
        alice.db.connection.execute(
            "UPDATE mail_messages SET link_relay_handoff_at = ? WHERE link_event_content_id = ?",
            ("2000-01-01T00:00:00.000000Z", old.content_id),
        )
        alice.db.connection.execute(
            "UPDATE mail_messages SET link_relay_handoff_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE link_event_content_id = ?",
            (recent.content_id,),
        )
        alice.db.connection.commit()

        asyncio.run(_push_pending_link_mail(LinkNode(identity=alice_identity), None, alice.lane))

        rows = {
            row["subject"]: tuple(row)[1:]
            for row in alice.db.connection.execute(
                "SELECT subject, link_delivery_status, link_delivery_reason, link_delivery_notice_pending "
                "FROM mail_messages"
            )
        }
        assert rows == {"old": ("expired", "no_answer", 1), "recent": ("pending", None, 0)}
    finally:
        alice.close()


def test_full_relay_round_trip_delivers_an_acknowledgement_back_to_an_outgoing_only_sender(tmp_path):
    """
    Issue #94's ack-relay sibling fix to issue #58: alice is outgoing-
    only and sends mail to carol, a full peer she can dial directly (no
    relay needed for the original message -- alice is the one doing the
    dialing; being outgoing-only only ever means unreachable *inbound*).
    carol accepts and immediately queues an acknowledgement addressed
    back to alice, whom *she* cannot dial directly. Before this fix,
    that acknowledgement had no relay fallback at all (`_push_pending_
    link_mail`'s own prior docstring said only `link_message` got one,
    "never an acknowledgement") -- it would retry forever and eventually
    dead-letter, leaving alice's own view of her sent mail stuck on
    "pending" no matter how long real time passed. Found live during
    issue #83's dogfood run.

    Proves the full chain over real sockets: alice selects bob as her
    relay (a real pass, same mechanics the sibling "message to an
    outgoing-only recipient" test above already proves); carol -- whose
    copy of alice's descriptor already reflects that relay, sidestepping
    the peer-list-discovery mechanics that sibling test covers instead
    of this one -- deposits her acknowledgement at bob since she can't
    reach alice directly; alice's next pass picks it up from bob and
    resolves her own `mail_messages` row to "delivered".
    """
    from netbbs.link.store import save_peer

    alice_identity = bootstrap_node_identity("alice")
    bob_identity = bootstrap_node_identity("bob")
    carol_identity = bootstrap_node_identity("carol")
    alice_node = LinkNode(identity=alice_identity)
    bob_node = LinkNode(identity=bob_identity)
    carol_node = LinkNode(identity=carol_identity)
    alice = _NodeDb(tmp_path, "alice")
    bob = _NodeDb(tmp_path, "bob")
    carol = _NodeDb(tmp_path, "carol")

    alice_user = create_user(alice.db, "alice", password="hunter2", user_level=10)
    create_user(carol.db, "carolusername", password="hunter2", user_level=10)

    def _alice_hello():
        return alice_node.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00")

    async def scenario():
        bob_server = LinkServer(
            host="127.0.0.1", port=0, node=bob_node,
            own_hello_provider=lambda: bob_node.build_hello(
                addresses=[{"protocol": "http", "address": "127.0.0.1", "port": bob_server.port}],
                outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
            ),
            lane=bob.lane,
        )
        carol_server = LinkServer(
            host="127.0.0.1", port=0, node=carol_node,
            own_hello_provider=lambda: carol_node.build_hello(
                addresses=[{"protocol": "http", "address": "127.0.0.1", "port": carol_server.port}],
                outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
            ),
            lane=carol.lane,
        )
        await bob_server.start()
        await carol_server.start()
        bob_url = f"http://127.0.0.1:{bob_server.port}"
        try:
            async with aiohttp.ClientSession() as session:
                # alice already knows carol directly (prior hello,
                # persisted -- compose_link_message needs it to resolve
                # her encryption key, and her own sync pass needs it to
                # resolve carol's dialable address); not something this
                # test is trying to prove.
                carol_peer = PeerRecord(
                    fingerprint=carol_identity.fingerprint,
                    root_public_key=bytes(carol_identity.root.verify_key),
                    transitions=carol_identity.transitions,
                    descriptor=build_endpoint_descriptor(
                        signing_identity=carol_identity.signing_key,
                        subject_fingerprint=carol_identity.fingerprint,
                        addresses=[{"protocol": "http", "address": "127.0.0.1", "port": carol_server.port}],
                        outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
                    ),
                )
                save_peer(alice.db, carol_peer)
                alice_node.peers[carol_identity.fingerprint] = carol_peer

                # carol already knows bob directly too -- needed to
                # resolve alice's relay fingerprint into a dialable
                # address once she learns of it below.
                carol_node.peers[bob_identity.fingerprint] = PeerRecord(
                    fingerprint=bob_identity.fingerprint,
                    root_public_key=bytes(bob_identity.root.verify_key),
                    transitions=bob_identity.transitions,
                    descriptor=build_endpoint_descriptor(
                        signing_identity=bob_identity.signing_key,
                        subject_fingerprint=bob_identity.fingerprint,
                        addresses=[{"protocol": "http", "address": "127.0.0.1", "port": bob_server.port}],
                        outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
                    ),
                )

                # Step 1: alice selects bob as her relay and gets
                # consent -- a real pass, so bob_node.relaying_for/
                # alice_node.relays_serving_me both end up populated for
                # real, the same way the sibling test above proves it
                # for carol selecting bob.
                alice_task = asyncio.create_task(
                    run_link_sync(alice_node, session, [bob_url], _alice_hello, alice.lane, interval_seconds=0.2)
                )
                await _run_sync_briefly(alice_task, settle=2.0)

                # carol already knows alice too (prior hello) -- and her
                # copy of alice's descriptor already reflects the relay
                # alice just selected above, sidestepping the peer-list-
                # discovery mechanics the sibling test already covers
                # (real discovery would need carol to separately query
                # bob's own peer list, which isn't this test's point).
                carol_node.peers[alice_identity.fingerprint] = PeerRecord(
                    fingerprint=alice_identity.fingerprint,
                    root_public_key=bytes(alice_identity.root.verify_key),
                    transitions=alice_identity.transitions,
                    descriptor=build_endpoint_descriptor(
                        signing_identity=alice_identity.signing_key,
                        subject_fingerprint=alice_identity.fingerprint,
                        addresses=None, outgoing_only=True, relays=[bob_identity.fingerprint],
                        created_at="2026-01-01T00:00:00+00:00",
                    ),
                )

                compose_link_message(
                    alice.db, alice_user, f"carolusername@{carol_identity.fingerprint}", "hello",
                    "delivered directly, but the ack needs a relay back", node_identity=alice_identity,
                )

                # Step 2: alice's own next pass pushes the message
                # directly to carol -- resolved via alice_node.peers,
                # independent of the seed list above. Carol's own server
                # accepts it and immediately queues her own acknowledgement
                # (deliver_link_message, netbbs.link.mail).
                alice_task_2 = asyncio.create_task(
                    run_link_sync(alice_node, session, [bob_url], _alice_hello, alice.lane, interval_seconds=60.0)
                )
                await _run_sync_briefly(alice_task_2, settle=1.0)

                # Step 3: carol's own sync pass tries to push her queued
                # acknowledgement -- direct delivery to alice is
                # impossible (alice has no server at all in this test),
                # so this is exactly the path under test: fall back to
                # depositing it at bob.
                carol_task = asyncio.create_task(
                    run_link_sync(
                        carol_node, session, [bob_url],
                        lambda: carol_node.build_hello(
                            addresses=[{"protocol": "http", "address": "127.0.0.1", "port": carol_server.port}],
                            outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
                        ),
                        carol.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(carol_task, settle=1.0)

                # Step 4: alice's next pass picks the acknowledgement up
                # from bob and resolves her own mail_messages row.
                alice_task_3 = asyncio.create_task(
                    run_link_sync(alice_node, session, [bob_url], _alice_hello, alice.lane, interval_seconds=60.0)
                )
                await _run_sync_briefly(alice_task_3, settle=1.0)
        finally:
            await bob_server.stop()
            await carol_server.stop()

    try:
        asyncio.run(scenario())

        assert alice_identity.fingerprint in bob_node.relaying_for
        assert bob_identity.fingerprint in alice_node.relays_serving_me

        carol_row = carol.db.connection.execute("SELECT subject, body FROM mail_messages").fetchone()
        assert carol_row is not None
        assert carol_row["subject"] == "hello"

        alice_row = alice.db.connection.execute("SELECT link_delivery_status FROM mail_messages").fetchone()
        assert alice_row is not None
        assert alice_row["link_delivery_status"] == "delivered"

        # The relay's own mailbox is empty again -- picked up and cleared.
        assert bob.db.connection.execute("SELECT * FROM link_relay_mailbox").fetchone() is None
    finally:
        alice.close()
        bob.close()
        carol.close()


# -- graceful drain via stop_event (design doc §13.11, issue #60) ---------


def test_sync_exits_normally_once_stop_event_is_set_mid_sleep(tmp_path):
    """The cooperative counterpart to test_sync_is_cleanly_cancellable_
    mid_sleep above: setting stop_event lets the task finish its
    current pass and return normally -- no CancelledError, no explicit
    .cancel() needed -- once the loop notices the signal at the top of
    its next iteration."""
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    dialer = _NodeDb(tmp_path, "dialer")
    stop_event = asyncio.Event()

    async def scenario():
        async with aiohttp.ClientSession() as session:
            task = asyncio.create_task(
                run_link_sync(
                    dialer_node, session, [], lambda: _hello_for(dialer_node), dialer.lane,
                    interval_seconds=60.0, stop_event=stop_event,
                )
            )
            await asyncio.sleep(0.05)  # past the (empty) seed pass, into the sleep
            stop_event.set()
            await asyncio.wait_for(task, timeout=5.0)
            assert task.cancelled() is False

    try:
        asyncio.run(scenario())
    finally:
        dialer.close()


def test_sync_runs_no_pass_at_all_when_stop_event_is_already_set(tmp_path):
    """A stop_event set before the task ever starts must return
    immediately, without dialing anything -- confirms the check really
    is "at the top of the loop," not merely "somewhere before the next
    sleep returns.\""""
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    hello_count = 0
    real_handle_hello = seed_node.handle_hello

    def _counting_handle_hello(message, **kwargs):
        nonlocal hello_count
        hello_count += 1
        return real_handle_hello(message, **kwargs)

    seed_node.handle_hello = _counting_handle_hello

    stop_event = asyncio.Event()
    stop_event.set()

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane,
                        interval_seconds=60.0, stop_event=stop_event,
                    )
                )
                await asyncio.wait_for(task, timeout=5.0)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert hello_count == 0
    finally:
        dialer.close()
        seed.close()


def _run_passes(node, node_db, seeds, *, passes: int, outgoing_only: bool = True):
    """Run exactly `passes` sync passes, then stop. Counts passes from
    `refresh`, which `run_link_sync` calls once at the top of every
    pass, and sets `stop_event` on the last one -- the loop condition is
    only re-checked between passes, so the pass in progress still
    finishes normally (`run_link_sync`'s own docstring)."""
    stop_event = asyncio.Event()
    seen = {"passes": 0}

    class CountingHello:
        async def refresh(self, lane):
            seen["passes"] += 1
            if seen["passes"] >= passes:
                stop_event.set()

        def __call__(self):
            return node.build_hello(
                addresses=None if outgoing_only else [
                    {"protocol": "http", "address": "127.0.0.1", "port": 7862}
                ],
                outgoing_only=outgoing_only,
                created_at="2026-01-01T00:00:00+00:00",
            )

    async def scenario():
        async with aiohttp.ClientSession() as session:
            await run_link_sync(
                node, session, seeds, CountingHello(), node_db.lane,
                interval_seconds=0.0, stop_event=stop_event,
            )

    asyncio.run(scenario())
    return seen["passes"]


def _isolation_warnings(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and "consecutive passes" in record.getMessage()
    ]


def test_sync_warns_once_a_node_has_reached_nothing_for_several_passes(tmp_path, caplog):
    """Issue #313: every individual dial failure is already logged, but
    indistinguishable from ordinary churn -- which is how a reliable
    node that had quietly stopped answering went unnoticed. Reaching
    nothing at all, pass after pass, gets its own WARNING (and so its
    own bounded-diagnostic-log entry, §13.11)."""
    node = LinkNode(identity=bootstrap_node_identity("isolated"))
    node_db = _NodeDb(tmp_path, "isolated")
    # Port 1 on loopback: nothing listens, so every pass fails its dial
    # and there are no discovered candidates to fall back to either.
    dead_seed = "http://127.0.0.1:1"

    try:
        with caplog.at_level("WARNING", logger="netbbs.link.sync"):
            _run_passes(node, node_db, [dead_seed], passes=2)
        assert _isolation_warnings(caplog) == [], "must tolerate a couple of failed passes quietly"

        caplog.clear()
        with caplog.at_level("WARNING", logger="netbbs.link.sync"):
            _run_passes(node, node_db, [dead_seed], passes=3)
        warnings = _isolation_warnings(caplog)
        assert len(warnings) == 1, warnings
        assert "3 consecutive passes" in warnings[0]
        assert dead_seed in warnings[0], "names what it tried, so a SysOp can act on it"

        # Sixth consecutive pass warns again, the third and fourth and
        # fifth do not -- a proportionate trail, not one per pass.
        caplog.clear()
        with caplog.at_level("WARNING", logger="netbbs.link.sync"):
            _run_passes(node, node_db, [dead_seed], passes=6)
        assert len(_isolation_warnings(caplog)) == 2
    finally:
        node_db.close()


def test_events_a_peer_refused_one_by_one_are_set_aside_then_offered_again(tmp_path, monkeypatch, caplog):
    """Issue #897, sender side: a refused event stays in the peer's `wanted`
    list. Sent every pass, refused events could fill a whole request again,
    so they wait out the retry time; the SysOp hears about each once."""
    import netbbs.link.sync as sync_module
    from netbbs.link.protocol import DEFERRED_EVENT_RETRY_SECONDS
    from netbbs.link.transport import LinkPolicyRefused, RefusedEvent

    class Event:
        def __init__(self, content_id):
            self.content_id = content_id

    events = [Event("chat-1"), Event("post-2"), Event("post-3")]
    node = LinkNode(identity=bootstrap_node_identity("setting-aside"))
    node_db = _NodeDb(tmp_path, "setting-aside")
    monkeypatch.setattr(sync_module, "load_own_board_events", lambda db, fp: events)
    monkeypatch.setattr(sync_module, "load_own_channel_events", lambda db, fp: [])
    monkeypatch.setattr(sync_module, "load_own_file_area_events", lambda db, fp: [])
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(sync_module.time, "time", lambda: clock["now"])
    pushed: list[list[str]] = []
    reason = "link_policy_user_probationary_approval_required"

    async def fake_push(node, session, url, chunk):
        ids = [event.content_id for event in chunk if isinstance(event, Event)]
        pushed.append(ids)
        refused = [RefusedEvent("chat-1", reason)] if "chat-1" in ids else []
        if refused and len(ids) == 1:
            raise LinkPolicyRefused("refused", reason, refused)
        return [cid for cid in ids if cid != "chat-1"], refused

    monkeypatch.setattr(sync_module, "push_events_partial", fake_push)

    def push(wanted, declared=frozenset({"chat-1", "post-2", "post-3"})):
        asyncio.run(sync_module._push_own_events(
            node, None, "http://peer", node_db.lane, wanted=wanted,
            peer_fingerprint="peer", fallback_offsets={}, declared=declared,
        ))

    def warnings():
        return [r.getMessage() for r in caplog.records
                if r.levelname == "WARNING" and "one by one" in r.getMessage()]

    try:
        with caplog.at_level(logging.WARNING, logger="netbbs.link.sync"):
            push(["chat-1", "post-2", "post-3"])
            exchange = node.peer_exchange["peer"]
            assert pushed[-1] == ["chat-1", "post-2", "post-3"]
            assert set(exchange.set_aside) == {"chat-1"}
            assert exchange.refused_reason is None, "one author's refusal is not about this node"
            assert len(warnings()) == 1 and "peer" in warnings()[0] and reason in warnings()[0]

            # Next pass the peer still wants it; it is not offered yet.
            pushes = len(pushed)
            push(["chat-1"])
            assert all(ids == [] for ids in pushed[pushes:]), "only key transitions go out"

            # After the retry time it is offered again, alone, refused with a
            # 403, and set aside again without a second warning.
            clock["now"] += DEFERRED_EVENT_RETRY_SECONDS + 1
            push(["chat-1"])
            assert pushed[-1] == ["chat-1"]
            assert exchange.set_aside["chat-1"][1] > clock["now"]
            assert len(warnings()) == 1

            # A paged inventory that did not declare it this pass says
            # nothing about it: it stays set aside.
            push([], declared=frozenset({"post-2"}))
            assert set(exchange.set_aside) == {"chat-1"}

            # Once the peer, asked about it, no longer wants it, it is forgotten.
            push([])
            assert exchange.set_aside == {}
    finally:
        node_db.close()


def test_a_sync_pass_releases_an_elapsed_recovery_hold(tmp_path, caplog):
    """Issue #802: a recovery hold's release has no event of its own, so a
    quiet subject stayed quarantined until the node restarted. A sync pass
    now re-evaluates trust against the current time."""
    from datetime import datetime, timedelta, timezone

    def stamp(value):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    node = LinkNode(identity=bootstrap_node_identity("holding"))
    node_db = _NodeDb(tmp_path, "holding")
    now = datetime.now(timezone.utc)
    subject = TrustSubject.node("held-subject")
    register_subject(node_db.db, subject, first_accepted_at=stamp(now - timedelta(days=60)),
                     now_iso=stamp(now - timedelta(days=60)))
    record_local_observation(
        node_db.db, observation_id="proof", subject=subject,
        dimension=TrustDimension.IDENTITY_INTEGRITY, category="signed_equivocation",
        evidence_class=EvidenceClass.SELF_VERIFYING,
        observed_at=stamp(now - timedelta(hours=26)), now_iso=stamp(now - timedelta(hours=26)),
    )
    # Cleared 25 hours ago, so the 24-hour hold ran out an hour ago.
    clear_local_observation(node_db.db, "proof", now_iso=stamp(now - timedelta(hours=25)))
    held = get_effective_trust_state(node_db.db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert (held.state, held.reason_code) == (TrustState.QUARANTINED, "recovery_hold")

    try:
        with caplog.at_level(logging.INFO, logger="netbbs.link.sync"):
            _run_passes(node, node_db, [], passes=1)
        released = get_effective_trust_state(node_db.db, subject, TrustDimension.IDENTITY_INTEGRITY)
        assert (released.state, released.reason_code) == (TrustState.PROBATIONARY, "automatic_recovery")
        assert any(
            "identity_integrity went from quarantined to probationary (automatic_recovery)"
            in record.getMessage()
            for record in caplog.records
        )
    finally:
        node_db.close()


def test_sync_isolation_counter_resets_once_a_seed_answers(tmp_path, caplog):
    """A node that recovers must not accumulate toward the warning
    across an intervening success -- otherwise a flaky seed eventually
    reports permanent isolation that never actually happened.

    Six passes inside one `run_link_sync` (the counter is per-loop, so
    it has to be one call): two that reach nothing, two that reach a
    real seed, two that reach nothing again. Six failing dials in total,
    never three in a row, so no warning."""
    node = LinkNode(identity=bootstrap_node_identity("recovering"))
    seed_node = LinkNode(identity=bootstrap_node_identity("live-seed"))
    node_db = _NodeDb(tmp_path, "recovering")
    seed = _NodeDb(tmp_path, "live-seed")
    dead_seed = "http://127.0.0.1:1"

    async def scenario():
        server = await _run_server(seed_node, seed.lane)
        live_seed = f"http://127.0.0.1:{server.port}"
        # run_link_sync re-reads this list every pass, so mutating it in
        # place from `refresh` is how one loop sees a seed go away and
        # come back without restarting. `refresh` runs at the *top* of a
        # pass, before that pass reads the list, so switching on pass N
        # takes effect from pass N: dead, dead, live, live, dead, dead.
        seeds = [dead_seed]
        stop_event = asyncio.Event()
        seen = {"passes": 0}

        class FlakyNetwork:
            async def refresh(self, lane):
                seen["passes"] += 1
                if seen["passes"] in (3, 5):
                    seeds[:] = [live_seed] if seen["passes"] == 3 else [dead_seed]
                if seen["passes"] >= 6:
                    stop_event.set()

            def __call__(self):
                return _hello_for(node)

        try:
            async with aiohttp.ClientSession() as session:
                await run_link_sync(
                    node, session, seeds, FlakyNetwork(), node_db.lane,
                    interval_seconds=0.0, stop_event=stop_event,
                )
        finally:
            await server.stop()
        return seen["passes"]

    try:
        with caplog.at_level("WARNING", logger="netbbs.link.sync"):
            assert asyncio.run(scenario()) == 6
        assert _isolation_warnings(caplog) == []
    finally:
        node_db.close()
        seed.close()


def test_sync_does_not_call_an_inbound_only_node_isolated(tmp_path, caplog):
    """Issue #313 review: a full peer may decline the reliable roster,
    configure no seeds, and serve inbound helloes perfectly well. This
    outbound loop never observes that inbound traffic, and completed
    peers are removed from candidate_descriptors, so counting "reached
    nothing" would accuse a healthy node of being cut off -- forever,
    every third pass. A pass with nowhere to reach is not an isolated
    pass."""
    node = LinkNode(identity=bootstrap_node_identity("inbound-only"))
    node_db = _NodeDb(tmp_path, "inbound-only")
    try:
        with caplog.at_level("WARNING", logger="netbbs.link.sync"):
            _run_passes(node, node_db, [], passes=9, outgoing_only=False)
        assert _isolation_warnings(caplog) == []
    finally:
        node_db.close()


def test_sync_ignores_undialable_candidates_when_counting_isolation(tmp_path, caplog):
    """Issue #313 review round 2: the guard added for an inbound-only
    node checked that `candidate_descriptors` was non-empty, but a
    candidate whose descriptor is outgoing-only carries no address, so
    `_try_candidate_fallback` skips it without ever attempting a dial.
    Merely knowing of such peers is not "somewhere to reach", and
    counting it as such walks straight back into the false warning."""
    node = LinkNode(identity=bootstrap_node_identity("knows-only-undialable"))
    other = bootstrap_node_identity("outgoing-only-peer")
    node.candidate_descriptors[other.fingerprint] = build_endpoint_descriptor(
        signing_identity=other.signing_key,
        subject_fingerprint=other.fingerprint,
        addresses=None,
        outgoing_only=True,
        created_at="2026-01-01T00:00:00+00:00",
    )
    node_db = _NodeDb(tmp_path, "knows-only-undialable")
    try:
        with caplog.at_level("WARNING", logger="netbbs.link.sync"):
            _run_passes(node, node_db, [], passes=9, outgoing_only=False)
        assert _isolation_warnings(caplog) == []
    finally:
        node_db.close()


def test_sync_still_warns_an_outgoing_only_node_with_nothing_to_dial(tmp_path, caplog):
    """Issue #313 review round 3: the "nowhere to reach" exemption is
    only valid for a node that can still be *reached*. An outgoing-only
    node accepts nothing inbound, so with no seed, no roster entry and
    no dialable candidate it genuinely cannot touch the network at all
    — exactly the state the warning exists for. Exempting it would have
    made the silent case silent again."""
    node = LinkNode(identity=bootstrap_node_identity("outgoing-and-stranded"))
    node_db = _NodeDb(tmp_path, "outgoing-and-stranded")
    try:
        with caplog.at_level("WARNING", logger="netbbs.link.sync"):
            _run_passes(node, node_db, [], passes=3, outgoing_only=True)
        warnings = _isolation_warnings(caplog)
        assert len(warnings) == 1, warnings
        assert "(none configured)" in warnings[0]
    finally:
        node_db.close()


def test_sync_still_warns_when_a_retained_relay_is_offline(tmp_path, caplog):
    """Issue #313 review round 5: keying the exemption on
    `relays_serving_me` being non-empty was wrong. A pickup failure is
    logged and skipped *without* recording a dial outcome, so a relay
    that has gone offline sits in that mapping indefinitely — and would
    have suppressed this warning forever, which is the exact blind spot
    the issue is about. Only actually reaching a relay counts."""
    node = LinkNode(identity=bootstrap_node_identity("dead-relay"))
    dead = bootstrap_node_identity("offline-relay")
    node.relays_serving_me[dead.fingerprint] = "http://127.0.0.1:1"
    node.candidate_descriptors[dead.fingerprint] = build_endpoint_descriptor(
        signing_identity=dead.signing_key,
        subject_fingerprint=dead.fingerprint,
        # Port 1: nothing listens, so every pickup fails.
        addresses=[{"protocol": "http", "address": "127.0.0.1", "port": 1}],
        outgoing_only=False,
        created_at="2026-01-01T00:00:00+00:00",
    )
    node_db = _NodeDb(tmp_path, "dead-relay")
    try:
        with caplog.at_level("WARNING", logger="netbbs.link.sync"):
            _run_passes(node, node_db, [], passes=3, outgoing_only=True)
        assert len(_isolation_warnings(caplog)) == 1
    finally:
        node_db.close()


def _recording_push(monkeypatch):
    """Wraps `netbbs.link.sync`'s own `push_events_partial` (what the
    content push sends with, issue #897) so a test can see exactly how many
    push requests one pass made and what each carried -- the whole point of
    issue #478 is the shape of those requests, not only what ends up on the
    peer."""
    import netbbs.link.sync as sync_module

    real_push = sync_module.push_events_partial
    calls: list[list[str]] = []

    async def recording(node, session, base_url, events, **kwargs):
        calls.append([event.content_id for event in events])
        return await real_push(node, session, base_url, events, **kwargs)

    monkeypatch.setattr(sync_module, "push_events_partial", recording)
    return calls


def test_sync_pushes_only_the_events_the_seed_says_it_lacks(tmp_path, monkeypatch):
    """Design doc §8.6/§8.8, issue #478: the inventory response now also
    reports which of the requester's declared content IDs the seed
    itself is missing, and the push sends exactly those. A second pass
    against a seed already holding the first post must carry the new
    post and nothing else -- before this, every pass re-offered the
    node's entire originated history."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    genesis = link_board(dialer.db, board, node_identity=dialer_identity)
    first = queue_board_post_if_linked(
        dialer.db, create_post(dialer.db, board, creator, "one", "first"), board,
        node_identity=dialer_identity,
    )

    calls = _recording_push(monkeypatch)
    second_holder: list = []

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                seeds = [f"http://127.0.0.1:{seed_server.port}"]
                first_pass = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, seeds,
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(first_pass)
                second_holder.append(
                    queue_board_post_if_linked(
                        dialer.db, create_post(dialer.db, board, creator, "two", "second"), board,
                        node_identity=dialer_identity,
                    )
                )
                calls.clear()
                second_pass = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, seeds,
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(second_pass)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        second = second_holder[0]
        assert len(calls) == 1
        pushed = set(calls[0])
        assert second.content_id in pushed
        assert genesis.content_id not in pushed
        assert first.content_id not in pushed
        assert second.content_id in seed_node.known_event_ids
    finally:
        dialer.close()
        seed.close()


def test_sync_push_is_one_request_however_much_this_node_has_originated(tmp_path, monkeypatch):
    """The actual defect issue #478 names: the old push sliced its whole
    originated history into `MAX_EVENTS_PER_REQUEST`-sized requests, so
    a large node spent its peer's entire per-source request budget
    (§13.9) on the same early slices every pass and never reached the
    tail. A pass must now cost exactly one push request no matter how
    many own events exist."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    link_board(dialer.db, board, node_identity=dialer_identity)
    for i in range(MAX_EVENTS_PER_REQUEST + 10):
        queue_board_post_if_linked(
            dialer.db, create_post(dialer.db, board, creator, f"post {i}", "body"), board,
            node_identity=dialer_identity,
        )

    calls = _recording_push(monkeypatch)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task, settle=2.0)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert len(calls) == 1
        assert len(calls[0]) <= MAX_EVENTS_PER_REQUEST
    finally:
        dialer.close()
        seed.close()


def test_sync_eventually_pushes_the_tail_of_a_large_own_event_list(tmp_path):
    """The converse half of issue #478: bounding one pass to a single
    request must still converge. Each pass the seed's declared inventory
    grows, so the next `wanted` list covers the next stretch -- the tail
    is reached rather than starved behind the same early batches
    forever."""
    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    genesis = link_board(dialer.db, board, node_identity=dialer_identity)
    posts = [
        queue_board_post_if_linked(
            dialer.db, create_post(dialer.db, board, creator, f"post {i}", "body"), board,
            node_identity=dialer_identity,
        )
        for i in range(MAX_EVENTS_PER_REQUEST + 10)
    ]
    expected = {genesis.content_id} | {post.content_id for post in posts}

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=0.05,
                    )
                )
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 30.0
                while not expected <= seed_node.known_event_ids and loop.time() < deadline:
                    await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert expected <= seed_node.known_event_ids
    finally:
        dialer.close()
        seed.close()


def test_sync_still_pushes_when_a_seeds_inventory_route_fails(tmp_path, monkeypatch):
    """A seed whose `/inventory` route errors while `/events` still
    accepts a push cannot say what it lacks, and that must not silence
    the push -- a first-contact peer still has to receive this node's
    genesis events."""
    import netbbs.link.sync as sync_module

    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    genesis = link_board(dialer.db, board, node_identity=dialer_identity)

    async def broken_inventory_route(*args, **kwargs):
        raise LinkTransportError("inventory route is down")

    monkeypatch.setattr(sync_module, "request_inventory", broken_inventory_route)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert genesis.content_id in seed_node.known_event_ids
    finally:
        dialer.close()
        seed.close()


def test_sync_walks_its_own_events_while_a_seeds_inventory_route_stays_broken(tmp_path, monkeypatch):
    """Codex review of #498, on the one reading of it that survives:
    every node here runs the same release, so a peer that cannot say what
    it lacks is a peer whose inventory route is *failing*, not an old
    one. Re-offering the same leading page every pass would never deliver
    the rest to such a seed -- and in an asymmetric topology it never
    dials this node, so its own pull cannot make up the difference.

    `MAX_EVENTS_PER_REQUEST` is lowered so more than one page exists
    without needing hundreds of posts."""
    import netbbs.link.sync as sync_module

    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    genesis = link_board(dialer.db, board, node_identity=dialer_identity)
    posts = [
        queue_board_post_if_linked(
            dialer.db, create_post(dialer.db, board, creator, f"post {i}", "body"), board,
            node_identity=dialer_identity,
        )
        for i in range(11)
    ]
    expected = {genesis.content_id} | {post.content_id for post in posts}

    monkeypatch.setattr(sync_module, "MAX_EVENTS_PER_REQUEST", 4)

    async def broken_inventory_route(*args, **kwargs):
        raise LinkTransportError("inventory route is down")

    monkeypatch.setattr(sync_module, "request_inventory", broken_inventory_route)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=0.05,
                    )
                )
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 30.0
                while not expected <= seed_node.known_event_ids and loop.time() < deadline:
                    await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert expected <= seed_node.known_event_ids
    finally:
        dialer.close()
        seed.close()

def test_sync_pushes_own_events_even_behind_a_wall_of_carried_ones(tmp_path):
    """The asymmetric topology the Codex review of issue #478 named. The
    dialer carries more events originated *elsewhere* than one request
    holds, and the seed lacks all of them -- but the dialer may not push
    carried content ("no relay from a stranger"), and the seed never
    dials the dialer, so its own pull cannot resolve this either.

    While the seed's `wanted` list was prefix-capped, those unsendable
    IDs filled it, the filter dropped every one of them, the seed's
    state never changed, and the identical page came back every pass:
    the dialer's own file descriptor was never offered at all. The
    carried board events are walked before file areas, so this ordering
    is deterministic rather than incidental.
    """
    from netbbs.files.areas import create_file_area, get_file_area_by_name
    from netbbs.files.entries import upload_file
    from netbbs.link.boards import materialize_carried_board, materialize_carried_post
    from netbbs.link.events import build_board_genesis, build_board_post
    from netbbs.link.files import link_file_area, list_remote_files, queue_file_descriptor_if_linked

    dialer_identity = bootstrap_node_identity("dialer")
    elsewhere_identity = bootstrap_node_identity("elsewhere")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    carried_genesis = build_board_genesis(
        signing_identity=elsewhere_identity.signing_key,
        origin_fingerprint=elsewhere_identity.fingerprint,
        board_id="carried-board-id", name="Somebody Else's Board",
        created_at="2026-01-01T00:00:00Z",
    )
    materialize_carried_board(dialer.db, carried_genesis)
    for i in range(MAX_EVENTS_PER_REQUEST + 10):
        materialize_carried_post(
            dialer.db,
            build_board_post(
                signing_identity=elsewhere_identity.signing_key,
                home_node_fingerprint=elsewhere_identity.fingerprint,
                local_user_id="wanderer", board_id="carried-board-id",
                subject=f"post {i}", body="body", created_at="2026-01-01T00:00:00Z",
                nonce=f"nonce-{i}",
            ),
            sender_fingerprint=elsewhere_identity.fingerprint,
        )

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(dialer.db, "downloads", creator=creator)
    entry = upload_file(dialer.db, area, creator, "game.bin", b"contents")
    link_file_area(dialer.db, area, node_identity=dialer_identity)
    own_descriptor = queue_file_descriptor_if_linked(
        dialer.db, entry, area, node_identity=dialer_identity
    )

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task, settle=2.0)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert own_descriptor.content_id in seed_node.known_event_ids
        carried_area = get_file_area_by_name(seed.db, "downloads")
        assert [f.filename for f in list_remote_files(seed.db, carried_area)] == ["game.bin"]
    finally:
        dialer.close()
        seed.close()


def test_sync_push_keeps_room_for_resource_events_behind_a_long_rotation_history(
    tmp_path, monkeypatch,
):
    """A node's `key_transition` history is append-only and rides along
    with every push. Spending the whole request budget on it would leave
    resource events permanently unsent (Codex review of issue #478);
    they keep at least half a request whatever the history looks like.
    `MAX_EVENTS_PER_REQUEST` is lowered here so the condition is reached
    with a handful of rotations instead of a hundred."""
    import netbbs.link.sync as sync_module

    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    genesis = link_board(dialer.db, board, node_identity=dialer_identity)

    monkeypatch.setattr(sync_module, "MAX_EVENTS_PER_REQUEST", 10)
    while len(dialer_identity.transitions) <= 12:
        dialer_identity = rotate_operational_key(dialer_identity, purpose="signing")
    dialer_node = LinkNode(identity=dialer_identity)

    calls = _recording_push(monkeypatch)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    )
                )
                await _run_sync_briefly(task, settle=1.0)
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        pushed = {content_id for call in calls for content_id in call}
        assert genesis.content_id in pushed
    finally:
        dialer.close()
        seed.close()


def test_sync_push_is_not_pinned_by_a_resource_the_seed_refused_to_carry(tmp_path, monkeypatch):
    """Codex review of #498, the end-to-end shape. The seed carries no
    boards at all (`max_carried_boards=0`), so it accepts the dialer's
    `board_genesis` and posts into protocol state and refuses only the
    local materialization -- leaving no `boards` row for
    `_all_board_events` to read.

    Boards are walked before file areas, so while those accepted-but-
    unmaterialized events stayed "wanted" they filled the push page on
    every pass and the dialer's own file descriptor was never sent.
    `MAX_EVENTS_PER_REQUEST` is lowered so one board's worth of posts
    exceeds a page without needing hundreds of them."""
    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import upload_file
    from netbbs.link.files import link_file_area, queue_file_descriptor_if_linked
    import netbbs.link.sync as sync_module

    dialer_identity = bootstrap_node_identity("dialer")
    seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
    dialer_node = LinkNode(identity=dialer_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")

    creator = create_user(dialer.db, "alice", password="hunter2", user_level=10)
    board = create_board(dialer.db, "general", creator=creator)
    link_board(dialer.db, board, node_identity=dialer_identity)
    for i in range(12):
        queue_board_post_if_linked(
            dialer.db, create_post(dialer.db, board, creator, f"post {i}", "body"), board,
            node_identity=dialer_identity,
        )

    area = create_file_area(dialer.db, "downloads", creator=creator)
    entry = upload_file(dialer.db, area, creator, "game.bin", b"contents")
    link_file_area(dialer.db, area, node_identity=dialer_identity)
    own_descriptor = queue_file_descriptor_if_linked(
        dialer.db, entry, area, node_identity=dialer_identity
    )

    monkeypatch.setattr(sync_module, "MAX_EVENTS_PER_REQUEST", 5)

    async def scenario():
        seed_server = await _run_server(seed_node, seed.lane, max_carried_boards=0)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(
                    run_link_sync(
                        dialer_node, session, [f"http://127.0.0.1:{seed_server.port}"],
                        lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=0.05,
                    )
                )
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 20.0
                while (
                    own_descriptor.content_id not in seed_node.known_event_ids
                    and loop.time() < deadline
                ):
                    await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            await seed_server.stop()

    try:
        asyncio.run(scenario())
        assert own_descriptor.content_id in seed_node.known_event_ids
    finally:
        dialer.close()
        seed.close()


# -- remote identity attestations over real sync passes (issue #584) --------


async def _one_pass(node, session, seeds, hello_provider, lane, **sync_options):
    """Run exactly one full pass of `run_link_sync`, then return.

    The stop event is set from inside the hello provider, which the loop calls
    once per pass before it does any work -- so the pass it is already in
    completes normally and the loop exits at the top of the next one, with no
    sleep and nothing cancelled mid-flight.
    """
    stop_event = asyncio.Event()

    def provider():
        stop_event.set()
        return hello_provider()

    await run_link_sync(
        node, session, seeds, provider, lane,
        interval_seconds=0.0, stop_event=stop_event, **sync_options,
    )


class _AttestationPair:
    """An issuer with one Link-visible attestation and a subscriber to it."""

    def __init__(self, tmp_path, label: str, *, named: bool = True) -> None:
        self.issuer_identity = bootstrap_node_identity(f"{label}-issuer")
        self.subscriber_identity = bootstrap_node_identity(f"{label}-subscriber")
        self.issuer_node = LinkNode(identity=self.issuer_identity)
        self.subscriber_node = LinkNode(identity=self.subscriber_identity)
        self.issuer = _NodeDb(tmp_path, f"{label}-issuer")
        self.subscriber = _NodeDb(tmp_path, f"{label}-subscriber")
        self.port = 0

        sysop = create_user(
            self.issuer.db, "sysop", password="password", user_level=SYSOP_LEVEL
        )
        self.alice = create_user(self.issuer.db, "alice", password="password")
        attest_age(self.issuer.db, self.alice, date(1990, 4, 1), verifier=sysop)
        set_attestation_link_visible(self.issuer.db, self.alice, "age", True)

        self.subject = TrustSubject.user(self.issuer_identity.fingerprint, "alice")
        register_subject(
            self.subscriber.db, self.subject,
            first_accepted_at="2026-09-14T12:00:00+00:00",
            now_iso="2026-09-15T12:00:00+00:00",
        )
        configure_attestation_authority(
            self.subscriber.db, self.issuer_identity.fingerprint, attributes=["age"],
            reason="peer operator", now_iso="2026-09-15T12:00:00+00:00",
        )
        # Issue #596: the other half of the relationship. The subscriber
        # accepting this issuer is its own SysOp's decision; the issuer telling
        # the subscriber anything is the issuer's.
        if named:
            self.name_the_subscriber()

    def name_the_subscriber(self) -> None:
        configure_attestation_recipient(
            self.issuer.db, self.subscriber_identity.fingerprint, reason="peer operator asked",
        )

    def issuer_hello(self):
        # The issuer has to be dialable for a subscriber to pull from it, so it
        # advertises a real address rather than the outgoing-only hello the
        # rest of this module's nodes use.
        return self.issuer_node.build_hello(
            addresses=[{"protocol": "http", "address": "127.0.0.1", "port": self.port}],
            outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
        )

    async def start(self):
        server = LinkServer(
            host="127.0.0.1", port=0, node=self.issuer_node, lane=self.issuer.lane,
            own_hello_provider=self.issuer_hello,
        )
        await server.start()
        self.port = server.port
        self.seeds = [f"http://127.0.0.1:{server.port}"]
        return server

    async def issuer_pass(self, session):
        await _one_pass(self.issuer_node, session, [], self.issuer_hello, self.issuer.lane)

    async def subscriber_pass(self, session):
        await _one_pass(
            self.subscriber_node, session, self.seeds,
            lambda: _hello_for(self.subscriber_node), self.subscriber.lane,
        )

    def close(self):
        self.issuer.close()
        self.subscriber.close()


def test_one_sync_pass_signs_serves_pulls_and_accepts_an_attestation(tmp_path):
    """The loop-level half of design doc §5.5's issuing path.

    `tests/test_link_attestation_issuance.py` proves the domain functions and
    `tests/test_link_transport.py` proves the endpoint; both would stay green
    if `run_link_sync` never called either, which is exactly the failure issue
    #584 catalogued. This drives real passes of the loop and asserts on the
    *subscriber's* tables, so the wiring itself is what is under test.
    """
    pair = _AttestationPair(tmp_path, "attesting")

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                # One pass on the issuer: this is what signs the object.
                await pair.issuer_pass(session)
                # One pass on the subscriber, with the issuer as its seed: the
                # dial completes the hello, and the attestation pull later in
                # that same pass brings the object across.
                await pair.subscriber_pass(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert pair.issuer.db.connection.execute(
            """SELECT COUNT(*) FROM link_issued_remote_attestations
               WHERE object_type = 'remote_identity_attestation'"""
        ).fetchone()[0] == 1
        assert pair.subscriber.db.connection.execute(
            "SELECT attested_value FROM link_remote_attestations WHERE subject_id = ?",
            (pair.subject.subject_id,),
        ).fetchone()[0] == "1990-04-01"
        assert remote_meets_age(pair.subscriber.db, pair.subject, 18)
        # Restart-safe: the cursor is on disk, so the next pass resumes rather
        # than re-reading the whole stream.
        assert pair.subscriber.db.connection.execute(
            """SELECT after_content_id FROM link_attestation_pull_cursors
               WHERE issuer_fingerprint = ?""",
            (pair.issuer_identity.fingerprint,),
        ).fetchone() is not None
    finally:
        pair.close()


def test_a_withdrawn_opt_in_reaches_the_subscriber_over_the_loop(tmp_path):
    """The direction the Profile toggle's promise actually rests on: switching
    sharing off has to reach a node that already accepted the attestation."""
    pair = _AttestationPair(tmp_path, "withdrawing")

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                await pair.issuer_pass(session)
                await pair.subscriber_pass(session)
                assert remote_meets_age(pair.subscriber.db, pair.subject, 18)

                set_attestation_link_visible(pair.issuer.db, pair.alice, "age", False)
                await pair.issuer_pass(session)
                await pair.subscriber_pass(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert not remote_meets_age(pair.subscriber.db, pair.subject, 18)
        assert pair.subscriber.db.connection.execute(
            "SELECT COUNT(*) FROM link_remote_attestation_revocations"
        ).fetchone()[0] == 1
        # Issue #596: withdrawn means forgotten, on both nodes, not merely no
        # longer relied on. The rows stay; the birthdate does not.
        for database, table in (
            (pair.subscriber.db, "link_remote_attestations"),
            (pair.issuer.db, "link_issued_remote_attestations"),
        ):
            rows = [tuple(row) for row in database.connection.execute(f"SELECT * FROM {table}")]
            assert rows and "1990-04-01" not in repr(rows), table
    finally:
        pair.close()


def test_a_subscriber_the_issuer_has_not_named_gets_nothing_and_is_told_why(tmp_path, caplog):
    """Issue #596 over the loop. Configuring an authority is one SysOp's
    decision and being told anything is the other's, so a subscriber the
    issuer never named pulls nothing -- and its cursor must not move, or
    being named later would deliver nothing either."""
    pair = _AttestationPair(tmp_path, "unnamed", named=False)

    def held():
        return pair.subscriber.db.connection.execute(
            "SELECT COUNT(*) FROM link_remote_attestations"
        ).fetchone()[0]

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                await pair.issuer_pass(session)
                with caplog.at_level(logging.WARNING, logger="netbbs.link.sync"):
                    await pair.subscriber_pass(session)
                assert held() == 0
                assert pair.subscriber.db.connection.execute(
                    "SELECT COUNT(*) FROM link_attestation_pull_cursors"
                ).fetchone()[0] == 0

                pair.name_the_subscriber()
                await pair.subscriber_pass(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        refusals = [r.getMessage() for r in caplog.records if "attestation recipient" in r.getMessage()]
        assert len(refusals) == 1
        assert pair.issuer_identity.fingerprint in refusals[0]
        assert held() == 1
        assert remote_meets_age(pair.subscriber.db, pair.subject, 18)
    finally:
        pair.close()


def test_a_node_with_no_opt_in_signs_and_serves_nothing(tmp_path):
    """The reconcile runs every pass whether or not anything changed, so the
    no-consent case has to stay a no-op rather than an empty object."""
    pair = _AttestationPair(tmp_path, "unshared")
    set_attestation_link_visible(pair.issuer.db, pair.alice, "age", False)

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                await pair.issuer_pass(session)
                await pair.issuer_pass(session)
                await pair.subscriber_pass(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert pair.issuer.db.connection.execute(
            "SELECT COUNT(*) FROM link_issued_remote_attestations"
        ).fetchone()[0] == 0
        assert pair.subscriber.db.connection.execute(
            "SELECT COUNT(*) FROM link_remote_attestations"
        ).fetchone()[0] == 0
        assert not remote_meets_age(pair.subscriber.db, pair.subject, 18)
    finally:
        pair.close()


# -- trust vouches over real sync passes (issue #589, slice 1) ----------------


class _VouchPair:
    """An issuer whose SysOp vouches for identities, and a subscriber that has
    named it a trusted reporter."""

    NODE_SUBJECT = TrustSubject.node("a-third-node-fingerprint")
    USER_SUBJECT = TrustSubject.user("a-third-node-fingerprint", "carol")

    def __init__(self, tmp_path, label: str, *, users: bool = True) -> None:
        self.issuer_identity = bootstrap_node_identity(f"{label}-issuer")
        self.subscriber_identity = bootstrap_node_identity(f"{label}-subscriber")
        self.issuer_node = LinkNode(identity=self.issuer_identity)
        self.subscriber_node = LinkNode(identity=self.subscriber_identity)
        self.issuer = _NodeDb(tmp_path, f"{label}-issuer")
        self.subscriber = _NodeDb(tmp_path, f"{label}-subscriber")
        self.port = 0
        for subject in (self.NODE_SUBJECT, self.USER_SUBJECT):
            register_subject(
                self.issuer.db, subject,
                first_accepted_at="2026-08-01T12:00:00+00:00", now_iso="2026-09-15T12:00:00+00:00",
            )
        configure_trust_domain(self.subscriber.db, "friends", display_name="Friends")
        configure_trusted_reporter(
            self.subscriber.db, self.issuer_identity.fingerprint, domain_id="friends",
            scopes=[], can_vouch_nodes=True, can_vouch_users=users,
        )

    def issuer_hello(self):
        return self.issuer_node.build_hello(
            addresses=[{"protocol": "http", "address": "127.0.0.1", "port": self.port}],
            outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
        )

    async def start(self):
        server = LinkServer(
            host="127.0.0.1", port=0, node=self.issuer_node, lane=self.issuer.lane,
            own_hello_provider=self.issuer_hello,
        )
        await server.start()
        self.port = server.port
        self.seeds = [f"http://127.0.0.1:{server.port}"]
        return server

    async def issuer_pass(self, session):
        await _one_pass(self.issuer_node, session, [], self.issuer_hello, self.issuer.lane)

    async def subscriber_pass(self, session, **sync_options):
        await _one_pass(
            self.subscriber_node, session, self.seeds,
            lambda: _hello_for(self.subscriber_node), self.subscriber.lane, **sync_options,
        )

    def held(self, subject):
        return self.subscriber.db.connection.execute(
            "SELECT revoked_at FROM link_trust_vouches WHERE subject_id = ? ORDER BY received_at",
            (subject.subject_id,),
        ).fetchall()

    def cursor(self):
        row = self.subscriber.db.connection.execute(
            "SELECT after_content_id FROM link_trust_pull_cursors WHERE issuer_fingerprint = ?",
            (self.issuer_identity.fingerprint,),
        ).fetchone()
        return row[0] if row else None

    def last_served(self):
        return self.issuer.db.connection.execute(
            "SELECT content_id FROM link_trust_wire_objects ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]

    def close(self):
        self.issuer.close()
        self.subscriber.close()


def test_one_sync_pass_signs_serves_pulls_and_records_a_vouch(tmp_path):
    """Issue #589 said no dogfood run, however long, could exercise trust
    propagation, because no node could issue a trust object. This drives real
    passes of the loop and asserts on the *subscriber's* tables, so what is
    under test is that `run_link_sync` calls the reconcile at all."""
    pair = _VouchPair(tmp_path, "vouching")
    record_vouch_intent(pair.issuer.db, pair.NODE_SUBJECT, explanation="known operator")

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                await pair.issuer_pass(session)
                await pair.subscriber_pass(session)
                assert [row[0] for row in pair.held(pair.NODE_SUBJECT)] == [None]

                withdraw_vouch_intent(pair.issuer.db, pair.NODE_SUBJECT)
                await pair.issuer_pass(session)
                await pair.subscriber_pass(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        [row] = pair.held(pair.NODE_SUBJECT)
        assert row[0] is not None
        assert pair.cursor() == pair.last_served()
    finally:
        pair.close()


def test_a_vouch_outside_a_subscribers_grant_does_not_wedge_its_subscription(tmp_path):
    """The first real issuer reaches this immediately: its SysOp vouches for a
    caller, and one subscriber only ever granted it node vouches. Aborting the
    batch left the cursor where it was, so every later pass met the same
    object first and nothing after it ever arrived."""
    pair = _VouchPair(tmp_path, "narrow", users=False)
    record_vouch_intent(pair.issuer.db, pair.USER_SUBJECT, explanation="long-standing caller")

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                await pair.issuer_pass(session)
                await pair.subscriber_pass(session)
                # The user vouch was skipped, and the subscription moved on.
                assert pair.held(pair.USER_SUBJECT) == []
                assert pair.cursor() == pair.last_served()

                record_vouch_intent(pair.issuer.db, pair.NODE_SUBJECT, explanation="known operator")
                await pair.issuer_pass(session)
                await pair.subscriber_pass(session)
                assert [row[0] for row in pair.held(pair.NODE_SUBJECT)] == [None]

                # Widening the grant resets the cursor, so the next pass
                # re-reads the stream and reaches what it skipped.
                configure_trusted_reporter(
                    pair.subscriber.db, pair.issuer_identity.fingerprint, domain_id="friends",
                    scopes=[], can_vouch_nodes=True, can_vouch_users=True,
                )
                await pair.subscriber_pass(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert [row[0] for row in pair.held(pair.USER_SUBJECT)] == [None]
        assert len(pair.held(pair.NODE_SUBJECT)) == 1
    finally:
        pair.close()


def test_an_object_the_previous_key_signed_does_not_wedge_a_new_subscriber(tmp_path):
    """A subscriber resolves only the issuer's current operational key. After a
    rotation the issuer's stream still holds what the old key signed, and a
    page parsed all-or-nothing turned that one object into a subscription
    that could never start. The subscriber knows the old key from the issuer's
    own transition chain, which is what lets it skip the object for good."""
    pair = _VouchPair(tmp_path, "rotated")
    record_vouch_intent(pair.issuer.db, pair.NODE_SUBJECT, explanation="known operator")
    reconcile_issued_vouches(
        pair.issuer.db, pair.issuer_identity.signing_key,
        home_node_fingerprint=pair.issuer_identity.fingerprint,
    )
    pair.issuer_identity = rotate_operational_key(pair.issuer_identity, purpose="signing")
    pair.issuer_node.identity = pair.issuer_identity

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                await pair.issuer_pass(session)  # re-signs under the current key
                await pair.subscriber_pass(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert pair.issuer.db.connection.execute(
            "SELECT COUNT(*) FROM link_trust_wire_objects WHERE object_type = 'trust_vouch'"
        ).fetchone()[0] == 2
        assert [row[0] for row in pair.held(pair.NODE_SUBJECT)] == [None]
        assert pair.cursor() == pair.last_served()
    finally:
        pair.close()


def test_a_probationary_reporter_is_neither_pulled_nor_counted_under_the_production_policy(tmp_path):
    """`netbbs.__main__` runs the loop with `enforce_trust_policy=True`, where a
    reporter this node has not established is not pulled at all, and a vouch
    from one would not count. Naming a reporter is therefore not enough: the
    subscriber's SysOp has to establish it, and everything above this test
    runs with the flag off."""
    from netbbs.link.trust import TrustDimension, TrustState, set_trust_override

    pair = _VouchPair(tmp_path, "enforced")
    record_vouch_intent(pair.issuer.db, pair.NODE_SUBJECT, explanation="known operator")
    reporter = TrustSubject.node(pair.issuer_identity.fingerprint)

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                await pair.issuer_pass(session)
                await pair.subscriber_pass(session, enforce_trust_policy=True)
                assert pair.held(pair.NODE_SUBJECT) == []

                for dimension in (TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR):
                    set_trust_override(
                        pair.subscriber.db, reporter, dimension, TrustState.ESTABLISHED,
                        reason="operator known in person", actor_user_id=None,
                    )
                await pair.subscriber_pass(session, enforce_trust_policy=True)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert [row[0] for row in pair.held(pair.NODE_SUBJECT)] == [None]
        explanation = json.loads(pair.subscriber.db.connection.execute(
            """SELECT explanation_json FROM link_trust_effective_states
               WHERE subject_id = ? AND dimension = 'identity_integrity'""",
            (pair.NODE_SUBJECT.subject_id,),
        ).fetchone()[0])
        assert explanation["vouch_domains"] == ["friends"]
    finally:
        pair.close()


def _served_vouch(identity, vouch_id):
    from netbbs.link.trust_wire import build_trust_vouch

    return build_trust_vouch(
        signing_identity=identity.signing_key, issuer_fingerprint=identity.fingerprint,
        vouch_id=vouch_id, subject=TrustSubject.node("some-subject"),
        issued_at="2026-09-18T12:00:00.000000Z", expires_at="2026-12-17T12:00:00.000000Z",
    ).to_dict()


def test_an_object_signed_by_a_key_this_node_has_not_learned_stops_the_page_instead_of_being_skipped():
    """The mirror image of a rotated issuer, and the dangerous one: *this* node
    is the stale party. The issuer rotated and re-signed everything, and this
    node has not completed a hello with it since. Skipping what does not
    verify would move the cursor past every re-issued vouch and every
    revocation, and none of them would ever be offered again."""
    from netbbs.link.events import event_content_id
    from netbbs.link.sync import _parse_trust_page

    known = bootstrap_node_identity("issuer-as-this-node-knows-it")
    rotated = rotate_operational_key(known, purpose="signing")
    before = _served_vouch(known, "signed-by-the-key-this-node-knows")
    after = _served_vouch(rotated, "signed-by-a-key-it-has-not-learned")
    known_key = LinkNode(identity=known).identity.signing_key.verify_key

    parsed, cursor, stalled = _parse_trust_page([before, after, before], known_key, [], known.fingerprint)

    assert stalled
    assert [obj.payload["vouch_id"] for obj in parsed] == ["signed-by-the-key-this-node-knows"]
    assert cursor == event_content_id(before["envelope"])


def test_an_object_signed_by_a_superseded_key_is_skipped_for_good():
    from netbbs.link.events import event_content_id
    from netbbs.link.sync import _parse_trust_page

    old = bootstrap_node_identity("issuer-before-rotation")
    new = rotate_operational_key(old, purpose="signing")
    stale = _served_vouch(old, "signed-before-the-rotation")
    fresh = _served_vouch(new, "signed-after-it")
    node = LinkNode(identity=bootstrap_node_identity("subscriber"))
    node.peers[new.fingerprint] = PeerRecord(
        fingerprint=new.fingerprint, root_public_key=bytes(new.root.verify_key),
        transitions=new.transitions, descriptor=_hello_for(LinkNode(identity=new)).descriptor,
    )

    superseded = node.resolve_peer_superseded_signing_keys(new.fingerprint)
    assert len(superseded) == 1
    parsed, cursor, stalled = _parse_trust_page(
        [stale, fresh], node.resolve_peer_signing_key(new.fingerprint), superseded, new.fingerprint
    )

    assert not stalled
    assert [obj.payload["vouch_id"] for obj in parsed] == ["signed-after-it"]
    assert cursor == event_content_id(fresh["envelope"])


def test_a_page_entry_that_cannot_be_canonicalized_rejects_the_page_rather_than_escaping():
    """`event_content_id` raises a bare `Exception` subclass for a float, which
    the pull's own handler does not catch; one such entry would otherwise end
    the whole background sync task instead of one reporter's pull."""
    from netbbs.link.sync import _parse_trust_page
    from netbbs.link.trust_wire import TrustWireError

    identity = bootstrap_node_identity("issuer")
    poisoned = _served_vouch(identity, "poisoned")
    poisoned["envelope"]["payload"]["explanation"] = 1.5

    with pytest.raises(TrustWireError, match="malformed entry"):
        _parse_trust_page([poisoned], identity.signing_key.verify_key, [], identity.fingerprint)
    with pytest.raises(TrustWireError, match="malformed entry"):
        _parse_trust_page(["not-a-dict"], identity.signing_key.verify_key, [], identity.fingerprint)


def test_an_object_naming_another_issuer_rejects_the_page_before_its_signature_is_tried():
    from netbbs.link.sync import _parse_trust_page
    from netbbs.link.trust_wire import TrustWireError

    asked = bootstrap_node_identity("the-issuer-asked-for")
    other = bootstrap_node_identity("someone-else")

    with pytest.raises(TrustWireError, match="another issuer"):
        _parse_trust_page(
            [_served_vouch(other, "theirs")], asked.signing_key.verify_key, [], asked.fingerprint
        )


# -- a cursor the responder no longer knows (issue #621) -----------------------


def test_a_trust_subscription_recovers_when_the_reporter_no_longer_knows_its_cursor(tmp_path):
    """The reporter was restored from an older backup, or recreated: the object
    this subscriber's cursor names will never exist there again. The
    subscriber used to send the same cursor on every pass, for good."""
    from netbbs.link.trust_wire import save_trust_pull_cursor

    pair = _VouchPair(tmp_path, "restored")
    record_vouch_intent(pair.issuer.db, pair.NODE_SUBJECT, explanation="known operator")
    fingerprint = pair.issuer_identity.fingerprint
    save_trust_pull_cursor(pair.subscriber.db, fingerprint, fingerprint, "f" * 64)

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                await pair.issuer_pass(session)
                # A pass reaches a reporter that is also its seed twice, so the
                # second attempt may already succeed; what matters is that the
                # subscription is moving again, which it never used to.
                await pair.subscriber_pass(session)
                await pair.subscriber_pass(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert [row[0] for row in pair.held(pair.NODE_SUBJECT)] == [None]
        assert pair.cursor() == pair.last_served()
    finally:
        pair.close()


def test_an_attestation_subscription_recovers_when_the_authority_no_longer_knows_its_cursor(tmp_path):
    """The same defect on the other pull, where nothing at all could recover it
    short of editing the cursor table by hand."""
    from netbbs.link.remote_attestation import save_attestation_pull_cursor

    pair = _AttestationPair(tmp_path, "restored-authority")
    fingerprint = pair.issuer_identity.fingerprint
    save_attestation_pull_cursor(pair.subscriber.db, fingerprint, fingerprint, "f" * 64)

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                await pair.issuer_pass(session)
                await pair.subscriber_pass(session)
                assert not remote_meets_age(pair.subscriber.db, pair.subject, 18)
                await pair.subscriber_pass(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert remote_meets_age(pair.subscriber.db, pair.subject, 18)
    finally:
        pair.close()


def test_a_subscriber_holding_a_stale_key_keeps_what_it_can_verify_and_waits_for_the_rest(tmp_path):
    """The stall, through the real pull and a real server. The issuer rotated
    and re-signed; this subscriber has not completed a hello since. It must
    ingest what its key still verifies, leave its cursor there, and get the
    rest after its next hello -- not skip past it."""
    from netbbs.link import sync as sync_module

    pair = _VouchPair(tmp_path, "stale")
    record_vouch_intent(pair.issuer.db, pair.NODE_SUBJECT, explanation="known operator")

    async def scenario():
        server = await pair.start()
        try:
            async with aiohttp.ClientSession() as session:
                await pair.issuer_pass(session)
                await pair.subscriber_pass(session)  # hello: learns the first key
                first_cursor = pair.cursor()

                pair.issuer_identity = rotate_operational_key(pair.issuer_identity, purpose="signing")
                pair.issuer_node.identity = pair.issuer_identity
                record_vouch_intent(pair.issuer.db, pair.USER_SUBJECT, explanation="long-standing caller")
                await pair.issuer_pass(session)  # re-signs one vouch, signs another, all under the new key

                # A pull with no hello in between: exactly the stale subscriber.
                await sync_module._pull_one_trust_reporter(
                    pair.subscriber_node, session, pair.subscriber.lane,
                    pair.issuer_identity.fingerprint, pair.seeds,
                )
                assert pair.cursor() == first_cursor
                assert pair.held(pair.USER_SUBJECT) == []

                await pair.subscriber_pass(session)  # the next hello teaches it the new key
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert [row[0] for row in pair.held(pair.USER_SUBJECT)] == [None]
        assert pair.cursor() == pair.last_served()
    finally:
        pair.close()


def test_two_rotations_leave_two_superseded_keys_and_never_the_current_one():
    from netbbs.link.node_identity import resolve_current_operational_key, superseded_operational_keys

    identity = bootstrap_node_identity("twice-rotated")
    for _ in range(2):
        identity = rotate_operational_key(identity, purpose="signing")
    arguments = dict(
        root_verify_key=identity.root.verify_key, subject_fingerprint=identity.fingerprint, purpose="signing",
    )

    superseded = superseded_operational_keys(identity.transitions, **arguments)

    assert len(superseded) == 2 and len(set(superseded)) == 2
    assert resolve_current_operational_key(identity.transitions, **arguments) not in superseded


def test_a_historical_chain_entry_that_is_not_a_key_is_ignored_rather_than_fatal(monkeypatch):
    """Nothing before the trust pull ever decoded a *historical* key, so a
    root-signed chain may carry an old entry that is not one. Raised from
    where the pull resolves it, that ended the whole background sync task."""
    import base64

    from netbbs.link import protocol as protocol_module

    identity = rotate_operational_key(bootstrap_node_identity("odd-history"), purpose="signing")
    node = LinkNode(identity=bootstrap_node_identity("subscriber"))
    node.peers[identity.fingerprint] = PeerRecord(
        fingerprint=identity.fingerprint, root_public_key=bytes(identity.root.verify_key),
        transitions=identity.transitions, descriptor=_hello_for(LinkNode(identity=identity)).descriptor,
    )
    real = protocol_module.superseded_operational_keys
    monkeypatch.setattr(
        protocol_module, "superseded_operational_keys",
        lambda *args, **kwargs: ["not base64 at all", base64.b64encode(b"short").decode()] + real(*args, **kwargs),
    )

    assert len(node.resolve_peer_superseded_signing_keys(identity.fingerprint)) == 1


def test_a_reporter_or_authority_without_a_usable_key_costs_its_own_pull_not_the_sync_task(tmp_path):
    """Both pulls resolve the issuer's key before their per-address handler. A
    chain that ends in a bare revoke, or no longer verifies, raised from there
    straight out of `run_link_sync`, and outbound Link did not resume until the
    node was restarted."""
    from netbbs.link import sync as sync_module
    from netbbs.link.protocol import LinkProtocolError

    pair = _VouchPair(tmp_path, "keyless")

    def _no_key(fingerprint, kind="signed object"):
        raise LinkProtocolError(f"rejected {kind} from {fingerprint}: no currently-authorized signing key")

    pair.subscriber_node.resolve_peer_signing_key = _no_key

    async def scenario():
        async with aiohttp.ClientSession() as session:
            for pull in (sync_module._pull_one_trust_reporter, sync_module._pull_one_attestation_authority):
                await pull(
                    pair.subscriber_node, session, pair.subscriber.lane,
                    pair.issuer_identity.fingerprint, ["http://127.0.0.1:9"],
                )

    try:
        asyncio.run(scenario())  # returns; does not raise
    finally:
        pair.close()


def test_the_sync_pass_survives_a_revocation_re_signed_after_a_rotation(tmp_path, caplog):
    """That change carries no subject, and the pass logs every change: an
    attribute error there would end the background sync task unnoticed."""
    pair = _VouchPair(tmp_path, "resigned")
    record_vouch_intent(pair.issuer.db, pair.NODE_SUBJECT, explanation="known operator")

    async def scenario():
        async with aiohttp.ClientSession() as session:
            await pair.issuer_pass(session)
            withdraw_vouch_intent(pair.issuer.db, pair.NODE_SUBJECT)
            await pair.issuer_pass(session)
            pair.issuer_identity = rotate_operational_key(pair.issuer_identity, purpose="signing")
            pair.issuer_node.identity = pair.issuer_identity
            with caplog.at_level(logging.INFO, logger="netbbs.link.sync"):
                await pair.issuer_pass(session)

    try:
        asyncio.run(scenario())
        assert any("re-signed a vouch revocation" in record.getMessage() for record in caplog.records)
        assert pair.issuer.db.connection.execute(
            "SELECT COUNT(*) FROM link_trust_wire_objects WHERE object_type = 'trust_vouch_revocation'"
        ).fetchone()[0] == 2
    finally:
        pair.close()


# -- two nodes that have never met, sharing a board through one seed (issue #630) --


class _ThreeNodes:
    """R is full and originates a linked board. A and B are outgoing-only, seed
    off R, and never exchange a hello with each other: the shape of the
    project's own three live nodes, and of most real networks."""

    def __init__(self, tmp_path, *, enforce: bool) -> None:
        from netbbs.boards.boards import create_board
        from netbbs.link.store import load_link_node

        self.enforce = enforce
        self.ids = {name: bootstrap_node_identity(f"three-{name}") for name in ("R", "A", "B")}
        self.dbs = {name: _NodeDb(tmp_path, f"three-{name}") for name in self.ids}
        self.sysops = {
            name: create_user(self.dbs[name].db, "sysop", password="password1", user_level=SYSOP_LEVEL)
            for name in self.ids
        }
        board = create_board(self.dbs["R"].db, "general", creator=self.sysops["R"])
        link_board(self.dbs["R"].db, board, node_identity=self.ids["R"])
        # As a started node would: R knows its own genesis from its database.
        self.nodes = {"R": load_link_node(self.dbs["R"].db, self.ids["R"])}
        self.nodes.update({name: LinkNode(identity=self.ids[name]) for name in ("A", "B")})
        self.port = 0
        if enforce:
            # Under policy nothing moves between a dialer and its seed until
            # each has established the other, which is a SysOp's act.
            for dialer in ("A", "B"):
                self.establish(dialer, "R")
                self.establish("R", dialer)

    def establish(self, on: str, who: str) -> None:
        from netbbs.link.trust import TrustDimension, TrustState, set_trust_override

        subject = TrustSubject.node(self.ids[who].fingerprint)
        register_subject(self.dbs[on].db, subject, first_accepted_at="2026-08-01T00:00:00+00:00")
        for dimension in (TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR):
            set_trust_override(
                self.dbs[on].db, subject, dimension, TrustState.ESTABLISHED,
                reason="known operator", actor_user_id=None,
            )

    def r_hello(self):
        return self.nodes["R"].build_hello(
            addresses=[{"protocol": "http", "address": "127.0.0.1", "port": self.port}],
            outgoing_only=False, created_at="2026-01-01T00:00:00+00:00",
        )

    async def start(self):
        server = LinkServer(
            host="127.0.0.1", port=0, node=self.nodes["R"], lane=self.dbs["R"].lane,
            own_hello_provider=self.r_hello, enforce_trust_policy=self.enforce,
        )
        await server.start()
        self.port = server.port
        self.seeds = [f"http://127.0.0.1:{server.port}"]
        return server

    async def dial(self, name, session):
        await _one_pass(
            self.nodes[name], session, self.seeds, lambda: _hello_for(self.nodes[name]),
            self.dbs[name].lane, enforce_trust_policy=self.enforce,
        )

    def post(self, name, subject, board_name="general"):
        from netbbs.boards.boards import get_board_by_name

        board = get_board_by_name(self.dbs[name].db, board_name)
        post = create_post(self.dbs[name].db, board, author=self.sysops[name], subject=subject, body="hi")
        assert queue_board_post_if_linked(self.dbs[name].db, post, board, node_identity=self.ids[name])

    def subjects_on(self, name):
        rows = self.dbs[name].db.connection.execute(
            """SELECT p.subject FROM posts AS p JOIN boards AS b ON b.id = p.board_id
               WHERE b.name = 'general' ORDER BY p.id"""
        ).fetchall()
        return [row[0] for row in rows]

    def close(self):
        for node_db in self.dbs.values():
            node_db.close()


def test_a_compromise_reaches_a_node_that_knows_the_signer_only_by_introduction(tmp_path, caplog):
    """Issue #914, the Phase 4 exercise's row 9 on real sockets. B learned A
    from R before A rotated its signing key as compromised. B's bundle still
    held the compromised key as current, so a copy R had kept from before the
    rotation verified on B, was accepted, and nothing was logged. R now serves
    A's key history beside A's content, so B skips the old-key copy on the
    same pull -- and takes what A signed after the rotation without needing
    a fresh bundle first."""
    from netbbs.link.node_identity import rotate_operational_key

    net = _ThreeNodes(tmp_path, enforce=False)

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                for name in ("A", "B"):
                    await net.dial(name, session)
                net.post("A", "before")
                await net.dial("A", session)
                await net.dial("B", session)  # B is introduced to A here
                net.post("A", "old key, after B looked")
                await net.dial("A", session)  # R keeps this copy
                rotated = rotate_operational_key(net.ids["A"], purpose="signing", compromised=True)
                net.ids["A"] = rotated
                net.nodes["A"].identity = rotated
                net.post("A", "new key")
                await net.dial("A", session)  # R learns the compromise from A
                with caplog.at_level(logging.INFO, logger="netbbs.link.sync"):
                    await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert net.subjects_on("B") == ["before", "new key"]
        messages = [record.getMessage() for record in caplog.records]
        assert any("carried newer key history" in m and net.ids["A"].fingerprint in m for m in messages)
        # Since issue #672 R, which heard A's revoke, holds its old-key copy
        # as stale and does not hand it on, so B is never offered it to skip.
        # Both posts A signed before the rotation are stale there; A has not
        # re-signed them in this test.
        stale = net.dbs["R"].db.connection.execute(
            "SELECT COUNT(*) FROM link_events WHERE stale_signer = ?", (net.ids["A"].fingerprint,)
        ).fetchone()[0]
        assert stale == 2
        # Persisted, so a restart does not bring the compromised key back.
        stored = net.dbs["B"].db.connection.execute(
            "SELECT transitions_json FROM link_introduced_identities WHERE fingerprint = ?",
            (net.ids["A"].fingerprint,),
        ).fetchone()[0]
        assert '"compromised": true' in stored
    finally:
        net.close()


def test_after_a_compromise_carriers_replace_their_stale_copies_with_the_re_signed_ones(tmp_path, caplog):
    """Issue #672. R carries A's post and B pulled it from R before A rotated
    its signing key as compromised and re-signed its content. Inventory diffs
    by content ID, and the re-signed copy has the same one, so both kept
    serving the old-signed copy and nothing ever asked for the fresh one.
    Now each, on learning the compromise -- R from A's own revoke, B from the
    chain R carries (#914) -- holds its copy as stale: not declared, not
    served, and so asked for again, and replaced in place by the re-signed
    copy. The post itself stays visible throughout and is never doubled."""
    import base64

    import nacl.signing

    from netbbs.identity.keys import verify_signature
    from netbbs.link.events import canonical_bytes
    from netbbs.link.key_rotation import resign_own_content
    from netbbs.link.node_identity import resolve_current_operational_key, rotate_operational_key
    from netbbs.link.store import board_event_diff

    net = _ThreeNodes(tmp_path, enforce=False)
    a = net.ids["A"].fingerprint
    board_id = net.dbs["R"].db.connection.execute(
        "SELECT board_id FROM boards WHERE name = 'general'"
    ).fetchone()[0]

    def stored(name, content_id):
        return net.dbs[name].db.connection.execute(
            "SELECT envelope_json, stale_signer FROM link_events WHERE content_id = ?", (content_id,)
        ).fetchone()

    def signed_by_current_key(raw):
        identity = net.ids["A"]
        key = resolve_current_operational_key(
            identity.transitions, root_verify_key=identity.root.verify_key,
            subject_fingerprint=a, purpose="signing",
        )
        return verify_signature(
            nacl.signing.VerifyKey(base64.b64decode(key)), canonical_bytes(raw["envelope"]),
            base64.b64decode(raw["signature"]),
        )

    observed = {}

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                for name in ("A", "B"):
                    await net.dial(name, session)
                net.post("A", "before")
                await net.dial("A", session)
                await net.dial("B", session)  # B now holds R's copy too
                [post_id] = [row[0] for row in net.dbs["R"].db.connection.execute(
                    "SELECT content_id FROM link_events WHERE object_type = 'board_post'"
                )]
                observed["post"] = post_id

                rotated = rotate_operational_key(net.ids["A"], purpose="signing", compromised=True)
                net.ids["A"] = rotated
                net.nodes["A"].identity = rotated
                assert resign_own_content(net.dbs["A"].db, rotated) >= 1

                await net.dial("A", session)  # R hears A's revoke: its copy is stale now
                observed["r_stale"] = stored("R", post_id)[1]
                observed["r_serves"] = [
                    event for event in board_event_diff(net.dbs["R"].db, {board_id: []}, limit=50)[0]
                    if event["envelope"]["object_type"] == "board_post"
                ]
                with caplog.at_level(logging.INFO, logger="netbbs.link"):
                    await net.dial("A", session)  # R wants it again; A pushes the re-signed copy
                    await net.dial("B", session)  # R carries A's chain: B's copy is stale
                    observed["b_stale"] = stored("B", post_id)[1]
                    await net.dial("B", session)  # B asks again and R serves the fresh copy
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        post_id = observed["post"]
        assert observed["r_stale"] == a
        assert observed["r_serves"] == [], "a stale copy is not handed on"
        assert observed["b_stale"] == a
        for name in ("R", "B"):
            envelope_json, stale_signer = stored(name, post_id)
            assert stale_signer is None, name
            assert signed_by_current_key(json.loads(envelope_json)), name
            assert post_id in net.nodes[name].known_event_ids
            assert post_id not in net.nodes[name].stale_copies
            assert net.subjects_on(name) == ["before"], name
        messages = [record.getMessage() for record in caplog.records]
        assert any("replaced 1 stale copies" in m for m in messages)
    finally:
        net.close()


def test_a_stale_copy_survives_a_restart_as_stale(tmp_path):
    """Issue #672: what was found stale stays out of inventory after a
    restart, until a fresh copy replaces it."""
    from netbbs.link.store import load_link_node, record_stale_copy_changes

    node_db = _NodeDb(tmp_path, "restart")
    identity = bootstrap_node_identity("restart")
    try:
        node_db.db.connection.execute(
            "INSERT INTO link_events (content_id, sender_fingerprint, object_type, envelope_json, received_at) "
            "VALUES ('cid', 'peer', 'board_post', ?, '2026-01-01T00:00:00Z')",
            (json.dumps({"envelope": {"object_type": "board_post", "payload": {}}, "signature": ""}),),
        )
        node_db.db.connection.commit()
        record_stale_copy_changes(node_db.db, marked={"cid": "signer"}, refreshed={})
        node = load_link_node(node_db.db, identity)
        assert "cid" not in node.known_event_ids
        assert node.stale_copies == {"cid": "signer"}
    finally:
        node_db.close()


def test_two_nodes_that_never_met_see_each_others_posts_through_their_common_seed(tmp_path):
    """The defect, with policy out of the way. B had never completed a hello
    with A, refused A's post as coming from a stranger, and with it the whole
    inventory response, on every pass: it never received anything from R
    again, R's own posts included."""
    net = _ThreeNodes(tmp_path, enforce=False)

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                for name in ("A", "B"):
                    await net.dial(name, session)
                net.post("A", "hello from A")
                await net.dial("A", session)
                await net.dial("B", session)
                net.post("R", "hello from R")
                await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert net.subjects_on("B") == ["hello from A", "hello from R"]
        a = net.ids["A"].fingerprint
        assert a in net.nodes["B"].introduced and a not in net.nodes["B"].peers
        assert net.dbs["B"].db.connection.execute(
            "SELECT introduced_by FROM link_introduced_identities WHERE fingerprint = ?", (a,)
        ).fetchone()[0] == net.ids["R"].fingerprint
    finally:
        net.close()


def test_under_production_policy_an_unmet_author_is_withheld_visible_and_establishable(tmp_path, caplog):
    """What a real node does, since it always enforces trust policy. An author
    from a node B has never met is on probation, so its post is withheld --
    but B keeps receiving everything else, the refusal is not downloaded and
    logged again on every pass, and the SysOp can see the node and establish
    it, after which the post arrives. Before, the node was not even listed,
    and establishing it by any other route wedged the subscription outright."""
    from netbbs.link.trust import TrustDimension, TrustState, get_effective_trust_state

    net = _ThreeNodes(tmp_path, enforce=True)
    a_subject = TrustSubject.node(net.ids["A"].fingerprint)

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                for name in ("A", "B"):
                    await net.dial(name, session)
                net.post("A", "hello from A")
                await net.dial("A", session)
                with caplog.at_level(logging.DEBUG, logger="netbbs.link.sync"):
                    for _ in range(3):
                        await net.dial("B", session)
                net.post("R", "hello from R")
                await net.dial("B", session)
                assert net.subjects_on("B") == ["hello from R"]
                assert get_effective_trust_state(
                    net.dbs["B"].db, a_subject, TrustDimension.IDENTITY_INTEGRITY
                ).state == TrustState.PROBATIONARY

                net.establish("B", "A")
                net.nodes["B"].deferred_events.release_identity(net.ids["A"].fingerprint)
                await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        refusals = [r for r in caplog.records if "withheld inventory event" in r.getMessage()]
        assert len(refusals) == 1
        # Issue #834: probation is routine. One plain INFO line, no warning.
        explained = [r for r in caplog.records if "on probation here" in r.getMessage()]
        assert [r.levelno for r in explained] == [logging.INFO]
        assert net.ids["A"].fingerprint in explained[0].getMessage()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert sorted(net.subjects_on("B")) == ["hello from A", "hello from R"]
        # Establishing the node does not establish its callers: a remote user
        # is a subject of its own, so the post waits in the approval queue.
        assert net.dbs["B"].db.connection.execute(
            "SELECT status FROM posts WHERE subject = 'hello from A'"
        ).fetchone()[0] == "pending"
    finally:
        net.close()


def test_a_carrier_that_cannot_introduce_an_author_costs_that_post_and_nothing_else(tmp_path, monkeypatch):
    """The introduction can fail: the carrier predates it, refuses, or does not
    hold the bundle. The post is then set aside, not the response."""
    from netbbs.link import sync as sync_module
    from netbbs.link.transport import LinkTransportError

    asked: list[int] = []

    async def _refuses(*args, **kwargs):
        asked.append(1)
        failure = LinkTransportError("identity request failed: HTTP 404")
        failure.status = 404
        raise failure

    monkeypatch.setattr(sync_module, "request_identities", _refuses)
    net = _ThreeNodes(tmp_path, enforce=False)

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                for name in ("A", "B"):
                    await net.dial(name, session)
                net.post("A", "hello from A")
                await net.dial("A", session)
                net.post("R", "hello from R")
                for _ in range(2):
                    await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert net.subjects_on("B") == ["hello from R"]
        assert len(net.nodes["B"].deferred_events.entries) == 1
        # A carrier without the route is asked once, not on every pass. One
        # that merely failed to answer is another matter; see the next test.
        assert len(asked) == 1
    finally:
        net.close()


def test_a_board_whose_origin_is_on_probation_is_not_downloaded_again_on_every_pass(tmp_path, caplog):
    """The board is not carried *because* its genesis was withheld, so nothing
    about it is in what this node declares as held. Its genesis and everything
    posted to it used to be downloaded, refused and logged on every pass, and
    two hundred such events were the last thing the node received."""
    from netbbs.boards.boards import create_board
    from netbbs.link.store import load_link_node

    net = _ThreeNodes(tmp_path, enforce=True)
    board = create_board(net.dbs["A"].db, "from-a", creator=net.sysops["A"])
    link_board(net.dbs["A"].db, board, node_identity=net.ids["A"])
    net.nodes["A"] = load_link_node(net.dbs["A"].db, net.ids["A"])

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.dial("A", session)
                net.post("A", "on A's own board", board_name="from-a")
                await net.dial("A", session)
                with caplog.at_level(logging.DEBUG, logger="netbbs.link.sync"):
                    for _ in range(3):
                        await net.dial("B", session)
                net.post("R", "hello from R")
                await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        carried_on_r = net.dbs["R"].db.connection.execute(
            "SELECT COUNT(*) FROM boards WHERE name = 'from-a'"
        ).fetchone()[0]
        assert carried_on_r == 1, "the scenario needs R to carry A's board"
        refusals = [r for r in caplog.records if "withheld inventory event" in r.getMessage()]
        # The genesis and the post, once each, and not once per pass.
        assert len(refusals) == 2
        # Explained to the SysOp once for the node, not once per event.
        assert len([r for r in caplog.records if "on probation here" in r.getMessage()]) == 1
        assert net.subjects_on("B") == ["hello from R"]
    finally:
        net.close()


def test_a_carrier_is_not_asked_again_on_every_pass_for_an_identity_it_could_not_supply(tmp_path, monkeypatch):
    from netbbs.link import sync as sync_module

    asked: list[tuple[str, ...]] = []

    async def _knows_nobody(node, session, base_url, identity_request):
        asked.append(identity_request.subjects)
        return []

    monkeypatch.setattr(sync_module, "request_identities", _knows_nobody)
    net = _ThreeNodes(tmp_path, enforce=False)

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                for name in ("A", "B"):
                    await net.dial(name, session)
                net.post("A", "hello from A")
                await net.dial("A", session)
                for _ in range(3):
                    await net.dial("B", session)
                    # As the retry interval would: the event is offered again.
                    net.nodes["B"].deferred_events.entries.clear()
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert asked == [(net.ids["A"].fingerprint,)]
    finally:
        net.close()


def test_a_wrong_event_ends_a_response_without_losing_what_was_accepted_before_it(tmp_path, monkeypatch):
    """What was accepted is in the node's memory and counts as known. Unless it
    is persisted too, it is never accepted, and so never persisted, again."""
    import base64

    from netbbs.link import sync as sync_module

    net = _ThreeNodes(tmp_path, enforce=False)
    real_request_inventory = sync_module.request_inventory
    forge = {"on": True}

    async def _with_a_forgery(node, session, base_url, inventory_request):
        events, more, wanted, key_chains = await real_request_inventory(node, session, base_url, inventory_request)
        if forge["on"] and len(events) >= 2:
            forged = {**events[-1], "signature": base64.b64encode(b"x" * 64).decode("ascii")}
            events = [*events[:-1], forged]
        return events, more, wanted, key_chains

    monkeypatch.setattr(sync_module, "request_inventory", _with_a_forgery)

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                net.post("R", "one")
                net.post("R", "two")
                await net.dial("B", session)
                first = net.subjects_on("B")
                forge["on"] = False
                await net.dial("B", session)
                return first
        finally:
            await server.stop()

    try:
        first = asyncio.run(scenario())
        assert len(first) == 1, "the pass with the forgery keeps what came before it"
        assert sorted(net.subjects_on("B")) == ["one", "two"]
    finally:
        net.close()


def test_a_request_that_merely_failed_is_repeated_on_the_next_occasion(tmp_path, monkeypatch):
    """A timeout, a 429 or a 5xx says nothing about what the carrier knows. Only
    a carrier without the route, or one that answered without the identity, is
    left alone for the hour."""
    from netbbs.link import sync as sync_module
    from netbbs.link.transport import LinkTransportError

    asked: list[int] = []

    async def _times_out(*args, **kwargs):
        asked.append(1)
        raise LinkTransportError("could not reach the carrier: timed out")

    monkeypatch.setattr(sync_module, "request_identities", _times_out)
    net = _ThreeNodes(tmp_path, enforce=False)

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                for name in ("A", "B"):
                    await net.dial(name, session)
                net.post("A", "hello from A")
                await net.dial("A", session)
                for _ in range(2):
                    await net.dial("B", session)
                    net.nodes["B"].deferred_events.entries.clear()
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert net.nodes["B"].unanswered_identities == {}
        assert len(asked) >= 2
    finally:
        net.close()


# -- what a node nobody can dial issues, reaching a subscriber (issue #627) ----------------


def _vouching_three_nodes(tmp_path):
    net = _ThreeNodes(tmp_path, enforce=True)
    subject = TrustSubject.node("a-fourth-node-fingerprint")
    register_subject(
        net.dbs["A"].db, subject,
        first_accepted_at="2026-08-01T12:00:00+00:00", now_iso="2026-09-15T12:00:00+00:00",
    )
    record_vouch_intent(net.dbs["A"].db, subject, explanation="known operator")

    def held_on(name):
        return net.dbs[name].db.connection.execute(
            "SELECT revoked_at FROM link_trust_vouches WHERE subject_id = ?", (subject.subject_id,)
        ).fetchall()

    async def pass_on_r(session):
        # R dials nobody; its pass is where it reads what it carries.
        await _one_pass(
            net.nodes["R"], session, [], net.r_hello, net.dbs["R"].lane, enforce_trust_policy=True,
        )

    return net, subject, held_on, pass_on_r


def test_a_vouch_from_a_node_nobody_can_dial_reaches_a_subscriber_through_its_relay(tmp_path):
    """Trust objects are pulled from their issuer, and nobody can dial an
    outgoing-only node, which is what most nodes are. A deposits what it signs
    at R, which relays for it; B, which has never met A, learns who A is from R,
    pulls A's objects from R and checks them against A's own key. R acts on
    none of it.

    In the order a real deployment meets it: B's SysOp names A by fingerprint
    before B knows anything about A, B first learns of A while A's descriptor
    names no relay yet, and only then can the SysOp establish A at all."""
    net, subject, held_on, _pass_on_r = _vouching_three_nodes(tmp_path)
    a = net.ids["A"].fingerprint
    configure_trust_domain(net.dbs["B"].db, "friends", display_name="Friends")
    configure_trusted_reporter(
        net.dbs["B"].db, a, domain_id="friends", scopes=[], can_vouch_nodes=True,
    )

    def listed_on_b():
        return net.dbs["B"].db.connection.execute(
            "SELECT COUNT(*) FROM link_trust_subjects WHERE node_fingerprint = ? AND subject_kind = 'node'",
            (a,),
        ).fetchone()[0]

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                # A's first hello reaches R before R has agreed to relay for
                # it, so what R can say of A names no relay yet.
                await net.dial("A", session)
                assert a in net.nodes["R"].relaying_for
                assert listed_on_b() == 0
                await net.dial("B", session)
                # Asked about before its state was: that is what lists it.
                assert listed_on_b() == 1 and a in net.nodes["B"].introduced
                assert held_on("B") == []
                net.establish("B", "A")

                await net.dial("A", session)  # this hello names R as A's relay
                await net.dial("B", session)  # B refreshes what it knows of A, and pulls
                assert [row[0] for row in held_on("B")] == [None]
                assert a not in net.nodes["B"].peers

                withdraw_vouch_intent(net.dbs["A"].db, subject)
                await net.dial("A", session)
                await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        [row] = held_on("B")
        assert row[0] is not None
        # Carried, and not admitted: R never named A a reporter.
        assert held_on("R") == []
        assert net.dbs["R"].db.connection.execute(
            "SELECT COUNT(*) FROM link_trust_carried_objects WHERE issuer_fingerprint = ?", (a,)
        ).fetchone()[0] == 2
    finally:
        net.close()


def test_a_relay_admits_what_it_carries_once_its_sysop_names_the_depositor_a_reporter(tmp_path):
    """The relay cannot pull from the depositor any more than anyone else can,
    so it reads its own carried store, under a cursor, in its own pass. Naming
    the depositor *after* the deposit is the ordinary order of events, and an
    object the relay cannot use must cost that object and nothing else."""
    import base64

    from netbbs.link.events import build_envelope, canonical_bytes
    from netbbs.link.trust_wire import SignedTrustObject, store_issued_trust_object

    net, subject, held_on, pass_on_r = _vouching_three_nodes(tmp_path)
    a = net.ids["A"]
    # Authentic, and of a version this release does not know. First in A's
    # stream, so everything else has to get past it.
    envelope = build_envelope("trust_vouch", {
        "object_version": 2, "issuer_fingerprint": a.fingerprint, "something": "newer",
    })
    store_issued_trust_object(
        net.dbs["A"].db,
        SignedTrustObject(envelope=envelope, signature=a.signing_key.sign(canonical_bytes(envelope))),
        issued_at="2026-09-18T12:00:00+00:00",
    )
    net.dbs["A"].db.connection.commit()  # the reconcile that calls this in production commits

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.dial("A", session)
                await pass_on_r(session)
                assert held_on("R") == []

                configure_trust_domain(net.dbs["R"].db, "friends", display_name="Friends")
                configure_trusted_reporter(
                    net.dbs["R"].db, a.fingerprint, domain_id="friends", scopes=[], can_vouch_nodes=True,
                )
                await pass_on_r(session)
                assert [row[0] for row in held_on("R")] == [None]

                withdraw_vouch_intent(net.dbs["A"].db, subject)
                await net.dial("A", session)
                await pass_on_r(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        [row] = held_on("R")
        assert row[0] is not None
        assert net.dbs["R"].db.connection.execute(
            "SELECT COUNT(*) FROM link_trust_carried_objects WHERE issuer_fingerprint = ?", (a.fingerprint,)
        ).fetchone()[0] == 3
    finally:
        net.close()


def test_a_subscriber_learns_an_unmet_reporters_new_key_from_the_relay_and_reads_on(tmp_path):
    """A node known only by introduction has nobody but a carrier to learn a
    rotation from. The pull stops at the first object under the key B has not
    learned, B asks the relay for a fresher bundle although it asked within
    the hour and was told nothing had changed, and reads on in the same pass."""
    net, _subject, _held_on, _pass_on_r = _vouching_three_nodes(tmp_path)
    a = net.ids["A"].fingerprint
    second = TrustSubject.node("a-fifth-node-fingerprint")
    register_subject(
        net.dbs["A"].db, second,
        first_accepted_at="2026-08-01T12:00:00+00:00", now_iso="2026-09-15T12:00:00+00:00",
    )
    configure_trust_domain(net.dbs["B"].db, "friends", display_name="Friends")
    configure_trusted_reporter(net.dbs["B"].db, a, domain_id="friends", scopes=[], can_vouch_nodes=True)
    net.establish("B", "A")

    def held_for_second():
        return net.dbs["B"].db.connection.execute(
            "SELECT COUNT(*) FROM link_trust_vouches WHERE subject_id = ? AND revoked_at IS NULL",
            (second.subject_id,),
        ).fetchone()[0]

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                for name in ("A", "A", "B", "B"):
                    await net.dial(name, session)
                # B has refreshed what it knows of A within the hour.
                assert ("reporter-refresh", a) in net.nodes["B"].unanswered_identities

                net.ids["A"] = rotate_operational_key(net.ids["A"], purpose="signing")
                net.nodes["A"].identity = net.ids["A"]
                record_vouch_intent(net.dbs["A"].db, second, explanation="another known operator")
                await net.dial("A", session)  # tells R the new key, re-signs, signs, deposits
                await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert held_for_second() == 1
        assert len(net.nodes["B"].introduced[a].transitions) > 2
    finally:
        net.close()


def test_a_quiet_depositor_still_notices_a_relay_that_lost_what_it_was_handed(tmp_path, caplog):
    """A depositor sends only what is new, so with nothing new it would never
    find out, and the relay could go on serving a vouch without the revocation
    that followed it. One request a pass names what was last handed over."""
    net, _subject, _held_on, _pass_on_r = _vouching_three_nodes(tmp_path)
    a = net.ids["A"].fingerprint

    def carried():
        return net.dbs["R"].db.connection.execute(
            "SELECT COUNT(*) FROM link_trust_carried_objects WHERE issuer_fingerprint = ?", (a,)
        ).fetchone()[0]

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.dial("A", session)
                assert carried() == 1
                # R is restored to a backup from before the deposit.
                connection = net.dbs["R"].db.connection
                connection.execute("DELETE FROM link_trust_carried_objects")
                connection.execute("DELETE FROM link_trust_carriage_marks")
                connection.commit()

                with caplog.at_level(logging.WARNING, logger="netbbs.link.sync"):
                    await net.dial("A", session)  # nothing new to send; told it is out of step
                assert carried() == 0
                await net.dial("A", session)      # everything again
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert carried() == 1
        assert any("no longer holds what this node last handed it" in r.getMessage() for r in caplog.records)
    finally:
        net.close()


def test_a_relays_refusal_is_remembered_for_the_vouch_screen_until_it_takes_a_deposit_again(tmp_path):
    from netbbs.link.trust_carriage import relays_refusing_trust_deposits

    net, _subject, _held_on, _pass_on_r = _vouching_three_nodes(tmp_path)
    a, r = net.ids["A"].fingerprint, net.ids["R"].fingerprint

    def refusing():
        return relays_refusing_trust_deposits(net.dbs["A"].db, [r])

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.dial("A", session)
                assert refusing() == []
                agreed = net.nodes["R"].relaying_for.pop(a)
                await net.dial("A", session)
                assert refusing() == [r]
                net.nodes["R"].relaying_for[a] = agreed
                await net.dial("A", session)
                # Left alone for an hour after a refusal, not asked every pass.
                assert refusing() == [r]
                connection = net.dbs["A"].db.connection
                connection.execute(
                    "UPDATE link_trust_deposit_cursors SET updated_at = '2026-01-01T00:00:00.000000Z'"
                )
                connection.commit()
                await net.dial("A", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert refusing() == []
    finally:
        net.close()


def test_a_relay_stops_reading_its_own_copy_once_the_reporter_no_longer_names_it(tmp_path, caplog):
    """A node that drops a relay tells nobody; it just stops naming it. What
    the relay carried is never added to again, so reading it would mean never
    seeing a later revocation. The relay goes by the reporter's descriptor,
    looks for its relays like any other subscriber, and says so when it finds
    none. Its own record of whom it relays for never shrinks and is no guide."""
    net, subject, held_on, pass_on_r = _vouching_three_nodes(tmp_path)
    a = net.ids["A"].fingerprint
    configure_trust_domain(net.dbs["R"].db, "friends", display_name="Friends")
    configure_trusted_reporter(net.dbs["R"].db, a, domain_id="friends", scopes=[], can_vouch_nodes=True)

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.dial("A", session)
                await pass_on_r(session)
                assert [row[0] for row in held_on("R")] == [None]
                # A drops R. Nothing tells R; A's next hello names no relay.
                from netbbs.link.transport import dial_hello

                net.nodes["A"].relays_serving_me.clear()
                await dial_hello(
                    net.nodes["A"], session, net.seeds[0],
                    _hello_for(net.nodes["A"], created_at="2026-02-01T00:00:00+00:00"), net.dbs["A"].lane,
                )
                assert a in net.nodes["R"].relaying_for
                with caplog.at_level(logging.WARNING, logger="netbbs.link.sync"):
                    await pass_on_r(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert any("names no relay" in r.getMessage() for r in caplog.records)
    finally:
        net.close()


def test_a_blocked_reporter_is_not_asked_about(tmp_path, monkeypatch):
    from netbbs.link import sync as sync_module
    from netbbs.link.trust import TrustDimension, TrustState, set_trust_override

    asked: list[tuple[str, ...]] = []

    async def _counting(node, session, base_url, identity_request):
        asked.append(identity_request.subjects)
        return []

    monkeypatch.setattr(sync_module, "request_identities", _counting)
    net, _subject, _held_on, _pass_on_r = _vouching_three_nodes(tmp_path)
    a = net.ids["A"].fingerprint
    configure_trust_domain(net.dbs["B"].db, "friends", display_name="Friends")
    configure_trusted_reporter(net.dbs["B"].db, a, domain_id="friends", scopes=[], can_vouch_nodes=True)

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.dial("B", session)
                assert asked == [(a,)]
                subject = TrustSubject.node(a)
                register_subject(net.dbs["B"].db, subject, first_accepted_at="2026-08-01T00:00:00+00:00")
                set_trust_override(
                    net.dbs["B"].db, subject, TrustDimension.IDENTITY_INTEGRITY, TrustState.BLOCKED,
                    reason="known bad", actor_user_id=None,
                )
                net.nodes["B"].unanswered_identities.clear()
                await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert asked == [(a,)]
    finally:
        net.close()


# -- Issue #669: what a node holds a genesis for and does not carry ------------


def _deleted_board_scenario(tmp_path, monkeypatch, *, page_size=None, strip_capability=False):
    """R originates two linked boards; B carries both, then deletes the one
    that sorts first. R posts four times to the deleted one and, with the
    responder's page shrunk to `page_size`, once to the other. Returns the
    number of inventory events B received on each pass after the delete, and
    the subjects B ended up with on the board it kept."""
    import functools

    from netbbs.boards.boards import delete_board, get_board_by_name
    from netbbs.link import protocol as protocol_module
    from netbbs.link import sync as sync_module
    from netbbs.link import transport as transport_module
    from netbbs.link.events import build_endpoint_descriptor as real_build_descriptor
    from netbbs.link.store import load_link_node

    if strip_capability:
        # A responder from before #669: its descriptor advertises nothing.
        monkeypatch.setattr(
            protocol_module, "build_endpoint_descriptor",
            functools.partial(real_build_descriptor, capabilities=()),
        )
    net = _ThreeNodes(tmp_path, enforce=False)
    second = create_board(net.dbs["R"].db, "second", creator=net.sysops["R"])
    link_board(net.dbs["R"].db, second, node_identity=net.ids["R"])
    net.nodes["R"] = load_link_node(net.dbs["R"].db, net.ids["R"])
    general = get_board_by_name(net.dbs["R"].db, "general")
    by_id = sorted([(general.board_id, "general"), (second.board_id, "second")])
    deleted_name, kept_name = by_id[0][1], by_id[1][1]

    received: list[int] = []
    real_request_inventory = sync_module.request_inventory

    async def counting_request_inventory(*args, **kwargs):
        result = await real_request_inventory(*args, **kwargs)
        received.append(len(result[0]))
        return result

    monkeypatch.setattr(sync_module, "request_inventory", counting_request_inventory)

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.dial("B", session)
                board = get_board_by_name(net.dbs["B"].db, deleted_name)
                assert board is not None, "the scenario needs B to carry both boards"
                delete_board(net.dbs["B"].db, board, deleted_by=net.sysops["B"])
                for i in range(4):
                    net.post("R", f"on the deleted board {i}", board_name=deleted_name)
                if page_size is not None:
                    monkeypatch.setattr(transport_module, "_MAX_EVENTS_PER_REQUEST", page_size)
                net.post("R", "on the kept board", board_name=kept_name)
                received.clear()
                for _ in range(3):
                    await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        kept = [
            row[0] for row in net.dbs["B"].db.connection.execute(
                """SELECT p.subject FROM posts AS p JOIN boards AS b ON b.id = p.board_id
                   WHERE b.name = ?""",
                (kept_name,),
            )
        ]
        return received, kept
    finally:
        net.close()


def test_a_deleted_carried_board_is_not_sent_again_on_every_pass(tmp_path, monkeypatch):
    """The board's genesis stays in `link_events` after the delete, so it was
    absent from what B declared and R answered "never seen" with the genesis
    and every post, on every pass. Only the kept board's new post arrives now,
    and once."""
    received, kept = _deleted_board_scenario(tmp_path, monkeypatch)
    assert received == [1, 0, 0]
    assert kept == ["on the kept board"]


def test_a_deleted_carried_board_no_longer_starves_what_sorts_after_it(tmp_path, monkeypatch):
    """With a page smaller than the deleted board's history, that history was
    the whole page on every pass and the kept board's post never arrived --
    at the real page of 200, a declined board with 200 posts does this to
    every board, channel and file area behind it."""
    received, kept = _deleted_board_scenario(tmp_path, monkeypatch, page_size=3)
    assert kept == ["on the kept board"]
    assert received == [1, 0, 0]


def test_the_field_is_not_sent_to_a_responder_that_does_not_advertise_it(tmp_path, monkeypatch):
    """An older responder rebuilds the signed payload without `not_carried`
    and refuses the whole request, so a requester must not send it there.
    Against such a responder the old behaviour remains -- the deleted board is
    resent -- and sync keeps working rather than failing outright."""
    received, kept = _deleted_board_scenario(tmp_path, monkeypatch, strip_capability=True)
    assert kept == ["on the kept board"]
    assert received[1:] == [received[1]] * 2 and received[1] > 0


def test_a_linked_board_whose_name_is_taken_locally_is_carried_and_receives_posts(tmp_path):
    """Issue #671. B already has its own `general` when R's linked `general`
    arrives. The insert used to raise `IntegrityError: UNIQUE constraint
    failed: boards.name` out of B's first pass; the genesis was already saved,
    so every later pass skipped it and R's board was never carried. Now it is
    carried under a suffixed name, and R's post lands in it rather than in
    B's own board."""
    from netbbs.boards.boards import get_board_by_name

    net = _ThreeNodes(tmp_path, enforce=False)
    # A creator of its own, not B's `sysop`: a board id hashes name, creator
    # and `created_at`, both nodes' sysops are a fingerprint-less `sysop`,
    # and on Windows two boards made within one 15.6 ms clock tick share a
    # timestamp -- so B's `general` came out with R's board id about one run
    # in four, and was then mistaken for R's board rather than colliding
    # with it by name.
    local = create_user(net.dbs["B"].db, "local-owner", password="password1", user_level=SYSOP_LEVEL)
    create_board(net.dbs["B"].db, "general", creator=local)
    r_board_id = get_board_by_name(net.dbs["R"].db, "general").board_id

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.dial("B", session)
                net.post("R", "hello from R")
                await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        rows = net.dbs["B"].db.connection.execute(
            """SELECT b.name, b.board_id = ?, p.subject FROM boards AS b
               LEFT JOIN posts AS p ON p.board_id = b.id ORDER BY b.id""",
            (r_board_id,),
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("general", 0, None),
            (f"general-{r_board_id[:8]}", 1, "hello from R"),
        ]
    finally:
        net.close()


def test_one_seed_failing_unexpectedly_does_not_end_the_sync_task(monkeypatch):
    """Issue #703: an exception from one seed's pass propagated out of
    `run_link_sync` and ended outbound Link for the node's whole uptime."""
    import asyncio

    import netbbs.link.sync as sync
    from netbbs.link.protocol import LinkProtocolError

    async def boom(*_args, **_kwargs):
        raise LinkProtocolError("peer list carries 495 descriptors")

    monkeypatch.setattr(sync, "_sync_one_seed", boom)
    assert asyncio.run(sync._sync_seed_safely(None, None, "http://seed.example")) is False

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(sync, "_sync_one_seed", cancelled)
    try:
        asyncio.run(sync._sync_seed_safely(None, None, "http://seed.example"))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancellation must still propagate")


def test_a_curated_node_is_offered_a_board_and_accepting_it_pulls_its_content(tmp_path):
    """Issue #683. With a cap of 0 B carries nothing unasked: R's board is
    offered, and nothing under it is fetched meanwhile. Accepting it creates
    the local copy, and the next pass brings its posts like any newly carried
    board's."""
    from netbbs.boards.boards import get_board_by_name
    from netbbs.link.carry import OFFERED, accept_offer, list_carry_decisions

    net = _ThreeNodes(tmp_path, enforce=False)
    r_board = get_board_by_name(net.dbs["R"].db, "general")
    r_board_id = r_board.board_id
    net.post("R", "before accepting")
    # And an edit of it, which `handle_events` also records in an in-memory
    # edit chain that must not keep it once it was not stored.
    original = net.dbs["R"].db.connection.execute(
        "SELECT post_id FROM posts WHERE subject = 'before accepting'"
    ).fetchone()["post_id"]
    from netbbs.boards.posts import get_post

    edited = edit_post(
        net.dbs["R"].db, get_post(net.dbs["R"].db, original), r_board,
        subject="before accepting (edited)", body="hi", edited_by=net.sysops["R"],
    )
    assert queue_board_post_edit_if_linked(
        net.dbs["R"].db, edited, r_board, node_identity=net.ids["R"], edited_by=net.sysops["R"]
    )

    async def dial_b(session):
        await _one_pass(
            net.nodes["B"], session, net.seeds, lambda: _hello_for(net.nodes["B"]),
            net.dbs["B"].lane, max_carried_boards=0,
        )

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await dial_b(session)
                await dial_b(session)
                [offer] = list_carry_decisions(net.dbs["B"].db, OFFERED)
                assert offer.resource_id == r_board_id
                assert net.subjects_on("B") == []
                accept_offer(net.dbs["B"].db, "boards", r_board_id, actor=net.sysops["B"])
                await dial_b(session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert net.subjects_on("B") == ["before accepting", "before accepting (edited)"]
        assert list_carry_decisions(net.dbs["B"].db, OFFERED) == []
    finally:
        net.close()


def test_a_hidden_board_is_left_alone_by_sync_and_restore_pulls_what_it_missed(tmp_path):
    """Issue #683: B hides R's board. What R posts meanwhile is neither sent
    (B declares it not carried) nor projected; Restore brings the board back
    as it was, and the next pass pulls the post it missed."""
    from netbbs.boards.boards import get_board_by_name
    from netbbs.link.carry import hide_carried_resource, restore_excluded

    net = _ThreeNodes(tmp_path, enforce=False)
    r_board_id = get_board_by_name(net.dbs["R"].db, "general").board_id
    net.post("R", "before hiding")

    async def scenario():
        server = await net.start()
        try:
            async with aiohttp.ClientSession() as session:
                await net.dial("B", session)
                assert net.subjects_on("B") == ["before hiding"]
                hide_carried_resource(
                    net.dbs["B"].db, "boards", r_board_id, actor=net.sysops["B"],
                    own_fingerprint=net.ids["B"].fingerprint,
                )
                net.post("R", "while hidden")
                await net.dial("B", session)
                hidden_count = net.dbs["B"].db.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
                assert hidden_count == 1
                restore_excluded(net.dbs["B"].db, "boards", r_board_id, actor=net.sysops["B"])
                await net.dial("B", session)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        assert net.subjects_on("B") == ["before hiding", "while hidden"]
    finally:
        net.close()


# -- Issue #685: a declaration larger than the responder's body limit ----------


def _oversized_declaration_scenario(tmp_path, monkeypatch, *, strip_capability=False):
    """B carries R's board and holds 100 posts of it. The responder's body
    limit is then lowered to just under B's whole declaration -- the state a
    node reaches at about 30,000 held events against the real 2 MiB -- and R
    posts twice more and links a board B has never seen. Returns what B sent
    and received on each later pass, the whole declaration's size, the limit,
    and B's subjects on `general` and whether it discovered the new board."""
    import functools

    from netbbs.boards.boards import get_board_by_name
    from netbbs.link import protocol as protocol_module
    from netbbs.link import store as store_module
    from netbbs.link import sync as sync_module
    from netbbs.link import transport as transport_module
    from netbbs.link.events import build_endpoint_descriptor as real_build_descriptor

    if strip_capability:
        # A responder from before #685 (and #669): its descriptor advertises nothing.
        monkeypatch.setattr(
            protocol_module, "build_endpoint_descriptor",
            functools.partial(real_build_descriptor, capabilities=()),
        )
    net = _ThreeNodes(tmp_path, enforce=False)
    for i in range(100):
        net.post("R", f"history {i:03d}")

    sent: list[tuple[int, tuple[int, int] | None]] = []
    received: list[int] = []
    failures: list[Exception] = []
    real_request_inventory = sync_module.request_inventory

    async def recording_request_inventory(node, session, url, request):
        sent.append((len(json.dumps(request.to_dict())), request.page))
        try:
            result = await real_request_inventory(node, session, url, request)
            received.append(len(result[0]))
            return result
        except Exception as exc:
            failures.append(exc)
            raise

    monkeypatch.setattr(sync_module, "request_inventory", recording_request_inventory)
    sizes: dict[str, int] = {}

    def has_later():
        return net.dbs["B"].db.connection.execute("SELECT 1 FROM boards WHERE name = 'later'").fetchone() is not None

    async def scenario():
        async with aiohttp.ClientSession() as session:
            server = await net.start()
            try:
                await net.dial("B", session)
            finally:
                await server.stop()
            assert len(net.subjects_on("B")) == 100
            whole = store_module.build_inventory_request(
                net.dbs["B"].db, signing_identity=net.ids["B"].signing_key,
                requester_fingerprint=net.ids["B"].fingerprint,
                responder_fingerprint=net.ids["R"].fingerprint,
            )
            sizes["whole"] = len(json.dumps(whole.to_dict()))
            sizes["limit"] = sizes["whole"] - 1
            monkeypatch.setattr(transport_module, "_LINK_CLIENT_MAX_SIZE_BYTES", sizes["limit"])
            monkeypatch.setattr(store_module, "INVENTORY_DECLARATION_BUDGET_BYTES", sizes["limit"] // 2)
            net.post("R", "new one")
            net.post("R", "new two")
            later = create_board(net.dbs["R"].db, "later", creator=net.sysops["R"])
            link_board(net.dbs["R"].db, later, node_identity=net.ids["R"])
            net.nodes["R"] = store_module.load_link_node(net.dbs["R"].db, net.ids["R"])
            sent.clear()
            received.clear()
            server = await net.start()
            try:
                # Each request's split is new, so an item is on the page sent
                # with chance 1/count; the walk ends once B has caught up.
                for _ in range(40):
                    await net.dial("B", session)
                    if len(net.subjects_on("B")) == 102 and has_later():
                        break
            finally:
                await server.stop()

    try:
        asyncio.run(scenario())
        return (
            sent, received, failures, sizes, net.subjects_on("B"), has_later(),
        )
    finally:
        net.close()


def test_a_declaration_over_the_body_limit_is_sent_in_pages(tmp_path, monkeypatch):
    """Before #685 the whole declaration went out on every pass, the responder
    refused it with 413, and pull stopped for good. Now each request fits, and
    what R has since is caught up -- posts on a board B carries, and a board B
    has never seen."""
    sent, received, failures, sizes, subjects, discovered = _oversized_declaration_scenario(tmp_path, monkeypatch)
    assert failures == []
    assert all(size <= sizes["limit"] for size, _page in sent)
    pages = [page for _size, page in sent]
    assert all(page is not None and page[1] >= 2 for page in pages)
    # The cursor walks the page indexes in turn.
    count = pages[0][1]
    assert [(page[0] - pages[0][0]) % count for page in pages] == [i % count for i in range(len(pages))]
    assert {"new one", "new two"} <= set(subjects)
    assert len(subjects) == 102
    assert discovered
    # Two posts and one genesis, each once: the responder compared only the
    # page it was told, so nothing B holds on another page came back.
    assert sum(received) == 3


def test_an_older_responder_is_sent_a_slice_that_fits(tmp_path, monkeypatch):
    """A responder that does not advertise pages cannot be told which page it
    has. It is still sent a request that fits and takes the rest as missing.
    Here its duplicates fit beside the new events in one response, so sync
    goes on; with a history past its 200-event page they can fill every
    response, and pull from it waits for its upgrade (see
    `build_inventory_request`). Either way nothing is refused with 413."""
    sent, _received, failures, sizes, subjects, discovered = _oversized_declaration_scenario(
        tmp_path, monkeypatch, strip_capability=True,
    )
    assert failures == []
    assert all(size <= sizes["limit"] and page is None for size, page in sent)
    assert len(subjects) == 102
    assert discovered


def test_a_paged_diff_answers_an_undeclared_resource_only_on_its_own_page(tmp_path):
    """The requester cut its `not_carried` to the same page, so only there
    does a resource missing from its maps still mean "never seen". Anywhere
    else it may be one the requester declined, and sending it would be the
    #669 resend on every pass."""
    from netbbs.boards.boards import get_board_by_name
    from netbbs.link.protocol import inventory_page
    from netbbs.link.store import board_event_diff

    net = _ThreeNodes(tmp_path, enforce=False)
    try:
        db = net.dbs["R"].db
        board_id = get_board_by_name(db, "general").board_id
        salt = "0123456789abcdef0123456789abcdef"
        own_page = inventory_page(board_id, 3, salt)
        other_page = (own_page + 1) % 3
        events, _ = board_event_diff(db, {}, limit=200, page=(own_page, 3), page_salt=salt)
        assert len(events) == 1
        assert board_event_diff(db, {}, limit=200, page=(other_page, 3), page_salt=salt) == ([], False)
    finally:
        net.close()


# -- relay mailbox retention (issue #891) -----------------------------------


def test_sync_pass_drops_relay_mail_held_past_the_retention_time(tmp_path, caplog):
    """A relay holding mail for a node that never came back: each pass drops
    what that node left uncollected past `RELAY_MAILBOX_RETENTION_DAYS`,
    keeps what is younger, and says so in the SysOp-visible log. The prune
    runs whatever the node's own mode; a node that stopped serving relays
    still holds what it took before."""
    from datetime import datetime, timedelta, timezone

    from netbbs.link.events import build_link_message
    from netbbs.link.relay_mailbox import (
        RELAY_MAILBOX_RETENTION_DAYS,
        deposit_relay_mailbox_envelope,
        mailbox_holdings,
    )

    relay_node = LinkNode(identity=bootstrap_node_identity("relay"))
    relay = _NodeDb(tmp_path, "relay")
    sender = bootstrap_node_identity("sender")
    stop_event = asyncio.Event()

    def _message(user: str):
        return build_link_message(
            signing_identity=sender.signing_key,
            home_node_fingerprint=sender.fingerprint,
            local_user_id=user,
            recipient_home_node_fingerprint="abandoned-recipient",
            recipient_local_user_id="someone",
            confidentiality_tier="tier1_home_node_key",
            ciphertext=b"opaque",
            created_at="2026-01-01T00:00:00+00:00",
        )

    old, young = _message("old"), _message("young")
    deposit_relay_mailbox_envelope(relay.db, "abandoned-recipient", old)
    deposit_relay_mailbox_envelope(relay.db, "abandoned-recipient", young)
    expired_at = datetime.now(timezone.utc) - timedelta(days=RELAY_MAILBOX_RETENTION_DAYS + 1)
    relay.db.connection.execute(
        "UPDATE link_relay_mailbox SET received_at = ? WHERE content_id = ?",
        (expired_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), old.content_id),
    )
    relay.db.connection.commit()

    def provider():
        stop_event.set()  # one pass only
        return _hello_for(relay_node)

    async def scenario():
        async with aiohttp.ClientSession() as session:
            await run_link_sync(
                relay_node, session, [], provider, relay.lane,
                interval_seconds=60.0, stop_event=stop_event,
            )

    try:
        with caplog.at_level(logging.WARNING, logger="netbbs.link"):
            asyncio.run(scenario())
        [holding] = mailbox_holdings(relay.db)
        assert holding.count == 1
        remaining = relay.db.connection.execute("SELECT content_id FROM link_relay_mailbox").fetchall()
        assert [row["content_id"] for row in remaining] == [young.content_id]
        assert any(
            "dropped 1 envelope(s)" in record.getMessage() and "abandoned-recipient (1)" in record.getMessage()
            for record in caplog.records
        )
    finally:
        relay.close()
