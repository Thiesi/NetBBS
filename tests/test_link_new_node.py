"""A new node on NetBBS Link can tell "working, just new" from "broken"
(issue #844): what probation holds back in each direction, what a peer
offers while it waits, and where this node's own linked content has got to."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.link.boards import LinkContext, link_board
from netbbs.link.enforcement import REASON_NODE_PROBATIONARY, ensure_node_subject
from netbbs.link.events import build_board_genesis
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import DeferredEvents, LinkNode, PeerExchange
from netbbs.link.store import save_peer
from netbbs.link.sync import run_link_sync
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    configure_trust_domain,
    configure_trusted_reporter,
    node_probation,
    set_trust_override,
)
from netbbs.net.admin_flow import admin_menu
from netbbs.net.node_map_flow import own_content_at_peer
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.link_sync_wait import run_sync_briefly
from tests.test_admin_flow import FakeSession, _normalized_visible, _visible, _written_text
from tests.test_admin_flow_node_map import _record
from tests.test_link_sync import _NodeDb, _hello_for, _run_server


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def _genesis(identity, name: str, board_id: str = "b-1"):
    return build_board_genesis(
        signing_identity=identity.signing_key, origin_fingerprint=identity.fingerprint,
        board_id=board_id, name=name, created_at="2026-09-01T00:00:00+00:00",
    )


# -- what is held back, and from whom ----------------------------------------------


def test_held_back_events_are_counted_and_named_per_node():
    origin = bootstrap_node_identity("origin")
    other = bootstrap_node_identity("other")
    held = DeferredEvents()
    held.defer(_genesis(origin, "Fountain Pens").to_dict(), waiting_for=None, now=0.0, held_from=origin.fingerprint)
    held.defer(_genesis(origin, "Inks", "b-2").to_dict(), waiting_for=None, now=0.0, held_from=origin.fingerprint)
    held.defer(_genesis(other, "Elsewhere", "b-3").to_dict(), waiting_for=None, now=0.0, held_from=other.fingerprint)
    # Set aside for a missing identity, not refused: not held *from* anyone.
    held.defer(_genesis(origin, "Waiting", "b-4").to_dict(), waiting_for=origin.fingerprint, now=0.0)

    summary = held.held_from(origin.fingerprint)
    assert summary.count == 2
    assert summary.names == {"boards": ("Fountain Pens", "Inks")}


def test_establishing_a_node_releases_what_was_held_back_from_it():
    origin = bootstrap_node_identity("origin")
    held = DeferredEvents()
    # What an event names first need not be its author's node.
    held.defer(
        _genesis(origin, "Fountain Pens").to_dict(), waiting_for="someone-else", now=0.0,
        held_from=origin.fingerprint,
    )
    held.release_identity(origin.fingerprint)
    assert held.held_from(origin.fingerprint).count == 0
    assert held.entries == {}


def test_a_sync_pass_names_what_a_probationary_peer_offers(tmp_path):
    """Content from a peer on probation here is held back, and the held
    events remember the node and the names of what it offers."""
    seed_identity = bootstrap_node_identity("seed")
    seed_node = LinkNode(identity=seed_identity)
    dialer_node = LinkNode(identity=bootstrap_node_identity("dialer"))
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")
    creator = create_user(seed.db, "margo", password="hunter2", user_level=10)
    link_board(seed.db, create_board(seed.db, "Fountain Pens", creator=creator), node_identity=seed_identity)

    async def scenario():
        server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(run_link_sync(
                    dialer_node, session, [f"http://127.0.0.1:{server.port}"],
                    lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                    enforce_trust_policy=True,
                ))
                await run_sync_briefly(task)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        summary = dialer_node.deferred_events.held_from(seed_identity.fingerprint)
        assert summary.count >= 1
        assert summary.names.get("boards") == ("Fountain Pens",)
        # Nothing of the dialer's own was sent to a peer on probation here.
        assert seed_identity.fingerprint not in dialer_node.peer_exchange
    finally:
        dialer.close()
        seed.close()


# -- whether a peer takes this node's own content ------------------------------------


def test_a_push_refused_for_probation_is_recorded(tmp_path, caplog):
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")
    creator = create_user(dialer.db, "margo", password="hunter2", user_level=10)
    link_board(dialer.db, create_board(dialer.db, "Inks", creator=creator), node_identity=dialer_identity)

    async def scenario():
        # The seed enforces its trust policy, so the dialer is on probation there.
        server = await _run_server(seed_node, seed.lane, enforce_trust_policy=True)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(run_link_sync(
                    dialer_node, session, [f"http://127.0.0.1:{server.port}"],
                    lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                ))
                await run_sync_briefly(task)
        finally:
            await server.stop()

    try:
        with caplog.at_level(logging.INFO, logger="netbbs.link.sync"):
            asyncio.run(scenario())
        exchange = dialer_node.peer_exchange[seed_identity.fingerprint]
        assert exchange.refused_reason == REASON_NODE_PROBATIONARY
        assert exchange.holds == set()
        # Routine, so said once at INFO, never as a push failure.
        said = [r for r in caplog.records if "holds this node on probation" in r.getMessage()]
        assert len(said) == 1 and said[0].levelno == logging.INFO
        assert not [r for r in caplog.records if "could not push events" in r.getMessage()]
    finally:
        dialer.close()
        seed.close()


def test_a_peer_that_took_a_linked_board_is_recorded_as_holding_it(tmp_path):
    dialer_identity = bootstrap_node_identity("dialer")
    seed_identity = bootstrap_node_identity("seed")
    dialer_node = LinkNode(identity=dialer_identity)
    seed_node = LinkNode(identity=seed_identity)
    dialer = _NodeDb(tmp_path, "dialer")
    seed = _NodeDb(tmp_path, "seed")
    creator = create_user(dialer.db, "margo", password="hunter2", user_level=10)
    genesis = link_board(dialer.db, create_board(dialer.db, "Inks", creator=creator), node_identity=dialer_identity)

    async def scenario():
        server = await _run_server(seed_node, seed.lane)
        try:
            async with aiohttp.ClientSession() as session:
                task = asyncio.create_task(run_link_sync(
                    dialer_node, session, [f"http://127.0.0.1:{server.port}"],
                    lambda: _hello_for(dialer_node), dialer.lane, interval_seconds=60.0,
                ))
                await run_sync_briefly(task)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
        exchange = dialer_node.peer_exchange[seed_identity.fingerprint]
        assert exchange.refused_reason is None
        assert genesis.content_id in exchange.holds
        assert genesis.content_id in seed_node.known_event_ids
    finally:
        dialer.close()
        seed.close()


def test_what_a_peer_holds_reads_the_same_everywhere():
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    at = now.timestamp() - 120
    held = PeerExchange(at=at, holds={"g1"})
    assert own_content_at_peer("probationary", held, "g1", now=now)[0] == "nothing sent while it is probationary here"
    assert own_content_at_peer("established", None, "g1", now=now)[0].startswith("not known")
    assert own_content_at_peer("established", held, "g1", now=now)[0].startswith("has it")
    assert own_content_at_peer("established", held, "g2", now=now)[0].startswith("not yet")
    refused = PeerExchange(at=at, refused_reason=REASON_NODE_PROBATIONARY)
    assert "your node is on probation there" in own_content_at_peer("established", refused, "g1", now=now)[0]
    assert own_content_at_peer("established", held, own_total=2, now=now)[0].startswith("holds 1 of your 2")


# -- how probation ends --------------------------------------------------------------


def test_probation_progress_says_when_it_can_end(db):
    peer = bootstrap_node_identity("peer")
    ensure_node_subject(db, peer.fingerprint, accepted_at="2026-09-01T00:00:00+00:00")
    probation = node_probation(db, peer.fingerprint)
    assert probation is not None
    assert probation.graduates_no_earlier_than.startswith("2026-10-01")
    assert (probation.activity_days, probation.required_activity_days) == (0, 3)
    assert (probation.vouch_domains, probation.required_vouch_domains) == (0, 2)
    assert probation.vouch_reporters == 0

    configure_trust_domain(db, "friends", display_name="Friends")
    configure_trusted_reporter(
        db, bootstrap_node_identity("reporter").fingerprint, domain_id="friends",
        scopes=[], can_vouch_nodes=True, can_vouch_users=False,
    )
    assert node_probation(db, peer.fingerprint).vouch_reporters == 1

    for dimension in (TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR):
        set_trust_override(
            db, TrustSubject.node(peer.fingerprint), dimension, TrustState.ESTABLISHED, reason="known",
        )
    assert node_probation(db, peer.fingerprint) is None


# -- the screens -----------------------------------------------------------------------


def _link_context_with_peer(db, name: str = "ReLink"):
    identity = bootstrap_node_identity("roanoke")
    context = LinkContext(link_node=LinkNode(identity=identity))
    peer = bootstrap_node_identity("peer")
    record = _record(peer, name=name)
    save_peer(db, record)
    ensure_node_subject(db, peer.fingerprint)
    context.link_node.peers[peer.fingerprint] = record
    return context, peer


def _run(session, lane, sysop, link_context):
    session.terminal_height = 80
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))
    return _normalized_visible(_visible(_written_text(session)))


def test_link_status_explains_probation_both_ways(db, lane):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    link_context, _peer = _link_context_with_peer(db)
    text = _run(FakeSession(["s", "l", "b", "b", "b", "b"]), lane, sysop, link_context)

    assert "On probation here: 1 of 1 -- nothing is exchanged with them until you establish them" in text
    assert "Your node at peers: not known yet" in text
    assert "Every node starts on probation with every other, both ways." in text
    assert "outside any Community" in text


def test_the_dashboard_counts_peers_on_probation(db, lane):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    link_context, _peer = _link_context_with_peer(db)
    text = _run(FakeSession(["b"]), lane, sysop, link_context)
    assert "On probation: 1" in text


def test_a_peer_screen_shows_probation_what_it_offers_and_what_yours_sends(db, lane):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    link_context, peer = _link_context_with_peer(db)
    link_context.link_node.deferred_events.defer(
        _genesis(peer, "Pen Talk").to_dict(), waiting_for=None, now=10**12, held_from=peer.fingerprint,
    )
    text = _run(
        FakeSession(["s", "l", "p", "0", "1", "b", "b", "b", "b", "b"]), lane, sysop, link_context,
    )
    detail = text[text.index("Name: ReLink"):]
    detail = detail[: detail.index("Choice: ")]
    assert "On probation here, as every node is at first" in detail
    assert "Ends by itself: no earlier than" in detail
    assert "only Establish ends it" in detail
    assert "What it sends: held back: 1 item(s) while it is probationary here" in detail
    assert "What yours sends: nothing sent while it is probationary here" in detail
    assert "Board: Pen Talk" in detail


def test_an_own_linked_board_shows_where_it_has_got_to(db, lane):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    link_context, peer = _link_context_with_peer(db)
    for dimension in (TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR):
        set_trust_override(db, TrustSubject.node(peer.fingerprint), dimension, TrustState.ESTABLISHED, reason="known")
    board = create_board(db, "Inks", creator=sysop)
    genesis = link_board(db, board, node_identity=link_context.node_identity)
    link_context.link_node.boards[board.board_id] = genesis
    link_context.link_node.peer_exchange[peer.fingerprint] = PeerExchange(
        at=datetime.now(timezone.utc).timestamp(), refused_reason=REASON_NODE_PROBATIONARY,
    )

    text = _run(FakeSession(["m", "m", "l", "0", "1", "b", "b", "b", "b", "b"]), lane, sysop, link_context)
    assert "At ReLink: refused: your node is on probation there" in text


def test_linking_a_board_says_who_gets_it(db, lane):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    create_board(db, "General", creator=sysop)
    link_context, _peer = _link_context_with_peer(db)
    text = _run(FakeSession(["m", "m", "l", "0", "1", "l", "s", "b", "b", "b", "b", "b"]), lane, sysop, link_context)
    assert "Linked 'General'. Peers you have established get it on the next sync pass" in text
