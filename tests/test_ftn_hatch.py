"""TIC hatching (design doc §6.8 "Hatching", issue #1476): the TIC writer
and its round trip through the stage A parser and checks, [H]atch on a
file area's listing, the record that keeps a file from going out twice,
automatic hatching of approved uploads, the guards, and the outbound queue
carrying both halves -- against real SQLite files and a real lane."""

from __future__ import annotations

import asyncio
import hashlib
import zlib
from pathlib import Path

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.files.areas import create_file_area
from netbbs.files.entries import approve_file, get_file, upload_file, upload_file_from_temp
from netbbs.files.storage import new_incoming_temp_path
from netbbs.ftn import FtnFormatError, fileecho, queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import MAX_FILE_BYTES, ReceivedFile, SessionResult, safe_file_name
from netbbs.ftn.exchange import toss_received
from netbbs.ftn.hatch import (
    HatchError,
    auto_hatch_enabled,
    hatch_file,
    hatch_target,
    set_auto_hatch,
)
from netbbs.ftn.networks import FtnNetwork, save_network, set_file_area_tic
from netbbs.ftn.outbound import outbound_for_node, pending_files, spool_dir
from netbbs.ftn.tic import Tic, dos_name, is_dos_name, parse_tic, write_tic
from netbbs.moderation.log import list_recent_actions
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


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def network(db):
    return save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB, uplink_host="hub.example",
        session_password="SECRET", tic_password="TicPw", enabled=True))


@pytest.fixture
def area(db, sysop, network):
    area = create_file_area(db, "fsx files", creator=sysop)
    set_file_area_tic(db, area, network, "fsx_file")
    return area


def _upload(db, area, user, name="NETBBS.ZIP", data=b"a NetBBS release", description="NetBBS, a BBS.\nFor nodes."):
    return upload_file(db, area, user, name, data, description=description)


def _upload_from_temp(db, area, user, name="tool.zip", data=b"a caller's tool"):
    temp = new_incoming_temp_path(db)
    temp.write_bytes(data)
    return upload_file_from_temp(db, area, user, name, temp_path=temp, sha256=hashlib.sha256(data).hexdigest(),
                                 size_bytes=len(data))


def _hatches(db):
    return db.connection.execute("SELECT * FROM ftn_hatches ORDER BY id").fetchall()


def _tic_of(db, network) -> bytes:
    row = next(r for r in pending_files(db, network.id) if r.path is not None)
    return Path(row.path).read_bytes()


# --- the TIC writer ------------------------------------------------------------


def _outgoing(**overrides) -> Tic:
    fields = dict(area="FSX_FILE", file="NETBSD9A.ZIP", crc=0xB0A29EE6, long_name="NetBSD-10.1-tools.zip",
                  size=5000, desc="Tools for NetBSD", ldesc=("Tools for NetBSD", "Second line"),
                  replaces=("NETBSD9A.ZIP",), origin=NODE, from_address=NODE, password="TicPw")
    fields.update(overrides)
    return Tic(**fields)


def test_a_written_tic_reads_back_through_the_parser():
    tic = _outgoing()
    data = write_tic(tic, to=HUB, path=[(NODE, 1761200000)], seen_by=[NODE, HUB], date=1761100000,
                     created="NetBBS 7.36.0")
    assert parse_tic(data) == tic
    text = data.decode("cp437")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    lines = text.split("\r\n")
    assert "To 21:1/100" in lines and "Date 1761100000" in lines and "Crc B0A29EE6" in lines
    assert "Path 21:1/199 1761200000 Thu Oct 23 06:13:20 2025 UTC" in lines
    assert lines.count("Seenby 21:1/199") == 1 and "Seenby 21:1/100" in lines
    assert "Lfile NetBSD-10.1-tools.zip" in lines and "Pw TicPw" in lines


def test_the_writer_uses_cp437_when_it_fits_and_utf8_otherwise():
    cp = write_tic(_outgoing(desc="Café", ldesc=()), to=HUB, path=[], seen_by=[])
    assert "Desc Café".encode("cp437") in cp
    wide = write_tic(_outgoing(desc="Ein Bär 🐻", ldesc=()), to=HUB, path=[], seen_by=[])
    assert parse_tic(wide).desc == "Ein Bär 🐻"


def test_a_description_cannot_break_a_tic_line():
    data = write_tic(_outgoing(desc="one\rPw stolen", ldesc=("two\nArea OTHER",)), to=HUB, path=[], seen_by=[])
    tic = parse_tic(data)
    assert tic.area == "FSX_FILE" and tic.password == "TicPw"
    assert tic.desc == "one Pw stolen" and tic.ldesc == ("two Area OTHER",)


def test_the_writer_refuses_what_no_tic_can_carry():
    with pytest.raises(FtnFormatError):
        write_tic(_outgoing(file="NetBSD-10.1-tools.zip"), to=HUB, path=[], seen_by=[])
    with pytest.raises(FtnFormatError):
        write_tic(_outgoing(origin=None), to=HUB, path=[], seen_by=[])
    with pytest.raises(FtnFormatError):
        write_tic(_outgoing(password="a\r\nArea X"), to=HUB, path=[], seen_by=[])


def test_a_long_name_gets_an_8_3_name_that_keeps_two_apart():
    assert dos_name("README.TXT") == "README.TXT" and dos_name("a-b_c.7z") == "a-b_c.7z"
    first, second = dos_name("voidrunner-1.2.zip"), dos_name("voidrunner-1.3.zip")
    assert first != second
    for name in (first, second, dos_name(".hidden name"), dos_name("noext but long"), dos_name("ünï.tar.gz")):
        assert is_dos_name(name) and safe_file_name(name) == name
    assert first.startswith("VOIDRU") and first.endswith(".ZIP")


# --- hatching ----------------------------------------------------------------------


def test_hatching_queues_the_file_and_its_tic_to_the_uplink(db, sysop, network, area):
    entry = _upload(db, area, sysop, name="NetBBS-7.36.0.zip")
    hatched = hatch_file(db, entry.file_id, actor=sysop)

    rows = pending_files(db, network.id)
    assert [(r.route, r.destination, r.flavour) for r in rows] == [("uplink", "21:1/100", "normal")] * 2
    payload, tic_row = rows
    assert payload.file_id == entry.file_id and payload.after_sent == "keep"
    assert payload.send_name == hatched.send_name == dos_name("NetBBS-7.36.0.zip")
    assert tic_row.after_sent == "kill" and tic_row.send_name == hatched.tic_name
    assert Path(tic_row.path).resolve().is_relative_to(spool_dir(db).resolve())

    tic = parse_tic(_tic_of(db, network))
    data = b"a NetBBS release"
    assert (tic.area, tic.file, tic.long_name) == ("FSX_FILE", payload.send_name, "NetBBS-7.36.0.zip")
    assert tic.crc == zlib.crc32(data) and tic.size == len(data)
    assert tic.origin == NODE and tic.from_address == NODE and tic.password == "TicPw"
    assert tic.desc == "NetBBS, a BBS." and tic.ldesc == ("NetBBS, a BBS.", "For nodes.")
    assert tic.replaces == ()

    record = _hatches(db)
    assert len(record) == 1 and record[0]["hatched_by"] == sysop.id and not record[0]["automatic"]
    assert any(a.action == "hatch_ftn_file" and "NetBBS-7.36.0.zip" in a.detail for a in list_recent_actions(db))


def test_a_hatched_pair_passes_the_receiving_nodes_checks(tmp_path, db, sysop, network, area):
    """The other end of the link, a NetBBS node whose uplink is this one:
    the TIC and file this node hatched pass stage A's checks there."""
    entry = _upload(db, area, sysop, name="NetBBS-7.36.0.zip")
    hatched = hatch_file(db, entry.file_id, actor=sysop)
    tic_bytes = _tic_of(db, network)

    receiver = Database(tmp_path / "hub.db")
    try:
        hub_sysop = create_user(receiver, "hub", password="hunter2", user_level=SYSOP_LEVEL)
        hub_network = save_network(receiver, FtnNetwork(
            name="fsxNet", domain="fsxnet", our_address=HUB, uplink_address=NODE, uplink_host="node.example",
            session_password="SECRET", tic_password="TICPW", enabled=True))
        hub_area = create_file_area(receiver, "fsx files", creator=hub_sysop)
        set_file_area_tic(receiver, hub_area, hub_network, "FSX_FILE")
        hub_lane = DatabaseLane(receiver.path)
        try:
            result = asyncio.run(toss_received(hub_lane, hub_network, SessionResult(
                remote_addresses=[NODE], secure=True,
                received=[ReceivedFile(hatched.send_name, b"a NetBBS release"),
                          ReceivedFile(hatched.tic_name, tic_bytes)])))
        finally:
            hub_lane.close()
        assert result.files == 1 and result.held == 0
        row = receiver.connection.execute("SELECT * FROM files WHERE area_id = ?", (hub_area.id,)).fetchone()
        assert row["filename"] == "NetBBS-7.36.0.zip" and row["ftn_origin"] == "21:1/199"
        assert row["description"].startswith("NetBBS, a BBS.")
    finally:
        receiver.close()


def test_a_file_is_hatched_once_unless_hatched_again_with_replaces(db, sysop, network, area):
    entry = _upload(db, area, sysop, name="NetBBS-7.36.0.zip")
    hatch_file(db, entry.file_id, actor=sysop)
    with pytest.raises(HatchError, match="already hatched"):
        hatch_file(db, entry.file_id, actor=sysop)
    assert len(pending_files(db, network.id)) == 2

    again = hatch_file(db, entry.file_id, actor=sysop, again=True)
    assert again.replaces == (dos_name("NetBBS-7.36.0.zip"), "NetBBS-7.36.0.zip")
    newest = [r for r in pending_files(db, network.id) if r.path is not None][-1]
    assert parse_tic(Path(newest.path).read_bytes()).replaces == again.replaces
    assert len(_hatches(db)) == 2


@pytest.mark.parametrize("problem", ["by_tic", "held", "no_password", "too_big", "pending", "no_echo", "tic_name"])
def test_what_is_not_hatched_says_why(db, sysop, network, area, problem):
    name = "x.tic" if problem == "tic_name" else "NETBBS.ZIP"
    target_area = area
    if problem == "no_echo":
        target_area = create_file_area(db, "local", creator=sysop)
    if problem == "pending":
        target_area = create_file_area(db, "held back", creator=sysop, moderated=True)
        set_file_area_tic(db, target_area, network, "OTHER")
    entry = _upload(db, target_area, sysop, name=name)
    expected = {
        "by_tic": "arrived by TIC", "held": "came over FTN", "no_password": "No TIC password",
        "too_big": "BinkP sends at most", "pending": "not approved", "no_echo": "carries no file echo",
        "tic_name": "control file",
    }[problem]
    if problem == "by_tic":
        db.connection.execute("UPDATE files SET ftn_file_echo = 'FSX_FILE', ftn_origin = '21:2/1' WHERE id = ?",
                              (entry.id,))
    elif problem == "held":
        db.connection.execute("UPDATE files SET ftn_origin = '21:1/100' WHERE id = ?", (entry.id,))
    elif problem == "no_password":
        db.connection.execute("UPDATE ftn_networks SET tic_password = '' WHERE id = ?", (network.id,))
    elif problem == "too_big":
        db.connection.execute("UPDATE files SET size_bytes = ? WHERE id = ?", (MAX_FILE_BYTES + 1, entry.id))
    db.connection.commit()
    with pytest.raises(HatchError, match=expected):
        hatch_file(db, entry.file_id, actor=sysop)
    assert pending_files(db, network.id) == [] and _hatches(db) == []
    assert not list((spool_dir(db) / "hatch").glob("*")) if (spool_dir(db) / "hatch").exists() else True


def test_a_file_saved_from_the_held_store_is_never_hatched(db, lane, sysop, network, area):
    data = b"stray"
    asyncio.run(toss_received(lane, network, SessionResult(
        remote_addresses=[HUB], secure=True, received=[ReceivedFile("STRAY.ZIP", data)])))
    held = queue.list_held(db)[0]
    entry = fileecho.save_held_to_area(db, held.id, area, sysop, name="STRAY.ZIP", description=None)
    with pytest.raises(HatchError, match="came over FTN"):
        hatch_target(db, get_file(db, entry.file_id))


# --- automatic hatching ------------------------------------------------------------------


def test_automatic_hatching_is_off_by_default(db, alice, network, area):
    assert not auto_hatch_enabled(db, area.id)
    _upload_from_temp(db, area, alice)
    assert pending_files(db, network.id) == []


def test_an_approved_upload_is_hatched_once_where_the_area_hatches_by_itself(db, sysop, alice, network, area):
    set_auto_hatch(db, area.id, True, actor=sysop)
    entry = _upload_from_temp(db, area, alice)
    record = _hatches(db)
    assert len(record) == 1 and record[0]["automatic"] == 1 and record[0]["hatched_by"] is None
    assert [r.file_id for r in pending_files(db, network.id) if r.file_id] == [entry.file_id]
    with pytest.raises(HatchError, match="already hatched"):
        hatch_file(db, entry.file_id, actor=sysop)


def test_a_moderated_upload_is_hatched_when_it_is_approved(db, sysop, alice, network):
    area = create_file_area(db, "moderated", creator=sysop, moderated=True)
    set_file_area_tic(db, area, network, "MOD_FILE")
    set_auto_hatch(db, area.id, True, actor=sysop)
    entry = _upload_from_temp(db, area, alice)
    assert _hatches(db) == []
    approve_file(db, entry, approved_by=sysop)
    assert len(_hatches(db)) == 1


def test_an_automatic_hatch_that_is_refused_leaves_the_upload_alone(db, sysop, alice, network, area):
    set_auto_hatch(db, area.id, True, actor=sysop)
    db.connection.execute("UPDATE ftn_networks SET tic_password = '' WHERE id = ?", (network.id,))
    db.connection.commit()
    entry = _upload_from_temp(db, area, alice)
    assert get_file(db, entry.file_id).status == "approved"
    assert _hatches(db) == [] and pending_files(db, network.id) == []


# --- the session ------------------------------------------------------------------------


def test_a_session_sends_both_and_deletes_only_the_tic(db, lane, sysop, network, area):
    entry = _upload(db, area, sysop)
    hatched = hatch_file(db, entry.file_id, actor=sysop)
    tic_path = Path(next(r.path for r in pending_files(db, network.id) if r.path))

    async def _run():
        outbound = await outbound_for_node(lane, network, HUB, uplink=True)
        try:
            names = [f.name for f in outbound.files]
            await outbound.settle(lane, names)
            return names
        finally:
            outbound.close()

    assert asyncio.run(_run()) == [hatched.send_name, hatched.tic_name]
    assert pending_files(db, network.id) == []
    assert not tic_path.exists() and Path(entry.storage_path).exists()


def test_a_hatched_file_deleted_before_its_session_takes_its_tic_with_it(db, lane, sysop, network, area):
    entry = _upload(db, area, sysop)
    hatch_file(db, entry.file_id, actor=sysop)
    tic_path = Path(next(r.path for r in pending_files(db, network.id) if r.path))
    db.connection.execute("DELETE FROM files WHERE id = ?", (entry.id,))
    db.connection.commit()

    async def _run():
        outbound = await outbound_for_node(lane, network, HUB, uplink=True)
        outbound.close()
        return outbound.files

    assert asyncio.run(_run()) == []
    assert pending_files(db, network.id) == [] and not tic_path.exists()


# --- the screens -------------------------------------------------------------------------


def test_hatch_on_the_file_listing(db, lane, sysop, network, area):
    from netbbs.net.file_flow import _show_area

    entry = _upload(db, area, sysop)
    session = FakeSession(["h", "y", "b"])
    asyncio.run(_show_area(session, lane, area, sysop))
    text = _visible(_written_text(session))
    assert "[H]atch" in text and "hatched into FSX_FILE" in text
    assert [r.file_id for r in pending_files(db, network.id) if r.file_id] == [entry.file_id]


def test_hatching_again_from_the_listing_asks_about_the_earlier_copy(db, lane, sysop, network, area):
    from netbbs.net.file_flow import _show_area

    entry = _upload(db, area, sysop)
    hatch_file(db, entry.file_id, actor=sysop)
    session = FakeSession(["h", "n", "b"])
    asyncio.run(_show_area(session, lane, area, sysop))
    text = _visible(_written_text(session))
    assert "was already hatched into FSX_FILE" in text and "Nothing was hatched." in text
    assert len(_hatches(db)) == 1


def test_a_caller_is_not_offered_hatch(db, lane, sysop, alice, network, area):
    from netbbs.net.file_flow import _show_area

    _upload(db, area, sysop)
    session = FakeSession(["h", "b"])
    asyncio.run(_show_area(session, lane, area, alice))
    assert "[H]atch" not in _visible(_written_text(session))
    assert pending_files(db, network.id) == []


def test_a_local_area_offers_no_hatch(db, lane, sysop, network):
    from netbbs.net.file_flow import _show_area

    local = create_file_area(db, "local", creator=sysop)
    _upload(db, local, sysop)
    session = FakeSession(["b"])
    asyncio.run(_show_area(session, lane, local, sysop))
    assert "[H]atch" not in _visible(_written_text(session))


def test_auto_hatch_toggles_on_the_file_area_screen(db, lane, sysop, network, area):
    from netbbs.net.admin_files import _area_detail_screen

    session = FakeSession(["a", "b"])
    asyncio.run(_area_detail_screen(session, lane, sysop, area))
    text = _visible(_written_text(session))
    assert "[A]uto-hatch" in text and "Auto-hatch on" in text
    assert auto_hatch_enabled(db, area.id)
