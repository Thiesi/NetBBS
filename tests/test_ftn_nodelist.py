"""Nodelists and direct netmail (design doc §6.8, Decision 5; issue #1135
slice 6): parsing FTS-5000 with FTS-5004's BinkP flags, importing, routing
at send time, and the mailer's direct calls on a real loopback socket."""

from __future__ import annotations

import asyncio
import socket

import pytest

from netbbs.auth.users import create_user
from netbbs.ftn import mailer as mailer_module
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import SystemInfo, run_session
from netbbs.ftn.mailer import FtnMailer
from netbbs.ftn.netmail import send_netmail
from netbbs.ftn.networks import FtnNetwork, get_network, save_network
from netbbs.ftn.nodelist import NodelistError, direct_route, import_nodelist, parse_nodelist
from netbbs.ftn.packet import parse_packet
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)

NODELIST = """\
;A fsxNet nodelist excerpt
Zone,21,fsxNet_Zone_21,New_Zealand,Paul_Hayton,-Unpublished-,300,CM,IBN,INA:zone.example
Host,1,Net_1,NZ,Paul,-Unpublished-,300,CM,IBN:hub.example
,100,The_Hub,NZ,Paul,-Unpublished-,300,CM,IBN:hub.example:24555
,110,Plain_IBN,NZ,Sys,-Unpublished-,300,IBN,INA:plain.example
,111,Port_Only,NZ,Sys,-Unpublished-,300,INA:port.example,IBN:24600
,112,No_Host,NZ,Sys,-Unpublished-,300,IBN
,113,Telnet_Only,NZ,Sys,-Unpublished-,300,ITN:bbs.example
Hub,120,A_Hub,NZ,Sys,-Unpublished-,300,IBN:hub120.example
Pvt,130,Private,NZ,Sys,-Unpublished-,300,IBN:pvt.example
Hold,140,Held,NZ,Sys,-Unpublished-,300,IBN:held.example
Down,150,Down,NZ,Sys,-Unpublished-,300,IBN:down.example
,160,Bad_Host,NZ,Sys,-Unpublished-,300,IBN:bad host
Region,3,Region_3,AU,Sys,-Unpublished-,300,IBN:region3.example
,110,Three,AU,Sys,-Unpublished-,300,IBN:three.example
\x1a,999,After_EOF,X,Y,-,300,IBN:never.example
"""


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def network(db):
    return save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB, uplink_host="127.0.0.1",
        uplink_port=1, enabled=True, netmail_min_level=0))


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2pw", user_level=10)


def test_the_nodelist_parses_into_nodes_and_their_binkp_hosts():
    entries = {(e.zone, e.net, e.node): (e.name, e.binkp_host, e.binkp_port) for e in parse_nodelist(NODELIST)}
    assert entries[(21, 21, 0)] == ("fsxNet Zone 21", "zone.example", 24554)
    assert entries[(21, 1, 100)] == ("The Hub", "hub.example", 24555)
    assert entries[(21, 1, 110)][1:] == ("plain.example", 24554)
    assert entries[(21, 1, 111)][1:] == ("port.example", 24600)
    assert entries[(21, 1, 112)][1:] == (None, None)
    assert entries[(21, 1, 113)][1:] == (None, None)
    assert entries[(21, 1, 120)][1] == "hub120.example"
    assert entries[(21, 1, 130)][1] == "pvt.example"
    assert (21, 1, 140) not in entries and (21, 1, 150) not in entries
    assert entries[(21, 1, 160)][1:] == (None, None)
    assert entries[(21, 3, 110)][1] == "three.example"
    assert not any(net == 999 or node == 999 for _, net, node in entries)


def test_a_node_before_any_zone_is_refused():
    with pytest.raises(NodelistError, match="before any Zone"):
        parse_nodelist(",1,Orphan,X,Y,-,300,IBN:x.example\n")


def test_import_replaces_the_whole_list(db, network):
    assert import_nodelist(db, network.id, NODELIST) == 12
    assert get_network(db, network.id) is not None
    assert direct_route(db, network.id, FtnAddress(21, 1, 110)) == (FtnAddress(21, 1, 110), "plain.example", 24554)
    import_nodelist(db, network.id, "Zone,21,Z,X,Y,-,300\n,5,Only,X,Y,-,300,IBN:only.example\n")
    assert direct_route(db, network.id, FtnAddress(21, 1, 110)) is None
    assert direct_route(db, network.id, FtnAddress(21, 21, 5))[1] == "only.example"


def test_a_list_with_no_nodes_changes_nothing(db, network):
    import_nodelist(db, network.id, NODELIST)
    with pytest.raises(NodelistError, match="no nodes"):
        import_nodelist(db, network.id, "; just a comment\n")
    assert direct_route(db, network.id, FtnAddress(21, 1, 110)) is not None


def test_a_point_is_reached_through_its_boss(db, network):
    import_nodelist(db, network.id, NODELIST)
    assert direct_route(db, network.id, FtnAddress(21, 1, 110, 7))[0] == FtnAddress(21, 1, 110)
    assert direct_route(db, network.id, FtnAddress(21, 1, 112)) is None  # IBN, but no host


def test_netmail_is_routed_direct_only_where_the_nodelist_says(db, network, alice):
    import_nodelist(db, network.id, NODELIST)
    send_netmail(db, alice, "Joe (21:1/110)", "s", "direct")
    send_netmail(db, alice, "Ann (21:1/112)", "s", "no host")
    send_netmail(db, alice, "Hub Op (21:1/100)", "s", "the uplink itself")
    send_netmail(db, alice, "Nobody (21:7/7)", "s", "unlisted")
    routes = {m.destination: m.route for m in queue.pending_outbound(db, network.id)}
    routes.update({m.destination: m.route for m in queue.pending_outbound(db, network.id, route="direct")})
    assert routes == {"21:1/110": "direct", "21:1/112": "uplink", "21:1/100": "uplink", "21:7/7": "uplink"}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_mailer_calls_the_node_itself(db, network, alice):
    received = []

    async def run():
        async def handle(reader, writer):
            try:
                result = await run_session(reader, writer, originating=False, our_addresses=[FtnAddress(21, 1, 110)],
                                           system=SystemInfo("Joe's BBS", "joe"), password_for=lambda a: None,
                                           timeout=5)
                received.extend(result.received)
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        import_nodelist(db, network.id, f"Zone,21,Z,X,Y,-,300\nHost,1,N,X,Y,-,300\n,110,Joe,X,Y,-,300,IBN:127.0.0.1:{port}\n")
        send_netmail(db, alice, "Joe (21:1/110)", "Direct", "Straight to you")
        lane = DatabaseLane(db.path)
        try:
            await FtnMailer(lane).deliver_direct(network)
        finally:
            lane.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())
    (packet_file,) = received
    packet = parse_packet(packet_file.data)
    assert packet.header.dest == FtnAddress(21, 1, 110)
    assert queue.count_pending_outbound(db, network.id) == 0


def test_after_three_failed_direct_calls_netmail_goes_via_the_uplink(db, network, alice):
    import_nodelist(db, network.id, f"Zone,21,Z,X,Y,-,300\nHost,1,N,X,Y,-,300\n,110,Joe,X,Y,-,300,IBN:127.0.0.1:{_free_port()}\n")
    send_netmail(db, alice, "Joe (21:1/110)", "Direct", "Nobody answers")

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()

    async def run():
        lane = DatabaseLane(db.path)
        mailer = FtnMailer(lane, clock=clock)
        try:
            for _ in range(mailer_module.DIRECT_ATTEMPTS):
                assert queue.count_pending_outbound(db, network.id, route="uplink") == 0
                await mailer.deliver_direct(network)
                clock.now += mailer_module.MIN_CALL_GAP
        finally:
            lane.close()

    asyncio.run(run())
    assert queue.count_pending_outbound(db, network.id, route="uplink") == 1
    assert queue.count_pending_outbound(db, network.id, route="direct") == 0


def test_netmail_whose_node_left_the_nodelist_goes_via_the_uplink(db, network, alice):
    import_nodelist(db, network.id, NODELIST)
    send_netmail(db, alice, "Joe (21:1/110)", "s", "b")
    import_nodelist(db, network.id, "Zone,21,Z,X,Y,-,300\n,5,Only,X,Y,-,300\n")

    async def run():
        lane = DatabaseLane(db.path)
        try:
            await FtnMailer(lane).deliver_direct(network)
        finally:
            lane.close()

    asyncio.run(run())
    assert queue.count_pending_outbound(db, network.id, route="uplink") == 1
