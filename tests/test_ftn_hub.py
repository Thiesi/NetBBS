"""The hub's second part (design doc §6.8, "Points and downlinks"; issue
#1479): echomail fan-out with SEEN-BY and PATH, the AreaFix/FileFix server,
TIC forwarding, files attached to transit netmail, the console's areas and
passwords, and a message travelling each way through a hub with an uplink, a
downlink and a point over loopback BinkP."""

from __future__ import annotations

import asyncio
import datetime
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import approve_post, create_post, get_post
from netbbs.config import set_config
from netbbs.files.areas import create_file_area
from netbbs.files.entries import approve_file, get_file, upload_file
from netbbs.ftn import fileecho
from netbbs.ftn import listener as listener_module
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.attach import claim_received
from netbbs.ftn.binkp import OutgoingFile, SystemInfo, run_session
from netbbs.ftn.hatch import hatch_file
from netbbs.ftn.links import (
    DOWNLINK,
    ECHO,
    FILE_ECHO,
    PAUSED,
    POINT,
    FtnLink,
    get_link,
    link_areas,
    remove_link_area_without_commit,
    save_link,
    set_link_area_without_commit,
)
from netbbs.ftn.listener import FtnListener
from netbbs.ftn.mailer import FtnMailer
from netbbs.ftn.message import FtnMessage, decode_message, encode_message
from netbbs.ftn.networks import FtnNetwork, clear_board_area, save_network, set_board_area, set_file_area_tic
from netbbs.ftn.packet import ATTR_FILE_ATTACHED, PacketHeader, build_packet, build_packet_from_packed, parse_packet
from netbbs.ftn.robot_server import set_offered
from netbbs.ftn.scanner import export_post_if_ftn
from netbbs.ftn.tic import crc32, parse_tic
from netbbs.ftn.tosser import TossResult, toss_packet
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)
POINT_1 = FtnAddress(21, 1, 199, 1)
DOWN = FtnAddress(21, 1, 200)
DOWN_2 = FtnAddress(21, 1, 201)
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
def hub(db):
    """A node with an uplink, a point and a downlink, both carrying FSX_GEN."""
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    network = save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB, uplink_host="127.0.0.1",
        session_password="SECRET", tic_password="TICPW", enabled=True, answers_calls=True))
    board = create_board(db, "general", creator=sysop)
    set_board_area(db, board, network.id, "FSX_GEN")
    point = save_link(db, FtnLink(network_id=network.id, kind=POINT, address=POINT_1, session_password="PTPW",
                                  name="Pat", areafix_password="PTFIX"))
    down = save_link(db, FtnLink(network_id=network.id, kind=DOWNLINK, address=DOWN, session_password="DNPW",
                                 name="Dana", areafix_password="DNFIX", tic_password="DNTIC"))
    for link in (point, down):
        set_link_area_without_commit(db, link.id, ECHO, "FSX_GEN")
    db.connection.commit()
    return sysop, network, board, point, down


def _echomail(body: str, sender: FtnAddress, *, origin: FtnAddress | None = None, seen_by=(), path=(),
              area: str = "FSX_GEN", serial: int | None = None) -> FtnMessage:
    origin = origin or sender
    return FtnMessage(
        to_name="All", from_name="Someone", subject="s", body=body, area=area,
        date=datetime.datetime(2026, 10, 7, 9, 0),
        kludges=[("MSGID", f"{origin} {serial if serial is not None else len(body) + 0x1000:08x}")],
        tear_line="tosser", origin=f"Somewhere ({origin})", seen_by=list(seen_by), path=list(path),
        orig_net=sender.net, orig_node=sender.node, dest_net=NODE.net, dest_node=NODE.node)


def _packet(orig: FtnAddress, *messages: FtnMessage, dest: FtnAddress = NODE) -> bytes:
    return build_packet(PacketHeader(orig=orig, dest=dest, created=None), [encode_message(m) for m in messages])


def _pending(db, network):
    return db.connection.execute(
        "SELECT * FROM ftn_outbound WHERE network_id = ? AND status = 'pending' ORDER BY id", (network.id,)
    ).fetchall()


def _decode(packed: bytes) -> FtnMessage:
    data = build_packet_from_packed(PacketHeader(orig=NODE, dest=HUB, created=None), [bytes(packed)])
    return decode_message(parse_packet(data).messages[0])


def _by_link(db, network) -> dict:
    """The pending echomail per destination: a link's id, or "uplink"."""
    found = {}
    for row in _pending(db, network):
        if row["kind"] == "echomail":
            found[row["link_id"] if row["link_id"] is not None else "uplink"] = _decode(row["packed"])
    return found


def _toss(db, network, data, *, link=None, remote=None):
    remote = remote or (str(link.address) if link is not None else str(HUB))
    return toss_packet(db, network, data, secure=True, remote_address=remote, file_name="x.pkt", link=link)


# --- echomail fan-out ---------------------------------------------------------------------


def test_uplink_echomail_reaches_the_point_and_the_downlink_with_seen_by_and_path(db, hub):
    _sysop, network, _board, point, down = hub
    echo = _echomail("From the hub", HUB, seen_by=[(1, 100), (1, 199)], path=[(1, 100)])
    result = _toss(db, network, _packet(HUB, echo))
    assert (result.posts, result.forwarded) == (1, 1)
    copies = _by_link(db, network)
    assert set(copies) == {point.id, down.id}  # never back to the uplink
    for copy in copies.values():
        # This node and the downlink join SEEN-BY; the point never does.
        assert copy.seen_by == [(1, 100), (1, 199), (1, 200)]
        assert copy.path == [(1, 100), (1, 199)]
        assert copy.body == "From the hub" and copy.origin == "Somewhere (21:1/100)"
    row = next(r for r in _pending(db, network) if r["link_id"] == down.id)
    assert (row["route"], row["destination"]) == ("direct", "21:1/200")


def test_a_downlink_in_seen_by_and_a_paused_link_get_nothing(db, hub):
    _sysop, network, _board, point, down = hub
    echo = _echomail("Seen by the downlink", HUB, seen_by=[(1, 100), (1, 199), (1, 200)], path=[(1, 100)])
    _toss(db, network, _packet(HUB, echo))
    assert set(_by_link(db, network)) == {point.id}
    save_link(db, replace(get_link(db, point.id), status=PAUSED))
    _toss(db, network, _packet(HUB, _echomail("While paused", HUB, seen_by=[(1, 100)], path=[(1, 100)])))
    assert [r["link_id"] for r in _pending(db, network)] == [point.id, down.id]  # only the first copy, and down's


def test_a_downlinks_echomail_goes_to_the_uplink_and_the_point_never_back(db, hub):
    _sysop, network, _board, point, down = hub
    echo = _echomail("From the downlink", DOWN, seen_by=[(1, 199), (1, 200)], path=[(1, 200)])
    result = _toss(db, network, _packet(DOWN, echo), link=down)
    assert (result.posts, result.forwarded) == (1, 1)
    copies = _by_link(db, network)
    assert set(copies) == {"uplink", point.id}
    for copy in copies.values():
        assert copy.seen_by == [(1, 100), (1, 199), (1, 200)]
        assert copy.path == [(1, 200), (1, 199)]
    uplink_row = next(r for r in _pending(db, network) if r["link_id"] is None)
    assert (uplink_row["route"], uplink_row["destination"]) == ("uplink", "21:1/100@fsxnet")


def test_a_points_echomail_goes_on_as_this_nodes_own(db, hub):
    _sysop, network, _board, point, down = hub
    echo = _echomail("From the point", POINT_1, seen_by=[(1, 199)], path=[(1, 199)])
    _toss(db, network, _packet(POINT_1, echo), link=point)
    copies = _by_link(db, network)
    assert set(copies) == {"uplink", down.id}
    for copy in copies.values():
        assert copy.seen_by == [(1, 100), (1, 199), (1, 200)]
        assert copy.path == [(1, 199)]
        assert copy.origin == "Somewhere (21:1/199.1)"


def test_a_message_whose_path_names_this_node_goes_nowhere(db, hub):
    _sysop, network, _board, _point, down = hub
    echo = _echomail("Looped", DOWN, seen_by=[(1, 200)], path=[(1, 100), (1, 199), (1, 200)])
    result = _toss(db, network, _packet(DOWN, echo), link=down)
    assert (result.posts, result.forwarded) == (1, 0)
    assert _pending(db, network) == []


def test_a_link_posts_only_in_an_echo_it_carries_read_write(db, hub):
    _sysop, network, _board, _point, down = hub
    set_link_area_without_commit(db, down.id, ECHO, "FSX_GEN", "read")
    db.connection.commit()
    result = _toss(db, network, _packet(DOWN, _echomail("Read-only", DOWN)), link=down)
    assert (result.posts, result.held) == (0, 1)
    assert "read-only" in queue.list_held(db)[0].reason
    remove_link_area_without_commit(db, down.id, ECHO, "FSX_GEN")
    db.connection.commit()
    result = _toss(db, network, _packet(DOWN, _echomail("Not carried", DOWN)), link=down)
    assert (result.posts, result.held) == (0, 1)
    assert any("does not carry FSX_GEN" in item.reason for item in queue.list_held(db))
    assert _pending(db, network) == []


def test_a_duplicate_is_never_sent_on_again(db, hub):
    _sysop, network, _board, _point, _down = hub
    echo = _echomail("Once", HUB, seen_by=[(1, 100)], path=[(1, 100)])
    _toss(db, network, _packet(HUB, echo))
    result = _toss(db, network, _packet(HUB, echo))
    assert (result.duplicates, result.forwarded) == (1, 0)
    assert len(_pending(db, network)) == 2


def test_a_downlinks_post_on_a_moderated_board_goes_on_once_approved(db, hub):
    sysop, network, board, point, down = hub
    clear_board_area(db, board)
    moderated = create_board(db, "moderated", creator=sysop, moderated=True)
    set_board_area(db, moderated, network.id, "FSX_GEN")
    result = _toss(db, network, _packet(DOWN, _echomail("Needs a look", DOWN, path=[(1, 200)])), link=down)
    assert (result.posts, result.forwarded) == (1, 0)
    assert _pending(db, network) == []
    post_id = db.connection.execute("SELECT post_id FROM posts").fetchone()["post_id"]
    approved = approve_post(db, get_post(db, post_id), approved_by=sysop)
    assert export_post_if_ftn(db, approved, moderated)
    assert set(_by_link(db, network)) == {"uplink", point.id}
    assert db.connection.execute("SELECT COUNT(*) FROM ftn_point_waiting").fetchone()[0] == 0


def test_a_local_post_goes_to_the_uplink_and_every_link_with_one_seen_by(db, hub):
    sysop, network, board, point, down = hub
    assert export_post_if_ftn(db, create_post(db, board, sysop, "Hello", "Written here"), board)
    copies = _by_link(db, network)
    assert set(copies) == {"uplink", point.id, down.id}
    msgids = {copy.msgid for copy in copies.values()}
    assert len(msgids) == 1
    for copy in copies.values():
        assert copy.seen_by == [(1, 100), (1, 199), (1, 200)]
        assert copy.path == [(1, 199)]


# --- the AreaFix/FileFix server --------------------------------------------------------------


def _robot(to: str, sender: FtnAddress, subject: str, body: str, *, serial: int = 1) -> FtnMessage:
    return FtnMessage(
        to_name=to, from_name="Dana Down", subject=subject, body=body, date=datetime.datetime(2026, 10, 7, 9, 0),
        kludges=[("MSGID", f"{sender} {serial:08x}")],
        orig_net=sender.net, orig_node=sender.node, dest_net=NODE.net, dest_node=NODE.node)


def _replies(db, network) -> list[str]:
    return [_decode(row["packed"]).body for row in _pending(db, network) if row["kind"] == "netmail"]


@pytest.fixture
def offered(db, hub):
    sysop, network, _board, _point, down = hub
    second = create_board(db, "test", creator=sysop)
    set_board_area(db, second, network.id, "FSX_TEST")
    third = create_board(db, "secret", creator=sysop)
    set_board_area(db, third, network.id, "FSX_ADMIN")
    set_offered(db, network.id, ECHO, "FSX_GEN", True)
    set_offered(db, network.id, ECHO, "FSX_TEST", True)
    remove_link_area_without_commit(db, down.id, ECHO, "FSX_GEN")
    db.connection.commit()
    return hub


def test_areafix_links_offered_echoes_and_answers_by_netmail(db, offered):
    _sysop, network, _board, _point, down = offered
    request = _robot("areafix", DOWN, "dnfix", "+FSX_GEN\nFSX_TEST\n+FSX_ADMIN\n%QUERY\n--- tosser")
    result = _toss(db, network, _packet(DOWN, request), link=down)
    assert (result.robot, result.netmail) == (1, 0)
    assert [area.tag for area in link_areas(db, down.id, ECHO)] == ["FSX_GEN", "FSX_TEST"]
    (row,) = [r for r in _pending(db, network) if r["kind"] == "netmail"]
    assert row["link_id"] == down.id
    reply = _decode(row["packed"])
    assert (reply.from_name, reply.to_name, reply.subject) == ("AreaFix", "Dana Down", "AreaFix reply")
    assert "FSX_GEN: linked." in reply.body and "FSX_TEST: linked." in reply.body
    assert "FSX_ADMIN: no echo by that name is offered to you here." in reply.body
    assert reply.reply == "21:1/200 00000001"


def test_a_robot_request_with_a_wrong_password_does_nothing(db, offered):
    _sysop, network, _board, _point, down = offered
    _toss(db, network, _packet(DOWN, _robot("AreaFix", DOWN, "WRONG", "+FSX_GEN")), link=down)
    assert link_areas(db, down.id, ECHO) == []
    (reply,) = _replies(db, network)
    assert "not your AreaFix password" in reply
    save_link(db, replace(get_link(db, down.id), areafix_password=""))
    _toss(db, network, _packet(DOWN, _robot("AreaFix", DOWN, "", "+FSX_GEN", serial=2)), link=get_link(db, down.id))
    assert "No AreaFix password is set" in _replies(db, network)[1]
    assert link_areas(db, down.id, ECHO) == []


def test_areafix_lists_pauses_resumes_and_changes_its_password(db, offered):
    _sysop, network, _board, _point, down = offered
    set_link_area_without_commit(db, down.id, ECHO, "FSX_GEN", "read")
    db.connection.commit()
    body = "%LIST\n%UNLINKED\n%HELP\n%PAUSE\n+FSX_GEN\n%PWD NEWFIX\n%BOGUS"
    _toss(db, network, _packet(DOWN, _robot("AreaFix", DOWN, "DNFIX", body)), link=down)
    (reply,) = _replies(db, network)
    assert "* FSX_GEN" in reply and "  FSX_TEST" in reply and "FSX_ADMIN" not in reply
    assert "%RESCAN [TAG] [n]" in reply and "Paused" in reply
    assert "FSX_GEN: you carry it already." in reply and "%BOGUS is not a command" in reply
    link = get_link(db, down.id)
    assert link.status == PAUSED
    # AreaFix's password changed; FileFix had none, so it keeps the old one.
    assert (link.areafix_password, link.filefix_password) == ("NEWFIX", "DNFIX")
    assert link_areas(db, down.id, ECHO)[0].mode == "read"  # a read-only area stays so
    _toss(db, network, _packet(DOWN, _robot("AreaFix", DOWN, "NEWFIX", "%RESUME", serial=2)),
          link=get_link(db, down.id))
    assert get_link(db, down.id).status == "active"


def test_areafix_rescan_sends_the_newest_messages_again(db, offered):
    sysop, network, board, _point, down = offered
    for index in range(3):
        _toss(db, network, _packet(HUB, _echomail(f"Old {index}", HUB, serial=index + 10)))
    export_post_if_ftn(db, create_post(db, board, sysop, "Local", "Written here"), board)
    db.connection.execute("UPDATE ftn_outbound SET status = 'sent'")
    db.connection.commit()
    request = _robot("AreaFix", DOWN, "DNFIX", "+FSX_GEN\n%RESCAN 2")
    _toss(db, network, _packet(DOWN, request), link=down)
    rescanned = [_decode(r["packed"]) for r in _pending(db, network) if r["kind"] == "echomail"]
    assert [m.body for m in rescanned] == ["Old 2", "Written here"]
    assert all(m.kludge("RESCANNED") == "21:1/199" for m in rescanned)
    assert rescanned[0].origin == "rescanned at 21:1/199 (21:1/100)" and rescanned[0].msgid == "21:1/100 0000000c"
    assert all(m.seen_by == [(1, 199), (1, 200)] for m in rescanned)
    assert "%RESCAN FSX_GEN: 2 messages sent again." in _replies(db, network)[0]


def test_a_robot_request_from_anyone_but_a_link_goes_to_the_sysop(db, offered):
    _sysop, network, _board, _point, down = offered
    result = _toss(db, network, _packet(HUB, _robot("AreaFix", HUB, "DNFIX", "+FSX_GEN")))
    assert (result.robot, result.netmail) == (0, 1)
    body = db.connection.execute("SELECT body FROM mail_messages").fetchone()["body"]
    assert "not a point or downlink of this node" in body
    assert link_areas(db, down.id, ECHO) == [] and _replies(db, network) == []


def test_filefix_links_offered_file_echoes(db, offered):
    sysop, network, _board, _point, down = offered
    area = create_file_area(db, "fsx files", creator=sysop)
    set_file_area_tic(db, area, network, "FSX_FILE")
    set_offered(db, network.id, FILE_ECHO, "FSX_FILE", True)
    _toss(db, network, _packet(DOWN, _robot("FileFix", DOWN, "dnfix", "+FSX_FILE\n+FSX_GEN\n%RESCAN")), link=down)
    assert [a.tag for a in link_areas(db, down.id, FILE_ECHO)] == ["FSX_FILE"]
    assert link_areas(db, down.id, ECHO) == []
    (reply,) = _replies(db, network)
    assert "FSX_FILE: linked." in reply and "FileFix doesn't rescan" in reply


# --- TIC forwarding --------------------------------------------------------------------------


DATA = b"PK\x05\x06" + b"\x00" * 18 + b"some tools"


def _tic(sender: FtnAddress, password: str, *, path=(HUB,), seen=(HUB, NODE), area="FSX_FILE") -> bytes:
    lines = [f"Area {area}", f"Origin {HUB.four_d}", f"From {sender.four_d}", f"To {NODE.four_d}", "File TOOLS.ZIP",
             f"Size {len(DATA)}", "Desc Some tools", f"Crc {crc32(DATA):08X}"]
    lines += [f"Path {address.four_d} 1761200000 Thu Oct 23 06:13:20 2025 UTC" for address in path]
    lines += [f"Seenby {address.four_d}" for address in seen]
    lines.append(f"Pw {password}")
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


@pytest.fixture
def files(db, hub):
    sysop, network, _board, point, down = hub
    area = create_file_area(db, "fsx files", creator=sysop)
    set_file_area_tic(db, area, network, "FSX_FILE")
    for link in (point, down):
        set_link_area_without_commit(db, link.id, FILE_ECHO, "FSX_FILE")
    db.connection.commit()
    return (*hub, area)


def _settle(db, network, tic_bytes: bytes, remote: str) -> TossResult:
    result = TossResult()
    fileecho.judge_and_settle(db, network, remote, fileecho.Half("TOOLS.TIC", tic_bytes), parse_tic(tic_bytes),
                              fileecho.Half("TOOLS.ZIP", DATA), None, result)
    return result


def _queued_tics(db) -> dict:
    """The TICs queued per destination, parsed, with their file rows."""
    found = {}
    for row in db.connection.execute("SELECT * FROM ftn_outbound_files ORDER BY id"):
        if row["path"] and row["send_name"].endswith(".TIC"):
            found[row["destination"]] = parse_tic(Path(row["path"]).read_bytes())
    return found


def test_a_tic_from_the_uplink_goes_to_the_links_under_their_own_passwords(db, files):
    _sysop, network, _board, point, down, _area = files
    result = _settle(db, network, _tic(HUB, "ticpw"), str(HUB))
    assert result.files == 1
    tics = _queued_tics(db)
    # The point has no TIC password set, so it gets nothing.
    assert set(tics) == {"21:1/200"}
    tic = tics["21:1/200"]
    assert (tic.from_address, tic.password, tic.origin, tic.crc) == (NODE, "DNTIC", HUB, crc32(DATA))
    assert tic.path_addresses == (HUB, NODE)
    assert tic.seen_by == (HUB, NODE, DOWN)
    rows = db.connection.execute("SELECT * FROM ftn_outbound_files WHERE link_id = ?", (down.id,)).fetchall()
    assert len(rows) == 2 and rows[0]["file_id"] is not None and rows[0]["send_name"] == "TOOLS.ZIP"
    assert {row["reference_kind"] for row in rows} == {"ticfwd"}
    assert point.tic_password == ""


def test_a_tic_from_a_downlink_goes_to_the_uplink_and_the_others_never_back(db, files):
    _sysop, network, _board, point, down, _area = files
    save_link(db, replace(get_link(db, point.id), tic_password="PTTIC"))
    result = _settle(db, network, _tic(DOWN, "dntic", path=(DOWN,), seen=(DOWN, NODE)), str(DOWN))
    assert result.files == 1
    tics = _queued_tics(db)
    assert set(tics) == {"21:1/100", "21:1/199.1"}
    assert tics["21:1/100"].password == "TICPW" and tics["21:1/199.1"].password == "PTTIC"
    assert tics["21:1/100"].path_addresses == (DOWN, NODE)
    assert set(tics["21:1/100"].seen_by) == {DOWN, NODE, POINT_1, HUB}
    uplink_rows = db.connection.execute("SELECT route FROM ftn_outbound_files WHERE link_id IS NULL").fetchall()
    assert {row["route"] for row in uplink_rows} == {"uplink"}


def test_a_tic_from_a_downlink_with_the_wrong_password_or_echo_is_held(db, files):
    _sysop, network, _board, _point, down, _area = files
    assert _settle(db, network, _tic(DOWN, "TICPW", path=(DOWN,)), str(DOWN)).held == 1
    assert "does not match 21:1/200's" in queue.list_held(db)[-1].reason
    assert _settle(db, network, _tic(HUB, "dntic"), str(DOWN)).held == 1
    assert "but 21:1/200's session brought it" in queue.list_held(db)[-1].reason
    set_link_area_without_commit(db, down.id, FILE_ECHO, "FSX_FILE", "read")
    db.connection.commit()
    assert _settle(db, network, _tic(DOWN, "dntic", path=(DOWN,)), str(DOWN)).held == 1
    assert "read-only" in queue.list_held(db)[-1].reason
    assert _queued_tics(db) == {}


def test_a_tic_whose_path_names_this_node_is_stored_but_not_sent_on(db, files):
    _sysop, network, *_rest = files
    assert _settle(db, network, _tic(HUB, "TICPW", path=(HUB, NODE, FAR)), str(HUB)).files == 1
    assert _queued_tics(db) == {}


def test_a_tic_file_in_a_moderated_area_is_forwarded_once_approved(db, files):
    sysop, network, _board, _point, _down, area = files
    db.connection.execute("UPDATE file_areas SET moderated = 1 WHERE id = ?", (area.id,))
    db.connection.commit()
    assert _settle(db, network, _tic(HUB, "TICPW"), str(HUB)).files == 1
    assert _queued_tics(db) == {}
    file_id = db.connection.execute("SELECT file_id FROM files").fetchone()["file_id"]
    approve_file(db, get_file(db, file_id), approved_by=sysop)
    assert set(_queued_tics(db)) == {"21:1/200"}
    assert db.connection.execute("SELECT COUNT(*) FROM ftn_tic_waiting").fetchone()[0] == 0


def test_a_hatched_file_goes_to_the_links_too(db, files):
    sysop, network, _board, _point, _down, area = files
    entry = upload_file(db, area, sysop, "tools.zip", DATA, description="Some tools")
    hatched = hatch_file(db, entry.file_id, actor=sysop)
    tics = _queued_tics(db)
    assert set(tics) == {"21:1/100", "21:1/200"}
    assert tics["21:1/200"].password == "DNTIC" and tics["21:1/100"].password == "TICPW"
    assert set(tics["21:1/200"].seen_by) == {NODE, HUB, DOWN}  # the point has no TIC password
    assert hatched.tag == "FSX_FILE"


# --- transit netmail's files ----------------------------------------------------------------------


def _attach(sender: FtnAddress, dest: FtnAddress, names: str) -> FtnMessage:
    return FtnMessage(
        to_name="Far Person", from_name="Dana Down", subject=names, body="Files for you",
        date=datetime.datetime(2026, 10, 7, 9, 0), attributes=ATTR_FILE_ATTACHED,
        kludges=[("INTL", f"{dest.zone}:{dest.net}/{dest.node} {sender.zone}:{sender.net}/{sender.node}"),
                 *([("TOPT", str(dest.point))] if dest.point else []), ("MSGID", f"{sender} 0000a77a")],
        orig_net=sender.net, orig_node=sender.node, dest_net=dest.net, dest_node=dest.node)


def test_files_attached_to_transit_netmail_follow_it(db, hub):
    _sysop, network, _board, _point, down = hub
    result = _toss(db, network, _packet(DOWN, _attach(DOWN, FAR, "C:\\OUT\\TOOLS.ZIP")), link=down)
    assert result.routed == 1
    (message,) = _pending(db, network)
    assert message["route"] == "uplink"
    total = TossResult()
    assert claim_received(db, network, str(DOWN), "TOOLS.ZIP", DATA, total)
    assert total.files_routed == 1
    (row,) = db.connection.execute("SELECT * FROM ftn_outbound_files").fetchall()
    assert (row["destination"], row["route"], row["reference_kind"], row["reference_id"], row["after_sent"]) == (
        "21:3/110", "uplink", "netmail", message["reference_id"], "kill")
    assert Path(row["path"]).read_bytes() == DATA
    assert db.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0  # never in a file area


def test_a_transit_file_that_came_first_is_claimed_by_its_netmail(db, hub):
    _sysop, network, _board, point, down = hub
    assert fileecho.hold_waiting_file(db, network, str(DOWN), "TOOLS.ZIP", DATA, "waiting for its .TIC")
    result = _toss(db, network, _packet(DOWN, _attach(DOWN, POINT_1, "TOOLS.ZIP")), link=down)
    assert (result.routed, result.files_routed) == (1, 1)
    (row,) = db.connection.execute("SELECT * FROM ftn_outbound_files").fetchall()
    assert (row["link_id"], row["route"]) == (point.id, "direct")
    assert queue.list_held(db) == []


# --- the console ------------------------------------------------------------------------------------


def test_the_link_editor_sets_the_robot_and_tic_passwords(db, hub):
    from netbbs.net.ftn_links_console import ftn_links_screen
    from tests.test_admin_flow import FakeSession, _visible, _written_text

    sysop, network, _board, point, _down = hub
    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["0", "1", "0", "7", "NEWFIX", "0", "8", "FFPW", "0", "9", "PTTIC", "s", "b"])
        asyncio.run(ftn_links_screen(session, lane, sysop, network))
    finally:
        lane.close()
    assert "Saved 21:1/199.1" in _visible(_written_text(session))
    link = get_link(db, point.id)
    assert (link.areafix_password, link.filefix_password, link.tic_password) == ("NEWFIX", "FFPW", "PTTIC")
    assert link.session_password == "PTPW"


def test_the_areas_screen_adds_an_area_and_switches_one_read_only(db, offered):
    from netbbs.net.ftn_links_console import ftn_links_screen
    from tests.test_admin_flow import FakeSession, _visible, _written_text

    sysop, network, _board, point, _down = offered
    lane = DatabaseLane(db.path)
    try:
        # a 01: the point's areas; a 0 1: add the first area it lacks
        # (FSX_ADMIN, offered or not); 0 1: switch FSX_ADMIN read-only; back
        # out twice.
        session = FakeSession(["a", "01", "a", "0", "1", "0", "1", "b", "b"])
        asyncio.run(ftn_links_screen(session, lane, sysop, network))
    finally:
        lane.close()
    text = _visible(_written_text(session))
    assert "carries FSX_ADMIN now" in text
    modes = {area.tag: area.mode for area in link_areas(db, point.id, ECHO)}
    assert modes == {"FSX_ADMIN": "read", "FSX_GEN": "rw"}


def test_the_offered_areas_screen_toggles_an_offer(db, offered):
    from netbbs.ftn.robot_server import offered_tags
    from netbbs.net.ftn_links_console import ftn_links_screen
    from tests.test_admin_flow import FakeSession

    sysop, network, *_rest = offered
    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["r", "0", "1", "b", "b"])  # FSX_ADMIN, first by tag, becomes offered
        asyncio.run(ftn_links_screen(session, lane, sysop, network))
    finally:
        lane.close()
    assert offered_tags(db, network.id, ECHO) == {"FSX_ADMIN", "FSX_GEN", "FSX_TEST"}


# --- a hub over loopback BinkP: uplink, this node, a downlink and a point -------------------------


async def _call(listener, *, address, password, files=()):
    host, port = listener.listening_on
    reader, writer = await asyncio.open_connection(host, port)
    try:
        return await run_session(reader, writer, originating=True, our_addresses=[address],
                                 system=SystemInfo("Caller", "op"), password=password, outgoing=list(files),
                                 timeout=5)
    finally:
        writer.close()


async def _settled(listener, calls: int) -> None:
    for _ in range(500):
        if len(listener.recent) >= calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the listener never finished the call")


def _messages(result) -> list[FtnMessage]:
    return [decode_message(message) for received in result.received
            for message in parse_packet(received.data).messages]


def test_a_message_travels_each_way_through_the_hub_without_loops(db, hub):
    _sysop, network, _board, _point, _down = hub
    uplink_received = []
    uplink_sends = []

    async def run():
        async def handle(reader, writer):
            try:
                result = await run_session(
                    reader, writer, originating=False, our_addresses=[HUB], system=SystemInfo("Hub", "hubop"),
                    password_for=lambda addresses: "SECRET",
                    outgoing_for=lambda addresses, secure: list(uplink_sends), timeout=5)
                uplink_received.extend(result.received)
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        save_network(db, replace(network, uplink_port=port))
        lane = DatabaseLane(db.path)
        listener = FtnListener(lane)
        mailer = FtnMailer(lane)
        try:
            await listener.reconcile()
            # The downlink posts: its message goes up to the uplink and over
            # to the point.
            from_down = _echomail("Up from the downlink", DOWN, seen_by=[(1, 199), (1, 200)], path=[(1, 200)])
            first = await _call(listener, address=DOWN, password="DNPW",
                                files=[OutgoingFile("dn.pkt", _packet(DOWN, from_down))])
            await _settled(listener, 1)
            # The uplink holds a message from elsewhere in the network, which
            # comes down to the downlink and the point.
            from_hub = _echomail("Down from the hub", HUB, seen_by=[(1, 100), (1, 199)], path=[(1, 100)],
                                 serial=0x77)
            uplink_sends.append(OutgoingFile("hub.pkt", _packet(HUB, from_hub)))
            fresh = replace(network, uplink_port=port)
            status = await mailer.poll(fresh)
            point_call = await _call(listener, address=POINT_1, password="PTPW")
            await _settled(listener, 2)
            down_call = await _call(listener, address=DOWN, password="DNPW")
            await _settled(listener, 3)
            # The uplink sends the downlink's message back, as a misconfigured
            # hub might: this node is in its PATH and it is a duplicate, so
            # nothing goes anywhere again.
            echoed = replace(from_down, seen_by=[(1, 100), (1, 199), (1, 200)], path=[(1, 200), (1, 199), (1, 100)])
            uplink_sends[:] = [OutgoingFile("echo.pkt", _packet(HUB, echoed))]
            again = await mailer.poll(fresh)
            return first, status, point_call, down_call, again
        finally:
            await mailer.close()
            await listener.close()
            lane.close()
            server.close()
            await server.wait_closed()

    first, status, point_call, down_call, again = asyncio.run(run())
    assert first.secure and first.received == []
    assert status.failures == 0, status.last_error
    # The uplink got the downlink's message, with this node in SEEN-BY and PATH.
    (up,) = [m for received in uplink_received[:1] for m in map(decode_message, parse_packet(received.data).messages)]
    assert up.body == "Up from the downlink"
    assert up.seen_by == [(1, 100), (1, 199), (1, 200)] and up.path == [(1, 200), (1, 199)]
    # The point got both, the downlink only the hub's: never its own back.
    assert sorted(m.body for m in _messages(point_call)) == ["Down from the hub", "Up from the downlink"]
    (down_copy,) = _messages(down_call)
    assert down_copy.body == "Down from the hub"
    assert down_copy.seen_by == [(1, 100), (1, 199), (1, 200)] and down_copy.path == [(1, 100), (1, 199)]
    for message in _messages(point_call):
        assert (1, 199) in message.seen_by and message.path[-1] == (1, 199)
    # Nothing waits for anyone after the echo came back.
    assert again.failures == 0
    assert _pending(db, network) == []
    posts = [row["body"] for row in db.connection.execute("SELECT body FROM posts ORDER BY id")]
    assert posts == ["Up from the downlink", "Down from the hub"]
