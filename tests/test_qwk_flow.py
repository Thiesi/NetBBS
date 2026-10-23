"""The offline mail screen and its transfers (issue #1389, design doc §6.10):
`[Q]WK offline mail` on the board list, a packet sent over Zmodem or a
single-use browser link, a reply packet received either way, [M]ark read and
the QWK conferences screen. The browser links run over a real loopback
listener."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from netbbs.activity import ensure_board_baseline, follow, unread_post_count
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post
from netbbs.config import set_qwk_bbs_id
from netbbs.net import board_flow, qwk_flow, zmodem
from netbbs.net.file_transfer import DOWNLOAD, UPLOAD, PrivateTransfer, TransferGateway, TransferGrants
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.net.zmodem import ReceivedFile
from netbbs.qwk.offline import list_conferences, packet_conferences, receive_rep_file, sent_count
from netbbs.qwk.packet import read_packet
from netbbs.qwk.rep import OutgoingReply, build_rep
from netbbs.storage.database import Database
from tests.test_board_list_and_reader import FakeSession


def _flat(session) -> str:
    """What the session showed, with the terminal's line wrapping undone."""
    return " ".join(session.visible().split())


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    set_qwk_bbs_id(database, "TESTBBS")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "Carrier", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_redraw_in_place_enabled(db, user, True)
    return user


@pytest.fixture
def general(db, sysop, alice):
    board = create_board(db, "General", creator=sysop)
    follow(db, alice, "board", board.id)
    ensure_board_baseline(db, alice, board)
    create_post(db, board, sysop, "Welcome", "Hello there")
    return board


def test_the_board_list_offers_qwk_and_q_opens_it(db, alice, general):
    session = FakeSession(["q", "b", "b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    text = _flat(session)
    assert "[Q]WK offline mail" in text
    assert "Offline mail (QWK)" in text
    assert "TESTBBS.QWK" in text and "1 post" in text


def test_a_category_list_does_not_offer_it(db, alice, sysop):
    from netbbs.boards.categories import create_category

    category = create_category(db, "Retro", created_by=sysop)
    create_board(db, "Inside", creator=sysop, category_id=category.id)
    session = FakeSession(["b"])
    asyncio.run(board_flow._browse_boards_in_category(session, db, alice, category_id=category.id))
    assert "[Q]WK" not in _flat(session)


def test_download_sends_the_packet_over_zmodem(db, alice, general, monkeypatch):
    sent = []

    async def fake_send(session, filename, data):
        sent.append((filename, data))

    monkeypatch.setattr(zmodem, "send_file", fake_send)
    session = FakeSession(["d", "b"])
    asyncio.run(qwk_flow.offline_mail(session, db, alice))
    ((filename, data),) = sent
    assert filename == "TESTBBS.QWK"
    assert [m.subject for m in read_packet(data).messages] == ["Welcome"]
    assert "Sent TESTBBS.QWK: 1 post from 1 board." in _flat(session)
    assert sent_count(db, alice) == 1 and unread_post_count(db, alice, general) == 1


def test_a_repeat_download_asks_and_no_leaves_the_carried_messages_out(db, alice, general, monkeypatch):
    sent = []

    async def fake_send(session, filename, data):
        sent.append(data)

    monkeypatch.setattr(zmodem, "send_file", fake_send)
    asyncio.run(qwk_flow.offline_mail(FakeSession(["d", "b"]), db, alice))
    session = FakeSession(["d", "n", "b"])
    asyncio.run(qwk_flow.offline_mail(session, db, alice))
    assert len(sent) == 1
    assert "not marked read yet. Include them again?" in _flat(session)
    assert "Nothing new besides what your earlier packets carried." in _flat(session)


def test_mark_read_asks_then_marks(db, alice, general, monkeypatch):
    async def fake_send(session, filename, data):
        pass

    monkeypatch.setattr(zmodem, "send_file", fake_send)
    session = FakeSession(["d", "m", "y", "b"])
    asyncio.run(qwk_flow.offline_mail(session, db, alice))
    assert "Marked 1 message read." in _flat(session)
    assert unread_post_count(db, alice, general) == 0


def test_upload_over_zmodem_posts_and_reports(db, alice, general, monkeypatch):
    # A board has its number once a packet carried it.
    number = next(c.number for c in packet_conferences(db, alice) if c.board.id == general.id)
    data = build_rep("TESTBBS", [OutgoingReply(number, "All", "alice", "From my reader", "Offline hello")])

    async def fake_receive(session, *, max_bytes, dest_path: Path):
        dest_path.write_bytes(data)
        return ReceivedFile(filename="TESTBBS.REP", sha256="", size_bytes=len(data))

    monkeypatch.setattr(zmodem, "receive_file", fake_receive)
    session = FakeSession(["u", "r", "b", "b"])
    asyncio.run(qwk_flow.offline_mail(session, db, alice))
    text = _flat(session)
    assert "1 of 1 reply went out." in text
    assert "'From my reader' -- posted on General." in text
    assert "Upload report" in text
    assert db.connection.execute("SELECT COUNT(*) FROM posts WHERE subject = 'From my reader'").fetchone()[0] == 1


def test_a_failed_zmodem_upload_points_at_the_browser_link(db, alice, general, monkeypatch):
    async def failing(session, *, max_bytes, dest_path):
        raise zmodem.ZmodemError("cancelled")

    monkeypatch.setattr(zmodem, "receive_file", failing)
    session = FakeSession(["u", "b"])
    asyncio.run(qwk_flow.offline_mail(session, db, alice, transfers=TransferGrants(base_url="https://bbs.example")))
    text = _flat(session)
    assert "Upload failed: cancelled" in text and "Press [W] for a browser upload link instead." in text


def test_without_zmodem_download_hands_out_a_private_link(db, alice, general):
    grants = TransferGrants(base_url="https://bbs.example")
    session = FakeSession(["d", "b"])
    session.supports_zmodem = False
    asyncio.run(qwk_flow.offline_mail(session, db, alice, transfers=grants))
    assert "https://bbs.example/transfer/" in _flat(session)
    (grant,) = grants._grants.values()
    assert grant.direction == DOWNLOAD and grant.private is not None
    assert grant.private.filename == "TESTBBS.QWK"
    assert read_packet(grant.private.path.read_bytes()).messages[0].subject == "Welcome"


def test_the_conferences_screen_saves_only_on_s(db, alice, general, sysop):
    other = create_board(db, "Other", creator=sysop)
    session = FakeSession(["0", "2", "b"])
    asyncio.run(qwk_flow._conferences(session, db, alice))
    assert not next(c for c in list_conferences(db, alice) if c.board.id == other.id).included
    session = FakeSession(["0", "2", "s", "b"])
    asyncio.run(qwk_flow._conferences(session, db, alice))
    assert next(c for c in list_conferences(db, alice) if c.board.id == other.id).included


def test_the_guest_account_is_turned_away(db, alice, monkeypatch):
    from netbbs.qwk import offline

    monkeypatch.setattr(offline, "guest_is_eligible", lambda _db, _user: True)
    session = FakeSession([])
    asyncio.run(qwk_flow.offline_mail(session, db, alice))
    from netbbs.net.notices import take_notices

    assert any("guest account is shared" in line for line in take_notices(session))


# -- the browser links, over a real listener ---------------------------------

aiohttp = pytest.importorskip("aiohttp")


class _Node:
    def __init__(self, tmp_path):
        from netbbs.net.web import WebServer
        from netbbs.storage.execution import DatabaseLane

        self.db = Database(tmp_path / "node.db")
        set_qwk_bbs_id(self.db, "TESTBBS")
        self.lane = DatabaseLane(tmp_path / "node.db")
        self.grants = TransferGrants()
        self.server = WebServer(
            host="127.0.0.1", port=0, session_handler=self._no_sessions,
            transfers=TransferGateway(self.grants, self.lane),
        )

    async def _no_sessions(self, session):  # pragma: no cover
        raise AssertionError("no terminal sessions here")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    async def __aenter__(self):
        await self.server.start()
        return self

    async def __aexit__(self, *exc):
        await self.server.stop()
        self.lane.close()


def test_a_private_download_link_serves_the_packet_once(tmp_path):
    node = _Node(tmp_path)
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    packet = tmp_path / "qwk-packet"
    packet.write_bytes(b"PK packet bytes")

    async def scenario():
        async with node:
            grant = node.grants.issue_private(direction=DOWNLOAD, user=alice, transfer=PrivateTransfer(
                label="QWK packet", filename="TESTBBS.QWK", path=packet, size=packet.stat().st_size,
            ))
            url = f"{node.base}/transfer/{grant.token}"
            async with aiohttp.ClientSession() as client:
                async with client.head(url) as head:
                    assert head.status == 200 and head.headers["Content-Length"] == str(packet.stat().st_size)
                async with client.post(url, data=b"x") as wrong:
                    assert wrong.status == 405
            # The POST spent the link: a link is one attempt.
            grant = node.grants.issue_private(direction=DOWNLOAD, user=alice, transfer=PrivateTransfer(
                label="QWK packet", filename="TESTBBS.QWK", path=packet, size=packet.stat().st_size,
            ))
            url = f"{node.base}/transfer/{grant.token}"
            async with aiohttp.ClientSession() as client:
                async with client.get(url) as first:
                    assert first.status == 200
                    assert "TESTBBS.QWK" in first.headers["Content-Disposition"]
                    body = await first.read()
                async with client.get(url) as second:
                    return body, second.status

    try:
        body, second = asyncio.run(scenario())
    finally:
        node.db.close()
    assert body == b"PK packet bytes" and second == 404


def test_a_private_upload_link_posts_the_replies_and_tells_the_terminal(tmp_path):
    node = _Node(tmp_path)
    sysop = create_user(node.db, "Carrier", password="hunter2", user_level=SYSOP_LEVEL)
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    board = create_board(node.db, "General", creator=sysop)
    follow(node.db, alice, "board", board.id)
    ensure_board_baseline(node.db, alice, board)
    number = packet_conferences(node.db, alice)[0].number
    good = build_rep("TESTBBS", [OutgoingReply(number, "All", "alice", "Via browser", "hello")])
    told = []

    async def scenario():
        async with node:
            results = []
            for payload in (good, build_rep("OTHER", [])):
                grant = node.grants.issue_private(direction=UPLOAD, user=alice, transfer=PrivateTransfer(
                    label="a QWK reply packet", max_bytes=64 * 1024, receive=receive_rep_file,
                    on_received=told.append,
                ))
                form = aiohttp.FormData()
                form.add_field("file", payload, filename="TESTBBS.REP")
                async with aiohttp.ClientSession() as client:
                    async with client.post(
                        f"{node.base}/transfer/{grant.token}", data=form, headers={"Accept": "text/html"},
                    ) as response:
                        results.append((response.status, await response.text()))
            return results

    try:
        (ok_status, ok_page), (bad_status, bad_page) = asyncio.run(scenario())
        posted = node.db.connection.execute("SELECT COUNT(*) FROM posts WHERE subject = 'Via browser'").fetchone()[0]
    finally:
        node.db.close()
    assert ok_status == 200 and "Replies received" in ok_page and "posted on General" in ok_page
    assert bad_status == 200 and "Reply packet not used" in bad_page
    assert posted == 1
    assert [result["ok"] for result in told] == [True, False]


def test_an_oversized_reply_packet_is_refused_by_the_link(tmp_path):
    node = _Node(tmp_path)
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)

    async def scenario():
        async with node:
            grant = node.grants.issue_private(direction=UPLOAD, user=alice, transfer=PrivateTransfer(
                label="a QWK reply packet", max_bytes=1024, receive=receive_rep_file,
            ))
            form = aiohttp.FormData()
            form.add_field("file", b"x" * 5000, filename="TESTBBS.REP")
            async with aiohttp.ClientSession() as client:
                async with client.post(f"{node.base}/transfer/{grant.token}", data=form) as response:
                    return response.status

    try:
        status = asyncio.run(scenario())
    finally:
        node.db.close()
    assert status == 413
