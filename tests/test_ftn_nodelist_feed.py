"""The nodelist from a file echo (design doc §6.8 "Nodelist"; §16 "Issue
#1154", Decision 5; issue #1464): the FTS-5000 CRC-16, NODEDIFF applied to
the kept raw list with its fall-back, ZIP-archived lists, the by-hand
import of a ZIP, and the status screen."""

from __future__ import annotations

import asyncio
import difflib
import io
import zipfile

import pytest

from netbbs.admin.__main__ import run_ftn_import_nodelist
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.ftn import queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.networks import FtnNetwork, FtnNetworkError, get_network, save_network
from netbbs.ftn.nodelist import (
    NodelistError,
    apply_nodediff,
    direct_route,
    import_nodelist,
    kept_list,
    list_crc,
    list_lines,
    nodelist_status,
    parse_header,
)
from netbbs.ftn.nodelist_feed import handle_received_file
from netbbs.net.ftn_console import ftn_status_screen
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_admin_flow import FakeSession, _visible, _written_text

NODE = FtnAddress(86, 549, 40)
HUB = FtnAddress(86, 549, 0)

BODY_289 = [
    ";A Copyright 2026, Test Net, All rights reserved.",
    ";A",
    "Zone,86,Test_Zone,Somewhere,Zone_Op,-Unpublished-,300,CM,IBN:zone.example",
    "Host,549,Test_Net,Somewhere,Net_Op,-Unpublished-,300,CM,IBN:hub.example",
    ",1,First,Somewhere,One,-Unpublished-,300,IBN:one.example",
    ",2,Second,Somewhere,Two,-Unpublished-,300,IBN:two.example",
    ",3,Third,Somewhere,Three,-Unpublished-,300,IBN:three.example",
    ",40,ReLink,Somewhere,Carrier,-Unpublished-,300,IBN:relink.example",
    ";A",
]
BODY_296 = [
    ";A Copyright 2026, Test Net, All rights reserved.",
    ";A",
    "Zone,86,Test_Zone,Somewhere,Zone_Op,-Unpublished-,300,CM,IBN:zone.example",
    "Host,549,Test_Net,Somewhere,Net_Op,-Unpublished-,300,CM,IBN:hub.example",
    ",1,First,Somewhere,One,-Unpublished-,300,IBN:one.example",
    ",3,Third,Elsewhere,Three,-Unpublished-,300,IBN:new-three.example",
    ",40,ReLink,Somewhere,Carrier,-Unpublished-,300,IBN:relink.example",
    ",41,Newcomer,Somewhere,Four,-Unpublished-,300,IBN:four.example",
    ";A",
]


def _crc16_reference(data: bytes) -> int:
    """A bit-by-bit CRC-16 with FTS-5000's polynomial, 2**16 + 2**12 + 2**5 + 1,
    written out independently of `binascii.crc_hqx`."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def _nodelist(day: int, body: list[str], *, crc: int | None = None) -> bytes:
    """A list as a network hatches it: CR LF lines, the CRC in the first line,
    and an end-of-file byte."""
    text = "".join(line + "\r\n" for line in body).encode("ascii")
    check = _crc16_reference(text) if crc is None else crc
    header = f";A Test Net Nodelist for Friday, October 16, 2026 -- Day number {day:03d} : {check:05d}\r\n"
    return header.encode("ascii") + text + b"\x1a"


def _nodediff(old: bytes, new: bytes) -> bytes:
    """A NODEDIFF making `new` of `old`, as a nodelist hub generates it."""
    old_lines, new_lines = list_lines(old), list_lines(new)
    out = [old_lines[0]]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            out.append(b"C%d" % (i2 - i1))
            continue
        if i2 > i1:
            out.append(b"D%d" % (i2 - i1))
        if j2 > j1:
            out.append(b"A%d" % (j2 - j1))
            out.extend(new_lines[j1:j2])
    return b"".join(line + b"\r\n" for line in out)  # a diff has no end-of-file byte


def _zip(name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return buffer.getvalue()


LIST_289 = _nodelist(289, BODY_289)
LIST_296 = _nodelist(296, BODY_296)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def network(db):
    return save_network(db, FtnNetwork(
        name="TestNet", domain="testnet", our_address=NODE, uplink_address=HUB, uplink_host="hub.example",
        nodelist_echo="test_list", nodelist_name="testlist", nodediff_name="testdiff"))


def _arrive(db, network, tmp_path, name: str, content: bytes, *, tag: str = "TEST_LIST") -> bool:
    path = tmp_path / name
    path.write_bytes(content)
    return handle_received_file(db, get_network(db, network.id), tag, name, path)


# --- CRC-16 and the first line ------------------------------------------------


def test_the_crc_is_fts5000s_polynomial_over_the_lines_after_the_first():
    lines = list_lines(LIST_289)
    assert list_crc(lines) == _crc16_reference("".join(line + "\r\n" for line in BODY_289).encode())
    assert list_crc([b"header", b"123456789"]) == _crc16_reference(b"123456789\r\n")
    assert _crc16_reference(b"123456789") == 0x31C3  # CRC-16/XMODEM's published check value


def test_the_crc_is_computed_as_if_the_list_had_cr_lf_lines():
    lf_only = LIST_289.replace(b"\r\n", b"\n").rstrip(b"\x1a")
    assert list_crc(list_lines(lf_only)) == list_crc(list_lines(LIST_289))
    assert list_lines(LIST_289 + b"trailing junk after the EOF") == list_lines(LIST_289)


def test_the_first_line_gives_the_day_number_and_the_check_value():
    header = parse_header(b";A NixNet Nodelist for Friday, October 16, 2026 -- Day number 289 : 52372")
    assert (header.day, header.crc) == (289, 52372)
    assert (parse_header(b";A FidoNet Nodelist for Friday, July 3, 1987 -- Day number 184 : 05943").day) == 184
    assert (parse_header(b";A a list without a check value").crc, parse_header(b";A").day) == (None, None)
    assert parse_header(b";A Day number 1 : not-a-number").crc is None


# --- full lists from the echo -------------------------------------------------


def test_a_full_list_from_the_echo_is_imported_and_kept(db, network, tmp_path):
    assert _arrive(db, network, tmp_path, "TESTLIST.289", LIST_289)
    status = nodelist_status(db, network.id)
    assert (status.day, status.how, status.entries, status.crc) == (289, "full", 6, parse_header(
        list_lines(LIST_289)[0]).crc)
    assert status.failure is None and not status.waiting_for_full
    assert kept_list(db, network.id) == list_lines(LIST_289)
    assert direct_route(db, network.id, FtnAddress(86, 549, 2))[1] == "two.example"


def test_a_zipped_list_is_opened_by_content(db, network, tmp_path):
    assert _arrive(db, network, tmp_path, "TESTLIST.Z89", _zip("TESTLIST.289", LIST_289))
    assert nodelist_status(db, network.id).day == 289
    assert _arrive(db, network, tmp_path, "testlist.zip", _zip("testlist.296", LIST_296))
    assert nodelist_status(db, network.id).day == 296


def test_a_list_failing_its_crc_is_held_and_the_old_list_stays(db, network, tmp_path):
    _arrive(db, network, tmp_path, "TESTLIST.289", LIST_289)
    damaged = LIST_296.replace(b"four.example", b"evil.example")
    assert _arrive(db, network, tmp_path, "TESTLIST.296", damaged)
    status = nodelist_status(db, network.id)
    assert status.day == 289 and "CRC-16 mismatch" in status.failure
    assert direct_route(db, network.id, FtnAddress(86, 549, 41)) is None
    (held,) = queue.list_held(db)
    assert held.file_name == "TESTLIST.296" and "CRC-16 mismatch" in held.reason
    assert queue.held_content(db, held.id) == damaged


def test_a_list_from_the_echo_must_carry_a_crc(db, network, tmp_path):
    no_check = b";A no check value\r\n" + LIST_289.split(b"\r\n", 1)[1]
    assert _arrive(db, network, tmp_path, "TESTLIST.289", no_check)
    assert nodelist_status(db, network.id).imported_at is None
    assert "carries no CRC-16" in nodelist_status(db, network.id).failure


def test_other_files_and_other_echoes_are_left_to_the_caller(db, network, tmp_path):
    assert not _arrive(db, network, tmp_path, "TESTINFO.ZIP", b"PK\x05\x06" + bytes(18))
    assert not _arrive(db, network, tmp_path, "TESTLIST.289", LIST_289, tag="TEST_INFO")
    assert not _arrive(db, network, tmp_path, "TESTLIST.TXT", LIST_289)
    unset = save_network(db, FtnNetwork(name="Other", domain="other", our_address=NODE, uplink_address=HUB))
    path = tmp_path / "NODELIST.289"
    path.write_bytes(LIST_289)
    assert not handle_received_file(db, unset, "", "NODELIST.289", path)
    assert nodelist_status(db, network.id).imported_at is None and queue.list_held(db) == []


def test_another_archive_kind_is_refused_and_held(db, network, tmp_path):
    rar = b"Rar!\x1a\x07\x00" + bytes(40)
    assert _arrive(db, network, tmp_path, "TESTLIST.R89", rar)
    assert "RAR archive" in nodelist_status(db, network.id).failure
    (held,) = queue.list_held(db)
    assert held.file_name == "TESTLIST.R89"


def test_a_zip_without_the_list_is_refused(db, network, tmp_path):
    assert _arrive(db, network, tmp_path, "TESTLIST.Z89", _zip("README.TXT", b"hello"))
    assert "holds no TESTLIST.nnn" in nodelist_status(db, network.id).failure


# --- NODEDIFF ---------------------------------------------------------------


def test_the_nodediff_generator_and_applier_agree():
    diff = list_lines(_nodediff(LIST_289, LIST_296))
    assert apply_nodediff(list_lines(LIST_289), diff) == list_lines(LIST_296)
    assert diff[0] == list_lines(LIST_289)[0]


def test_a_diff_is_applied_to_the_kept_list(db, network, tmp_path):
    _arrive(db, network, tmp_path, "TESTLIST.289", LIST_289)
    assert _arrive(db, network, tmp_path, "TESTDIFF.Z96", _zip("TESTDIFF.296", _nodediff(LIST_289, LIST_296)))
    status = nodelist_status(db, network.id)
    assert (status.day, status.how, status.entries) == (296, "diff", 6)
    assert kept_list(db, network.id) == list_lines(LIST_296)
    assert direct_route(db, network.id, FtnAddress(86, 549, 41))[1] == "four.example"
    assert direct_route(db, network.id, FtnAddress(86, 549, 2)) is None
    assert queue.list_held(db) == []


def test_a_diff_for_another_list_waits_for_a_full_list(db, network, tmp_path):
    _arrive(db, network, tmp_path, "TESTLIST.289", LIST_289)
    list_303 = _nodelist(303, BODY_289)
    assert _arrive(db, network, tmp_path, "TESTDIFF.303", _nodediff(LIST_296, list_303))
    status = nodelist_status(db, network.id)
    assert status.day == 289 and status.waiting_for_full
    assert "another list" in status.failure and "waiting for a full list" in status.failure
    (held,) = queue.list_held(db)
    assert held.file_name == "TESTDIFF.303"
    # The fall-back ends with the next full list.
    _arrive(db, network, tmp_path, "TESTLIST.303", list_303)
    status = nodelist_status(db, network.id)
    assert status.day == 303 and not status.waiting_for_full


def test_a_diff_with_no_kept_list_waits_for_a_full_list(db, network, tmp_path):
    assert _arrive(db, network, tmp_path, "TESTDIFF.296", _nodediff(LIST_289, LIST_296))
    status = nodelist_status(db, network.id)
    assert status.imported_at is None and status.waiting_for_full and "no kept list" in status.failure


def test_a_diff_whose_result_fails_its_crc_changes_nothing(db, network, tmp_path):
    _arrive(db, network, tmp_path, "TESTLIST.289", LIST_289)
    bad_target = _nodelist(296, BODY_296, crc=12345)
    assert _arrive(db, network, tmp_path, "TESTDIFF.296", _nodediff(LIST_289, bad_target))
    status = nodelist_status(db, network.id)
    assert status.day == 289 and status.waiting_for_full and "fails its check" in status.failure
    assert kept_list(db, network.id) == list_lines(LIST_289)


def test_a_diff_running_past_the_kept_list_is_refused(db, network, tmp_path):
    _arrive(db, network, tmp_path, "TESTLIST.289", LIST_289)
    diff = list_lines(LIST_289)[0] + b"\r\nC500\r\n"
    assert _arrive(db, network, tmp_path, "TESTDIFF.296", diff)
    assert "past the end of the kept list" in nodelist_status(db, network.id).failure


def test_a_diff_after_its_full_list_is_skipped(db, network, tmp_path):
    _arrive(db, network, tmp_path, "TESTLIST.296", LIST_296)
    assert _arrive(db, network, tmp_path, "TESTDIFF.296", _nodediff(LIST_289, LIST_296))
    status = nodelist_status(db, network.id)
    assert (status.day, status.how, status.failure, status.waiting_for_full) == (296, "full", None, False)
    assert queue.list_held(db) == []


def test_a_list_imported_by_hand_is_kept_so_diffs_apply(db, network, tmp_path):
    import_nodelist(db, network.id, LIST_289.decode("ascii"))
    assert nodelist_status(db, network.id).how == "manual"
    _arrive(db, network, tmp_path, "TESTDIFF.296", _nodediff(LIST_289, LIST_296))
    assert nodelist_status(db, network.id).day == 296


# --- by hand ------------------------------------------------------------------


def test_the_cli_imports_a_zip_and_checks_the_crc(db, network, tmp_path):
    path = tmp_path / "NODELIST.Z89"
    path.write_bytes(_zip("TESTLIST.289", LIST_289))
    assert run_ftn_import_nodelist(db, "TestNet", path) == "Imported 6 nodes for TestNet from NODELIST.Z89 (day 289)."
    assert nodelist_status(db, network.id).how == "manual"
    damaged = tmp_path / "TESTLIST.296"
    damaged.write_bytes(LIST_296.replace(b"four", b"five"))
    with pytest.raises(NodelistError, match="CRC-16 mismatch"):
        run_ftn_import_nodelist(db, "TestNet", damaged)
    assert nodelist_status(db, network.id).day == 289


def test_a_zip_given_by_hand_may_hold_any_one_list(db, tmp_path):
    network = save_network(db, FtnNetwork(name="Plain", domain="plain", our_address=NODE, uplink_address=HUB))
    path = tmp_path / "list.zip"
    path.write_bytes(_zip("NODELIST.289", LIST_289))
    assert "Imported 6 nodes" in run_ftn_import_nodelist(db, "Plain", path)
    two = io.BytesIO()
    with zipfile.ZipFile(two, "w") as archive:
        archive.writestr("A.289", LIST_289)
        archive.writestr("B.289", LIST_289)
    path.write_bytes(two.getvalue())
    with pytest.raises(NodelistError, match="more than one list"):
        run_ftn_import_nodelist(db, "Plain", path)
    assert network.id is not None


# --- settings and the status screen ------------------------------------------------


def test_the_nodelist_settings_are_checked(db):
    base = dict(name="N", domain="n", our_address=NODE, uplink_address=HUB)
    saved = save_network(db, FtnNetwork(**base, nodelist_echo=" nix_list ", nodelist_name="nixlist"))
    assert (saved.nodelist_echo, saved.nodelist_name, saved.nodediff_name) == ("NIX_LIST", "NIXLIST", "")
    assert get_network(db, saved.id).nodelist_name == "NIXLIST"
    with pytest.raises(FtnNetworkError, match="needs the nodelist's name"):
        save_network(db, FtnNetwork(**{**base, "name": "M"}, nodelist_echo="NIX_LIST"))
    with pytest.raises(FtnNetworkError, match="without its extension"):
        save_network(db, FtnNetwork(**{**base, "name": "M"}, nodelist_name="NIXLIST.289"))
    with pytest.raises(FtnNetworkError, match="different names"):
        save_network(db, FtnNetwork(**{**base, "name": "M"}, nodelist_name="X", nodediff_name="x"))


def test_the_status_screen_shows_the_day_the_crc_and_the_fall_back(db, network, tmp_path):
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    _arrive(db, network, tmp_path, "TESTLIST.289", LIST_289)
    _arrive(db, network, tmp_path, "TESTDIFF.303", _nodediff(LIST_296, _nodelist(303, BODY_289)))
    lane = DatabaseLane(db.path)
    controls = NodeControls(session_registry=ActiveSessionRegistry(), maintenance=MaintenanceMode(),
                            shutdown_event=asyncio.Event(), graceful_delay_seconds=60.0)
    try:
        session = FakeSession(["b"])
        asyncio.run(ftn_status_screen(session, lane, sysop, controls))
    finally:
        lane.close()
    text = _visible(_written_text(session))
    crc = parse_header(list_lines(LIST_289)[0]).crc
    assert "day 289, 6 nodes" in text
    assert "(full list)" in text
    assert f"{crc:05d} verified" in text
    assert "TEST_LIST: TESTLIST.nnn, diffs TESTDIFF.nnn" in text
    assert "for a full list" in text
    assert "TESTDIFF.303 not applied" in text


def test_the_network_editor_sets_the_nodelist_echo(db):
    from netbbs.net.ftn_console import edit_network

    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    existing = save_network(db, FtnNetwork(name="nixNet", domain="nixnet", our_address=NODE, uplink_address=HUB))
    lane = DatabaseLane(db.path)
    try:
        session = FakeSession(["2", "0", "nix_list", "s", "2", "1", "nixlist", "2", "2", "nixdiff", "s"])
        asyncio.run(edit_network(session, lane, sysop, existing))
    finally:
        lane.close()
    assert "needs the nodelist's name" in _visible(_written_text(session))
    saved = get_network(db, existing.id)
    assert (saved.nodelist_echo, saved.nodelist_name, saved.nodediff_name) == ("NIX_LIST", "NIXLIST", "NIXDIFF")


def test_a_zip_with_a_broken_member_is_refused_and_held(db, network, tmp_path):
    good = _zip("TESTLIST.289", LIST_289)
    start = good.index(b"TESTLIST.289") + len("TESTLIST.289")  # the local header's name, then the data
    broken = good[:start] + bytes(b ^ 0x5A for b in good[start:start + 40]) + good[start + 40:]
    assert _arrive(db, network, tmp_path, "TESTLIST.Z89", broken)
    assert "cannot unpack" in nodelist_status(db, network.id).failure or "damaged" in (
        nodelist_status(db, network.id).failure)
    assert len(queue.list_held(db)) == 1
