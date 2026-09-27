"""
The caller's node map screen (design doc §8.12, issue #777), reached from
the Directory: `Node [m]ap`, "Nodes known to <board>", its detail view, the
node-wide level gate, and the entry's absence when Link is disabled.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.config import get_node_map_min_level, set_node_display_name, set_node_map_min_level
from netbbs.link.boards import LinkContext, materialize_carried_board
from netbbs.link.enforcement import ensure_node_subject
from netbbs.link.events import build_board_genesis, build_endpoint_descriptor
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode, PeerRecord
from netbbs.link.store import save_introduced_identity, save_peer
from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, set_trust_override
from netbbs.net.directory_flow import _browse_directory
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession:
    def __init__(self, keys=None):
        self._keys = iter(keys or [])
        self.written: list[str] = []
        self.terminal_width = 100
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 40
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return ""

    @property
    def visible_output(self) -> str:
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", "".join(self.written))


def _record(identity, *, name: str, dns: str | None = None, addresses=None) -> PeerRecord:
    descriptor = build_endpoint_descriptor(
        signing_identity=identity.signing_key, subject_fingerprint=identity.fingerprint,
        addresses=addresses, outgoing_only=addresses is None,
        created_at="2026-09-01T00:00:00+00:00", friendly_name=name, canonical_dns_name=dns,
    )
    return PeerRecord(
        fingerprint=identity.fingerprint, root_public_key=bytes(identity.root.verify_key),
        transitions=identity.transitions, descriptor=descriptor,
    )


def _carry(db, origin, own, *, name: str, board_id: str, min_read: int | None = None) -> None:
    materialize_carried_board(db, build_board_genesis(
        signing_identity=origin.signing_key, origin_fingerprint=origin.fingerprint,
        board_id=board_id, name=name, created_at="2026-09-01T00:00:00+00:00",
        default_min_read_level=min_read,
    ), own_fingerprint=own.fingerprint)


@pytest.fixture
def rig(tmp_path):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    own = bootstrap_node_identity("own")
    yield db, lane, own, LinkContext(link_node=LinkNode(identity=own))
    lane.close()
    db.close()


def _browse(session, db, lane, user, link_context):
    asyncio.run(_browse_directory(session, db, user, lane=lane, link_context=link_context))


def test_the_directory_offers_the_node_map_and_it_lists_known_nodes(rig):
    db, lane, own, link_context = rig
    set_node_display_name(db, "Roanoke")
    peer = bootstrap_node_identity("peer")
    save_peer(db, _record(
        peer, name="Harbor BBS", dns="harbor.example.org",
        addresses=[{"protocol": "tcp", "address": "198.51.100.7", "port": 7862}],
    ))
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    # m: open the map; 01: the only node; b: back to the map; b: back to the
    # directory; b: leave.
    session = FakeSession(["m", "0", "1", "b", "b", "b"])

    _browse(session, db, lane, viewer, link_context)

    text = session.visible_output
    assert "Node [m]ap" in text
    assert "Nodes known to Roanoke" in text
    listing = text[text.index("Nodes known to Roanoke"):]
    listing = listing[: listing.index("Choice:")]
    # The friendly name alone, numbered from 1; the DNS name is the detail's.
    assert re.search(r"01\.\s+1\s+Harbor BBS\s+direct", listing)
    assert "harbor.example.org" not in listing
    assert "direct" in text
    assert "Directory › Nodes known to Roanoke › Harbor BBS" in text
    assert "DNS name" in text and "harbor.example.org" in text
    # Never shown to callers.
    assert "198.51.100.7" not in text
    assert "Reliability" not in text
    assert "relay" not in text.lower()


def test_the_node_map_is_not_offered_when_link_is_disabled(rig):
    db, lane, own, _link_context = rig
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["m", "b"])

    _browse(session, db, lane, viewer, None)

    text = session.visible_output
    assert "Node [m]ap" not in text
    assert "Nodes known to" not in text


def test_the_node_map_level_gate(rig):
    db, lane, own, link_context = rig
    assert get_node_map_min_level(db) == 0
    set_node_map_min_level(db, 20)
    below = create_user(db, "alice", password="hunter2", user_level=10)
    at = create_user(db, "bob", password="hunter2", user_level=20)

    refused = FakeSession(["m", "b"])
    _browse(refused, db, lane, below, link_context)
    assert "Node [m]ap" not in refused.visible_output
    assert "Nodes known to" not in refused.visible_output

    allowed = FakeSession(["m", "b"])
    _browse(allowed, db, lane, at, link_context)
    assert "Node [m]ap" in allowed.visible_output
    # An empty map says so and returns to the directory.
    assert "No other nodes are known here yet." in allowed.visible_output


def test_set_node_map_min_level_refuses_out_of_range(rig):
    db, *_ = rig
    with pytest.raises(ValueError):
        set_node_map_min_level(db, 256)
    with pytest.raises(ValueError):
        set_node_map_min_level(db, -1)


def test_the_detail_view_lists_only_carried_resources_the_caller_could_open(rig):
    db, lane, own, link_context = rig
    origin = bootstrap_node_identity("origin")
    save_peer(db, _record(origin, name="Origin BBS"))
    _carry(db, origin, own, name="Open Board", board_id="b-open")
    _carry(db, origin, own, name="Staff Board", board_id="b-staff", min_read=100)
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["m", "0", "1", "b", "b", "b"])

    _browse(session, db, lane, viewer, link_context)

    text = session.visible_output
    assert "Carried here" in text.upper() or "CARRIED HERE" in text
    assert "Open Board" in text
    assert "Staff Board" not in text


def test_hidden_nodes_are_left_off_and_their_introductions_say_another_node(rig):
    db, lane, own, link_context = rig
    carrier = bootstrap_node_identity("carrier")
    far = bootstrap_node_identity("far")
    save_peer(db, _record(carrier, name="Blocked Carrier"))
    save_introduced_identity(db, _record(far, name="Far Board"), introduced_by=carrier.fingerprint)
    ensure_node_subject(db, carrier.fingerprint)
    set_trust_override(
        db, TrustSubject.node(carrier.fingerprint), TrustDimension.CONTENT_CONDUCT, TrustState.BLOCKED,
        reason="test",
    )
    viewer = create_user(db, "alice", password="hunter2", user_level=10)
    session = FakeSession(["m", "b", "b"])

    _browse(session, db, lane, viewer, link_context)

    text = session.visible_output
    assert "Far Board" in text
    assert "via another node" in text
    assert "Blocked Carrier" not in text
