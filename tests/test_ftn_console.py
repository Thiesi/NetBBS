"""The SysOp console's FTN screens and tools (design doc §6.8, issue #1135
slice 8): Settings → FTN networks, Node → FTN mail, a board's [E]cho,
AreaFix, held packets and the nodelist CLI -- driven through the real
console with the scripted `FakeSession` of `tests.test_admin_flow`."""

from __future__ import annotations

import asyncio
import datetime

import pytest

from netbbs.admin.__main__ import run_ftn_import_nodelist
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.areafix import AreaFixError, areafix_commands, queue_areafix
from netbbs.ftn.message import FtnMessage, decode_message, encode_message
from netbbs.ftn.networks import FtnNetwork, board_area, list_networks, save_network, set_board_area
from netbbs.ftn.nodelist import direct_route
from netbbs.ftn.packet import PacketHeader, build_packet, build_packet_from_packed, parse_packet
from netbbs.ftn.tosser import release_held, toss_packet
from netbbs.moderation.log import list_recent_actions
from netbbs.net.admin_flow import admin_menu
from netbbs.net.ftn_console import board_echo_action, ftn_status_screen
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)


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


def _network(db, **changes):
    values = dict(name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB,
                  uplink_host="net1.fsxnet.nz", enabled=True, areafix_password="FIXPW")
    values.update(changes)
    return save_network(db, FtnNetwork(**values))


def _controls() -> NodeControls:
    return NodeControls(session_registry=ActiveSessionRegistry(), maintenance=MaintenanceMode(),
                        shutdown_event=asyncio.Event(), graceful_delay_seconds=60.0)


def test_settings_lists_ftn_networks(db, lane, sysop):
    session = FakeSession(["s", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert "chomail & netmail (FTN)" in _visible(_written_text(session))


def test_a_new_network_starts_as_fsxnet_and_needs_our_address(db, lane, sysop):
    # s: Settings, e: FTN networks, c: create; save at once is refused (no
    # address); a: our address; s: save; then back out.
    session = FakeSession(["s", "e", "c", "s", "a", "21:1/199", "s", "b", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _visible(_written_text(session))
    assert "starts as fsxNet's main hub" in text
    assert "Enter this node's address" in text
    (network,) = list_networks(db)
    assert (network.name, network.domain, str(network.uplink_address), network.uplink_host) == (
        "fsxNet", "fsxnet", "21:1/100@fsxnet", "net1.fsxnet.nz")
    assert network.our_address == FtnAddress(21, 1, 199, domain="fsxnet")
    assert network.enabled is False and network.netmail_min_level == SYSOP_LEVEL
    assert any(a.action == "set_ftn_network" for a in list_recent_actions(db, limit=10))


def test_a_board_carries_an_echo_from_its_screen(db, lane, sysop):
    network = _network(db)
    board = create_board(db, "general", creator=sysop)
    session = FakeSession(["fsx_gen"])
    assert asyncio.run(board_echo_action(session, lane, sysop, board))
    assert board_area(db, board).tag == "FSX_GEN"
    session = FakeSession([""])
    assert asyncio.run(board_echo_action(session, lane, sysop, board))
    assert board_area(db, board) is None
    assert network.id is not None


def test_the_status_screen_shows_each_network_and_queues_areafix(db, lane, sysop):
    network = _network(db)
    session = FakeSession(["a", "+fsx_gen -fsx_bot %list", "b"])
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    text = _visible(_written_text(session))
    assert "FSXNET (21:1/199)" in text  # section titles are drawn in capitals
    assert "AreaFix request queued: +FSX_GEN -FSX_BOT %LIST" in text
    (queued,) = queue.pending_outbound(db, network.id)
    header = PacketHeader(orig=NODE, dest=HUB, created=None)
    message = decode_message(parse_packet(build_packet_from_packed(header, [queued.packed])).messages[0])
    assert (message.to_name, message.from_name, message.subject) == ("AreaFix", "sysop", "FIXPW")
    assert message.body.split("\n") == ["+FSX_GEN", "-FSX_BOT", "%LIST"]
    assert message.kludge("INTL") == "21:1/100 21:1/199"


def test_areafix_needs_its_password_and_valid_commands(db, sysop):
    assert areafix_commands("fsx_gen +FSX_BOT -fsx_old %Query") == ["+FSX_GEN", "+FSX_BOT", "-FSX_OLD", "%QUERY"]
    with pytest.raises(AreaFixError, match="Nothing to ask"):
        areafix_commands("   ")
    with pytest.raises(AreaFixError, match="AreaFix password"):
        queue_areafix(db, _network(db, areafix_password=""), sysop, ["%LIST"])


def test_a_released_packet_is_tossed_as_from_a_known_system(db, sysop):
    network = _network(db)
    board = create_board(db, "general", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    message = FtnMessage(to_name="All", from_name="Stranger", subject="Hi", body="Let me in", area="FSX_GEN",
                         date=datetime.datetime(2026, 10, 7, 9, 0), kludges=[("MSGID", "21:9/9 00000001")],
                         origin="Far (21:9/9)", orig_net=9, orig_node=9, dest_net=1, dest_node=199)
    data = build_packet(PacketHeader(orig=FtnAddress(21, 9, 9), dest=NODE, created=None), [encode_message(message)])
    toss_packet(db, network, data, secure=False, remote_address="21:9/9", file_name="x.pkt")
    (held,) = queue.list_held(db)

    result = release_held(db, network, held.id)

    assert result.posts == 1
    assert queue.list_held(db) == []


def test_the_cli_imports_a_nodelist(db, tmp_path):
    network = _network(db)
    path = tmp_path / "FSXNET.280"
    path.write_bytes(b"Zone,21,Z,X,Y,-,300\r\nHost,1,N,X,Y,-,300\r\n,110,Joe,X,Y,-,300,IBN:joe.example\r\n")
    assert run_ftn_import_nodelist(db, "FSXNET", path) == "Imported 3 nodes for fsxNet from FSXNET.280."
    assert direct_route(db, network.id, FtnAddress(21, 1, 110))[1] == "joe.example"
    with pytest.raises(ValueError, match="no FTN network called 'other'"):
        run_ftn_import_nodelist(db, "other", path)


def test_a_released_packet_for_another_address_is_tossed_not_held_again(db, sysop):
    network = _network(db)
    board = create_board(db, "general", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    message = FtnMessage(to_name="All", from_name="Hub", subject="Hi", body="Misaddressed", area="FSX_GEN",
                         date=datetime.datetime(2026, 10, 7, 9, 0), kludges=[("MSGID", "21:1/100 00000009")],
                         origin="Hub (21:1/100)", orig_net=1, orig_node=100, dest_net=1, dest_node=5)
    data = build_packet(PacketHeader(orig=HUB, dest=FtnAddress(21, 1, 5), created=None), [encode_message(message)])
    toss_packet(db, network, data, secure=True, remote_address="21:1/100", file_name="x.pkt")
    (held,) = queue.list_held(db)
    assert "not to this node" in held.reason

    result = release_held(db, network, held.id)

    assert (result.posts, result.refused_packet) == (1, None)
    assert queue.list_held(db) == []


def test_with_two_networks_an_action_asks_which(db, lane, sysop):
    _network(db)
    _network(db, name="AgoraNet", domain="agoranet", our_address=FtnAddress(46, 1, 9),
             uplink_address=FtnAddress(46, 1, 1), uplink_host="agora.example")
    session = FakeSession(["a", "2", "", "+agn_gen", "b"])  # "" is Enter
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    text = _visible(_written_text(session))
    assert "Which network?" in text
    assert "AreaFix request queued: +AGN_GEN" in text


def test_a_full_queue_is_said_on_the_areafix_screen(db, lane, sysop, monkeypatch):
    _network(db)
    monkeypatch.setattr(queue, "MAX_PENDING_PER_NETWORK", 0)
    session = FakeSession(["a", "%list", "b"])
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    assert "already waiting" in _visible(_written_text(session))


def test_the_cli_import_is_attributed_to_the_sysop_named_with_as(db, sysop, tmp_path):
    _network(db)
    path = tmp_path / "FSXNET.280"
    path.write_bytes(b"Zone,21,Z,X,Y,-,300\r\n,110,Joe,X,Y,-,300,IBN:joe.example\r\n")
    run_ftn_import_nodelist(db, "fsxNet", path, as_username="sysop")
    (entry,) = [a for a in list_recent_actions(db, limit=10) if a.action == "import_ftn_nodelist"]
    assert entry.actor_user_id == sysop.id
    with pytest.raises(ValueError, match="not an active SysOp"):
        run_ftn_import_nodelist(db, "fsxNet", path, as_username="nobody")
