"""The SysOp's node map behind Link status `[P]eers` (design doc §8.12,
issue #777): the caller's list plus candidates and the nodes callers do not
see, with each dimension's trust state, Link addresses, relay roles and
reliability; and the node map level in Settings > Limits & retention."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.config import get_node_map_min_level
from netbbs.link.boards import materialize_carried_board
from netbbs.link.enforcement import ensure_node_subject
from netbbs.link.events import build_board_genesis, build_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import PeerRecord
from netbbs.link.store import save_candidate_descriptor, save_peer
from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, set_trust_override
from netbbs.moderation.log import list_recent_actions
from netbbs.net.admin_flow import admin_menu
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _link_context, _normalized_visible, _visible, _written_text


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


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


def _record(identity, *, name: str, dial_in=None) -> PeerRecord:
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key, subject_fingerprint=identity.fingerprint,
        addresses=None, outgoing_only=True, created_at="2026-09-01T00:00:00+00:00", friendly_name=name,
        dial_in=dial_in,
    )
    return PeerRecord(
        fingerprint=identity.fingerprint, root_public_key=bytes(identity.root.verify_key),
        transitions=identity.transitions, descriptor=descriptor,
    )


def _detail(visible: str, name: str) -> str:
    """One node's detail screen, from its first row to its prompt."""
    text = _normalized_visible(visible)
    detail = text[text.index(f"Name: {name}"):]
    return detail[: detail.index("Choice: ")]


def test_the_sysop_sees_candidates_and_blocked_nodes_callers_do_not(db, lane, sysop):
    link_context = _link_context()
    blocked = bootstrap_node_identity("blocked")
    candidate = bootstrap_node_identity("candidate")
    save_peer(db, _record(blocked, name="Alpha Blocked"))
    ensure_node_subject(db, blocked.fingerprint)
    set_trust_override(
        db, TrustSubject.node(blocked.fingerprint), TrustDimension.CONTENT_CONDUCT, TrustState.BLOCKED,
        reason="spam",
    )
    save_candidate_descriptor(db, candidate.fingerprint, _record(candidate, name="Beta Candidate").descriptor)

    # s, l: Link status; p: the node map; 01: Alpha Blocked; b: back to the
    # map; 02: Beta Candidate; b, b: back to Link status; b, b, b: out.
    session = FakeSession(["o", "l", "p", "0", "1", "b", "0", "2", "b", "b", "b", "b", "b"])
    session.terminal_height = 60  # each detail on one page
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    visible = _visible(_written_text(session))
    listing = visible[visible.index("Link status › Nodes known to NetBBS"):]
    listing = listing[: listing.index("Choice: ")]
    assert "Alpha Blocked" in listing and "blocked" in listing
    assert "Beta Candidate" in listing and "unverified" in listing and "never heard from" in listing

    blocked_detail = _detail(visible, "Alpha Blocked")
    assert "Content trust: blocked" in blocked_detail
    assert "Identity trust: probationary" in blocked_detail
    assert "callers do not see it" in blocked_detail

    candidate_detail = _detail(visible, "Beta Candidate")
    assert "Last heard: never heard from" in candidate_detail
    assert "First named by a peer list:" in candidate_detail
    assert "Unverified" in candidate_detail


def test_the_sysop_sees_an_origin_only_node_with_unknown_fields(db, lane, sysop):
    link_context = _link_context()
    origin = bootstrap_node_identity("gone")
    materialize_carried_board(db, build_board_genesis(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        board_id="b-gone", name="Left Behind", created_at="2026-09-01T00:00:00+00:00",
        default_min_read_level=200,
    ), own_fingerprint=link_context.node_identity.fingerprint)

    session = FakeSession(["o", "l", "p", "0", "1", "b", "b", "b", "b", "b"])
    session.terminal_height = 60
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    visible = _visible(_written_text(session))
    detail = _detail(visible, f"Unknown node {origin.fingerprint[:6]}")
    assert f"Technical identity: {origin.fingerprint}" in detail
    assert "Known: unknown" in detail
    assert "Last heard: unknown" in detail
    assert "DNS name: unknown" in detail
    assert "Addresses: unknown" in detail
    assert "Published relays: unknown" in detail
    assert "Live relays: unknown" in detail
    # The SysOp sees everything carried from it, gates or not.
    assert "Board: Left Behind" in detail


def test_the_node_map_level_is_a_limits_setting(db, lane, sysop):
    # s: Settings, l: Limits & retention, n: node map level, 30, s: save.
    session = FakeSession(["s", "l", "n", "30", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    assert get_node_map_min_level(db) == 30
    text = _visible(_written_text(session))
    assert "Node map level" in text
    audit = [a for a in list_recent_actions(db, limit=10) if a.action == "set_limits_and_retention"]
    assert len(audit) == 1 and "map_level=30" in audit[0].detail


def test_the_node_map_level_refuses_a_level_above_sysop(db, lane, sysop):
    session = FakeSession(["s", "l", "n", "300", "s", "b", "y", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))

    assert "Node map level must be 0-255." in _visible(_written_text(session))
    assert get_node_map_min_level(db) == 0


def test_the_sysop_detail_shows_dial_in_addresses_and_drops_bad_ones(db, lane, sysop):
    link_context = _link_context()
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(peer, name="Harbor BBS", dial_in=[
    "telnet://harbor.example.org:23",
    "http://plain.example.org/",
    "telnet://evil.example.org:23\x1b]0;pwned\x07",
    "https://harbor.example.org/web",
]))

    session = FakeSession(["o", "l", "p", "0", "1", "b", "b", "b", "b", "b"])
    session.terminal_height = 60
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    raw = _written_text(session)
    detail = _detail(_visible(raw), "Harbor BBS")
    assert "DIAL IN Address: telnet://harbor.example.org:23 Address: https://harbor.example.org/web" in detail
    assert "plain.example.org" not in detail and "evil.example.org" not in detail
    assert "pwned" not in raw and "\x1b]" not in raw


def test_a_candidate_that_is_also_an_origin_is_described_as_callers_see_it(db, lane, sysop):
    link_context = _link_context()
    node = bootstrap_node_identity("both")
    save_candidate_descriptor(db, node.fingerprint, _record(node, name="Named In A List").descriptor)
    materialize_carried_board(db, build_board_genesis(
        signing_identity=node.signing_key, origin_fingerprint=node.fingerprint,
        board_id="b-both", name="Carried", created_at="2026-09-01T00:00:00+00:00",
    ), own_fingerprint=link_context.node_identity.fingerprint)

    session = FakeSession(["o", "l", "p", "0", "1", "b", "b", "b", "b", "b"])
    session.terminal_height = 60
    asyncio.run(admin_menu(session, lane, sysop, link_context=link_context))

    detail = _detail(_visible(_written_text(session)), "Named In A List")
    assert "Callers see it only as the origin of what this board carries" in detail
    assert "Callers do not see it." not in detail
    # Its descriptor, though unverified, is on file: its relay counts are known.
    assert "Published relays: 0" in detail
