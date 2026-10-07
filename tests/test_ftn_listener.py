"""Answering FTN calls (design doc §6.8, issue #1135 slice 5): the listener
on a real loopback port with a real database lane, called by this
package's own originating session."""

from __future__ import annotations

import asyncio
import datetime
import socket
from dataclasses import replace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.config import set_config
from netbbs.ftn import listener as listener_module
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import M_BSY, BinkpError, OutgoingFile, SystemInfo, read_frame, run_session
from netbbs.ftn.listener import FtnListener
from netbbs.ftn.message import FtnMessage, decode_message, encode_message
from netbbs.ftn.networks import FtnNetwork, save_network, set_board_area
from netbbs.ftn.packet import PacketHeader, build_packet, parse_packet
from netbbs.ftn.scanner import export_post_if_ftn
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)
STRANGER = FtnAddress(21, 4, 4)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    set_config(database, listener_module.HOST_CONFIG_KEY, "127.0.0.1")
    set_config(database, listener_module.PORT_CONFIG_KEY, str(_free_port()))
    yield database
    database.close()


@pytest.fixture
def setup(db):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    network = save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB, uplink_host="hub.example",
        session_password="SECRET", enabled=True, answers_calls=True))
    board = create_board(db, "general", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    return sysop, network, board


def _echomail(body: str, sender: FtnAddress) -> bytes:
    message = encode_message(FtnMessage(
        to_name="All", from_name="Caller", subject="s", body=body, area="FSX_GEN",
        date=datetime.datetime(2026, 10, 7, 9, 0), kludges=[("MSGID", f"{sender} 00000001")],
        origin=f"Elsewhere ({sender})", orig_net=sender.net, orig_node=sender.node, dest_net=1, dest_node=199))
    return build_packet(PacketHeader(orig=sender, dest=NODE, created=None), [message])


async def _with_listener(db, test):
    lane = DatabaseLane(db.path)
    listener = FtnListener(lane)
    try:
        await listener.reconcile()
        return await test(listener)
    finally:
        await listener.close()
        lane.close()


async def _call(listener, *, address=HUB, password="SECRET", files=()):
    host, port = listener.listening_on
    reader, writer = await asyncio.open_connection(host, port)
    try:
        return await run_session(reader, writer, originating=True, our_addresses=[address],
                                 system=SystemInfo("Caller", "op"), password=password, outgoing=list(files),
                                 timeout=5)
    finally:
        writer.close()


async def _settled(listener, calls: int = 1) -> None:
    """Wait until the listener has recorded `calls` answered calls."""
    for _ in range(500):
        if len(listener.recent) >= calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the listener never finished the call")


def _posts(db):
    return [row["body"] for row in db.connection.execute("SELECT body FROM posts ORDER BY id")]


def test_the_uplink_calling_in_gets_waiting_mail_and_its_packets_are_tossed(db, setup):
    sysop, network, board = setup
    export_post_if_ftn(db, create_post(db, board, sysop, "Out", "Waiting for the hub"), board)

    async def test(listener):
        result = await _call(listener, files=[OutgoingFile("in.pkt", _echomail("Crash mail", HUB))])
        await _settled(listener)  # the listener tosses after the session
        return result

    result = asyncio.run(_with_listener(db, test))

    assert result.secure
    (received,) = result.received
    assert decode_message(parse_packet(received.data).messages[0]).body == "Waiting for the hub"
    assert queue.count_pending_outbound(db, network.id) == 0
    assert _posts(db) == ["Waiting for the hub", "Crash mail"]


def test_a_stranger_may_deliver_but_it_is_held_and_it_gets_nothing(db, setup):
    sysop, network, board = setup
    export_post_if_ftn(db, create_post(db, board, sysop, "Out", "Not for strangers"), board)

    async def test(listener):
        result = await _call(listener, address=STRANGER, password="",
                             files=[OutgoingFile("x.pkt", _echomail("Unproven", STRANGER))])
        await _settled(listener)
        return result

    result = asyncio.run(_with_listener(db, test))

    assert not result.secure
    assert result.received == []
    assert queue.count_pending_outbound(db, network.id) == 1
    assert _posts(db) == ["Not for strangers"]
    (held,) = queue.list_held(db)
    assert held.reason == "unsecure session"


def test_the_uplink_with_a_wrong_password_is_refused(db, setup):
    async def test(listener):
        with pytest.raises(BinkpError, match="Incorrect password"):
            await _call(listener, password="WRONG", files=[OutgoingFile("x.pkt", _echomail("No", HUB))])
        await _settled(listener)
        return list(listener.recent)

    recent = asyncio.run(_with_listener(db, test))
    assert _posts(db) == []
    assert recent[-1].outcome.startswith("failed")


def test_it_listens_only_while_an_enabled_network_answers(db, setup):
    _, network, _ = setup

    async def test(listener):
        assert listener.listening_on is not None
        save_network(db, replace(network, answers_calls=False))
        await listener.reconcile()
        assert listener.listening_on is None
        save_network(db, replace(network, answers_calls=True, enabled=False))
        await listener.reconcile()
        assert listener.listening_on is None
        save_network(db, replace(network, answers_calls=True, enabled=True))
        await listener.reconcile()
        return listener.listening_on

    assert asyncio.run(_with_listener(db, test)) is not None


def test_a_caller_over_the_session_limit_is_told_the_node_is_busy(db, setup, monkeypatch):
    monkeypatch.setattr(listener_module, "MAX_SESSIONS", 0)

    async def test(listener):
        host, port = listener.listening_on
        reader, writer = await asyncio.open_connection(host, port)
        try:
            return await read_frame(reader, 5)
        finally:
            writer.close()

    command, argument = asyncio.run(_with_listener(db, test))
    assert command == M_BSY and b"Too many" in argument


def test_a_port_already_taken_is_reported_not_raised(db, setup):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        set_config(db, listener_module.PORT_CONFIG_KEY, str(blocker.getsockname()[1]))

        async def test(listener):
            return listener.listening_on, listener.last_error

        listening, error = asyncio.run(_with_listener(db, test))
        assert listening is None
        assert "cannot listen" in error
    finally:
        blocker.close()
