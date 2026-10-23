"""FTN file requests (design doc §6.8, "File requests"; issue #1475): the
`.REQ` format, the settings and limits, the areas offered, and requests
made and answered over real loopback BinkP between two nodes' own
databases, in both roles."""

from __future__ import annotations

import asyncio
import datetime
import logging
import socket
from dataclasses import replace

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.config import set_config
from netbbs.files.areas import create_file_area, update_file_area
from netbbs.files.entries import upload_file
from netbbs.ftn import binkp, freq, outbound, queue
from netbbs.ftn import listener as listener_module
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import OutgoingFile, ReceivedFile, SessionResult, SystemInfo, run_session
from netbbs.ftn.exchange import toss_received
from netbbs.ftn.freq import FreqAnswerer, FreqError, FreqSettings, RequestLine
from netbbs.ftn.listener import FtnListener
from netbbs.ftn.mailer import FtnMailer
from netbbs.ftn.netmail import send_netmail
from netbbs.ftn.networks import BUNDLE_ZIP, FtnNetwork, get_network, save_network, set_file_area_tic
from netbbs.ftn.nodelist import import_nodelist
from netbbs.ftn.queue import held_content, list_held
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)
JOE = FtnAddress(21, 1, 110)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Node:
    """One BBS: its own database, SysOp, network and listen port."""

    def __init__(self, path, ours: FtnAddress, uplink: FtnAddress, *, uplink_port: int = 24554):
        self.db = Database(path)
        self.port = _free_port()
        set_config(self.db, listener_module.HOST_CONFIG_KEY, "127.0.0.1")
        set_config(self.db, listener_module.PORT_CONFIG_KEY, str(self.port))
        self.sysop = create_user(self.db, "Carrier", password="hunter2pw", user_level=SYSOP_LEVEL)
        self.network = save_network(self.db, FtnNetwork(
            name="fsxNet", domain="fsxnet", our_address=ours, uplink_address=uplink, uplink_host="127.0.0.1",
            uplink_port=uplink_port, session_password="SECRET", enabled=True, answers_calls=True))

    def offer(self, files: dict[str, bytes], *, name="uploads"):
        area = create_file_area(self.db, name, creator=self.sysop)
        for file_name, data in files.items():
            upload_file(self.db, area, self.sysop, file_name, data)
        freq.set_area_offered(self.db, area, True)
        return area

    def answer(self, **settings):
        freq.save_settings_without_commit(self.db, FreqSettings(self.network.id, answer=True, **settings))
        self.db.connection.commit()

    def held(self):
        return {h.file_name: (h.reason, held_content(self.db, h.id)) for h in list_held(self.db)}

    def records(self, direction):
        return [r for r in freq.recent_requests(self.db, self.network.id, limit=50) if r.direction == direction]


@pytest.fixture
def node(tmp_path):
    made = Node(tmp_path / "node.db", NODE, HUB)
    yield made
    made.db.close()


@pytest.fixture
def pair(tmp_path):
    """This node and its hub, each the other's uplink, both answering."""
    hub = Node(tmp_path / "hub.db", HUB, NODE)
    ours = Node(tmp_path / "node.db", NODE, HUB, uplink_port=hub.port)
    hub.network = save_network(hub.db, replace(hub.network, uplink_port=ours.port))
    yield ours, hub
    ours.db.close()
    hub.db.close()


async def _settled(listener, calls: int = 1) -> None:
    for _ in range(500):
        if len(listener.recent) >= calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the listener never finished the call")


def _poll(caller: Node, answerer: Node):
    """`caller`'s mailer calls `answerer`'s listener; both run the FREQ
    hook, as the node does."""

    async def run():
        caller_lane, answer_lane = DatabaseLane(caller.db.path), DatabaseLane(answerer.db.path)
        listener = FtnListener(answer_lane, batch_hook=FreqAnswerer(answer_lane))
        try:
            await listener.reconcile()
            status = await FtnMailer(caller_lane, batch_hook=FreqAnswerer(caller_lane)).poll(
                get_network(caller.db, caller.network.id))
            await _settled(listener)
            return status, list(listener.recent)
        finally:
            await listener.close()
            caller_lane.close()
            answer_lane.close()

    return asyncio.run(run())


# -- the .REQ format ---------------------------------------------------------


def test_a_req_names_its_node_in_hex_and_lines_follow_fts_0006():
    assert freq.req_send_name(FtnAddress(1, 12, 2)) == "000C0002.REQ"
    lines = [RequestLine("this.arc", "that"), RequestLine("nodelist.*"), RequestLine("NEWOPUS.*", update="+599634000")]
    data = freq.format_req(lines)
    assert data == b"this.arc !that\r\nnodelist.*\r\nNEWOPUS.* +599634000\r\n"
    assert freq.parse_req(data) == (lines, 0)
    # Other line ends, comments, words it does not know and a trailing EOF.
    parsed, cut = freq.parse_req(b"; a comment\nFILES\rABOUT junk !pw -100\x1aIGNORED\n")
    assert [(line.name, line.password, line.update) for line in parsed] == [("FILES", "", ""), ("ABOUT", "pw", "-100")]
    assert cut == 0


def test_a_req_is_read_within_its_bounds():
    data = b"".join(b"F%d.ZIP\r\n" % n for n in range(freq.MAX_REQ_LINES + 5))
    lines, cut = freq.parse_req(data)
    assert len(lines) == freq.MAX_REQ_LINES and cut == 5
    lines, cut = freq.parse_req(b"A" * (freq.MAX_REQ_BYTES + 10))
    assert cut == 1 and len(lines[0].name) == freq.MAX_REQ_BYTES


def test_what_the_sysop_types_becomes_request_lines():
    lines = freq.request_lines_from_text("files  THIS.ARC !secret nodelist.*")
    assert [(line.name, line.password) for line in lines] == [("FILES", ""), ("THIS.ARC", "secret"),
                                                            ("nodelist.*", "")]
    for text, reason in (("", "at least one"), ("!pw", "follows the file name"), ("../etc/passwd", "folder"),
                         ("C:BOOT.INI", "folder or a drive"), ("*.*", "wildcards alone"),
                         (" ".join(f"F{n}" for n in range(freq.MAX_REQUEST_NAMES + 1)), "at most")):
        with pytest.raises(FreqError, match=reason):
            freq.request_lines_from_text(text)


# -- settings and areas --------------------------------------------------------


def _relevel(node, area, level):
    return update_file_area(
        node.db, area, name=area.name, description=area.description, min_read_level=level,
        min_write_level=area.min_write_level, category_id=area.category_id, pinned=area.pinned,
        moderated=area.moderated, max_file_age_days=area.max_file_age_days, min_age=area.min_age,
        name_requirement=area.name_requirement, community_id=area.community_id, changed_by=node.sysop)


def test_answering_is_off_by_default_and_the_settings_are_bounded(node):
    settings = freq.get_settings(node.db, node.network.id)
    assert not settings.answer and settings.secure_only
    with pytest.raises(FreqError, match="Files per session"):
        freq.validate_settings(replace(settings, session_files=0))
    with pytest.raises(FreqError, match="MiB per node a day"):
        freq.validate_settings(replace(settings, day_bytes=5000 * freq.MIB))
    with pytest.raises(FreqError, match="ABOUT file"):
        freq.validate_settings(replace(settings, answer=True, about_path="/nowhere/at/all.txt"))


def test_only_an_area_anyone_could_read_is_offered(node):
    area = create_file_area(node.db, "members", creator=node.sysop)
    restricted = _relevel(node, area, 20)
    with pytest.raises(FreqError, match="read level"):
        freq.set_area_offered(node.db, restricted, True)
    open_area = _relevel(node, restricted, 0)
    freq.set_area_offered(node.db, open_area, True)
    assert freq.is_area_offered(node.db, open_area)
    assert [a.name for a in freq.servable_areas(node.db, node.network)] == ["members"]
    # Gaining a read level later stops it being served, without the SysOp.
    _relevel(node, open_area, 50)
    assert freq.servable_areas(node.db, node.network) == []


def test_an_area_carrying_another_networks_file_echo_is_not_served_on_this_one(node):
    area = node.offer({"A.ZIP": b"a"})
    other = save_network(node.db, FtnNetwork(name="AgoraNet", domain="agoranet", our_address=FtnAddress(46, 1, 9),
                                             uplink_address=FtnAddress(46, 1, 1)))
    set_file_area_tic(node.db, area, other.id, "AGN_FILES")
    assert freq.servable_areas(node.db, node.network) == []
    assert [a.name for a in freq.servable_areas(node.db, other)] == ["uploads"]


# -- answering -----------------------------------------------------------------


def _answer(node, text: bytes, *, secure=True, requester=HUB, tally=None):
    return freq.answer_request(node.db, node.network, requester, secure, text, tally or freq._Tally())


def test_a_request_is_answered_from_offered_areas_and_magic_names(node, tmp_path):
    node.offer({"README.ZIP": b"read me", "GAME.ZIP": b"game"})
    hidden = create_file_area(node.db, "private", creator=node.sysop)
    upload_file(node.db, hidden, node.sysop, "SECRET.ZIP", b"never")
    about = tmp_path / "about.txt"
    about.write_bytes(b"About this BBS")
    node.answer(about_path=str(about))
    import_nodelist(node.db, node.network.id, "Zone,21,Z,X,Y,-,300\n")

    planned = _answer(node, b"readme.zip\r\nFILES\r\nABOUT\r\nNODELIST\r\nSECRET.ZIP\r\n*.ZIP\r\n")
    names = [p.name for p in planned]
    assert names == ["README.ZIP", "FILES.TXT", "about.txt", "NODELIST.TXT", "GAME.ZIP"]
    listing = next(p.data for p in planned if p.name == "FILES.TXT").decode("cp437")
    assert "README.ZIP" in listing and "GAME.ZIP" in listing and "SECRET" not in listing
    (record,) = node.records("in")
    assert record.files == 5 and "not found: SECRET.ZIP" in record.outcome


def test_traversal_and_bare_wildcards_are_refused_and_a_password_is_never_kept(node, caplog):
    node.offer({"README.ZIP": b"read me"})
    node.answer()
    with caplog.at_level(logging.INFO, logger="netbbs.ftn.freq"):
        planned = _answer(node, b"../../node.db\r\n..\\x\r\nC:x\r\n*.*\r\n*\r\nREADME.ZIP !hunter2 +0\r\n")
    assert [p.name for p in planned] == ["README.ZIP"]
    (record,) = node.records("in")
    assert "refused (folder, drive or wildcards alone)" in record.outcome
    assert "hunter2" not in record.names + record.outcome and "hunter2" not in caplog.text


def test_an_update_request_keeps_newer_or_older_files(node):
    node.offer({"NEW.ZIP": b"n"})
    node.answer()
    assert [p.name for p in _answer(node, b"NEW.ZIP +4000000000\r\n")] == []
    assert [p.name for p in _answer(node, b"NEW.ZIP +0\r\n")] == ["NEW.ZIP"]
    assert [p.name for p in _answer(node, b"NEW.ZIP -0\r\n")] == []


def test_answering_off_or_an_unproven_caller_is_refused_and_logged(node):
    node.offer({"README.ZIP": b"read me"})
    assert _answer(node, b"README.ZIP\r\n") == []
    assert "answers no file requests" in node.records("in")[0].outcome
    node.answer()
    assert _answer(node, b"README.ZIP\r\n", secure=False) == []
    assert "only secure links" in node.records("in")[0].outcome
    node.answer(secure_only=False)
    assert [p.name for p in _answer(node, b"README.ZIP\r\n", secure=False)] == ["README.ZIP"]


def test_the_session_and_daily_limits_hold(node):
    node.offer({f"F{n}.ZIP": bytes([n]) * 100 for n in range(6)})
    node.answer(session_files=2, day_files=3)
    tally = freq._Tally()
    assert len(_answer(node, b"F*.ZIP\r\n", tally=tally)) == 2
    assert _answer(node, b"F5.ZIP\r\n", tally=tally) == []  # the session's two are spent
    assert "over the limits" in node.records("in")[0].outcome
    assert len(_answer(node, b"F*.ZIP\r\n")) == 1  # a new session: the node's day has one left
    assert _answer(node, b"F*.ZIP\r\n") == []
    # Another node has its own day; the bytes bound counts too.
    node.answer(session_files=10, day_files=10, session_bytes=freq.MIB)
    assert len(_answer(node, b"F*.ZIP\r\n", requester=JOE)) == 6


def test_a_request_the_hook_never_saw_is_refused_not_held(node):
    node.answer()
    result = SessionResult(remote_addresses=[HUB], secure=True, multi_batch=False,
                           received=[ReceivedFile("000C0002.REQ", b"README.ZIP\r\n")])
    lane = DatabaseLane(node.db.path)
    try:
        asyncio.run(toss_received(lane, node.network, result))
    finally:
        lane.close()
    assert node.held() == {}
    (record,) = node.records("in")
    assert "binkp/1.0" in record.outcome


# -- making requests -------------------------------------------------------------


def test_a_request_goes_to_the_uplink_or_a_listed_node_and_waits_crash(node):
    lines = freq.request_lines_from_text("FILES")
    with pytest.raises(FreqError, match="does not list 21:1/110"):
        freq.queue_request(node.db, node.network, JOE, lines)
    with pytest.raises(FreqError, match="own address"):
        freq.queue_request(node.db, node.network, NODE, lines)
    record = freq.queue_request(node.db, node.network, HUB, lines)
    import_nodelist(node.db, node.network.id,
                    "Zone,21,Z,X,Y,-,300\nHost,1,N,X,Y,-,300\n,110,Joe,X,Y,-,300,IBN:joe.example\n")
    freq.queue_request(node.db, node.network, JOE, lines)
    second = freq.queue_request(node.db, node.network, HUB, freq.request_lines_from_text("ABOUT"))
    rows = {(f.destination, f.route, f.flavour, f.send_name) for f in outbound.pending_files(node.db, node.network.id)}
    assert ("21:1/100", "uplink", "crash", "00010064.REQ") in rows
    assert ("21:1/110", "direct", "crash", "0001006E.REQ") in rows
    assert second.send_name != "00010064.REQ" and second.send_name.endswith(".REQ")
    assert freq.request_state(node.db, record) == "waiting for the next call"


def test_a_dropped_request_says_why(node):
    record = freq.queue_request(node.db, node.network, HUB, freq.request_lines_from_text("FILES"))
    (row,) = outbound.pending_files(node.db, node.network.id)
    for _ in range(outbound.CRASH_ATTEMPTS):
        outbound.note_failed_crash(node.db, node.network.id, [], [row.id])
    (record,) = node.records("out")
    assert freq.request_state(node.db, record).startswith("not sent: the crash call failed")


def _toss(node, files, *, secure=True, sent=(), remote=HUB):
    result = SessionResult(remote_addresses=[remote], secure=secure, sent=list(sent),
                           received=[ReceivedFile(name, data) for name, data in files])
    lane = DatabaseLane(node.db.path)
    try:
        return asyncio.run(toss_received(lane, node.network, result))
    finally:
        lane.close()


def test_a_reply_in_a_later_call_is_known_by_its_name_and_never_expires(node):
    freq.queue_request(node.db, node.network, HUB, freq.request_lines_from_text("GAME*.ZIP FILES"))
    _toss(node, [("GAME2.ZIP", b"PK game"), ("ALLFILES.TXT", b"list"), ("OTHER.ZIP", b"PK other")])
    held = {h.file_name: h for h in list_held(node.db)}
    assert held["GAME2.ZIP"].reason.startswith("requested from 21:1/100")
    assert held["ALLFILES.TXT"].reason.startswith("requested from 21:1/100")
    assert held["OTHER.ZIP"].reason.startswith("waiting for its .TIC")
    # Both still wait for a TIC, but only the requested one never expires.
    assert held["GAME2.ZIP"].waiting and held["OTHER.ZIP"].waiting
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    expired = {h.file_name for h in queue.expire_unpaired(node.db, now=later)}
    assert "OTHER.ZIP" in expired and "GAME2.ZIP" not in expired
    # From another node, the same name answers nothing.
    _toss(node, [("GAME3.ZIP", b"PK")], remote=JOE)
    assert next(h for h in list_held(node.db) if h.file_name == "GAME3.ZIP").reason.startswith("waiting")
    (record,) = node.records("out")
    assert record.files == 2


def test_received_requests_never_evict_the_requests_made_here(node, monkeypatch):
    monkeypatch.setattr(freq, "MAX_LOG_ROWS", 3)
    freq.queue_request(node.db, node.network, HUB, freq.request_lines_from_text("FILES"))
    for _ in range(5):
        _answer(node, b"README.ZIP\r\n")
    assert len(node.records("out")) == 1 and len(node.records("in")) == 3


def test_a_reply_from_an_unproven_session_is_held_unopened_and_says_so(node):
    import_nodelist(node.db, node.network.id,
                    "Zone,21,Z,X,Y,-,300\nHost,1,N,X,Y,-,300\n,110,Joe,X,Y,-,300,IBN:joe.example\n")
    record = freq.queue_request(node.db, node.network, JOE, freq.request_lines_from_text("README.ZIP"))
    _toss(node, [("README.ZIP", b"PK")], secure=False, sent=[record.send_name], remote=JOE)
    (held,) = list_held(node.db)
    assert held.reason.startswith("requested from 21:1/110") and held.reason.endswith("unsecure session")


# -- over real loopback BinkP, both roles --------------------------------------------


def test_a_request_to_the_hub_is_answered_in_the_same_call(pair):
    ours, hub = pair
    hub.offer({"README.ZIP": b"read me", "GAME.ZIP": b"a game"})
    hub.answer()
    freq.queue_request(ours.db, ours.network, HUB, freq.request_lines_from_text("FILES README.ZIP NOPE.ZIP"))

    status, calls = _poll(ours, hub)

    assert status.last_error is None
    held = ours.held()
    assert held["README.ZIP"][1] == b"read me"
    assert held["README.ZIP"][0].startswith("requested from 21:1/100")
    assert b"GAME.ZIP" in held["FILES.TXT"][1]
    assert outbound.pending_files(ours.db, ours.network.id) == []
    assert list((outbound.spool_dir(ours.db) / "freq").iterdir()) == []  # the .REQ went and was deleted
    (made,) = ours.records("out")
    assert freq.request_state(ours.db, made) == "sent; 2 files back (" + freq._size(len(held["FILES.TXT"][1]) + 7) \
        + "), in Held items"
    (answered,) = hub.records("in")
    assert answered.files == 2 and "not found: NOPE.ZIP" in answered.outcome and answered.secure
    assert hub.held() == {}  # the .REQ is never held


def test_a_request_and_its_answer_never_ride_in_a_mail_bundle(pair, monkeypatch):
    ours, hub = pair
    # Both bundle their mail, and both have mail waiting: the .REQ and the
    # answer still travel as files of their own, beside each side's bundle.
    for node, other in ((ours, "Hub Op (21:1/100)"), (hub, "Node Op (21:1/199)")):
        node.network = save_network(node.db, replace(node.network, bundle_outgoing=BUNDLE_ZIP))
        send_netmail(node.db, node.sysop, other, "Hello", "Mail that rides in a bundle")
    hub.offer({"README.ZIP": b"read me"})
    hub.answer()
    freq.queue_request(ours.db, ours.network, HUB, freq.request_lines_from_text("README.ZIP"))
    announced = []
    original = binkp._Session.send

    async def send(self, command, argument):
        if command == binkp.M_FILE:
            announced.append(argument.split(" ", 1)[0])
        await original(self, command, argument)

    monkeypatch.setattr(binkp._Session, "send", send)
    status, calls = _poll(ours, hub)

    assert status.last_error is None
    assert "00010064.REQ" in announced and "README.ZIP" in announced
    bundles = [name for name in announced if name not in ("00010064.REQ", "README.ZIP")]
    assert len(bundles) == 2 and not any(name.lower().endswith(".pkt") for name in bundles)
    assert ours.held()["README.ZIP"][1] == b"read me"
    assert hub.records("in")[0].files == 1


def test_a_request_from_a_node_this_bbs_calls_is_answered_by_the_mailer(pair):
    ours, hub = pair
    # The hub asks; this node's mailer, calling it, answers.
    ours.offer({"OURS.ZIP": b"from the node"})
    ours.answer()
    freq.queue_request(hub.db, hub.network, NODE, freq.request_lines_from_text("OURS.ZIP"))

    status, calls = _poll(ours, hub)

    assert status.last_error is None
    assert hub.held()["OURS.ZIP"][1] == b"from the node"
    (answered,) = ours.records("in")
    assert answered.files == 1 and answered.node == "21:1/100"


def test_a_stranger_is_refused_under_secure_links_only(node):
    node.offer({"README.ZIP": b"read me"})
    node.answer()

    async def run():
        lane = DatabaseLane(node.db.path)
        listener = FtnListener(lane, batch_hook=FreqAnswerer(lane))
        try:
            await listener.reconcile()
            reader, writer = await asyncio.open_connection("127.0.0.1", node.port)
            try:
                result = await run_session(reader, writer, originating=True, our_addresses=[FtnAddress(21, 4, 4)],
                                           system=SystemInfo("Stranger", "op"), password="",
                                           outgoing=[OutgoingFile("00040004.REQ", b"README.ZIP\r\n")], timeout=5)
            finally:
                writer.close()
            await _settled(listener)
            return result
        finally:
            await listener.close()
            lane.close()

    result = asyncio.run(run())
    assert result.received == []
    (record,) = node.records("in")
    assert "only secure links" in record.outcome and record.node == "21:4/4"
    assert node.held() == {}


def test_a_binkp_1_0_session_is_not_answered(pair, monkeypatch):
    ours, hub = pair
    hub.offer({"README.ZIP": b"read me"})
    hub.answer()
    freq.queue_request(ours.db, ours.network, HUB, freq.request_lines_from_text("README.ZIP"))
    original = binkp._Session.send_info

    async def binkp_1_0(self, system, *, offer_cram):
        # What a binkp/1.0 mailer says: no OPT MB, VER binkp/1.0.
        sent = []
        real_send = self.send

        async def send(command, argument):
            if command == binkp.M_NUL and argument == "OPT MB":
                return
            if command == binkp.M_NUL and argument.startswith("VER "):
                argument = argument.replace("binkp/1.1", "binkp/1.0")
            sent.append(argument)
            await real_send(command, argument)

        self.send = send
        try:
            await original(self, system, offer_cram=offer_cram)
        finally:
            del self.send

    monkeypatch.setattr(binkp._Session, "send_info", binkp_1_0)
    status, calls = _poll(ours, hub)
    assert status.last_error is None
    assert ours.held() == {}
    (record,) = hub.records("in")
    assert "binkp/1.0" in record.outcome
