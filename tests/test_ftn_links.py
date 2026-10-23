"""Points and downlinks (design doc §6.8, "Points and downlinks"; issues
#1478 and #1479): the link records, routing to them, their sessions on a
real loopback listener and mailer with a real database lane, a point's
echomail going on to the uplink, transit netmail, and the bounds."""

from __future__ import annotations

import asyncio
import datetime
import socket
from dataclasses import replace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.config import set_config
from netbbs.ftn import listener as listener_module
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import BinkpError, OutgoingFile, SystemInfo, run_session
from netbbs.ftn.links import (
    DISABLED,
    DOWNLINK,
    ECHO,
    POINT,
    POLL_LINK,
    FtnLink,
    FtnLinkError,
    count_link_waiting,
    delete_link,
    enqueue_for_link_without_commit,
    fanout_reference,
    get_link,
    link_areas,
    link_for_destination,
    links_carrying,
    list_links,
    next_point_number,
    save_link,
    set_link_area_without_commit,
)
from netbbs.ftn.listener import FtnListener
from netbbs.ftn.mailer import FtnMailer
from netbbs.ftn.message import FtnMessage, decode_message, encode_message
from netbbs.ftn.netmail import flavour_refusal, netmail_refusal, send_netmail, sent_outcome
from netbbs.ftn.networks import FtnNetwork, save_network, set_board_area
from netbbs.ftn.outbound import direct_nodes_waiting, expire_held
from netbbs.ftn.packet import PacketHeader, build_packet, parse_packet
from netbbs.ftn.route import (
    append_via,
    merge_seen_by_path,
    passed_through,
    point_echomail_for_uplink,
    seen_by_and_path,
    via_count,
)
from netbbs.ftn.tosser import MAX_VIA_HOPS, toss_packet
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)
POINT_1 = FtnAddress(21, 1, 199, 1)
DOWN = FtnAddress(21, 1, 200)
FAR = FtnAddress(21, 3, 110)


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
    point = save_link(db, FtnLink(network_id=network.id, kind=POINT, address=POINT_1, session_password="PTPW",
                                  name="Pat"))
    # A link posts only in an echo it carries read-write (issue #1479).
    set_link_area_without_commit(db, point.id, ECHO, "FSX_GEN")
    db.connection.commit()
    return sysop, network, board, point


def _echomail(body: str, sender: FtnAddress, *, origin: FtnAddress | None = None, seen_by=(), path=()) -> FtnMessage:
    origin = origin or sender
    return FtnMessage(
        to_name="All", from_name="Pat Point", subject="s", body=body, area="FSX_GEN",
        date=datetime.datetime(2026, 10, 7, 9, 0), kludges=[("MSGID", f"{origin} 0000{len(body):04x}")],
        tear_line="pointer", origin=f"Pat's point ({origin})", seen_by=list(seen_by), path=list(path),
        orig_net=sender.net, orig_node=sender.node, dest_net=NODE.net, dest_node=NODE.node)


def _netmail(to: str, dest: FtnAddress, sender: FtnAddress, *, kludges=(), body="Hello there",
             msgid: str = "0000beef") -> FtnMessage:
    return FtnMessage(
        to_name=to, from_name="Pat Point", subject="Hi", body=body,
        date=datetime.datetime(2026, 10, 7, 9, 0),
        kludges=[*kludges, ("MSGID", f"{sender} {msgid}")],
        orig_net=sender.net, orig_node=sender.node, dest_net=dest.net, dest_node=dest.node)


def _packet(orig: FtnAddress, *messages: FtnMessage, dest: FtnAddress = NODE, password: str = "") -> bytes:
    return build_packet(PacketHeader(orig=orig, dest=dest, created=None, password=password),
                        [encode_message(m) for m in messages])


def _posts(db):
    return [row["body"] for row in db.connection.execute("SELECT body FROM posts ORDER BY id")]


def _decoded(row) -> FtnMessage:
    header = PacketHeader(orig=NODE, dest=HUB, created=None)
    from netbbs.ftn.packet import build_packet_from_packed

    return decode_message(parse_packet(build_packet_from_packed(header, [row.packed])).messages[0])


def _pending(db, network):
    return db.connection.execute(
        "SELECT * FROM ftn_outbound WHERE network_id = ? AND status = 'pending' ORDER BY id", (network.id,)
    ).fetchall()


# --- records ---------------------------------------------------------------------


def test_a_point_is_this_nodes_address_with_a_point_number(db, setup):
    _sysop, network, _board, point = setup
    assert point.address == FtnAddress(21, 1, 199, 1, "fsxnet")
    assert next_point_number(db, network) == 2
    for address, reason in (
        (FtnAddress(21, 1, 198, 1), "this BBS's with a point number"),
        (FtnAddress(21, 1, 199), "this BBS's with a point number"),
    ):
        with pytest.raises(FtnLinkError, match=reason):
            save_link(db, FtnLink(network_id=network.id, kind=POINT, address=address, session_password="x"))
    with pytest.raises(FtnLinkError, match="already a link"):
        save_link(db, FtnLink(network_id=network.id, kind=POINT, address=POINT_1, session_password="x"))


def test_a_link_needs_a_session_password_and_a_point_is_never_polled(db, setup):
    _sysop, network, _board, _point = setup
    with pytest.raises(FtnLinkError, match="needs a session password"):
        save_link(db, FtnLink(network_id=network.id, kind=POINT, address=FtnAddress(21, 1, 199, 2),
                              session_password=""))
    with pytest.raises(FtnLinkError, match="never polled"):
        save_link(db, FtnLink(network_id=network.id, kind=POINT, address=FtnAddress(21, 1, 199, 2),
                              session_password="x", flavour=POLL_LINK, host="pt.example"))
    with pytest.raises(FtnLinkError, match="needs the host"):
        save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=DOWN, session_password="x",
                              flavour=POLL_LINK))
    with pytest.raises(FtnLinkError, match="without a point number"):
        save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=FtnAddress(21, 1, 200, 3),
                              session_password="x"))
    with pytest.raises(FtnLinkError, match="uplink can't be a link"):
        save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=HUB, session_password="x"))
    down = save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=DOWN, session_password="x",
                                 flavour=POLL_LINK, host="[2001:db8::7]:24555"))
    assert (down.host, down.port, down.polled) == ("2001:db8::7", 24555, True)


def test_a_point_network_can_have_no_links(db):
    network = save_network(db, FtnNetwork(name="net", domain="net", our_address=FtnAddress(21, 1, 199, 5),
                                          uplink_address=NODE))
    with pytest.raises(FtnLinkError, match="itself a point"):
        save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=DOWN, session_password="x"))


def test_routing_finds_a_links_own_address_and_a_downlinks_points(db, setup):
    _sysop, network, _board, point = setup
    down = save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=DOWN, session_password="x"))
    assert link_for_destination(db, network.id, POINT_1).id == point.id
    assert link_for_destination(db, network.id, FtnAddress(21, 1, 200, 7)).id == down.id
    assert link_for_destination(db, network.id, "21:1/200").id == down.id
    assert link_for_destination(db, network.id, FtnAddress(21, 1, 199, 2)) is None
    assert link_for_destination(db, network.id, FAR) is None
    save_link(db, replace(down, status=DISABLED))
    assert link_for_destination(db, network.id, DOWN) is None  # a disabled link takes nothing


# --- netmail to a link -------------------------------------------------------------


def test_netmail_to_a_point_waits_in_its_own_outbound(db, setup):
    sysop, network, _board, point = setup
    send_netmail(db, sysop, "Pat (21:1/199.1)", "Hi", "For the point")
    (row,) = _pending(db, network)
    assert (row["link_id"], row["route"], row["destination"]) == (point.id, "direct", "21:1/199.1")
    # Never a direct call to this node itself: the mailer doesn't see it.
    assert direct_nodes_waiting(db, network) == []
    assert count_link_waiting(db, point.id) == (1, 0)
    assert sent_outcome(db, "Pat (21:1/199.1)", "normal", 0) == "Netmail queued: it waits for 21:1/199.1 to call this BBS."
    message = _decoded(queue.pending_outbound(db, network.id, route="direct")[0])
    assert message.kludge("TOPT") == "1"


def test_netmail_to_a_point_this_bbs_does_not_serve_is_refused(db, setup):
    sysop, _network, _board, _point = setup
    assert netmail_refusal(db, sysop, "Nobody (21:1/199.9)") == "This BBS has no point 21:1/199.9 on fsxNet."


def test_crash_to_a_downlink_needs_no_nodelist(db, setup):
    _sysop, network, _board, _point = setup
    assert flavour_refusal(db, "Op (21:1/200)", "crash") is not None
    save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=DOWN, session_password="x"))
    assert flavour_refusal(db, "Op (21:1/200)", "crash") is None


# --- sessions on the listener ------------------------------------------------------


async def _with_listener(db, test):
    lane = DatabaseLane(db.path)
    listener = FtnListener(lane)
    try:
        await listener.reconcile()
        return await test(listener)
    finally:
        await listener.close()
        lane.close()


async def _call(listener, *, address, password, files=()):
    host, port = listener.listening_on
    reader, writer = await asyncio.open_connection(host, port)
    try:
        return await run_session(reader, writer, originating=True, our_addresses=[address],
                                 system=SystemInfo("Point", "pat"), password=password, outgoing=list(files),
                                 timeout=5)
    finally:
        writer.close()


async def _settled(listener, calls: int = 1) -> None:
    for _ in range(500):
        if len(listener.recent) >= calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the listener never finished the call")


def test_a_point_proving_its_password_collects_its_mail_and_its_echomail_goes_on(db, setup):
    sysop, network, _board, point = setup
    send_netmail(db, sysop, "Pat (21:1/199.1)", "Hi", "Held for the point")
    # The point's software wrote SEEN-BY and PATH of its own: they go.
    echo = _echomail("From the point", POINT_1, seen_by=[(1, 199)], path=[(1, 199)])

    async def test(listener):
        result = await _call(listener, address=POINT_1, password="PTPW",
                             files=[OutgoingFile("pt.pkt", _packet(POINT_1, echo))])
        await _settled(listener)
        return result

    result = asyncio.run(_with_listener(db, test))
    assert result.secure
    (received,) = result.received
    packet = parse_packet(received.data)
    assert packet.header.dest == FtnAddress(21, 1, 199, 1)
    assert decode_message(packet.messages[0]).body == "Held for the point"
    assert _posts(db) == ["From the point"]
    (row,) = _pending(db, network)  # the point's echomail, on its way to the uplink
    assert (row["kind"], row["route"], row["link_id"], row["destination"]) == (
        "echomail", "uplink", None, "21:1/100@fsxnet")
    forwarded = _decoded(queue.pending_outbound(db, network.id)[0])
    assert forwarded.seen_by == [(1, 100), (1, 199)]
    assert forwarded.path == [(1, 199)]
    assert forwarded.origin == "Pat's point (21:1/199.1)"
    link = get_link(db, point.id)
    assert link.last_call_at and "called in" in link.last_call_outcome


def test_a_caller_claiming_a_point_without_its_password_gets_nothing(db, setup):
    sysop, network, _board, _point = setup
    send_netmail(db, sysop, "Pat (21:1/199.1)", "Hi", "Not for impostors")

    async def test(listener):
        with pytest.raises(BinkpError):
            await _call(listener, address=POINT_1, password="")
        with pytest.raises(BinkpError):
            await _call(listener, address=POINT_1, password="WRONG")
        await _settled(listener, 2)

    asyncio.run(_with_listener(db, test))
    assert len(_pending(db, network)) == 1  # still waiting for the real point


def test_held_mail_for_unproven_callers_never_includes_a_links(db, setup):
    # `give_held_unproven` is for nodelist nodes, never for points.
    sysop, network, _board, _point = setup
    save_network(db, replace(network, give_held_unproven=True))
    send_netmail(db, sysop, "Pat (21:1/199.1)", "Hi", "Held", flavour="hold")
    assert direct_nodes_waiting(db, network, hold_only=True) == []


def test_a_disabled_point_must_still_prove_itself_and_gets_nothing(db, setup):
    sysop, network, _board, point = setup
    send_netmail(db, sysop, "Pat (21:1/199.1)", "Hi", "Waits")
    save_link(db, replace(point, status=DISABLED))
    echo = _echomail("While switched off", POINT_1)

    async def test(listener):
        with pytest.raises(BinkpError):
            await _call(listener, address=POINT_1, password="")
        result = await _call(listener, address=POINT_1, password="PTPW",
                             files=[OutgoingFile("pt.pkt", _packet(POINT_1, echo))])
        await _settled(listener, 2)
        return result

    result = asyncio.run(_with_listener(db, test))
    assert result.secure and result.received == []
    assert _posts(db) == []
    (held,) = queue.list_held(db)
    assert "switched off" in held.reason


def test_a_points_echomail_must_be_written_at_the_point(db, setup):
    _sysop, network, _board, point = setup
    forged = _echomail("Someone else's", POINT_1, origin=FAR)
    result = toss_packet(db, network, _packet(POINT_1, forged), secure=True, remote_address=str(POINT_1),
                         file_name="x.pkt", link=point)
    assert (result.posts, result.held) == (0, 1)
    assert _pending(db, network) == []


def test_a_points_packet_password_is_its_own(db, setup):
    _sysop, network, _board, point = setup
    point = save_link(db, replace(point, packet_password="PKT"))
    data = _packet(POINT_1, _echomail("x", POINT_1), password="WRONG")
    result = toss_packet(db, network, data, secure=True, remote_address=str(POINT_1), file_name="x.pkt", link=point)
    assert result.refused_packet == "packet password does not match"
    data = _packet(POINT_1, _echomail("y", POINT_1), password="pkt")
    assert toss_packet(db, network, data, secure=True, remote_address=str(POINT_1), file_name="y.pkt",
                       link=point).posts == 1


def test_a_points_post_on_a_moderated_board_goes_on_once_approved(db, setup):
    sysop, network, _board, point = setup
    from netbbs.boards.posts import approve_post, get_post
    from netbbs.ftn.networks import clear_board_area
    from netbbs.ftn.scanner import export_post_if_ftn

    clear_board_area(db, _board)
    board = create_board(db, "moderated", creator=sysop, moderated=True)
    set_board_area(db, board, network.id, "FSX_GEN")
    result = toss_packet(db, network, _packet(POINT_1, _echomail("Needs a look", POINT_1)), secure=True,
                         remote_address=str(POINT_1), file_name="x.pkt", link=point)
    assert (result.posts, result.forwarded) == (1, 0)
    assert _pending(db, network) == []
    post_id = db.connection.execute("SELECT post_id FROM posts").fetchone()["post_id"]
    approved = approve_post(db, get_post(db, post_id), approved_by=sysop)
    assert export_post_if_ftn(db, approved, board)
    (row,) = _pending(db, network)
    assert row["route"] == "uplink"
    assert db.connection.execute("SELECT COUNT(*) FROM ftn_point_waiting").fetchone()[0] == 0


def test_echomail_from_the_uplink_is_never_sent_back(db, setup):
    _sysop, network, _board, point = setup
    echo = _echomail("From the hub", HUB)
    assert toss_packet(db, network, _packet(HUB, echo), secure=True, remote_address=str(HUB),
                       file_name="x.pkt").posts == 1
    # Only the point carrying the echo gets it (issue #1479); never the uplink.
    assert [row["link_id"] for row in _pending(db, network)] == [point.id]


# --- transit netmail -----------------------------------------------------------------


def test_a_points_netmail_for_another_node_goes_to_the_uplink_with_a_via_line(db, setup):
    _sysop, network, _board, point = setup
    message = _netmail("Far Person", FAR, NODE, kludges=[("INTL", "21:3/110 21:1/199"), ("FMPT", "1")])
    result = toss_packet(db, network, _packet(POINT_1, message), secure=True, remote_address=str(POINT_1),
                         file_name="x.pkt", link=point)
    assert (result.routed, result.not_for_us, result.netmail) == (1, 0, 0)
    (row,) = _pending(db, network)
    assert (row["route"], row["link_id"], row["destination"]) == ("uplink", None, "21:3/110")
    forwarded = _decoded(queue.pending_outbound(db, network.id)[0])
    assert forwarded.kludge("FMPT") == "1" and forwarded.kludge("INTL") == "21:3/110 21:1/199"
    assert forwarded.trailing_kludges[-1][0] == "Via"
    assert forwarded.trailing_kludges[-1][1].startswith("21:1/199@fsxnet @")


def test_a_point_that_writes_no_fmpt_or_intl_gets_them_added(db, setup):
    _sysop, network, _board, point = setup
    result = toss_packet(db, network, _packet(POINT_1, _netmail("Far Person", FAR, NODE)), secure=True,
                         remote_address=str(POINT_1), file_name="x.pkt", link=point)
    assert result.routed == 1
    forwarded = _decoded(queue.pending_outbound(db, network.id)[0])
    assert forwarded.kludge("FMPT") == "1"
    assert forwarded.kludge("INTL") == "21:3/110 21:1/199"


def test_a_points_netmail_must_come_from_the_point(db, setup):
    _sysop, network, _board, point = setup
    forged = _netmail("Far Person", FAR, NODE, kludges=[("INTL", "21:3/110 21:7/7")])
    result = toss_packet(db, network, _packet(POINT_1, forged), secure=True, remote_address=str(POINT_1),
                         file_name="x.pkt", link=point)
    assert (result.routed, result.held) == (0, 1)


def test_netmail_from_the_uplink_for_a_point_is_routed_to_it(db, setup):
    _sysop, network, _board, point = setup
    message = _netmail("Pat", NODE, FAR, kludges=[("INTL", "21:1/199 21:3/110"), ("TOPT", "1")])
    result = toss_packet(db, network, _packet(HUB, message), secure=True, remote_address=str(HUB), file_name="x.pkt")
    assert result.routed == 1
    (row,) = _pending(db, network)
    assert (row["link_id"], row["route"]) == (point.id, "direct")
    # The same netmail again is a duplicate, not a second copy.
    again = toss_packet(db, network, _packet(HUB, message), secure=True, remote_address=str(HUB), file_name="y.pkt")
    assert (again.routed, again.duplicates) == (0, 1)


def test_netmail_from_the_uplink_for_another_node_is_still_not_ours(db, setup):
    # A leaf stays a leaf: only mail for a link, or from one, is routed.
    _sysop, network, _board, _point = setup
    message = _netmail("Far Person", FAR, HUB, kludges=[("INTL", "21:3/110 21:1/100")])
    result = toss_packet(db, network, _packet(HUB, message), secure=True, remote_address=str(HUB), file_name="x.pkt")
    assert (result.routed, result.not_for_us) == (0, 1)
    assert _pending(db, network) == []


def test_netmail_for_a_downlinks_point_goes_to_the_downlink(db, setup):
    _sysop, network, _board, _point = setup
    down = save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=DOWN, session_password="x"))
    message = _netmail("Someone", DOWN, FAR, kludges=[("INTL", "21:1/200 21:3/110"), ("TOPT", "4")])
    assert toss_packet(db, network, _packet(HUB, message), secure=True, remote_address=str(HUB),
                       file_name="x.pkt").routed == 1
    (row,) = _pending(db, network)
    assert (row["link_id"], row["destination"]) == (down.id, "21:1/200.4")


def test_a_netmail_that_passed_here_before_or_too_often_is_held(db, setup):
    _sysop, network, _board, point = setup
    looped = _netmail("Pat", NODE, FAR, kludges=[("INTL", "21:1/199 21:3/110"), ("TOPT", "1")])
    data = _packet(HUB, looped)
    packet = parse_packet(data)
    seen_here = append_via(packet.messages[0], NODE, "Other 1.0")
    data = build_packet(packet.header, [seen_here])
    result = toss_packet(db, network, data, secure=True, remote_address=str(HUB), file_name="x.pkt")
    assert (result.routed, result.held) == (0, 1)
    assert "routing loop" in queue.list_held(db)[0].reason
    many = _netmail("Pat", NODE, FAR, kludges=[("INTL", "21:1/199 21:3/110"), ("TOPT", "1")], msgid="0000cafe")
    packet = parse_packet(_packet(HUB, many))
    hopped = packet.messages[0]
    for net in range(MAX_VIA_HOPS):
        hopped = append_via(hopped, FtnAddress(21, 50 + net, 1), "Other 1.0")
    result = toss_packet(db, network, build_packet(packet.header, [hopped]), secure=True, remote_address=str(HUB),
                         file_name="y.pkt")
    assert (result.routed, result.held) == (0, 1)


def test_netmail_from_a_point_to_a_local_user_is_delivered(db, setup):
    sysop, network, _board, point = setup
    message = _netmail("sysop", NODE, NODE, kludges=[("FMPT", "1")])
    result = toss_packet(db, network, _packet(POINT_1, message), secure=True, remote_address=str(POINT_1),
                         file_name="x.pkt", link=point)
    assert result.netmail == 1
    row = db.connection.execute("SELECT sender_label FROM mail_messages").fetchone()
    assert row["sender_label"] == "Pat Point (21:1/199.1)"


# --- a polled downlink ------------------------------------------------------------------


def test_the_mailer_calls_a_polled_downlink_under_its_password(db, setup):
    sysop, network, _board, _point = setup
    seen = {}

    async def run():
        async def handle(reader, writer):
            try:
                result = await run_session(
                    reader, writer, originating=False, our_addresses=[DOWN], system=SystemInfo("Down", "op"),
                    password_for=lambda addresses: "DOWNPW", outgoing_for=lambda addresses, secure: [], timeout=5)
                seen["received"] = result.received
                seen["secure"] = result.secure
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        down = save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=DOWN, session_password="DOWNPW",
                                     flavour=POLL_LINK, host="127.0.0.1", port=port))
        send_netmail(db, sysop, "Op (21:1/200)", "Hi", "For the downlink")
        assert "this BBS calls it shortly" in sent_outcome(db, "Op (21:1/200)", "normal", 0)
        lane = DatabaseLane(db.path)
        mailer = FtnMailer(lane)
        try:
            await mailer.deliver_links(network)
        finally:
            await mailer.close()
            lane.close()
            server.close()
            await server.wait_closed()
        return down

    down = asyncio.run(run())
    assert seen["secure"]
    (received,) = seen["received"]
    assert decode_message(parse_packet(received.data).messages[0]).body == "For the downlink"
    assert count_link_waiting(db, down.id) == (0, 0)
    assert get_link(db, down.id).last_call_outcome.startswith("called:")


# --- bounds and expiry ---------------------------------------------------------------------


def test_what_waits_for_a_link_is_bounded(db, setup, monkeypatch):
    _sysop, network, _board, point = setup
    monkeypatch.setattr(queue, "MAX_PENDING_PER_LINK", 2)
    for index in range(2):
        enqueue_for_link_without_commit(db, point, kind="netmail", reference_id=f"r{index}", packed=b"x")
    with pytest.raises(queue.FtnQueueFullError, match="21:1/199.1"):
        enqueue_for_link_without_commit(db, point, kind="netmail", reference_id="r3", packed=b"x")


def test_a_links_mail_it_never_collected_expires(db, setup):
    sysop, network, _board, point = setup
    send_netmail(db, sysop, "Pat (21:1/199.1)", "Hi", "Never collected")
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=31)
    assert expire_held(db, now=later) == 1
    assert count_link_waiting(db, point.id) == (0, 0)
    letters = [row["subject"] for row in db.connection.execute("SELECT subject FROM mail_messages ORDER BY id")]
    assert letters[-1].startswith("Netmail not delivered")


def test_deleting_a_link_drops_its_mail_and_tells_the_sender(db, setup):
    sysop, network, _board, point = setup
    send_netmail(db, sysop, "Pat (21:1/199.1)", "Hi", "Orphaned")
    assert delete_link(db, point.id) == 1
    assert _pending(db, network) == []
    assert list_links(db, network.id) == []


# --- the extension points for the hub's part 2 ------------------------------------------------


def test_link_areas_say_who_a_fanout_sends_to(db, setup):
    _sysop, network, _board, point = setup
    down = save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=DOWN, session_password="x"))
    set_link_area_without_commit(db, point.id, ECHO, "fsx_gen")
    set_link_area_without_commit(db, down.id, ECHO, "FSX_GEN", mode="read")
    db.connection.commit()
    assert [a.tag for a in link_areas(db, point.id)] == ["FSX_GEN"]
    assert [link.id for link in links_carrying(db, network.id, ECHO, "fsx_gen")] == [point.id, down.id]
    save_link(db, replace(down, status="paused"))
    assert [link.id for link in links_carrying(db, network.id, ECHO, "FSX_GEN")] == [point.id]
    assert fanout_reference("post1", point) == f"post1>{point.id}"


# --- the byte work ---------------------------------------------------------------------------------


def test_a_points_echomail_leaves_with_this_nodes_seen_by_and_path():
    message = encode_message(_echomail("Body\nSEEN-BY: 9/9 quoted in the body", POINT_1,
                                       seen_by=[(1, 199), (5, 5)], path=[(1, 199)]))
    forwarded = point_echomail_for_uplink(message, NODE, HUB)
    assert seen_by_and_path(forwarded.text) == ([(1, 100), (1, 199)], [(1, 199)])
    assert (forwarded.orig_net, forwarded.orig_node, forwarded.dest_net, forwarded.dest_node) == (1, 199, 1, 100)
    assert b"SEEN-BY: 9/9 quoted in the body" in forwarded.text
    decoded = decode_message(forwarded)
    assert decoded.body.endswith("quoted in the body") and decoded.origin == "Pat's point (21:1/199.1)"


def test_a_downlinks_copy_merges_seen_by_and_extends_path():
    message = encode_message(_echomail("Body", HUB, seen_by=[(1, 100), (2, 2)], path=[(1, 100)]))
    forwarded = merge_seen_by_path(message, NODE, DOWN, [(1, 200)])
    assert seen_by_and_path(forwarded.text) == ([(1, 100), (1, 199), (1, 200), (2, 2)], [(1, 100), (1, 199)])


def test_via_lines_tell_a_loop_and_count_hops():
    message = encode_message(_netmail("x", FAR, NODE))
    once = append_via(message, FtnAddress(21, 4, 4), "Other")
    assert via_count(once.text) == 1 and not passed_through(once.text, NODE)
    twice = append_via(once, NODE, "NetBBS")
    assert passed_through(twice.text, NODE)
    assert passed_through(twice.text, FtnAddress(21, 1, 199, domain="fsxnet"))


# --- the console ----------------------------------------------------------------------------------


def test_the_links_screen_adds_the_next_point_and_needs_its_password(db, setup):
    from netbbs.moderation.log import list_recent_actions
    from netbbs.net.ftn_console import ftn_networks_screen
    from tests.test_admin_flow import FakeSession, _visible, _written_text

    _sysop, network, _board, _point = setup
    sysop = _sysop
    lane = DatabaseLane(db.path)
    try:
        # l: Links on the network; c: create (point .2); s: save is refused
        # without a password; 05: session password; s: save; back out.
        session = FakeSession(["l", "1", "c", "s", "0", "5", "NEWPW", "s", "b", "b", "b"])
        asyncio.run(ftn_networks_screen(session, lane, sysop))
    finally:
        lane.close()
    text = _visible(_written_text(session))
    assert "A link needs a session password" in text
    assert "Saved 21:1/199.2" in text
    second = [link for link in list_links(db, network.id) if link.address.point == 2]
    assert len(second) == 1 and second[0].session_password == "NEWPW" and second[0].kind == POINT
    assert any(a.action == "set_ftn_link" for a in list_recent_actions(db, limit=10))


def test_the_links_screen_deletes_a_link_after_asking(db, setup):
    from netbbs.net.ftn_links_console import ftn_links_screen
    from tests.test_admin_flow import FakeSession, _visible, _written_text

    sysop, network, _board, point = setup
    send_netmail(db, sysop, "Pat (21:1/199.1)", "Hi", "Dropped with it")
    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["d", "1", "y", "b"])
        asyncio.run(ftn_links_screen(session, lane, sysop, network))
    finally:
        lane.close()
    text = _visible(_written_text(session))
    assert "1 messages and 0 files waiting for it are dropped" in text
    assert list_links(db, network.id) == []


def test_the_status_screen_counts_links(db, setup):
    from netbbs.net.ftn_links_console import links_summary

    sysop, network, _board, _point = setup
    send_netmail(db, sysop, "Pat (21:1/199.1)", "Hi", "Waiting")
    assert links_summary(db, network).startswith("1 points, 0 downlinks; 1 messages waiting")


def test_the_retry_gap_never_overflows_after_weeks_of_failures():
    from netbbs.ftn.mailer import MIN_CALL_GAP, retry_after

    assert [retry_after(n, 3600.0) for n in (1, 2, 3)] == [60.0, 120.0, 240.0]
    assert retry_after(5000, MIN_CALL_GAP) == MIN_CALL_GAP
    assert retry_after(5000, 24 * 3600.0) == 24 * 3600.0


# --- file requests from and to a link (issue #1475 with #1478) ------------------------------------


def _offer_and_answer(db, network, sysop):
    from netbbs.files.areas import create_file_area
    from netbbs.files.entries import upload_file
    from netbbs.ftn import freq

    area = create_file_area(db, "uploads", creator=sysop)
    upload_file(db, area, sysop, "README.ZIP", b"read me")
    freq.set_area_offered(db, area, True)
    freq.save_settings_without_commit(db, freq.FreqSettings(network.id, answer=True))
    db.connection.commit()


async def _with_freq_listener(db, test):
    from netbbs.ftn.freq import FreqAnswerer

    lane = DatabaseLane(db.path)
    listener = FtnListener(lane, batch_hook=FreqAnswerer(lane))
    try:
        await listener.reconcile()
        return await test(listener)
    finally:
        await listener.close()
        lane.close()


def test_a_points_file_request_is_answered_in_its_proven_session(db, setup):
    from netbbs.ftn import freq

    sysop, network, _board, _point = setup
    _offer_and_answer(db, network, sysop)

    async def test(listener):
        result = await _call(listener, address=POINT_1, password="PTPW",
                             files=[OutgoingFile("00010001.REQ", b"README.ZIP\r\n")])
        await _settled(listener)
        return result

    result = asyncio.run(_with_freq_listener(db, test))
    assert result.secure
    assert [(r.name, r.data) for r in result.received] == [("README.ZIP", b"read me")]
    (record,) = [r for r in freq.recent_requests(db, network.id) if r.direction == "in"]
    assert record.secure


def test_a_disabled_point_gets_no_file_request_answer(db, setup):
    sysop, network, _board, point = setup
    _offer_and_answer(db, network, sysop)
    save_link(db, replace(point, status=DISABLED))

    async def test(listener):
        result = await _call(listener, address=POINT_1, password="PTPW",
                             files=[OutgoingFile("00010001.REQ", b"README.ZIP\r\n")])
        await _settled(listener)
        return result

    result = asyncio.run(_with_freq_listener(db, test))
    assert result.secure and result.received == []


def test_a_file_request_to_a_point_waits_for_its_session(db, setup):
    from netbbs.ftn import freq

    _sysop, network, _board, point = setup
    freq.queue_request(db, network, POINT_1, freq.request_lines_from_text("FILES"))
    row = db.connection.execute("SELECT link_id, route, flavour FROM ftn_outbound_files").fetchone()
    assert (row["link_id"], row["route"]) == (point.id, "direct")
    assert direct_nodes_waiting(db, network) == []
