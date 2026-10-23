"""The console's file-request pieces (design doc §6.8, "File requests";
issue #1475): [R]equest files on the FTN status screen, the network
editor's File requests section, the status rows and a file area's [O]ffer
to file requests -- driven through the real screens with the scripted
`FakeSession` of `tests.test_admin_flow`."""

from __future__ import annotations

import asyncio
import re

from netbbs.files.areas import create_file_area
from netbbs.ftn import freq, outbound
from netbbs.ftn.networks import get_network
from netbbs.net.ftn_console import edit_network, ftn_status_screen
from netbbs.net.ftn_freq_console import area_offer_rows, toggle_area_offer
from tests.test_admin_flow import FakeSession, _visible, _written_text
from tests.test_ftn_console import _controls, _network, db, lane, sysop  # noqa: F401 -- fixtures


def _text(session) -> str:
    return " ".join(_visible(_written_text(session)).split())



def _field(lane, sysop, network, label: str) -> list[str]:
    """The keys that choose `label` in the network editor, read off its
    sections, so a section another feature adds earlier moves nothing here."""
    session = FakeSession([">"] * 20 + ["b"])
    asyncio.run(edit_network(session, lane, sysop, network))
    number = re.search(rf"(\d\d) {label}:", _visible(_written_text(session))).group(1)
    return list(number)


def test_request_files_queues_a_req_for_the_uplink(db, lane, sysop):
    network = _network(db)
    session = FakeSession(["r", "0", "2", "FILES README.ZIP !pw", "s", "b"])
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    text = _text(session)
    assert "File request to 21:1/100 queued: FILES README.ZIP. It goes out at once" in text
    assert "to 21:1/100: FILES README.ZIP -- waiting for the next call" in text
    (record,) = freq.recent_requests(db, network.id)
    assert "pw" not in record.names  # the password is in the .REQ only
    (row,) = outbound.pending_files(db, network.id)
    assert (row.send_name, row.flavour, row.route) == ("00010064.REQ", "crash", "uplink")
    with open(row.path, "rb") as handle:
        assert handle.read() == b"FILES\r\nREADME.ZIP !pw\r\n"


def test_a_request_to_an_unlisted_node_keeps_the_draft_with_the_reason(db, lane, sysop):
    network = _network(db)
    session = FakeSession(["r", "0", "1", "21:1/110", "0", "2", "FILES", "s", "b", "y", "b"])
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    assert "does not list 21:1/110 as answering BinkP" in _text(session)
    assert outbound.pending_files(db, network.id) == []


def test_the_network_editor_turns_answering_on_and_checks_the_files(db, lane, sysop):
    network = _network(db)
    answer, about = _field(lane, sysop, network, "Answer requests"), _field(lane, sysop, network, "ABOUT file")
    session = FakeSession([*answer, *about, "/nowhere/about.txt", "s", "b", "y"])
    asyncio.run(edit_network(session, lane, sysop, network))
    assert "ABOUT file: /nowhere/about.txt is not a file" in _text(session)
    assert not freq.get_settings(db, network.id).answer

    session = FakeSession([*answer, "s"])
    asyncio.run(edit_network(session, lane, sysop, get_network(db, network.id)))
    settings = freq.get_settings(db, network.id)
    assert settings.answer and settings.secure_only


def test_the_status_screen_says_whether_requests_are_answered(db, lane, sysop):
    network = _network(db)
    area = create_file_area(db, "uploads", creator=sysop)
    freq.set_area_offered(db, area, True)
    freq.save_settings_without_commit(db, freq.FreqSettings(network.id, answer=True, secure_only=False))
    db.connection.commit()
    freq.answer_request(db, get_network(db, network.id), network.uplink_address, False, b"NOPE.ZIP\r\n",
                        freq._Tally())
    session = FakeSession(["b"])
    asyncio.run(ftn_status_screen(session, lane, sysop, _controls()))
    text = _text(session)
    assert "File requests: on, anyone; 1 file areas offered" in text
    assert "from 21:1/100: NOPE.ZIP -- sent 0 files (0 B); not found: NOPE.ZIP" in text


def test_a_file_area_is_offered_from_its_screen_unless_it_has_a_read_level(db, lane, sysop):
    _network(db)
    area = create_file_area(db, "uploads", creator=sysop)
    assert area_offer_rows(db, area)[0].value == "not offered"
    assert asyncio.run(toggle_area_offer(FakeSession([]), lane, sysop, area))
    assert area_offer_rows(db, area)[0].value == "offered"
    assert asyncio.run(toggle_area_offer(FakeSession([]), lane, sysop, area))
    assert not freq.is_area_offered(db, area)

    members = create_file_area(db, "members", creator=sysop, min_read_level=20)
    assert not asyncio.run(toggle_area_offer(FakeSession([]), lane, sysop, members))
    assert not freq.is_area_offered(db, members)
