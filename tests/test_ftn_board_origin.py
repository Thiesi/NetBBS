"""A board's own Origin line (issue #1474, design doc §6.8, §16 "Issue
#1154", Decision 10): set from the board's screen with [O]rigin (FTN),
bounded so the whole Origin line with this node's address stays within
FTS-0004's 79 characters, and empty for the network's Origin."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.message import MAX_ORIGIN_LINE, decode_message, origin_room
from netbbs.ftn.networks import (
    FtnNetwork,
    FtnNetworkError,
    board_area,
    clear_board_area,
    save_network,
    set_board_area,
    set_board_origin,
)
from netbbs.ftn.packet import PacketHeader, build_packet_from_packed, parse_packet
from netbbs.ftn.scanner import export_post_if_ftn
from netbbs.moderation.log import list_recent_actions
from netbbs.net.ftn_console import board_echo_rows, board_origin_action
from netbbs.net.notices import take_notices
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text

OURS = FtnAddress(21, 1, 199)
UPLINK = FtnAddress(21, 1, 100)


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


@pytest.fixture
def network(db):
    return save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=OURS, uplink_address=UPLINK, uplink_host="hub.example",
        enabled=True, origin_text="Test BBS",
    ))


@pytest.fixture
def board(db, sysop, network):
    board = create_board(db, "fsx general", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    return board


def _exported_origin(db, network, board, sysop) -> str:
    assert export_post_if_ftn(db, create_post(db, board, sysop, "Hi", "Hello."), board)
    queued = queue.pending_outbound(db, network.id)[-1]
    header = PacketHeader(orig=OURS, dest=UPLINK, created=None)
    return decode_message(parse_packet(build_packet_from_packed(header, [queued.packed])).messages[0]).origin


def test_a_board_s_origin_replaces_the_network_s_and_empty_brings_it_back(db, sysop, network, board):
    assert set_board_origin(db, board, "  The   General  Store ") == "The General Store"
    assert board_area(db, board).origin_text == "The General Store"
    assert _exported_origin(db, network, board, sysop) == "The General Store (21:1/199@fsxnet)"

    assert set_board_origin(db, board, "") == ""
    assert _exported_origin(db, network, board, sysop) == "Test BBS (21:1/199@fsxnet)"


def test_the_origin_is_bounded_by_the_whole_line_with_the_address(db, sysop, network, board):
    room = origin_room(network.our_address)
    assert room == MAX_ORIGIN_LINE - len(" * Origin: ") - len(" (21:1/199@fsxnet)")
    set_board_origin(db, board, "x" * room)
    assert len(f" * Origin: {_exported_origin(db, network, board, sysop)}") == MAX_ORIGIN_LINE
    with pytest.raises(FtnNetworkError, match=f"at most {room} characters"):
        set_board_origin(db, board, "x" * (room + 1))
    with pytest.raises(FtnNetworkError, match="printable"):
        set_board_origin(db, board, "bell\x07")
    assert board_area(db, board).origin_text == "x" * room


def test_only_a_board_carrying_an_echo_has_an_origin(db, sysop, network, board):
    local = create_board(db, "local", creator=sysop)
    with pytest.raises(FtnNetworkError, match="carries no echo"):
        set_board_origin(db, local, "Nope")
    set_board_origin(db, board, "Mine")
    clear_board_area(db, board)
    set_board_area(db, board, network.id, "FSX_GEN")
    assert board_area(db, board).origin_text == ""  # a local board kept none


def test_the_board_screen_shows_the_origin_and_sets_it(db, lane, sysop, network, board):
    assert [(f.label, f.value) for f in board_echo_rows(db, board)][1] == ("Origin", "the network's: Test BBS")

    session = FakeSession(["Corner Shop"])
    assert asyncio.run(board_origin_action(session, lane, sysop, board))
    assert "at most 50 characters" in _visible(_written_text(session))
    assert [(f.label, f.value) for f in board_echo_rows(db, board)][1] == ("Origin", "Corner Shop")
    assert any(a.action == "set_board_origin" for a in list_recent_actions(db, limit=5))

    session = FakeSession(["y" * 51])
    assert not asyncio.run(board_origin_action(session, lane, sysop, board))
    assert "The Origin is at most 50 characters on fsxNet" in _visible("".join(take_notices(session)))
    assert board_area(db, board).origin_text == "Corner Shop"

    session = FakeSession([""])
    assert asyncio.run(board_origin_action(session, lane, sysop, board))
    assert "carries fsxNet's Origin again" in _visible("".join(take_notices(session)))
    assert board_area(db, board).origin_text == ""


def test_the_board_screen_offers_origin_only_while_it_carries_an_echo(db, lane, sysop, network, board):
    from netbbs.net.admin_boards import _board_screen

    local = create_board(db, "local", creator=sysop)
    session = FakeSession(["b"])
    asyncio.run(_board_screen(session, lane, sysop, existing=local))
    assert "[O]rigin (FTN)" not in _visible(_written_text(session))

    session = FakeSession(["o", "Corner Shop", "b"])
    asyncio.run(_board_screen(session, lane, sysop, existing=board))
    text = _visible(_written_text(session))
    assert "[O]rigin (FTN)" in text and "at most 50 characters" in text
    assert board_area(db, board).origin_text == "Corner Shop"


def test_moving_to_another_network_drops_the_origin_and_another_tag_keeps_it(db, sysop, network, board):
    other = save_network(db, FtnNetwork(
        name="FidoNet", domain="fidonet", our_address=FtnAddress(2, 240, 5832, 1), uplink_address=FtnAddress(2, 240, 1),
        uplink_host="hub.example", enabled=True,
    ))
    set_board_origin(db, board, "x" * origin_room(network.our_address))
    set_board_area(db, board, network.id, "FSX_BOT")
    assert board_area(db, board).origin_text == "x" * origin_room(network.our_address)
    set_board_area(db, board, other.id, "FIDO_GEN")
    assert board_area(db, board).origin_text == ""
