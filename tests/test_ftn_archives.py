"""Mail bundles in LHA, ARC, 7-Zip, RAR and ARJ, and outbound ZIP bundling
(design doc §6.8, issue #1477).

Real archives are made where this machine can: 7-Zip archives with a real
`7z`, and LHA archives written here (level-0 headers, stored), which 7-Zip
also reads. A test needing a real tool skips cleanly without one. Each
tool's listing format is also driven by a stand-in that prints what the
real tool prints, so every parser runs everywhere."""

from __future__ import annotations

import asyncio
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from netbbs.files import extract
from netbbs.ftn import FtnFormatError, bundle, queue
from netbbs.ftn.address import FtnAddress
from netbbs.ftn.bundle import BundleToolMissing, archive_kind, open_bundle

NODE = FtnAddress(21, 1, 199)
HUB = FtnAddress(21, 1, 100)


# -- archives made here ---------------------------------------------------------


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _lzh(members: list[tuple[str, bytes]]) -> bytes:
    """An LHA archive with level-0 headers and stored (-lh0-) members."""
    out = bytearray()
    for name, content in members:
        raw = name.encode("latin-1")
        header = (b"-lh0-" + len(content).to_bytes(4, "little") + len(content).to_bytes(4, "little")
                  + (0x5B6E0000).to_bytes(4, "little") + b"\x20\x00" + bytes([len(raw)]) + raw
                  + _crc16(content).to_bytes(2, "little"))
        out += bytes([len(header), sum(header) & 0xFF]) + header + content
    return bytes(out + b"\x00")


def _arc(members: list[tuple[str, bytes]]) -> bytes:
    """An ARC archive with stored (method 2) members."""
    out = bytearray()
    for name, content in members:
        out += (b"\x1a\x02" + name.encode("latin-1").ljust(13, b"\0")[:13] + len(content).to_bytes(4, "little")
                + b"\x00\x00\x00\x00" + _crc16(content).to_bytes(2, "little") + len(content).to_bytes(4, "little")
                + content)
    return bytes(out + b"\x1a\x00")


def _seven_zip() -> str | None:
    for candidate in ("7z", "7za", "7zz"):
        found = shutil.which(candidate)
        if found:
            return found
    if sys.platform == "win32":
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if root and Path(root, "7-Zip", "7z.exe").is_file():
                return str(Path(root, "7-Zip", "7z.exe"))
    return None


SEVEN_ZIP = _seven_zip()
needs_7z = pytest.mark.skipif(SEVEN_ZIP is None, reason="no 7-Zip on this machine")


def _only(monkeypatch, tools: dict[str, list[str]]) -> None:
    """Make exactly `tools` (command -> argv prefix) the installed ones."""
    monkeypatch.setattr(extract, "_resolve", lambda command: tools.get(command))


def _make_7z(tmp_path: Path, members: dict[str, bytes]) -> bytes:
    source = tmp_path / "source"
    source.mkdir()
    for name, content in members.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_bytes(content)
    archive = tmp_path / "made.7z"
    subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(archive), *members], cwd=source, check=True,
                   stdout=subprocess.DEVNULL)
    return archive.read_bytes()


def _stand_in(tmp_path: Path, name: str, body: str) -> list[str]:
    """A stand-in tool: a Python script receiving the real tool's argv."""
    script = tmp_path / f"{name}_stand_in.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


def _open(data: bytes):
    return asyncio.run(open_bundle(data))


# -- real 7-Zip -----------------------------------------------------------------


def test_lha_and_arc_signatures_are_recognised_on_archives_made_here():
    assert archive_kind(_lzh([("0000002a.pkt", b"x")])) == "lha"
    assert archive_kind(_arc([("0000002A.PKT", b"x")])) == "arc"


@needs_7z
def test_a_7z_bundle_is_unpacked_by_7zip(tmp_path, monkeypatch):
    _only(monkeypatch, {"7z": [SEVEN_ZIP]})
    data = _make_7z(tmp_path, {"0000002a.pkt": b"packet one", "docs/readme.txt": b"hello"})
    assert archive_kind(data) == "7z"
    packets, others = _open(data)
    assert packets == [("0000002a.pkt", b"packet one")]
    assert others == [("readme.txt", b"hello")]


@needs_7z
def test_an_lha_bundle_is_unpacked_by_7zip_when_lha_is_missing(tmp_path, monkeypatch):
    _only(monkeypatch, {"7z": [SEVEN_ZIP]})
    packets, others = _open(_lzh([("0000002A.PKT", b"from the hub"), ("FILE_ID.DIZ", b"diz")]))
    assert packets == [("0000002A.PKT", b"from the hub")]
    assert others == [("FILE_ID.DIZ", b"diz")]


@needs_7z
def test_an_archive_without_packets_is_not_read(tmp_path, monkeypatch):
    _only(monkeypatch, {"7z": [SEVEN_ZIP]})
    assert _open(_make_7z(tmp_path, {"nodelist.289": b"list", "file_id.diz": b"diz"})) == ([], [])


@needs_7z
def test_a_bomb_over_the_unpacked_limit_is_refused(tmp_path, monkeypatch):
    _only(monkeypatch, {"7z": [SEVEN_ZIP]})
    monkeypatch.setattr(bundle, "MAX_UNPACKED_BYTES", 1024 * 1024)
    data = _make_7z(tmp_path, {"0000002a.pkt": b"\0" * (3 * 1024 * 1024)})
    assert len(data) < 64 * 1024
    with pytest.raises(FtnFormatError, match="more than"):
        _open(data)


@needs_7z
def test_too_many_members_are_refused_before_any_is_read(tmp_path, monkeypatch):
    _only(monkeypatch, {"7z": [SEVEN_ZIP]})
    monkeypatch.setattr(bundle, "MAX_MEMBERS", 3)
    data = _make_7z(tmp_path, {f"{n:08x}.pkt": b"p" for n in range(5)})
    with pytest.raises(FtnFormatError, match="5 files, more than 3"):
        _open(data)


@needs_7z
def test_a_traversal_member_name_writes_nothing_outside(tmp_path, monkeypatch):
    _only(monkeypatch, {"7z": [SEVEN_ZIP]})
    monkeypatch.chdir(tmp_path)
    data = _lzh([("..\\..\\evil.pkt", b"evil"), ("../../evil2.pkt", b"evil"), ("0000002a.pkt", b"good")])
    try:
        packets, _others = _open(data)
    except FtnFormatError:
        packets = []
    # Whatever the tool made of the names, members only ever came back by
    # base name, and nothing was written next to the bundle's folder.
    assert all("/" not in name and "\\" not in name for name, _ in packets)
    for folder in (tmp_path, tmp_path.parent, Path(tempfile.gettempdir())):
        assert not (folder / "evil.pkt").exists() and not (folder / "evil2.pkt").exists()


# -- missing and failing tools --------------------------------------------------


def test_no_tool_names_what_to_install(monkeypatch):
    _only(monkeypatch, {})
    with pytest.raises(BundleToolMissing, match=r"lha \(pkgsrc archivers/lha\) or 7z \(pkgsrc archivers/p7zip\)"):
        _open(_lzh([("0000002a.pkt", b"x")]))
    with pytest.raises(BundleToolMissing, match=r"nomarch \(pkgsrc archivers/nomarch\)"):
        _open(_arc([("0000002A.PKT", b"x")]))
    with pytest.raises(BundleToolMissing, match=r"unrar \(pkgsrc archivers/unrar\)"):
        _open(b"Rar!\x1a\x07\x00" + b"\0" * 30)


LHA_LISTING = """\
PERMISSION  UID  GID      SIZE  RATIO     STAMP           NAME
---------- ----------- ------- ------ ------------ --------------------
[MS-DOS]                    10 100.0% Oct 23 12:00 0000002A.PKT
-rw-r--r--  1000/1000        5  80.0% Oct 23  2019 docs/README.TXT
drwxr-xr-x  1000/1000        0 ****** Oct 23 12:00 docs/
---------- ----------- ------- ------ ------------ --------------------
 Total         3 files      15  90.0% Oct 23 12:00
"""


def _lha_stand_in(tmp_path, listing=LHA_LISTING, contents=None):
    contents = contents or {"0000002A.PKT": "packet one", "docs/README.TXT": "hello"}
    return _stand_in(tmp_path, "lha", f"""
        import sys
        command = sys.argv[1]
        if command == "l":
            sys.stdout.write({listing!r})
        elif command == "pq":
            sys.stdout.write({contents!r}[sys.argv[3]])
        else:
            sys.exit(2)
    """)


def test_lha_listing_is_read_and_each_member_extracted(tmp_path, monkeypatch):
    _only(monkeypatch, {"lha": _lha_stand_in(tmp_path)})
    packets, others = _open(_lzh([("0000002A.PKT", b"x")]))
    assert packets == [("0000002A.PKT", b"packet one")]
    assert others == [("README.TXT", b"hello")]


def test_a_member_that_unpacks_to_another_size_than_listed_is_refused(tmp_path, monkeypatch):
    _only(monkeypatch, {"lha": _lha_stand_in(tmp_path, contents={"0000002A.PKT": "", "docs/README.TXT": "hello"})})
    with pytest.raises(FtnFormatError, match="unpacked to 0 bytes, but the archive lists 10"):
        _open(_lzh([("0000002A.PKT", b"x")]))


@pytest.mark.parametrize("name", ["-rf.pkt", "a*.pkt", "b?.pkt", "bad\x07.pkt", "dir/.."])
def test_unsafe_member_names_refuse_the_bundle(tmp_path, monkeypatch, name):
    listing = LHA_LISTING.replace("docs/README.TXT", name)
    _only(monkeypatch, {"lha": _lha_stand_in(tmp_path, listing=listing)})
    with pytest.raises(FtnFormatError, match="member name"):
        _open(_lzh([("0000002A.PKT", b"x")]))


def test_a_link_member_refuses_the_bundle(tmp_path, monkeypatch):
    listing = LHA_LISTING.replace("-rw-r--r--  1000/1000        5", "lrwxrwxrwx  1000/1000        5")
    _only(monkeypatch, {"lha": _lha_stand_in(tmp_path, listing=listing)})
    with pytest.raises(FtnFormatError, match="link"):
        _open(_lzh([("0000002A.PKT", b"x")]))


def test_a_failing_tool_is_named_in_the_reason(tmp_path, monkeypatch):
    _only(monkeypatch, {"lha": _stand_in(tmp_path, "lha", "import sys; sys.exit(3)")})
    with pytest.raises(FtnFormatError, match="lha could not list it .lha_stand_in.py exited 3|exited 3"):
        _open(_lzh([("0000002A.PKT", b"x")]))


def test_the_next_installed_tool_is_tried_when_one_cannot_list(tmp_path, monkeypatch):
    _only(monkeypatch, {"lha": _stand_in(tmp_path, "broken", "import sys; sys.exit(3)"),
                        "7z": _stand_in(tmp_path, "seven", """
        import sys
        if sys.argv[1] == "l":
            sys.stdout.write("Path = 0000002a.pkt\\nSize = 3\\nAttributes = A\\n\\n")
        else:
            sys.stdout.write("pkt")
    """)})
    assert _open(_lzh([("0000002a.pkt", b"x")])) == ([("0000002a.pkt", b"pkt")], [])


def test_unrar_technical_listing_is_read(tmp_path, monkeypatch):
    listing = """
UNRAR 6.21 freeware      Copyright (c) 1993-2023 Alexander Roshal

Archive: archive.rar
Details: RAR 5

        Name: inbound/0000002a.pkt
        Type: File
        Size: 4
 Packed size: 4

        Name: inbound
        Type: Directory
"""
    tool = _stand_in(tmp_path, "unrar", f"""
        import sys
        if sys.argv[1] == "lt":
            sys.stdout.write({listing!r})
        elif sys.argv[1] == "p" and sys.argv[-1] == "inbound/0000002a.pkt" and "--" in sys.argv:
            sys.stdout.write("mail")
        else:
            sys.exit(10)
    """)
    _only(monkeypatch, {"unrar": tool})
    assert _open(b"Rar!\x1a\x07\x01\x00" + b"\0" * 30) == ([("0000002a.pkt", b"mail")], [])


def test_arc_is_listed_from_its_headers_and_split_by_size(tmp_path, monkeypatch):
    # nomarch -p prints every member in order; the stand-in prints the
    # stored members' bytes, as nomarch would.
    tool = _stand_in(tmp_path, "nomarch", """
        import sys
        data = open(sys.argv[2], "rb").read()
        position = 0
        while data[position + 1] != 0:
            packed = int.from_bytes(data[position + 15:position + 19], "little")
            sys.stdout.buffer.write(data[position + 29:position + 29 + packed])
            position += 29 + packed
    """)
    _only(monkeypatch, {"nomarch": tool})
    packets, others = _open(_arc([("0000002A.PKT", b"mail!"), ("README.TXT", b"hi")]))
    assert packets == [("0000002A.PKT", b"mail!")]
    assert others == [("README.TXT", b"hi")]


def test_arc_output_that_does_not_add_up_is_refused(tmp_path, monkeypatch):
    _only(monkeypatch, {"nomarch": _stand_in(tmp_path, "nomarch", "import sys; sys.stdout.write('short')")})
    with pytest.raises(FtnFormatError, match="lists 12"):
        _open(_arc([("0000002A.PKT", b"mail!mail!xx")]))


def test_a_damaged_arc_directory_is_refused(monkeypatch, tmp_path):
    _only(monkeypatch, {"nomarch": _stand_in(tmp_path, "nomarch", "pass")})
    with pytest.raises(FtnFormatError, match="damaged ARC"):
        _open(_arc([("0000002A.PKT", b"mail")])[:-8])


def test_a_hanging_tool_is_killed_within_its_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "SPAWN_TIMEOUT_SECONDS", 1)
    _only(monkeypatch, {"lha": _stand_in(tmp_path, "lha", "import time; time.sleep(120)")})
    with pytest.raises(FtnFormatError, match="timed out"):
        _open(_lzh([("0000002A.PKT", b"x")]))


# -- tossing (real sessions' sorting) ---------------------------------------------


@pytest.fixture
def db(tmp_path):
    from netbbs.storage.database import Database

    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def network(db):
    from netbbs.auth.users import SYSOP_LEVEL, create_user
    from netbbs.boards.boards import create_board
    from netbbs.ftn.networks import FtnNetwork, save_network, set_board_area

    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    saved = save_network(db, FtnNetwork(name="fsxNet", domain="fsxnet", our_address=NODE, uplink_address=HUB,
                                        uplink_host="127.0.0.1", session_password="SECRET", enabled=True))
    set_board_area(db, create_board(db, "general", creator=sysop), saved.id, "FSX_GEN")
    return saved


def _hub_mail(*bodies: str) -> bytes:
    from netbbs.ftn.message import FtnMessage, encode_message
    from netbbs.ftn.packet import PacketHeader, build_packet

    messages = [encode_message(FtnMessage(
        to_name="All", from_name="Hub Person", subject="From the hub", body=body, area="FSX_GEN",
        date=datetime.datetime(2026, 10, 7, 9, 0), kludges=[("MSGID", f"21:1/100 {index:08x}")],
        origin="Hub (21:1/100)", orig_net=1, orig_node=100, dest_net=1, dest_node=199,
    )) for index, body in enumerate(bodies, start=1)]
    return build_packet(PacketHeader(orig=HUB, dest=NODE, created=None), messages)


def _toss(db, network, files):
    from netbbs.ftn.binkp import ReceivedFile, SessionResult
    from netbbs.ftn.exchange import toss_received
    from netbbs.storage.execution import DatabaseLane

    async def run():
        lane = DatabaseLane(db.path)
        try:
            result = SessionResult(remote_addresses=[HUB], secure=True,
                                   received=[ReceivedFile(name, data) for name, data in files])
            return await toss_received(lane, network, result)
        finally:
            lane.close()

    return asyncio.run(run())


def _posts(db) -> list[str]:
    return [row["body"] for row in db.connection.execute("SELECT body FROM posts ORDER BY id")]


@needs_7z
def test_an_lha_bundle_from_the_hub_is_tossed(db, network, monkeypatch):
    _only(monkeypatch, {"7z": [SEVEN_ZIP]})
    total = _toss(db, network, [("00020063.we0", _lzh([("0000abcd.pkt", _hub_mail("one", "two"))]))])
    assert total.posts == 2
    assert _posts(db) == ["one", "two"]
    assert queue.list_held(db) == []


def test_a_bundle_no_tool_opens_is_held_saying_what_to_install_and_released_later(db, network, monkeypatch,
                                                                                    tmp_path):
    _only(monkeypatch, {})
    data = _lzh([("0000abcd.pkt", _hub_mail("held then tossed"))])
    _toss(db, network, [("00020063.we0", data)])
    (held,) = queue.list_held(db)
    assert held.kind == queue.HELD_FILE and held.waiting
    assert "install lha (pkgsrc archivers/lha)" in held.reason
    assert _posts(db) == []

    # The SysOp installs the tool, then [R]eleases the held bundle.
    from netbbs.ftn.exchange import release_held_bundle
    from netbbs.storage.execution import DatabaseLane

    _only(monkeypatch, {"lha": _stand_in(tmp_path, "lha", f"""
        import sys
        if sys.argv[1] == "l":
            sys.stdout.write("[MS-DOS]   {len(_hub_mail('held then tossed'))} 100.0% Oct 23 12:00 0000abcd.pkt\\n")
        else:
            sys.stdout.buffer.write({_hub_mail("held then tossed")!r})
    """)})

    async def release():
        lane = DatabaseLane(db.path)
        try:
            return await release_held_bundle(lane, network, held.id)
        finally:
            lane.close()

    result = asyncio.run(release())
    assert result.posts == 1
    assert _posts(db) == ["held then tossed"]
    assert queue.list_held(db) == []


def test_releasing_a_held_file_that_is_no_bundle_keeps_it_held(db, network):
    from netbbs.ftn.exchange import release_held_bundle
    from netbbs.storage.execution import DatabaseLane

    _toss(db, network, [("notes.txt", b"just some text")])
    (held,) = queue.list_held(db)

    async def release():
        lane = DatabaseLane(db.path)
        try:
            return await release_held_bundle(lane, network, held.id)
        finally:
            lane.close()

    with pytest.raises(FtnFormatError, match="not a mail packet or bundle"):
        asyncio.run(release())
    assert len(queue.list_held(db)) == 1


# -- nodelists in these formats ---------------------------------------------------


@needs_7z
def test_a_nodelist_in_an_lha_archive_is_read(monkeypatch):
    from netbbs.ftn.nodelist_feed import unpack

    _only(monkeypatch, {"7z": [SEVEN_ZIP]})
    data = _lzh([("NODELIST.289", b";A list\r\n"), ("README.TXT", b"x")])
    assert unpack(data, "NODELIST") == ("NODELIST.289", b";A list\r\n")


def test_a_nodelist_archive_with_no_tool_is_refused_naming_it(monkeypatch):
    from netbbs.ftn.nodelist import NodelistError
    from netbbs.ftn.nodelist_feed import unpack

    _only(monkeypatch, {})
    with pytest.raises(NodelistError, match="install nomarch"):
        unpack(_arc([("NODELIST.289", b";A list")]), "NODELIST")


# -- outbound ZIP bundles -------------------------------------------------------


def test_the_bundle_setting_is_stored_and_checked(db, network):
    from netbbs.ftn.networks import BUNDLE_ZIP, FtnNetworkError, get_network, save_network

    assert network.bundle_outgoing == "off"
    save_network(db, replace(network, bundle_outgoing=BUNDLE_ZIP))
    assert get_network(db, network.id).bundle_outgoing == "zip"
    with pytest.raises(FtnNetworkError, match="off or ZIP"):
        save_network(db, replace(network, bundle_outgoing="arj"))


def _queue_post(db, network):
    from netbbs.boards.boards import get_board_by_name
    from netbbs.boards.posts import create_post
    from netbbs.auth.users import get_user_by_username
    from netbbs.ftn.scanner import export_post_if_ftn

    board = get_board_by_name(db, "general")
    export_post_if_ftn(db, create_post(db, board, get_user_by_username(db, "sysop"), "Out", "Going out"), board)


def test_a_bundling_network_sends_its_packet_in_a_zip_named_for_the_day(db, network):
    import re

    from netbbs.ftn import outbound
    from netbbs.ftn.networks import BUNDLE_ZIP, save_network
    from netbbs.ftn.packet import parse_packet
    from netbbs.storage.execution import DatabaseLane

    network = save_network(db, replace(network, bundle_outgoing=BUNDLE_ZIP))
    _queue_post(db, network)

    async def run(confirmed):
        lane = DatabaseLane(db.path)
        try:
            batch = await outbound.outbound_for_node(lane, network, HUB, uplink=True)
            try:
                names = [f.name for f in batch.files]
                done = await batch.settle(lane, confirmed(names))
                return names, bytes(batch.files[0].data), done
            finally:
                batch.close()
        finally:
            lane.close()

    # A session whose remote confirmed nothing leaves the message waiting.
    (first,), _, done = asyncio.run(run(lambda names: []))
    assert done == (0, 0)
    assert queue.count_pending_outbound(db, network.id) == 1
    (second,), data, done = asyncio.run(run(lambda names: names))
    assert done == (1, 0)
    assert queue.count_pending_outbound(db, network.id) == 0

    day = ("mo", "tu", "we", "th", "fr", "sa", "su")[datetime.date.today().weekday()]
    assert re.fullmatch(rf"00000063\.{day}0", first) and re.fullmatch(rf"00000063\.{day}1", second)
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
        (member,) = archive.infolist()
        assert member.filename.endswith(".pkt")
        (message,) = parse_packet(archive.read(member)).messages
    assert b"Going out" in message.text


def test_a_network_that_does_not_bundle_sends_a_bare_packet(db, network):
    from netbbs.ftn import outbound
    from netbbs.storage.execution import DatabaseLane

    _queue_post(db, network)

    async def run():
        lane = DatabaseLane(db.path)
        try:
            batch = await outbound.outbound_for_node(lane, network, HUB, uplink=True)
            batch.close()
            return [f.name for f in batch.files]
        finally:
            lane.close()

    (name,) = asyncio.run(run())
    assert name.endswith(".pkt")


def test_a_call_to_the_hub_delivers_the_zip_bundle_and_marks_the_mail_sent(db, network):
    """A real BinkP session on loopback: the hub receives the bundle, and
    its M_GOT is what marks the message sent."""
    from netbbs.ftn.binkp import SystemInfo, run_session
    from netbbs.ftn.mailer import FtnMailer
    from netbbs.ftn.networks import BUNDLE_ZIP, save_network
    from netbbs.ftn.packet import parse_packet
    from netbbs.storage.execution import DatabaseLane

    network = save_network(db, replace(network, bundle_outgoing=BUNDLE_ZIP))
    _queue_post(db, network)
    received = []

    async def run():
        async def handle(reader, writer):
            try:
                result = await run_session(
                    reader, writer, originating=False, our_addresses=[HUB], system=SystemInfo("Hub", "hubop"),
                    password_for=lambda addresses: "SECRET", outgoing_for=lambda addresses, secure: [], timeout=5)
                received.extend(result.received)
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        called = save_network(db, replace(network, uplink_port=port))
        lane = DatabaseLane(db.path)
        mailer = FtnMailer(lane)
        try:
            return await mailer.poll(called)
        finally:
            await mailer.close()
            lane.close()
            server.close()
            await server.wait_closed()

    status = asyncio.run(run())
    assert status.failures == 0
    (sent,) = received
    assert archive_kind(sent.data) == "zip" and not sent.name.endswith(".pkt")
    (packet,) = bundle.extract_packets(sent.data)
    assert len(parse_packet(packet[1]).messages) == 1
    assert queue.count_pending_outbound(db, network.id) == 0
