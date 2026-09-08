"""
Tests for `services.reliable_nodes.check_roster` (issue #313) — the
roster reachability checker the project runs before publishing
`reliable-nodes.json` and periodically against the published copy.

The reachability tests drive a **real** `LinkServer`, not a stub of one.
That is the whole point of them: the checker asserts a specific
signature (HTTP 400 with a `malformed hello` error body) produced by
`netbbs.link.transport.LinkServer._handle_hello`, and the checker lives
in `services/` and deliberately does not import `netbbs`, so nothing but
a test that dials the real server would notice if that signature ever
changed. A checker that silently started reporting a live roster node as
`NOT_LINK` — or, worse, a dead one as `OK` — would recreate exactly the
blind spot it was written to close.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.link.transport import LinkServer
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from services.reliable_nodes.check_roster import (
    DOWN,
    NOT_LINK,
    OK,
    main,
    probe_link_node,
    validate_roster,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _probe_a_real_link_server(tmp_path) -> tuple[str, str]:
    """Stand up a real Link server, probe it, tear it down. Returns the
    probe's (status, detail)."""
    node = LinkNode(identity=bootstrap_node_identity("roster-node"))
    db = Database(tmp_path / "roster-node.db")
    lane = DatabaseLane(db.path)
    result: dict[str, tuple[str, str]] = {}

    async def scenario():
        server = LinkServer(
            host="127.0.0.1", port=0, node=node,
            own_hello_provider=lambda: node.build_hello(addresses=None, outgoing_only=False),
            lane=lane,
        )
        await server.start()
        try:
            url = f"http://127.0.0.1:{server.port}"
            # urllib is blocking; keep it off the loop running the server.
            probed = await asyncio.to_thread(probe_link_node, url, timeout=5.0)
            result["probe"] = (probed.status, probed.detail)
        finally:
            await server.stop()

    try:
        asyncio.run(scenario())
    finally:
        lane.close()
        db.close()
    return result["probe"]


def test_probe_reports_ok_against_a_real_link_server(tmp_path):
    """The signature the checker keys on is the one a real LinkServer
    actually produces -- see this module's docstring for why this is
    tested against the real thing rather than a canned 400."""
    status, detail = _probe_a_real_link_server(tmp_path)
    assert status == OK, detail
    assert detail == "answered a Link hello"


def test_probe_reports_down_when_nothing_listens():
    """ReLink's actual failure mode: the roster entry resolved and the
    port was permitted, but the node's Link participation was off, so
    nothing was listening at all."""
    result = probe_link_node(f"http://127.0.0.1:{_free_port()}", timeout=2.0)
    assert result.status == DOWN
    assert "could not connect" in result.detail


@pytest.fixture
def plain_http_server():
    """An ordinary HTTP server that is not a Link node -- a stale DNS
    record now pointing at someone's web server, or a reverse proxy in
    front of a node that is itself down."""
    responses: dict[str, object] = {"status": 404, "body": b"not found"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming
            # Drain the request body before replying. Closing a socket
            # with unread data still buffered makes Windows answer with
            # an RST, which the client sees as a connection error rather
            # than the HTTP status this fixture exists to serve -- a
            # flaky DOWN instead of the intended NOT_LINK.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(responses["status"])
            self.end_headers()
            self.wfile.write(responses["body"])

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_probe_reports_not_link_for_an_unrelated_http_server(plain_http_server):
    url, _ = plain_http_server
    result = probe_link_node(url, timeout=5.0)
    assert result.status == NOT_LINK
    assert "HTTP 404" in result.detail


def test_probe_reports_not_link_when_something_answers_a_hello_with_success(plain_http_server):
    """A 2xx to an unparseable hello is never a Link node -- catching it
    matters because a captive portal or a proxy answering everything
    with 200 would otherwise read as a healthy roster entry."""
    url, responses = plain_http_server
    responses["status"] = 200
    responses["body"] = b"{}"
    result = probe_link_node(url, timeout=5.0)
    assert result.status == NOT_LINK
    assert "HTTP 200" in result.detail


def test_probe_reports_not_link_for_a_400_that_is_not_a_link_rejection(plain_http_server):
    url, responses = plain_http_server
    responses["status"] = 400
    responses["body"] = json.dumps({"error": "bad request"}).encode()
    result = probe_link_node(url, timeout=5.0)
    assert result.status == NOT_LINK
    assert "unexpected body" in result.detail


def test_probe_treats_a_rate_limited_node_as_up(plain_http_server):
    """The Link server's own rate-limit middleware answers 429 before
    the hello handler runs -- still proof a Link node is there, and what
    a checker run too often would legitimately see."""
    url, responses = plain_http_server
    responses["status"] = 429
    responses["body"] = json.dumps({"error": "rate limit exceeded"}).encode()
    result = probe_link_node(url, timeout=5.0)
    assert result.status == OK
    assert "429" in result.detail


def test_validate_roster_accepts_the_shipped_roster():
    """The roster this repository actually publishes must be valid on
    its own terms -- a structural regression in it is exactly what a
    pre-publish check is for."""
    import pathlib

    roster = json.loads(
        (pathlib.Path(__file__).resolve().parents[1]
         / "services" / "reliable_nodes" / "reliable-nodes.json").read_text(encoding="utf-8")
    )
    entries, problems = validate_roster(roster)
    assert problems == []
    assert entries, "the shipped roster must list at least one node"


@pytest.mark.parametrize(
    "document, expected_fragment",
    [
        ({"version": 2, "nodes": [{"name": "n", "url": "http://a"}]}, "version is 2"),
        ({"version": 1}, "'nodes' is missing"),
        ({"version": 1, "nodes": []}, "empty"),
        ({"version": 1, "nodes": [{"url": "http://a"}]}, "'name' must be"),
        ({"version": 1, "nodes": [{"name": "n", "url": "ftp://a"}]}, "must start with"),
        ({"version": 1, "nodes": [{"name": "n", "url": "http://a"},
                                  {"name": "m", "url": "http://a/"}]}, "duplicate URL"),
        ({"version": 1, "nodes": [{"name": "x" * 65, "url": "http://a"}]}, "'name' must be"),
        ({"version": 1, "nodes": [{"name": "n\x07", "url": "http://a"}]}, "control characters"),
        ({"version": 1, "nodes": ["not-an-object"]}, "not an object"),
        ({"version": 1, "nodes": [{"name": str(i), "url": f"http://{i}"} for i in range(33)]},
         "keep only the first 32"),
    ],
)
def test_validate_roster_reports_what_a_node_would_reject(document, expected_fragment):
    _, problems = validate_roster(document)
    assert any(expected_fragment in problem for problem in problems), problems


def test_validate_roster_still_probes_the_entries_a_node_would_keep():
    """One bad entry must not suppress checking the good ones -- a node
    skips malformed entries individually, so the checker does too."""
    entries, problems = validate_roster(
        {"version": 1, "nodes": [{"name": "bad"}, {"name": "good", "url": "http://good"}]}
    )
    assert entries == [("good", "http://good")]
    assert len(problems) == 1


def test_main_exits_non_zero_for_an_unreachable_roster(tmp_path, capsys):
    """The exit code is the whole point of the pre-publish/cron use --
    it has to fail on a roster whose nodes do not answer."""
    roster = tmp_path / "reliable-nodes.json"
    roster.write_text(
        json.dumps({"version": 1, "nodes": [
            {"name": "Dead Link", "url": f"http://127.0.0.1:{_free_port()}"}
        ]}),
        encoding="utf-8",
    )
    assert main([str(roster), "--timeout", "2"]) == 1
    assert "DOWN" in capsys.readouterr().err


def test_main_exits_zero_for_a_healthy_roster(tmp_path, capsys, monkeypatch):
    from services.reliable_nodes import check_roster as module

    monkeypatch.setattr(
        module, "probe_link_node",
        lambda url, timeout=None: module.ProbeResult("", url, OK, "answered a Link hello"),
    )
    roster = tmp_path / "reliable-nodes.json"
    roster.write_text(
        json.dumps({"version": 1, "nodes": [{"name": "Reliable Link", "url": "http://relink"}]}),
        encoding="utf-8",
    )
    assert main([str(roster)]) == 0
    assert "1/1 roster nodes reachable" in capsys.readouterr().out


def test_main_exits_non_zero_when_the_roster_itself_is_malformed(tmp_path):
    """A roster that is reachable but partly unusable still fails: a
    wrong `version` makes every node discard the whole document."""
    roster = tmp_path / "reliable-nodes.json"
    roster.write_text(json.dumps({"version": 99, "nodes": []}), encoding="utf-8")
    assert main([str(roster)]) == 1


def test_main_exits_two_for_a_roster_that_cannot_be_read(tmp_path, capsys):
    assert main([str(tmp_path / "missing.json")]) == 2
    assert "could not read roster" in capsys.readouterr().err
