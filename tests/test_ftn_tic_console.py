"""The console side of file echoes and unknown echoes (design doc §6.8, issues
#1460, #1462, #1466): the held screen's [R]elease, [S]ave and [E]xport,
the Unknown echoes screen's [C]reate and [M]ap, a file area's [F]ile echo and
the network's TIC password -- driven by the scripted `FakeSession`."""

from __future__ import annotations

import asyncio
import datetime
import zlib

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.files.areas import create_file_area, list_file_areas
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import ReceivedFile, SessionResult
from netbbs.ftn.exchange import toss_received
from netbbs.ftn.message import FtnMessage, encode_message
from netbbs.ftn.networks import FtnNetwork, file_area_tic, get_network, list_networks, save_network
from netbbs.ftn.packet import PacketHeader, build_packet
from netbbs.ftn.tosser import toss_packet
from netbbs.moderation.log import list_recent_actions
from netbbs.net.ftn_console import file_area_echo_action, ftn_status_screen
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text
from tests.test_ftn_console import _controls

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


@pytest.fixture
def network(db):
    return save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB, uplink_host="hub.example",
        enabled=True, tic_password="TICPW"))


def _tic(file, data, *, area="FSX_INFO", pw="TICPW"):
    crc = format(zlib.crc32(data) & 0xFFFFFFFF, "08X")
    return (f"Area {area}\r\nOrigin 21:1/100\r\nFrom 21:1/100\r\nFile {file}\r\nSize {len(data)}\r\nCrc {crc}\r\n"
            f"Ldesc A held file.\r\nPw {pw}\r\n").encode()


def _toss(lane, network, *files):
    session = SessionResult(remote_addresses=[HUB], secure=True,
                            received=[ReceivedFile(name, data) for name, data in files])
    return asyncio.run(toss_received(lane, network, session))


def _status(lane, sysop, keys):
    session = FakeSession(keys)
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    return _visible(_written_text(session))


def test_a_held_file_cannot_be_released_and_says_why(db, lane, sysop, network):
    _toss(lane, network, ("stray.zip", b"a file"))
    text = _status(lane, sysop, ["h", "r", "1", "b", "b"])
    assert "Not released" in text and "not a mail packet" in text
    assert len(queue.list_held(db)) == 1


def test_a_held_file_is_saved_to_a_file_area_with_its_tics_description(db, lane, sysop, network):
    area = create_file_area(db, "incoming", creator=sysop)
    _toss(lane, network, ("a.tic", _tic("INFO.ZIP", b"pack", pw="wrong")), ("INFO.ZIP", b"pack"))
    text = _status(lane, sysop, ["h", "s", "1", "1", "", "b", "b"])
    assert "Saved INFO.ZIP to incoming." in text
    (row,) = db.connection.execute("SELECT * FROM files WHERE area_id = ?", (area.id,)).fetchall()
    assert (row["uploader_user_id"], row["description"]) == (sysop.id, "A held file.")
    assert queue.list_held(db) == []
    assert any(a.action == "save_ftn_held" for a in list_recent_actions(db, limit=5))


def test_a_held_file_is_exported_to_a_folder_and_stays_held(db, lane, sysop, network, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    _toss(lane, network, ("stray.zip", b"a file"))
    text = _status(lane, sysop, ["h", "e", "1", str(out), "b", "b"])
    assert "Exported to" in text
    assert (out / "stray.zip").read_bytes() == b"a file"
    assert len(queue.list_held(db)) == 1
    text = _status(lane, sysop, ["h", "e", "1", str(out), "b", "b"])
    assert "already exists" in text
    assert any(a.action == "export_ftn_held" for a in list_recent_actions(db, limit=5))


def _bad_echo_packet():
    message = FtnMessage(to_name="All", from_name="Joe", subject="Hi", body="Waiting for a board", area="FSX_NEW",
                         date=datetime.datetime(2026, 10, 7, 9, 0), kludges=[("MSGID", "21:3/110 00000001")],
                         origin="Far (21:3/110)", orig_net=3, orig_node=110, dest_net=1, dest_node=199)
    return build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), [encode_message(message)])


def test_a_bad_echo_is_mapped_to_a_local_board_and_re_tossed(db, lane, sysop, network):
    board = create_board(db, "new things", creator=sysop)
    toss_packet(db, network, _bad_echo_packet(), secure=True, remote_address="21:1/100", file_name="x.pkt")
    text = _status(lane, sysop, ["u", "m", "1", "1", "", "b", "b"])
    assert "FSX_NEW" in text and "Unknown echoes (fsxNet)" in text and "Unknown echoes:" in text
    assert "new things carries FSX_NEW: 1 posts tossed" in text
    (post,) = db.connection.execute("SELECT body FROM posts WHERE board_id = ?", (board.id,)).fetchall()
    assert post["body"].startswith("Waiting for a board")
    assert queue.list_bad_echoes(db, network.id) == []


def test_a_bad_file_echo_gets_a_new_file_area_named_after_its_tag(db, lane, sysop, network):
    _toss(lane, network, ("a.tic", _tic("NEWS.ZIP", b"news", area="FSX_NEWS")), ("NEWS.ZIP", b"news"))
    text = _status(lane, sysop, ["u", "c", "1", "s", "b", "b"])
    assert "FSX_NEWS (file echo)" in text
    (area,) = list_file_areas(db)
    assert area.name == "FSX_NEWS"
    assert file_area_tic(db, area).tag == "FSX_NEWS"
    assert "1 files stored" in text
    assert queue.list_held(db) == []


def test_a_file_area_carries_a_file_echo_from_its_screen(db, lane, sysop, network):
    area = create_file_area(db, "info", creator=sysop)
    assert asyncio.run(file_area_echo_action(FakeSession(["fsx_info"]), lane, sysop, area))
    assert file_area_tic(db, area).tag == "FSX_INFO"
    assert asyncio.run(file_area_echo_action(FakeSession([""]), lane, sysop, area))
    assert file_area_tic(db, area) is None


def test_the_file_area_screen_offers_the_file_echo(db, lane, sysop, network):
    from netbbs.net.admin_files import _area_detail_screen

    area = create_file_area(db, "info", creator=sysop)
    session = FakeSession(["f", "fsx_info", "b"])
    asyncio.run(_area_detail_screen(session, lane, sysop, area))
    text = _visible(_written_text(session))
    assert "[F]ile echo (FTN)" in text
    assert file_area_tic(db, area).tag == "FSX_INFO"
