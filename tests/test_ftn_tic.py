"""File echoes (design doc §6.8, issue #1462) and bad echoes (issue #1466):
TIC pairs from a session into file areas, the held store's pairing,
[S]ave and [E]xport, and echomail for tags no board carries -- against
real SQLite files and a real database lane."""

from __future__ import annotations

import asyncio
import datetime
import os
import zlib

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.config import set_max_upload_bytes
from netbbs.files.areas import create_file_area
from netbbs.files.entries import approve_file, get_file, upload_file
from netbbs.ftn import FtnFormatError, fileecho, queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import ReceivedFile, SessionResult
from netbbs.ftn.bundle import build_bundle
from netbbs.ftn.exchange import rerun_bad_file_echo, toss_received
from netbbs.ftn.message import FtnMessage, encode_message
from netbbs.ftn.networks import (
    FtnNetwork,
    FtnNetworkError,
    clear_file_area_tic,
    delete_network,
    file_area_tic,
    get_network,
    save_network,
    set_board_area,
    set_file_area_tic,
)
from netbbs.ftn.packet import PacketHeader, build_packet
from netbbs.ftn.tic import matches_replaces, name_problem, parse_tic, replaces_pattern_problem
from netbbs.ftn.tosser import release_held, retoss_bad_echo, toss_packet
from netbbs.link.files import LinkFilesError, link_file_area
from netbbs.link.node_identity import bootstrap_node_identity
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
        session_password="SECRET", tic_password="TicPw", enabled=True))


@pytest.fixture
def area(db, sysop, network):
    area = create_file_area(db, "fsx info", creator=sysop)
    set_file_area_tic(db, area, network, "fsx_info")
    return area


def _tic(file="FSXINFO.ZIP", data=b"the infopack", *, area="FSX_INFO", pw="ticpw", frm="21:1/100",
         origin="21:2/100", size=True, crc=None, replaces=(), ldesc=("The fsxNet infopack.",), desc=None,
         lfile=None, extra=()) -> bytes:
    lines = [f"Area {area}", f"Origin {origin}", f"From {frm}", f"File {file}"]
    if lfile:
        lines.append(f"Lfile {lfile}")
    if size:
        lines.append(f"Size {len(data)}")
    lines.append(f"Crc {crc if crc is not None else format(zlib.crc32(data) & 0xFFFFFFFF, '08X')}")
    if desc:
        lines.append(f"Desc {desc}")
    lines += [f"Ldesc {line}" for line in ldesc]
    lines += [f"Replaces {value}" for value in replaces]
    lines += ["Path 21:2/100 1761200000 Thu Oct 23 06:13:20 2026 UTC", "Seenby 21:1/100", "Seenby 21:1/199"]
    if pw is not None:
        lines.append(f"Pw {pw}")
    lines += list(extra)
    return ("\r\n".join(lines) + "\r\n").encode("cp437")


def _session(*files, secure=True):
    return SessionResult(remote_addresses=[HUB], secure=secure,
                         received=[ReceivedFile(name, data) for name, data in files])


def _toss(lane, network, *files, secure=True):
    return asyncio.run(toss_received(lane, network, _session(*files, secure=secure)))


def _files(db, area):
    return db.connection.execute("SELECT * FROM files WHERE area_id = ? ORDER BY id", (area.id,)).fetchall()


# --- the TIC format ----------------------------------------------------------


def test_a_tic_is_read_whatever_its_line_ends_and_keyword_case():
    data = b"AREA nix_list\nfile NIXLIST.Z23\rCRC 0000beef\r\nldesc one\nLDESC two\nReplaces NIXLIST.Z*\npw x\n"
    tic = parse_tic(data)
    assert (tic.area, tic.file, tic.crc, tic.password) == ("nix_list", "NIXLIST.Z23", 0xBEEF, "x")
    assert tic.description == "one\ntwo" and tic.replaces == ("NIXLIST.Z*",)


@pytest.mark.parametrize("data, reason", [
    (b"File X.ZIP\r\nCrc 00000000\r\n", "no Area"),
    (b"Area A\r\nCrc 00000000\r\n", "no File"),
    (b"Area A\r\nFile X.ZIP\r\nCrc nothex\r\n", "not an eight-digit hex"),
    (b"Area A\r\nFile X.ZIP\r\nCrc 1\r\nSize lots\r\n", "Size"),
    (b"Area A\r\nFile X.ZIP\r\nCrc 1\r\n" + b"Ldesc x\r\n" * 8000, "more than 65536"),
], ids=["no-area", "no-file", "bad-crc", "bad-size", "too-long"])
def test_a_tic_without_what_a_file_echo_needs_is_refused(data, reason):
    with pytest.raises(FtnFormatError, match=reason):
        parse_tic(data)


@pytest.mark.parametrize("name", ["../../etc/passwd", "a/b.zip", "a\\b.zip", "C:boot.ini", ".profile", "..",
                                  "bell\x07.zip", "x" * 256, " lead.zip"])
def test_unsafe_names_are_refused(name):
    assert name_problem(name) is not None


def test_replaces_matches_within_a_name_without_case_and_ignores_folders():
    assert matches_replaces("NODELIST.???", "nodelist.z23")
    assert matches_replaces("nodelist.*", "NODELIST.Z23")
    assert not matches_replaces("NODELIST.???", "NODELIST.Z234")
    assert matches_replaces("[x].zip", "[x].zip") and not matches_replaces("[x].zip", "x.zip")
    assert matches_replaces("*" * 200 + "a", "b" * 254 + "a")  # linear, not a regex blow-up
    for value in ("../NODELIST.*", "sub/x", "C:*", "a\\b"):
        assert replaces_pattern_problem(value) is not None


# --- a pair that passes -------------------------------------------------------


def test_a_pair_from_the_uplink_lands_in_its_area_as_a_tic_file(db, lane, network, area):
    data = b"the infopack"
    result = _toss(lane, network, ("ab12.tic", _tic(data=data)), ("FSXINFO.ZIP", data))

    assert (result.files, result.held) == (1, 0)
    (row,) = _files(db, area)
    assert (row["filename"], row["status"], row["uploader_user_id"]) == ("FSXINFO.ZIP", "approved", None)
    assert row["uploader_label"] == "fsxNet (21:2/100)"
    assert (row["ftn_file_echo"], row["ftn_origin"], row["ftn_network_id"]) == ("FSX_INFO", "21:2/100", network.id)
    assert row["description"] == "The fsxNet infopack."
    assert get_file(db, row["file_id"]).size_bytes == len(data)
    assert queue.list_held(db) == []


def test_a_moderated_area_holds_a_tic_file_for_approval_and_replaces_waits_for_it(db, lane, network, area, sysop):
    db.connection.execute("UPDATE file_areas SET moderated = 1 WHERE id = ?", (area.id,))
    db.connection.commit()
    old = b"week one"
    _toss(lane, network, ("a.tic", _tic("NODELIST.Z01", old)), ("NODELIST.Z01", old))
    db.connection.execute("UPDATE files SET status = 'approved'")
    db.connection.commit()
    new = b"week two"
    _toss(lane, network, ("b.tic", _tic("NODELIST.Z08", new, replaces=("NODELIST.Z*",))), ("NODELIST.Z08", new))

    rows = _files(db, area)
    assert [(r["filename"], r["status"]) for r in rows] == [("NODELIST.Z01", "approved"), ("NODELIST.Z08", "pending")]
    approve_file(db, get_file(db, rows[1]["file_id"]), approved_by=sysop)
    assert [r["filename"] for r in _files(db, area)] == ["NODELIST.Z08"]


def test_replaces_removes_only_tic_files_of_the_same_area(db, lane, network, area, sysop):
    other = create_file_area(db, "other", creator=sysop)
    set_file_area_tic(db, other, network, "FSX_OTHER")
    _toss(lane, network, ("a.tic", _tic("NODELIST.Z01", b"one")), ("NODELIST.Z01", b"one"))
    _toss(lane, network, ("o.tic", _tic("NODELIST.Z02", b"two", area="FSX_OTHER")), ("NODELIST.Z02", b"two"))
    upload_file(db, area, sysop, "NODELIST.TXT", b"a caller's upload")

    _toss(lane, network, ("b.tic", _tic("NODELIST.Z08", b"new", replaces=("NODELIST.*", "../x/*", "NODELIST.Z08"))),
          ("NODELIST.Z08", b"new"))

    assert sorted(r["filename"] for r in _files(db, area)) == ["NODELIST.TXT", "NODELIST.Z08"]
    assert [r["filename"] for r in _files(db, other)] == ["NODELIST.Z02"]


def test_a_long_name_is_used_when_it_is_safe_and_the_short_one_otherwise(db, lane, network, area):
    _toss(lane, network, ("a.tic", _tic("LONGNA~1.ZIP", b"x", lfile="Long name.zip")), ("LONGNA~1.ZIP", b"x"))
    _toss(lane, network, ("b.tic", _tic("SHORT.ZIP", b"y", lfile="../evil.zip")), ("SHORT.ZIP", b"y"))
    assert [r["filename"] for r in _files(db, area)] == ["Long name.zip", "SHORT.ZIP"]


def test_without_a_tic_description_the_archive_diz_is_used_and_sanitised(db, lane, network, area):
    archive = build_bundle([("FILE_ID.DIZ", b"\x1b[1;31mRed tools\x1b[0m\r\nline two"), ("tool.exe", b"MZ")])
    _toss(lane, network, ("a.tic", _tic("TOOLS.ZIP", archive, ldesc=())), ("TOOLS.ZIP", archive))
    (row,) = _files(db, area)
    assert row["description"] == "Red tools\nline two"


def test_a_tic_description_is_normalised(db, lane, network, area):
    _toss(lane, network, ("a.tic", _tic("X.ZIP", b"x", ldesc=("\x1b[5mBlink\x07", "", "second"))), ("X.ZIP", b"x"))
    (row,) = _files(db, area)
    assert row["description"] == "Blink\n\nsecond"


def test_the_same_file_again_is_dropped_and_counted(db, lane, network, area):
    _toss(lane, network, ("a.tic", _tic()), ("FSXINFO.ZIP", b"the infopack"))
    result = _toss(lane, network, ("b.tic", _tic()), ("FSXINFO.ZIP", b"the infopack"))
    assert (result.file_duplicates, result.files, result.held) == (1, 0, 0)
    assert len(_files(db, area)) == 1


# --- checks that hold ---------------------------------------------------------


@pytest.mark.parametrize("changes, reason", [
    ({"pw": "wrong"}, "TIC password does not match"),
    ({"pw": None}, "TIC password does not match"),
    ({"frm": "21:3/110"}, "not the uplink"),
    ({"crc": "DEADBEEF"}, "CRC-32"),
    ({"file": "../../etc/passwd"}, "not a safe name"),
])
def test_a_pair_failing_a_check_is_held_together_with_the_reason(db, lane, network, area, changes, reason):
    tic = _tic(**{"file": "FSXINFO.ZIP", **changes})
    name = changes.get("file", "FSXINFO.ZIP").rsplit("/", 1)[-1]
    result = _toss(lane, network, ("a.tic", tic), (name, b"the infopack"))

    assert (result.files, result.held) == (0, 1)
    (held,) = queue.list_held(db)
    assert reason in held.reason
    assert (held.kind, held.has_tic, held.waiting) == ("file", True, False)
    assert queue.held_tic(db, held.id) == tic
    assert _files(db, area) == []


def test_with_no_tic_password_every_tic_is_held(db, lane, network, area):
    network = save_network(db, FtnNetwork(**{**network.__dict__, "tic_password": ""}))
    _toss(lane, network, ("a.tic", _tic()), ("FSXINFO.ZIP", b"the infopack"))
    (held,) = queue.list_held(db)
    assert "no TIC password is set" in held.reason


def test_size_is_taken_from_the_bytes_received(db, lane, network, area):
    lying = _tic(data=b"the infopack").replace(b"Size 12", b"Size 3")
    _toss(lane, network, ("a.tic", lying), ("FSXINFO.ZIP", b"the infopack"))
    (held,) = queue.list_held(db)
    assert "is 12 bytes, but the TIC says 3" in held.reason


def test_a_file_over_the_upload_limit_is_held(db, lane, network, area):
    set_max_upload_bytes(db, 4)
    _toss(lane, network, ("a.tic", _tic()), ("FSXINFO.ZIP", b"the infopack"))
    (held,) = queue.list_held(db)
    assert "over this node's limit of 4" in held.reason


def test_a_name_taken_by_a_callers_upload_is_held_even_with_a_wildcard_replaces(db, lane, network, area, sysop):
    upload_file(db, area, sysop, "FSXINFO.ZIP", b"mine")
    _toss(lane, network, ("a.tic", _tic(replaces=("*",))), ("FSXINFO.ZIP", b"the infopack"))
    (held,) = queue.list_held(db)
    assert "already in the area" in held.reason
    assert [r["filename"] for r in _files(db, area)] == ["FSXINFO.ZIP"]
    assert _files(db, area)[0]["uploader_user_id"] == sysop.id


def test_an_unsecure_session_has_both_halves_held_unopened(db, lane, network, area):
    _toss(lane, network, ("a.tic", _tic()), ("FSXINFO.ZIP", b"the infopack"), secure=False)
    held = queue.list_held(db)
    assert [(h.kind, h.reason, h.waiting) for h in held] == [
        ("tic", "unsecure session", False), ("file", "unsecure session", False)]
    assert _files(db, area) == []


def test_an_unreadable_tic_is_held_and_its_file_waits(db, lane, network, area):
    _toss(lane, network, ("a.tic", b"File X.ZIP\r\n"), ("X.ZIP", b"x"))
    held = queue.list_held(db)
    assert [(h.kind, h.waiting) for h in held] == [("tic", False), ("file", True)]
    assert "unreadable TIC" in held[0].reason and "waiting for its .TIC" in held[1].reason


# --- pairing across sessions --------------------------------------------------


def test_a_tic_waits_for_its_file_and_pairs_when_it_comes(db, lane, network, area):
    first = _toss(lane, network, ("a.tic", _tic()))
    (held,) = queue.list_held(db)
    assert (held.kind, held.waiting, held.reason) == ("tic", True, "waiting for FSXINFO.ZIP")
    assert first.held == 1

    second = _toss(lane, network, ("fsxinfo.zip", b"the infopack"))
    assert second.files == 1 and queue.list_held(db) == []
    assert [r["filename"] for r in _files(db, area)] == ["FSXINFO.ZIP"]


def test_a_zip_with_no_packet_waits_for_its_tic(db, lane, network, area):
    archive = build_bundle([("FSXINFO.TXT", b"text")])
    _toss(lane, network, ("FSXINFO.ZIP", archive))
    (held,) = queue.list_held(db)
    assert (held.kind, held.waiting) == ("file", True)
    result = _toss(lane, network, ("a.tic", _tic(data=archive, ldesc=("Info",))))
    assert result.files == 1 and queue.list_held(db) == []


def test_a_file_named_by_a_tic_in_the_same_session_is_not_tossed_as_a_bundle(db, lane, network, area):
    archive = build_bundle([("0000abcd.pkt", build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), []))])
    result = _toss(lane, network, ("a.tic", _tic("WEIRD.ZIP", archive)), ("WEIRD.ZIP", archive))
    assert result.files == 1 and result.posts == 0


def test_an_unpaired_half_expires_after_seven_days(db, lane, network, area):
    _toss(lane, network, ("a.tic", _tic()))
    _toss(lane, network, ("other.zip", b"lonely"))
    assert len(queue.list_held(db)) == 2
    soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=6)
    assert queue.expire_unpaired(db, now=soon) == []
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=8)
    assert len(queue.expire_unpaired(db, now=later)) == 2
    assert queue.list_held(db) == []


def test_a_tic_held_for_its_password_pairs_and_passes_once_the_password_is_set(db, lane, network, area):
    network = save_network(db, FtnNetwork(**{**network.__dict__, "tic_password": ""}))
    _toss(lane, network, ("a.tic", _tic()))
    (held,) = queue.list_held(db)
    assert held.waiting and "no TIC password" in held.reason
    network = save_network(db, FtnNetwork(**{**network.__dict__, "tic_password": "TICPW"}))
    result = _toss(lane, network, ("FSXINFO.ZIP", b"the infopack"))
    assert result.files == 1 and queue.list_held(db) == []


# --- the nodelist echo ---------------------------------------------------------


def test_a_nodelist_echo_without_an_area_is_handed_to_the_nodelist_import(db, lane, network, monkeypatch):
    seen = []

    def feed(db, network, tag, name, path):
        seen.append((tag, name, path.read_bytes()))
        return True

    monkeypatch.setattr(fileecho, "_is_nodelist_echo", lambda network, tag: tag == "FSX_NODE")
    monkeypatch.setattr(fileecho, "_nodelist_feed", feed)
    result = _toss(lane, network, ("a.tic", _tic("FSXNET.Z23", b"list", area="FSX_NODE")), ("FSXNET.Z23", b"list"))
    assert seen == [("FSX_NODE", "FSXNET.Z23", b"list")]
    assert result.files == 1 and queue.list_held(db) == []


def test_a_nodelist_echo_file_that_is_not_the_list_is_held_without_an_area(db, lane, network, monkeypatch):
    monkeypatch.setattr(fileecho, "_is_nodelist_echo", lambda network, tag: tag == "FSX_NODE")
    monkeypatch.setattr(fileecho, "_nodelist_feed", lambda *args: False)
    _toss(lane, network, ("a.tic", _tic("README.TXT", b"hi", area="FSX_NODE")), ("README.TXT", b"hi"))
    (held,) = queue.list_held(db)
    assert "is not its nodelist" in held.reason


def test_a_real_nodelist_by_tic_is_imported_with_and_without_an_area(db, lane, network, sysop):
    from netbbs.ftn.nodelist import nodelist_status
    from tests.test_ftn_nodelist_feed import BODY_289, BODY_296, _nodelist

    network = save_network(db, FtnNetwork(**{**network.__dict__, "nodelist_echo": "FSX_NODE",
                                             "nodelist_name": "FSXNET"}))
    first = _nodelist(289, BODY_289)
    result = _toss(lane, network, ("a.tic", _tic("FSXNET.289", first, area="FSX_NODE")), ("FSXNET.289", first))
    assert result.files == 1 and queue.list_held(db) == []
    assert nodelist_status(db, network.id).day == 289

    area = create_file_area(db, "nodelists", creator=sysop)
    set_file_area_tic(db, area, network, "FSX_NODE")
    second = _nodelist(296, BODY_296)
    _toss(lane, network, ("b.tic", _tic("FSXNET.296", second, area="FSX_NODE")), ("FSXNET.296", second))
    assert nodelist_status(db, network.id).day == 296
    assert [r["filename"] for r in _files(db, area)] == ["FSXNET.296"]


# --- bad file echoes ----------------------------------------------------------


def test_an_unknown_file_echo_is_held_as_a_bad_echo_and_mapping_runs_it_again(db, lane, network, sysop):
    _toss(lane, network, ("a.tic", _tic(area="FSX_NEW")), ("FSXINFO.ZIP", b"the infopack"))
    _toss(lane, network, ("b.tic", _tic("LATER.ZIP", b"later", area="FSX_NEW")))
    assert [(e.tag, e.count) for e in queue.list_bad_file_echoes(db, network.id)] == [("FSX_NEW", 2)]

    new_area = create_file_area(db, "new", creator=sysop)
    set_file_area_tic(db, new_area, network, "FSX_NEW")
    result = asyncio.run(rerun_bad_file_echo(lane, network, "FSX_NEW"))

    assert result.files == 1
    (row,) = _files(db, new_area)
    assert (row["filename"], row["ftn_file_echo"]) == ("FSXINFO.ZIP", "FSX_NEW")
    (left,) = queue.list_held(db)
    assert (left.kind, left.waiting, left.bad_echo, left.reason) == ("tic", True, None, "waiting for LATER.ZIP")


# --- the held store's actions (issue #1460) ------------------------------------


def test_release_refuses_a_held_file_and_keeps_it(db, lane, network):
    _toss(lane, network, ("stray.zip", b"not a packet"))
    (held,) = queue.list_held(db)
    with pytest.raises(FtnFormatError, match="not a mail packet"):
        release_held(db, network, held.id)
    assert len(queue.list_held(db)) == 1


def test_a_held_file_is_saved_to_an_area_with_its_tics_description(db, lane, network, sysop):
    plain = create_file_area(db, "plain", creator=sysop)
    _toss(lane, network, ("a.tic", _tic(pw="wrong", lfile="Info pack.zip")), ("FSXINFO.ZIP", b"the infopack"))
    (held,) = queue.list_held(db)
    item = fileecho.held_file_for_saving(db, held.id)
    assert (item.name, item.description) == ("Info pack.zip", "The fsxNet infopack.")

    entry = fileecho.save_held_to_area(db, held.id, plain, sysop, name=item.name, description=item.description)
    assert (entry.filename, entry.uploader_user_id, entry.description) == ("Info pack.zip", sysop.id,
                                                                           "The fsxNet infopack.")
    assert queue.list_held(db) == []

    _toss(lane, network, ("b.tic", _tic(pw="wrong", lfile="Info pack.zip")), ("FSXINFO.ZIP", b"the infopack"))
    (again,) = queue.list_held(db)
    with pytest.raises(fileecho.HeldItemError, match="already in plain"):
        fileecho.save_held_to_area(db, again.id, plain, sysop, name="info PACK.zip", description=None)
    assert len(queue.list_held(db)) == 1


def test_export_writes_beside_and_never_overwrites(tmp_path):
    target = tmp_path / "out" / "file.zip"
    target.parent.mkdir()
    assert fileecho.export_held(b"data", target) == target
    assert target.read_bytes() == b"data"
    with pytest.raises(fileecho.HeldItemError, match="already exists"):
        fileecho.export_held(b"other", target)
    assert target.read_bytes() == b"data"
    with pytest.raises(fileecho.HeldItemError, match="not a folder"):
        fileecho.export_held(b"x", tmp_path / "missing" / "f")
    assert [p.name for p in target.parent.iterdir()] == ["file.zip"]


def test_a_held_bundle_member_with_a_hostile_name_saves_and_exports_under_a_safe_one(db, lane, network, tmp_path):
    queue.hold_inbound(db, network_id=network.id, remote_address="21:1/100", file_name="evil\x00name.txt",
                       content=b"x", reason="waiting for its .TIC", kind="file", waiting=True)
    (held,) = queue.list_held(db)
    item = fileecho.held_file_for_saving(db, held.id)
    assert item.name == "evil_name.txt"
    assert fileecho.export_held(item.data, tmp_path / item.name).read_bytes() == b"x"
    with pytest.raises(fileecho.HeldItemError, match="control characters"):
        fileecho.export_held(b"x", tmp_path / "bad\x00name")
    with pytest.raises(fileecho.HeldItemError, match="dot"):
        fileecho.export_held(b"x", tmp_path / ".hidden")


def test_export_never_writes_through_a_symlink(tmp_path):
    real = tmp_path / "real.txt"
    real.write_bytes(b"keep")
    link = tmp_path / "link.txt"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    with pytest.raises(fileecho.HeldItemError, match="symbolic link"):
        fileecho.export_held(b"evil", link)
    assert real.read_bytes() == b"keep"


def test_the_held_store_fits_a_full_size_file():
    assert queue.MAX_HELD_BYTES >= 64 * 1024 * 1024 * 2


# --- the file echo mapping ----------------------------------------------------


def test_an_area_carries_one_file_echo_and_is_never_linked_and_ftn(db, sysop, network):
    area = create_file_area(db, "one", creator=sysop)
    assert set_file_area_tic(db, area, network.id, " fsx_nl ").tag == "FSX_NL"
    assert file_area_tic(db, area).tag == "FSX_NL"
    two = create_file_area(db, "two", creator=sysop)
    with pytest.raises(FtnNetworkError, match="already carried"):
        set_file_area_tic(db, two, network, "FSX_NL")
    with pytest.raises(LinkFilesError, match="FTN"):
        link_file_area(db, area, node_identity=bootstrap_node_identity("roanoke"))
    link_file_area(db, two, node_identity=bootstrap_node_identity("roanoke"))
    with pytest.raises(FtnNetworkError, match="Linked"):
        set_file_area_tic(db, two, network, "FSX_OTHER")
    clear_file_area_tic(db, area)
    assert file_area_tic(db, area) is None


def test_deleting_a_network_makes_its_file_areas_local(db, sysop, network, area):
    delete_network(db, network.id)
    assert file_area_tic(db, area) is None


def test_the_tic_password_is_stored(db, network):
    assert get_network(db, network.id).tic_password == "TicPw"
    with pytest.raises(FtnNetworkError, match="TIC password"):
        save_network(db, FtnNetwork(**{**network.__dict__, "tic_password": "has space"}))


# --- bad echoes (issue #1466) -------------------------------------------------


def _echo_packet(*areas_and_ids):
    messages = [encode_message(FtnMessage(
        to_name="All", from_name="Joe", subject="Hi", body=f"message {msgid}", area=area,
        date=datetime.datetime(2026, 10, 7, 9, 0), kludges=[("MSGID", f"21:3/110 {msgid:08x}")],
        origin="Far (21:3/110)", orig_net=3, orig_node=110, dest_net=1, dest_node=199,
    )) for area, msgid in areas_and_ids]
    return build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), messages)


def test_echomail_for_an_uncarried_tag_is_held_and_mapping_re_tosses_it(db, network, sysop):
    result = toss_packet(db, network, _echo_packet(("FSX_NEW", 1), ("FSX_NEW", 2), ("FSX_BOT", 3)), secure=True,
                         remote_address="21:1/100", file_name="x.pkt")
    assert result.bad_echo == 3
    assert [(e.tag, e.count) for e in queue.list_bad_echoes(db, network.id)] == [("FSX_BOT", 1), ("FSX_NEW", 2)]

    board = create_board(db, "new", creator=sysop)
    set_board_area(db, board, network.id, "FSX_NEW")
    tossed = retoss_bad_echo(db, network, "FSX_NEW")
    assert tossed.posts == 2
    bodies = [r["body"] for r in db.connection.execute("SELECT body FROM posts WHERE board_id = ?", (board.id,))]
    assert sorted(bodies) == ["message 1", "message 2"]
    assert [e.tag for e in queue.list_bad_echoes(db, network.id)] == ["FSX_BOT"]

    assert retoss_bad_echo(db, network, "FSX_NEW").posts == 0
    assert queue.delete_bad_echo(db, network.id, "fsx_bot") == 1
    assert queue.list_bad_echoes(db, network.id) == []


def test_bad_echoes_are_bounded_oldest_first_and_drops_are_counted(db, network, monkeypatch):
    monkeypatch.setattr(queue, "MAX_BAD_ECHO_PER_TAG", 2)
    monkeypatch.setattr(queue, "MAX_BAD_ECHO_MESSAGES", 3)
    toss_packet(db, network, _echo_packet(("A_ONE", 1), ("A_ONE", 2), ("A_ONE", 3), ("B_TWO", 4), ("B_TWO", 5)),
                secure=True, remote_address="21:1/100", file_name="x.pkt")
    assert [(e.tag, e.count) for e in queue.list_bad_echoes(db, network.id)] == [("A_ONE", 1), ("B_TWO", 2)]
    assert queue.bad_echo_dropped(db, network.id) == 2


# --- the migration ------------------------------------------------------------


def test_upgrading_tells_held_packets_files_and_tics_apart(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "Issues #1462 and #1466" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    path = tmp_path / "old.db"
    old = Database(path)
    packet = build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), [])
    for name, content in (("a.pkt", packet), ("b.tic", b"Area X\r\n"), ("c.zip", b"PK\x03\x04rest")):
        old.connection.execute(
            "INSERT INTO ftn_held_inbound (network_id, remote_address, file_name, content, reason, received_at) "
            "VALUES (NULL, '21:1/100', ?, ?, 'old', '2026-10-01T00:00:00Z')", (name, content))
    old.connection.commit()
    old.close()
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)
    new = Database(path)
    try:
        assert [(h.file_name, h.kind, h.waiting) for h in queue.list_held(new)] == [
            ("a.pkt", "packet", False), ("b.tic", "tic", False), ("c.zip", "file", False)]
    finally:
        new.close()
