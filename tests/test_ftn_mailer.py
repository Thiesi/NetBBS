"""The FTN mailer (design doc §6.8, issue #1135 slice 4): calls to a hub on a
real loopback socket, with a real database lane. The hub is this package's
own answering session holding mail for the node."""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import replace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.ftn import mailer as mailer_module
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import OutgoingFile, SystemInfo, run_session
from netbbs.ftn.bundle import build_bundle
from netbbs.ftn.mailer import FtnMailer, PollStatus
from netbbs.ftn.message import FtnMessage, decode_message, encode_message
from netbbs.ftn.networks import FtnNetwork, get_network, save_network, set_board_area
from netbbs.ftn.packet import PacketHeader, build_packet, parse_packet
from netbbs.ftn.scanner import export_post_if_ftn
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def setup(db):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    network = save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB, uplink_host="127.0.0.1",
        session_password="SECRET", enabled=True))
    board = create_board(db, "general", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    return sysop, network, board


def _hub_mail(*bodies: str) -> bytes:
    messages = [encode_message(FtnMessage(
        to_name="All", from_name="Hub Person", subject="From the hub", body=body, area="FSX_GEN",
        date=datetime.datetime(2026, 10, 7, 9, 0), kludges=[("MSGID", f"21:1/100 {index:08x}")],
        origin=f"Hub (21:1/100)", orig_net=1, orig_node=100, dest_net=1, dest_node=199,
    )) for index, body in enumerate(bodies, start=1)]
    return build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), messages)


async def _with_hub(db, network, test, *, address=HUB, password="SECRET", files=()):
    """Serve a hub on loopback, point the network at it, and run `test(mailer,
    network)`; returns `(test's result, what the hub received)`."""
    received = []

    async def handle(reader, writer):
        try:
            result = await run_session(
                reader, writer, originating=False, our_addresses=[address], system=SystemInfo("Hub", "hubop"),
                password_for=lambda addresses: password, outgoing_for=lambda addresses, secure: list(files),
                timeout=5)
            received.extend(result.received)
        except Exception:  # noqa: BLE001 -- the test asserts on the node's side
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    network = save_network(db, replace(network, uplink_port=port))
    lane = DatabaseLane(db.path)
    mailer = FtnMailer(lane)
    try:
        return await test(mailer, network), received
    finally:
        await mailer.close()
        lane.close()
        server.close()
        await server.wait_closed()


def test_a_call_sends_waiting_posts_and_tosses_what_the_hub_holds(db, setup):
    sysop, network, board = setup
    export_post_if_ftn(db, create_post(db, board, sysop, "Out", "Going out"), board)

    async def call(mailer, network):
        return await mailer.poll(network)

    status, hub_received = asyncio.run(_with_hub(
        db, network, call, files=[OutgoingFile("0000abcd.pkt", _hub_mail("In from the hub"))]))

    assert status.failures == 0 and status.last_error is None
    (sent,) = hub_received
    (message,) = parse_packet(sent.data).messages
    assert decode_message(message).body == "Going out"
    assert queue.count_pending_outbound(db, network.id) == 0
    bodies = [row["body"] for row in db.connection.execute("SELECT body FROM posts ORDER BY id")]
    assert bodies == ["Going out", "In from the hub"]
    assert "1 posts" in status.last_summary


def test_a_zip_bundle_is_unpacked_and_another_file_is_held(db, setup):
    _, network, _ = setup
    bundle = build_bundle([("0000abcd.pkt", _hub_mail("one", "two"))])

    async def call(mailer, network):
        return await mailer.poll(network)

    status, _ = asyncio.run(_with_hub(db, network, call, files=[
        OutgoingFile("00020063.we0", bundle), OutgoingFile("fsxnet.na", b"FSX_GEN General chat\r\n")]))

    assert db.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2
    (held,) = queue.list_held(db)
    assert held.file_name == "fsxnet.na"
    assert "not a packet" in held.reason


def test_a_remote_that_is_not_the_uplink_is_refused_before_anything_moves(db, setup):
    sysop, network, board = setup
    export_post_if_ftn(db, create_post(db, board, sysop, "Out", "Going out"), board)

    async def call(mailer, network):
        return await mailer.poll(network)

    status, hub_received = asyncio.run(_with_hub(db, network, call, address=FtnAddress(21, 4, 4)))

    assert status.failures == 1 and "not 21:1/100" in status.last_error
    assert hub_received == []
    assert queue.count_pending_outbound(db, network.id) == 1


def test_a_wrong_password_fails_the_call_and_keeps_the_mail(db, setup):
    sysop, network, board = setup
    export_post_if_ftn(db, create_post(db, board, sysop, "Out", "Going out"), board)

    async def call(mailer, network):
        return await mailer.poll(network)

    status, _ = asyncio.run(_with_hub(db, network, call, password="OTHER"))
    assert status.failures == 1
    assert queue.count_pending_outbound(db, network.id) == 1


def test_an_unreachable_hub_is_a_failure_not_a_crash(db, setup):
    _, network, _ = setup
    network = save_network(db, replace(network, uplink_port=1))

    async def run():
        lane = DatabaseLane(db.path)
        try:
            return await FtnMailer(lane).poll(network)
        finally:
            lane.close()

    status = asyncio.run(run())
    assert status.failures == 1 and status.last_error


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_when_a_call_is_due():
    clock = _Clock()
    mailer = FtnMailer(lane=None, clock=clock)
    network = FtnNetwork(name="n", domain="d", our_address=NODE, uplink_address=HUB, poll_minutes=60, id=1)

    assert mailer._due(network, 0, clock.now)  # never called
    mailer.status[1] = PollStatus(last_attempt=clock.now, last_success=clock.now)
    assert not mailer._due(network, 0, clock.now + 60)
    assert not mailer._due(network, 3, clock.now + 60)  # mail waits for MIN_CALL_GAP
    assert mailer._due(network, 3, clock.now + mailer_module.MIN_CALL_GAP)
    assert mailer._due(network, 0, clock.now + 3600)

    mailer.status[1] = PollStatus(last_attempt=clock.now, failures=3)
    assert not mailer._due(network, 0, clock.now + 200)
    assert mailer._due(network, 0, clock.now + 240)  # 1, 2, 4 minutes
    mailer.status[1] = PollStatus(last_attempt=clock.now, failures=20)
    assert mailer._due(network, 0, clock.now + 3600)  # never beyond the poll interval


def test_the_mailer_task_starts_checks_and_closes(db, setup):
    _, network, _ = setup
    save_network(db, replace(network, enabled=False))

    async def run():
        lane = DatabaseLane(db.path)
        mailer = FtnMailer(lane, check_interval=0.05)
        try:
            await mailer.start()
            await asyncio.sleep(0.2)
            assert mailer.status == {}  # disabled: never called
        finally:
            await mailer.close()
            lane.close()

    asyncio.run(run())
    assert get_network(db, network.id).enabled is False


def test_an_unexpected_error_is_a_failed_call_in_the_status(db, setup, monkeypatch):
    _, network, _ = setup

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    async def run():
        lane = DatabaseLane(db.path)
        mailer = FtnMailer(lane)
        monkeypatch.setattr(mailer, "_session", broken)
        try:
            return await mailer.poll(network)
        finally:
            lane.close()

    status = asyncio.run(run())
    assert status.failures == 1 and "boom" in status.last_error
