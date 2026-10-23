"""The SysOp console's QWKnet screens (#1481, #1482; design doc §6.11):
Settings → QWKnet networks, conference mapping, node accounts and held
messages -- driven through the real console with the scripted
`FakeSession` of `tests.test_admin_flow`."""

from __future__ import annotations

import asyncio
import datetime

import pytest

from netbbs.auth.passwords import verify_password
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.moderation.log import list_recent_actions
from netbbs.net.admin_flow import admin_menu
from netbbs.net.qwknet_console import qwknet_conferences_screen, qwknet_held_screen, qwknet_nodes_screen
from netbbs.qwk.packet import Conference, PacketInfo, PacketMessage, build_packet
from netbbs.qwknet.exchange import list_held, toss_hub_packet
from netbbs.qwknet.networks import (
    QwknetNetwork,
    board_conference,
    list_networks,
    list_nodes,
    map_conference,
    node_conferences,
    node_login,
    save_network,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text


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


def test_a_new_network_starts_as_dove_net_through_vertrauen(db, lane, sysop):
    # s: Settings, k: QWKnet networks, c: create, s: save, then back out.
    session = FakeSession(["s", "k", "c", "s", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _visible(_written_text(session))
    assert "QW[k]net networks" in text or "net networks" in text
    assert "starts as DOVE-Net through Vertrauen" in text
    (network,) = list_networks(db)
    assert (network.name, network.hub_id, network.host, network.transport) == (
        "DOVE-Net", "VERT", "vert.synchro.net", "https")
    assert network.enabled is False
    assert any(a.action == "set_qwknet_network" for a in list_recent_actions(db, limit=10))


def test_a_board_gets_a_conference_number_and_back(db, lane, sysop):
    network = save_network(db, QwknetNetwork(name="DOVE-Net", our_id="RELINK", hub_id="VERT"))
    board = create_board(db, "General", creator=sysop)
    session = FakeSession(["1", "", "2001", "b"])
    asyncio.run(qwknet_conferences_screen(session, lane, sysop, network))
    assert board_conference(db, board).number == 2001
    assert "General carries conference 2001 on DOVE-Net." in _visible(_written_text(session))
    session = FakeSession(["1", "", "", "b"])
    asyncio.run(qwknet_conferences_screen(session, lane, sysop, network))
    assert board_conference(db, board) is None


def test_a_node_account_keeps_only_a_password_hash(db, lane, sysop):
    network = save_network(db, QwknetNetwork(name="HomeNet", our_id="RELINK", serve_nodes=True, enabled=True))
    map_conference(db, create_board(db, "General", creator=sysop), network.id, 1)
    # c: create; 01: QWK ID; 04: password (typed unseen); s: save; b: back.
    session = FakeSession(["c", "0", "1", "foo", "0", "4", "nodepass", "s", "b", "b"])
    asyncio.run(qwknet_nodes_screen(session, lane, sysop, network))
    (node,) = list_nodes(db, network.id)
    assert node.qwk_id == "FOO" and node_conferences(db, node.id) == {1}
    stored = node_login(db, "foo")[1]
    assert "nodepass" not in stored and verify_password("nodepass", stored)
    assert "qnet-http" in _visible(_written_text(session))


def test_held_messages_are_listed_and_retossed_once_mapped(db, lane, sysop):
    network = save_network(db, QwknetNetwork(name="DOVE-Net", our_id="RELINK", hub_id="VERT"))
    when = datetime.datetime(2026, 10, 1)
    packet = build_packet(PacketInfo(bbs_id="VERT", bbs_name="V", user_name="RELINK", created_at=when),
                          [Conference(2002, "Ads")],
                          [PacketMessage(conference=2002, number=5, when=when, to="All", sender="DM",
                                         subject="Ad", body="Buy")])
    toss_hub_packet(db, network, packet)
    session = FakeSession(["r", "1", "", "b"])
    asyncio.run(qwknet_held_screen(session, lane, sysop, network))
    assert "Map conference 2002 to a board first" in _visible(_written_text(session))
    map_conference(db, create_board(db, "Ads", creator=sysop), network.id, 2002)
    session = FakeSession(["r", "1", "", "b"])
    asyncio.run(qwknet_held_screen(session, lane, sysop, network))
    assert list_held(db, network.id) == []
    assert "1 posts" in _visible(_written_text(session))
