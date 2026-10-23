"""Netmail extras (design doc §6.8; issues #1470, #1471 and #1472): the
outbound files queue and its session interface, file attach out and in,
crash and hold flavours, and bouncing netmail no account can take -- on
real SQLite files, real loopback BinkP sessions and the real compose
screen."""

from __future__ import annotations

import asyncio
import datetime
import re
import socket
from dataclasses import replace

import pytest

from netbbs.attestation import attest_name
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.config import set_config
from netbbs.file_refs import mail_refs, ref_for_entry
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.ftn import attach as attach_module
from netbbs.ftn import listener as listener_module
from netbbs.ftn import outbound, queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.binkp import SessionResult, SystemInfo, run_session
from netbbs.ftn.bounce import BOUNCE_FROM, MAX_BOUNCES_PER_DAY
from netbbs.ftn.exchange import toss_received
from netbbs.ftn.listener import FtnListener
from netbbs.ftn.mailer import FtnMailer
from netbbs.ftn.message import FtnMessage, decode_message, encode_message
from netbbs.ftn.netmail import send_netmail
from netbbs.ftn.networks import FtnNetwork, get_network, save_network
from netbbs.ftn.nodelist import import_nodelist
from netbbs.ftn.packet import (
    ATTR_CRASH,
    ATTR_FILE_ATTACHED,
    ATTR_HOLD,
    PacketHeader,
    build_packet,
    build_packet_from_packed,
    parse_packet,
)
from netbbs.mail import MailError, list_inbox, list_sent
from netbbs.net.mail_flow import browse_mail
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_mail_flow import FakeSession, _written_text

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)
JOE = FtnAddress(21, 1, 110)
ANSI = re.compile(chr(27) + r"\[[0-9;]*m")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "Carrier", password="hunter2pw", user_level=SYSOP_LEVEL)


@pytest.fixture
def network(db, sysop):
    return save_network(db, FtnNetwork(
        name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB, uplink_host="127.0.0.1",
        session_password="SECRET", enabled=True, netmail_min_level=10))


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2pw", user_level=10)


@pytest.fixture
def area(db, sysop):
    return create_file_area(db, "netmail files", creator=sysop)


def _upload(db, area, sysop, name="REPORT.ZIP", data=b"PK report"):
    entry = upload_file(db, area, sysop, name, data)
    return ref_for_entry(entry, area)


def _unpack(packed: bytes) -> FtnMessage:
    data = build_packet_from_packed(PacketHeader(orig=NODE, dest=HUB, created=None), [packed])
    return decode_message(parse_packet(data).messages[0])


def _netmail(to="alice", *, frm="Joe Bloggs", subject="Hi", body="A private note", attributes=0,
             msgid="21:3/110 0000beef"):
    return FtnMessage(to_name=to, from_name=frm, subject=subject, body=body,
                      kludges=[("INTL", "21:1/199 21:3/110"), ("MSGID", msgid)],
                      date=datetime.datetime(2026, 10, 7, 9, 30), attributes=attributes,
                      orig_net=3, orig_node=110, dest_net=1, dest_node=199)


def _packet(*messages):
    return build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), [encode_message(m) for m in messages])


def _toss_session(db, network, files, *, secure=True):
    lane = DatabaseLane(db.path)
    try:
        result = SessionResult(remote_addresses=[HUB], secure=secure,
                               received=[file if not isinstance(file, tuple) else _received(*file) for file in files])
        return asyncio.run(toss_received(lane, network, result))
    finally:
        lane.close()


def _received(name, data):
    from netbbs.ftn.binkp import ReceivedFile

    return ReceivedFile(name, data)


# -- the outbound files queue ------------------------------------------------


def test_a_queued_file_is_an_area_entry_or_a_path_and_only_a_spooled_file_is_killed(db, network, area, sysop,
                                                                                     tmp_path):
    ref = _upload(db, area, sysop)
    common = dict(destination=HUB, route="uplink", send_name="REPORT.ZIP", size_bytes=9, reference_kind="test",
                  reference_id="1")
    with pytest.raises(ValueError, match="not both or neither"):
        outbound.queue_file_without_commit(db, network.id, **common)
    with pytest.raises(ValueError, match="not both or neither"):
        outbound.queue_file_without_commit(db, network.id, file_id=ref.file_id, path=tmp_path / "x", **common)
    outside = tmp_path / "elsewhere.txt"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError, match="spool folder"):
        outbound.queue_file_without_commit(db, network.id, path=outside, after_sent=outbound.KILL, **common)
    with pytest.raises(ValueError, match="BinkP sends as it is"):
        outbound.queue_file_without_commit(db, network.id, file_id=ref.file_id,
                                           **{**common, "send_name": "a b.zip"})
    spool = outbound.spool_dir(db)
    spool.mkdir(parents=True)
    (spool / "FILES.TXT").write_bytes(b"list")
    outbound.queue_file_without_commit(db, network.id, path=spool / "FILES.TXT", after_sent=outbound.KILL,
                                       **{**common, "send_name": "FILES.TXT"})
    outbound.queue_file_without_commit(db, network.id, file_id=ref.file_id, **common)
    db.connection.commit()
    assert [f.send_name for f in outbound.pending_files(db, network.id)] == ["FILES.TXT", "REPORT.ZIP"]


def test_the_files_queue_is_bounded(db, network, area, sysop, monkeypatch):
    ref = _upload(db, area, sysop)
    monkeypatch.setattr(outbound, "MAX_PENDING_FILES_PER_NETWORK", 1)
    kwargs = dict(destination=HUB, route="uplink", file_id=ref.file_id, send_name="REPORT.ZIP", size_bytes=9,
                  reference_kind="test", reference_id="1")
    outbound.queue_file_without_commit(db, network.id, **kwargs)
    with pytest.raises(queue.FtnQueueFullError):
        outbound.queue_file_without_commit(db, network.id, **kwargs)


def test_a_session_reads_its_files_lazily_and_settling_removes_what_was_confirmed(db, network, area, sysop):
    ref = _upload(db, area, sysop, data=b"0123456789" * 1000)
    spool = outbound.spool_dir(db)
    spool.mkdir(parents=True)
    (spool / "LIST.TXT").write_bytes(b"generated")
    (spool / "KEEP.TXT").write_bytes(b"truncate me")
    for name, kwargs in (("REPORT.ZIP", {"file_id": ref.file_id}),
                         ("LIST.TXT", {"path": spool / "LIST.TXT", "after_sent": outbound.KILL}),
                         ("KEEP.TXT", {"path": spool / "KEEP.TXT", "after_sent": outbound.TRUNCATE})):
        outbound.queue_file_without_commit(db, network.id, destination=HUB, route="uplink", send_name=name,
                                           size_bytes=1, reference_kind="test", reference_id=name, **kwargs)
    db.connection.commit()

    async def run():
        lane = DatabaseLane(db.path)
        try:
            batch = await outbound.outbound_for_node(lane, network, HUB, uplink=True)
            try:
                names = [f.name for f in batch.files]
                report = batch.files[0].data
                assert len(report) == 10_000 and report[5:12] == b"5678901"
                done = await batch.settle(lane, ["REPORT.ZIP", "LIST.TXT", "KEEP.TXT"])
            finally:
                batch.close()
            return names, done
        finally:
            lane.close()

    names, done = asyncio.run(run())
    assert names == ["REPORT.ZIP", "LIST.TXT", "KEEP.TXT"]
    assert done == (0, 3)
    assert outbound.pending_files(db, network.id) == []
    assert not (spool / "LIST.TXT").exists()
    assert (spool / "KEEP.TXT").read_bytes() == b""


def test_a_queued_file_that_is_gone_is_dropped_and_its_sender_told(db, network, area, sysop, alice):
    ref = _upload(db, area, sysop)
    letter = send_netmail(db, sysop, "Joe (21:1/100)", "Report", "Attached", attach=[ref])
    db.connection.execute("DELETE FROM files WHERE file_id = ?", (ref.file_id,))
    db.connection.commit()

    async def run():
        lane = DatabaseLane(db.path)
        try:
            batch = await outbound.outbound_for_node(lane, network, HUB, uplink=True)
            batch.close()
            return [f.name for f in batch.files]
        finally:
            lane.close()

    assert len(asyncio.run(run())) == 1  # the netmail's packet only
    assert outbound.pending_files(db, network.id) == []
    (notice,) = list_inbox(db, sysop)
    assert "not delivered" in notice.subject and "REPORT.ZIP" in notice.body
    assert str(letter.id) in {row["reference_id"] for row in db.connection.execute("SELECT * FROM ftn_outbound")}


# -- file attach, out (issue #1470) ------------------------------------------


def test_the_sysops_netmail_sends_its_files_as_a_file_attach(db, network, area, sysop):
    ref = _upload(db, area, sysop)
    letter = send_netmail(db, sysop, "Joe Bloggs (21:3/110)", "The report", "See the file.", attach=[ref])

    (queued,) = queue.pending_outbound(db, network.id)
    message = _unpack(queued.packed)
    assert message.subject == "REPORT.ZIP"
    assert message.attributes & ATTR_FILE_ATTACHED
    assert message.body.startswith("See the file.")
    (file_row,) = outbound.pending_files(db, network.id)
    assert (file_row.send_name, file_row.route, file_row.destination, file_row.reference_id) == (
        "REPORT.ZIP", "uplink", "21:3/110", str(letter.id))
    (sent,) = list_sent(db, sysop)
    assert sent.subject == "The report"
    assert [r.filename for r in mail_refs(db, sent.id)] == ["REPORT.ZIP"]


def test_only_the_sysop_attaches_and_a_file_over_the_limit_is_refused(db, network, area, sysop, alice):
    ref = _upload(db, area, sysop)
    with pytest.raises(MailError, match="Only the SysOp"):
        send_netmail(db, alice, "Joe (21:3/110)", "s", "b", attach=[ref])
    set_config(db, "max_upload_bytes", "4")
    with pytest.raises(MailError, match="at most 4"):
        send_netmail(db, sysop, "Joe (21:3/110)", "s", "b", attach=[ref])
    assert queue.pending_outbound(db, network.id) == []
    assert list_sent(db, sysop) == []


def test_names_too_long_for_one_subject_are_refused(db, network, area, sysop):
    refs = [_upload(db, area, sysop, name=f"{'x' * 30}{n}.ZIP", data=bytes([n])) for n in range(3)]
    with pytest.raises(MailError, match="subject"):
        send_netmail(db, sysop, "Joe (21:3/110)", "s", "b", attach=refs)


def test_the_hub_receives_the_netmail_and_its_file_in_one_call(db, network, area, sysop):
    ref = _upload(db, area, sysop, data=b"the report itself")
    send_netmail(db, sysop, "Joe (21:3/110)", "Report", "Attached", attach=[ref])
    received = []

    async def run():
        async def handle(reader, writer):
            try:
                result = await run_session(reader, writer, originating=False, our_addresses=[HUB],
                                           system=SystemInfo("Hub", "op"), password_for=lambda a: "SECRET",
                                           outgoing_for=lambda a, s: [], timeout=5)
                received.extend(result.received)
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        hub = save_network(db, replace(network, uplink_port=server.sockets[0].getsockname()[1]))
        lane = DatabaseLane(db.path)
        try:
            return await FtnMailer(lane).poll(hub)
        finally:
            lane.close()
            server.close()
            await server.wait_closed()

    status = asyncio.run(run())
    assert status.last_error is None
    (report,) = [r for r in received if r.name == "REPORT.ZIP"]
    assert report.data == b"the report itself"
    assert "and 1 files" in status.last_summary
    assert outbound.pending_files(db, network.id) == []
    assert queue.count_pending_outbound(db, network.id) == 0


# -- flavours (issue #1471) --------------------------------------------------


def test_crash_needs_a_direct_route_and_hold_waits_for_the_node(db, network, alice):
    with pytest.raises(MailError, match="Crash netmail goes straight to 21:1/110"):
        send_netmail(db, alice, "Joe (21:1/110)", "s", "b", flavour="crash")
    send_netmail(db, alice, "Hub Op (21:1/100)", "s", "crash to the hub itself", flavour="crash")
    import_nodelist(db, network.id, "Zone,21,Z,X,Y,-,300\nHost,1,N,X,Y,-,300\n,110,Joe,X,Y,-,300,IBN:joe.example\n")
    send_netmail(db, alice, "Joe (21:1/110)", "s", "crash direct", flavour="crash")
    send_netmail(db, alice, "Ann (21:7/7)", "s", "held for Ann", flavour="hold")

    rows = {row["destination"]: (row["route"], row["flavour"]) for row in db.connection.execute(
        "SELECT destination, route, flavour FROM ftn_outbound")}
    assert rows == {"21:1/100": ("uplink", "crash"), "21:1/110": ("direct", "crash"), "21:7/7": ("direct", "hold")}
    attributes = {m.destination: _unpack(m.packed).attributes for m in
                  queue.pending_outbound(db, network.id) + queue.pending_outbound(db, network.id, route="direct")}
    assert attributes["21:1/110"] & ATTR_CRASH and attributes["21:7/7"] & ATTR_HOLD
    # Held mail never makes this node call; crash mail counts as crash.
    assert outbound.count_callable(db, network.id, route="direct") == (1, 1)
    assert outbound.count_callable(db, network.id, route="uplink") == (1, 1)
    assert outbound.direct_nodes_waiting(db, network, include_hold=False) == [JOE]


def test_crash_mail_whose_calls_keep_failing_is_dropped_and_its_sender_told(db, network, alice):
    import_nodelist(db, network.id,
                    f"Zone,21,Z,X,Y,-,300\nHost,1,N,X,Y,-,300\n,110,Joe,X,Y,-,300,IBN:127.0.0.1:{_free_port()}\n")
    send_netmail(db, alice, "Joe (21:1/110)", "Urgent", "Nobody answers", flavour="crash")

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()

    async def run():
        lane = DatabaseLane(db.path)
        mailer = FtnMailer(lane, clock=clock)
        try:
            for _ in range(outbound.CRASH_ATTEMPTS):
                await mailer.deliver_direct(get_network(db, network.id))
                clock.now += 600
        finally:
            lane.close()

    asyncio.run(run())
    # Never via the uplink, and reported once dropped.
    assert queue.count_pending_outbound(db, network.id) == 0
    (notice,) = list_inbox(db, alice)
    assert "not delivered" in notice.subject
    assert f"failed {outbound.CRASH_ATTEMPTS} times" in notice.body


def test_held_mail_expires_and_its_sender_is_told(db, network, alice):
    send_netmail(db, alice, "Ann (21:7/7)", "Later", "When you call", flavour="hold")
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=outbound.HOLD_DAYS + 1)
    assert outbound.expire_held(db, now=later) == 1
    assert queue.count_pending_outbound(db, network.id, route="direct") == 0
    (notice,) = list_inbox(db, alice)
    assert "did not call" in notice.body


def test_a_node_calling_in_unproven_gets_only_what_is_held_for_it(db, network, alice):
    set_config(db, listener_module.HOST_CONFIG_KEY, "127.0.0.1")
    set_config(db, listener_module.PORT_CONFIG_KEY, str(_free_port()))
    network = save_network(db, replace(network, answers_calls=True))
    send_netmail(db, alice, "Ann (21:7/7)", "Held", "For Ann only", flavour="hold")
    send_netmail(db, alice, "Bob (21:8/8)", "Held", "For Bob", flavour="hold")
    send_netmail(db, alice, "Hub Op (21:1/100)", "Normal", "For the hub")

    async def call(listener, address):
        host, port = listener.listening_on
        reader, writer = await asyncio.open_connection(host, port)
        try:
            return await run_session(reader, writer, originating=True, our_addresses=[address],
                                     system=SystemInfo("Ann", "ann"), password="", outgoing=[], timeout=5)
        finally:
            writer.close()

    async def run():
        lane = DatabaseLane(db.path)
        listener = FtnListener(lane)
        try:
            await listener.reconcile()
            result = await call(listener, FtnAddress(21, 7, 7))
            for _ in range(500):
                if listener.recent:
                    break
                await asyncio.sleep(0.01)
            return result
        finally:
            await listener.close()
            lane.close()

    result = asyncio.run(run())
    assert not result.secure
    (packet,) = result.received
    (message,) = parse_packet(packet.data).messages
    assert decode_message(message).body.startswith("For Ann only")
    left = {row["destination"] for row in db.connection.execute(
        "SELECT destination FROM ftn_outbound WHERE status = 'pending'")}
    assert left == {"21:8/8", "21:1/100"}


def test_the_compose_screen_toggles_the_flavour(db, network, alice):
    session = FakeSession(keys=["c", "f", "f", "s", "b"], lines=["Ann (21:7/7)", "Later", "When you call", ""])
    lane = DatabaseLane(db.path)
    try:
        asyncio.run(browse_mail(session, lane, alice))
    finally:
        lane.close()
    text = ANSI.sub("", _written_text(session))
    assert "Flavour: Normal" in text and "Flavour: Crash" in text and "Flavour: Hold" in text
    assert "Netmail on hold: it waits for 21:7/7 to call this BBS." in text
    (row,) = db.connection.execute("SELECT flavour, route FROM ftn_outbound").fetchall()
    assert (row["flavour"], row["route"]) == ("hold", "direct")


# -- file attach, in (issue #1470) -------------------------------------------


def test_an_attached_file_lands_in_the_attach_area_with_a_ref_on_the_letter(db, network, alice, area):
    network = save_network(db, replace(network, attach_area_id=area.id))
    total = _toss_session(db, network, [
        ("NEWS.ZIP", b"PK not a bundle"),
        ("0000beef.pkt", _packet(_netmail(subject="C:\\OUT\\NEWS.ZIP", attributes=ATTR_FILE_ATTACHED))),
    ])

    assert (total.netmail, total.files, total.held) == (1, 1, 0)
    (letter,) = list_inbox(db, alice)
    assert letter.body.startswith("[Files were sent with this netmail: NEWS.ZIP.")
    (ref,) = mail_refs(db, letter.id)
    row = db.connection.execute("SELECT * FROM files WHERE file_id = ?", (ref.file_id,)).fetchone()
    assert (row["filename"], row["area_id"], row["uploader_label"], row["status"]) == (
        "NEWS.ZIP", area.id, "Joe Bloggs (21:3/110)", "approved")
    assert row["ftn_file_echo"] is None


def test_a_file_that_came_first_is_claimed_from_the_held_store(db, network, alice, area):
    network = save_network(db, replace(network, attach_area_id=area.id))
    first = _toss_session(db, network, [("NEWS.ZIP", b"PK early file")])
    assert first.held == 1
    second = _toss_session(db, network, [
        ("0000beef.pkt", _packet(_netmail(subject="NEWS.ZIP", attributes=ATTR_FILE_ATTACHED)))])
    assert second.files == 1
    assert queue.list_held(db) == []
    (letter,) = list_inbox(db, alice)
    assert [r.filename for r in mail_refs(db, letter.id)] == ["NEWS.ZIP"]


def test_without_an_attach_area_the_file_is_held_with_the_reason(db, network, alice):
    total = _toss_session(db, network, [
        ("0000beef.pkt", _packet(_netmail(subject="NEWS.ZIP", attributes=ATTR_FILE_ATTACHED))),
        ("NEWS.ZIP", b"PK file"),
    ])
    assert (total.netmail, total.held) == (1, 1)
    (held,) = queue.list_held(db)
    assert held.file_name == "NEWS.ZIP" and not held.waiting
    assert "no file area takes fsxNet's netmail files" in held.reason
    (letter,) = list_inbox(db, alice)
    assert "held for the SysOp" in letter.body


def test_an_unsecure_sessions_attach_is_held_and_never_stored(db, network, alice, area):
    network = save_network(db, replace(network, attach_area_id=area.id))
    total = _toss_session(db, network, [
        ("0000beef.pkt", _packet(_netmail(subject="NEWS.ZIP", attributes=ATTR_FILE_ATTACHED))),
        ("NEWS.ZIP", b"PK file"),
    ], secure=False)
    assert total.held == 2 and total.files == 0
    assert db.connection.execute("SELECT COUNT(*) FROM files WHERE area_id = ?", (area.id,)).fetchone()[0] == 0


def test_an_unsafe_attach_name_is_not_kept(db, network, alice, area):
    network = save_network(db, replace(network, attach_area_id=area.id))
    _toss_session(db, network, [
        ("0000beef.pkt", _packet(_netmail(subject=".hidden", attributes=ATTR_FILE_ATTACHED)))])
    (letter,) = list_inbox(db, alice)
    assert "Not kept, as their names are not safe here: .hidden." in letter.body
    assert db.connection.execute("SELECT COUNT(*) FROM ftn_attach_waits").fetchone()[0] == 0


def test_attach_waits_expire(db, network, alice, area):
    network = save_network(db, replace(network, attach_area_id=area.id))
    _toss_session(db, network, [
        ("0000beef.pkt", _packet(_netmail(subject="NEWS.ZIP", attributes=ATTR_FILE_ATTACHED)))])
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=queue.UNPAIRED_DAYS + 1)
    assert attach_module.expire_attach_waits(db, now=later) == 1


# -- bounce (issue #1472) ----------------------------------------------------


def _bounces(db, network):
    return [_unpack(m.packed) for m in queue.pending_outbound(db, network.id)
            if m.reference_id.startswith("bounce:")]


def test_netmail_no_account_can_take_goes_back_to_its_sender(db, network, sysop):
    total = _toss_session(db, network, [("0000beef.pkt", _packet(_netmail(to="Nobody Here", body="line\n" * 20)))])

    assert (total.bounced, total.netmail) == (1, 0)
    assert list_inbox(db, sysop) == []
    (bounce,) = _bounces(db, network)
    assert (bounce.to_name, bounce.from_name) == ("Joe Bloggs", BOUNCE_FROM)
    assert bounce.subject == "Undeliverable: Hi"
    assert bounce.kludge("INTL") == "21:3/110 21:1/199"
    assert bounce.kludge("REPLY") == "21:3/110 0000beef"
    assert "could not be delivered: no account here has that name" in bounce.body
    assert "> line" in bounce.body and "[10 more lines]" in bounce.body


def test_a_shared_verified_name_bounces_too(db, network, sysop):
    for name in ("owen1", "owen2"):
        attest_name(db, create_user(db, name, password="hunter2pw"), "Owen Night", verifier=sysop)
    _toss_session(db, network, [("0000beef.pkt", _packet(_netmail(to="Owen Night")))])
    (bounce,) = _bounces(db, network)
    assert "two accounts here share that verified real name" in bounce.body


def test_an_account_taking_no_mail_bounces(db, network, sysop, alice, monkeypatch):
    monkeypatch.setattr("netbbs.ftn.tosser.mail_recipient_refusal",
                        lambda db, user: "no mail" if user.id == alice.id else None)
    _toss_session(db, network, [("0000beef.pkt", _packet(_netmail(to="alice")))])
    (bounce,) = _bounces(db, network)
    assert "that account takes no mail" in bounce.body
    assert list_inbox(db, alice) == []


def test_netmail_to_sysop_always_reaches_the_sysop(db, network, sysop):
    impostor = create_user(db, "sysop", password="hunter2pw", user_level=10)
    _toss_session(db, network, [("0000beef.pkt", _packet(_netmail(to="SYSOP")))])
    (letter,) = list_inbox(db, sysop)
    assert letter.body == "A private note"
    assert list_inbox(db, impostor) == []
    assert _bounces(db, network) == []


@pytest.mark.parametrize(("frm", "subject"), [
    ("AreaFix", "Hi"),
    ("Mailer-Daemon", "Hi"),
    ("Joe Bloggs", "Undeliverable: Hi"),
])
def test_a_robot_or_a_bounce_is_never_bounced(db, network, sysop, frm, subject):
    _toss_session(db, network, [("0000beef.pkt", _packet(_netmail(to="Nobody", frm=frm, subject=subject)))])
    assert _bounces(db, network) == []
    (letter,) = list_inbox(db, sysop)
    assert "[It was not returned to its sender:" in letter.body


def test_bounces_to_one_address_are_limited_per_day(db, network, sysop):
    for serial in range(MAX_BOUNCES_PER_DAY + 1):
        _toss_session(db, network, [(f"{serial:08x}.pkt",
                                     _packet(_netmail(to="Nobody", msgid=f"21:3/110 {serial:08x}")))])
    assert len(_bounces(db, network)) == MAX_BOUNCES_PER_DAY
    (letter,) = list_inbox(db, sysop)
    assert "already got 5 returned netmails" in letter.body


def test_with_bouncing_off_unknown_netmail_goes_to_the_sysop(db, network, sysop):
    network = save_network(db, replace(network, bounce_unknown=False))
    _toss_session(db, network, [("0000beef.pkt", _packet(_netmail(to="Nobody Here")))])
    assert _bounces(db, network) == []
    (letter,) = list_inbox(db, sysop)
    assert letter.body.startswith("[This netmail was addressed to 'Nobody Here', who has no mailbox on this node.]")
    assert "not returned" not in letter.body


def test_bouncing_is_on_for_a_new_network(db, sysop):
    created = save_network(db, FtnNetwork(name="n", domain="n", our_address=NODE, uplink_address=HUB))
    assert created.bounce_unknown
    assert get_network(db, created.id).attach_area_id is None


def test_a_sender_with_a_full_mailbox_does_not_stop_the_drop(db, network, alice, monkeypatch):
    """Review of #1492: `MailboxFullError` is no `MailError`; telling the
    sender must never abort the mailer's pass."""
    send_netmail(db, alice, "Ann (21:7/7)", "Later", "When you call", flavour="hold")
    monkeypatch.setattr("netbbs.mail.make_room", lambda db, recipient: False)
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=outbound.HOLD_DAYS + 1)
    assert outbound.expire_held(db, now=later) == 1
    assert queue.count_pending_outbound(db, network.id, route="direct") == 0
