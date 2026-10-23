"""QWKnet transports (#1481, #1482; design doc §6.11) over real loopback
sockets: a NetBBS node calling a NetBBS hub's `/qwk.ssjs` the way
Synchronet's qnet-http does, the hub's login rules, and the FTP client
against a minimal FTP server whose PASV reply points elsewhere."""

from __future__ import annotations

import asyncio
import base64
import datetime
import socket
import socketserver
import threading
from dataclasses import replace

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from netbbs.auth.passwords import hash_password  # noqa: E402
from netbbs.auth.users import SYSOP_LEVEL, create_user  # noqa: E402
from netbbs.boards.boards import create_board  # noqa: E402
from netbbs.boards.posts import create_post  # noqa: E402
from netbbs.qwk.packet import Conference, PacketInfo, PacketMessage, build_packet  # noqa: E402
from netbbs.qwk.rep import parse_rep  # noqa: E402
from netbbs.qwknet.exchange import queue_post_if_qwknet  # noqa: E402
from netbbs.qwknet.hub import MAX_FAILURES, QwknetHub  # noqa: E402
from netbbs.qwknet.mailer import QwknetMailer  # noqa: E402
from netbbs.qwknet.networks import (  # noqa: E402
    TRANSPORT_FTP,
    TRANSPORT_HTTP,
    QwknetNetwork,
    QwknetNode,
    get_network,
    get_node,
    map_conference,
    save_network,
    save_node,
)
from netbbs.qwknet.transport import TransportError, call_hub  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402
from netbbs.storage.execution import DatabaseLane  # noqa: E402


@pytest.fixture
def hub_db(tmp_path):
    database = Database(tmp_path / "hub.db")
    yield database
    database.close()


@pytest.fixture
def node_db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _boards(db, name):
    sysop = create_user(db, name, password="hunter2", user_level=SYSOP_LEVEL)
    return sysop, create_board(db, "General", creator=sysop)


async def _serve(hub: QwknetHub):
    app = web.Application()
    hub.add_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]


def test_a_netbbs_node_exchanges_with_a_netbbs_hub_over_http(hub_db, node_db):
    hub_sysop, hub_board = _boards(hub_db, "HubOp")
    hub_net = save_network(hub_db, QwknetNetwork(name="HomeNet", our_id="HOMEHUB", serve_nodes=True, enabled=True))
    map_conference(hub_db, hub_board, hub_net.id, 1)
    account = save_node(hub_db, QwknetNode(network_id=hub_net.id, qwk_id="RELINK"),
                        password_hash=hash_password("s3cret"))
    hub_post = create_post(hub_db, hub_board, hub_sysop, "From the hub", "Hello nodes")
    queue_post_if_qwknet(hub_db, hub_post, hub_board)

    node_sysop, node_board = _boards(node_db, "Carrier")

    async def run():
        hub_lane, node_lane = DatabaseLane(hub_db.path), DatabaseLane(node_db.path)
        runner, port = await _serve(QwknetHub(hub_lane))
        try:
            network = save_network(node_db, QwknetNetwork(
                name="HomeNet", our_id="RELINK", hub_id="HOMEHUB", transport=TRANSPORT_HTTP, host="127.0.0.1",
                port=port, password="s3cret", enabled=True))
            map_conference(node_db, node_board, network.id, 1)
            node_post = create_post(node_db, node_board, node_sysop, "From the node", "Hello hub")
            queue_post_if_qwknet(node_db, node_post, node_board)
            mailer = QwknetMailer(node_lane)
            first = await mailer.poll(get_network(node_db, network.id))
            second = await mailer.poll(get_network(node_db, network.id))
            wrong = await mailer.poll(replace(get_network(node_db, network.id), password="nope"))
            return first, second, wrong
        finally:
            await runner.cleanup()
            hub_lane.close()
            node_lane.close()

    first, second, wrong = asyncio.run(run())
    assert first == "sent 1; received 1 posts"
    assert second == "sent nothing; nothing waiting"
    assert wrong.startswith("failed: the hub refused the login")
    hub_labels = [row["author_label"] for row in hub_db.connection.execute("SELECT author_label FROM posts ORDER BY id")]
    assert hub_labels == ["HubOp", "Carrier (RELINK)"]
    node_rows = node_db.connection.execute("SELECT author_label, subject FROM posts ORDER BY id").fetchall()
    assert [(r["author_label"], r["subject"]) for r in node_rows] == [
        ("Carrier", "From the node"), ("HubOp (HOMEHUB)", "From the hub")]
    # The hub's cursor for the node moved only on the confirmation.
    assert get_node(hub_db, account.id).pending_seq is None
    assert get_node(hub_db, account.id).sent_seq > 0


def test_the_hub_refuses_anonymous_and_throttles_failed_logins(hub_db):
    hub_net = save_network(hub_db, QwknetNetwork(name="HomeNet", our_id="HOMEHUB", serve_nodes=True, enabled=True))
    save_node(hub_db, QwknetNode(network_id=hub_net.id, qwk_id="RELINK"), password_hash=hash_password("s3cret"))

    async def run():
        lane = DatabaseLane(hub_db.path)
        runner, port = await _serve(QwknetHub(lane))
        url = f"http://127.0.0.1:{port}/qwk.ssjs"
        statuses = []
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(url) as response:
                    statuses.append((response.status, response.headers.get("WWW-Authenticate", "")))
                bad = {"Authorization": "Basic " + base64.b64encode(b"RELINK:wrong").decode()}
                for _ in range(MAX_FAILURES):
                    async with client.get(url, headers=bad) as response:
                        statuses.append((response.status, ""))
                good = {"Authorization": "Basic " + base64.b64encode(b"RELINK:s3cret").decode()}
                async with client.get(url, headers=good) as response:
                    statuses.append((response.status, ""))
        finally:
            await runner.cleanup()
            lane.close()
        return statuses

    statuses = asyncio.run(run())
    assert statuses[0][0] == 401 and statuses[0][1].startswith("Basic")
    assert {status for status, _ in statuses[1:-1]} == {401}
    assert statuses[-1][0] == 429  # locked out even with the right password, for the window


# --- FTP -----------------------------------------------------------------------------------------


class _FtpHandler(socketserver.StreamRequestHandler):
    """Just enough FTP for qnet-ftp's session. PASV names an address that
    is not this server's, as a hostile hub would."""

    def handle(self):
        server = self.server
        self._send("220 hub ready")
        data_listener = None
        while True:
            line = self.rfile.readline().decode("ascii", errors="replace").strip()
            if not line:
                return
            command, _, argument = line.partition(" ")
            command = command.upper()
            server.commands.append(command)
            if command == "USER":
                self._send("331 password")
            elif command == "PASS":
                self._send("230 in" if argument == "s3cret" else "530 no")
            elif command == "TYPE":
                self._send("200 ok")
            elif command == "PASV":
                data_listener = socket.socket()
                data_listener.bind(("127.0.0.1", 0))
                data_listener.listen(1)
                port = data_listener.getsockname()[1]
                self._send(f"227 Entering Passive Mode (10,255,255,1,{port // 256},{port % 256})")
            elif command in ("STOR", "RETR"):
                if command == "RETR" and argument.lower() not in server.files:
                    self._send("550 no such file")
                    continue
                self._send("150 data")
                conn, _ = data_listener.accept()
                with conn:
                    if command == "STOR":
                        chunks = []
                        while chunk := conn.recv(65536):
                            chunks.append(chunk)
                        server.stored[argument.lower()] = b"".join(chunks)
                    else:
                        conn.sendall(server.files[argument.lower()])
                data_listener.close()
                self._send("226 done")
            elif command == "QUIT":
                self._send("221 bye")
                return
            else:
                self._send("502 no")

    def _send(self, text):
        self.wfile.write(text.encode("ascii") + b"\r\n")


def test_the_ftp_client_uploads_then_downloads_in_passive_mode_to_the_hubs_own_address():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FtpHandler)
    server.daemon_threads = True
    server.commands, server.stored = [], {}
    info = PacketInfo(bbs_id="VERT", bbs_name="Vertrauen", user_name="RELINK",
                      created_at=datetime.datetime(2026, 10, 1))
    packet = build_packet(info, [Conference(2001, "General")], [PacketMessage(
        conference=2001, number=1, when=datetime.datetime(2026, 10, 1), to="All", sender="DM", subject="s",
        body="b")])
    server.files = {"vert.qwk": packet}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    network = QwknetNetwork(name="DOVE-Net", our_id="RELINK", hub_id="VERT", transport=TRANSPORT_FTP,
                            host="127.0.0.1", port=server.server_address[1], password="s3cret", enabled=True)
    tossed = []

    async def toss(data):
        tossed.append(data)
        return "1 posts"

    from netbbs.qwk.rep import OutgoingReply, build_rep

    rep = build_rep("VERT", [OutgoingReply(2001, "All", "Carrier", "Up", "going up")])
    try:
        outcome = asyncio.run(call_hub(network, rep, toss))
        with pytest.raises(TransportError, match="refused the login"):
            asyncio.run(call_hub(replace(network, password="bad"), None, toss))
        server.files = {}
        assert asyncio.run(call_hub(network, None, toss)).received == 0
    finally:
        server.shutdown()
        server.server_close()
    assert outcome.uploaded and outcome.received == len(packet)
    assert tossed == [packet]
    (message,) = parse_rep(server.stored["vert.rep"], bbs_id="VERT").messages
    assert message.subject == "Up"
    assert "PORT" not in server.commands and "EPRT" not in server.commands
