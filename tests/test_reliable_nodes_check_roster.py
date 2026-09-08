"""
Tests for `services.reliable_nodes.check_roster` (issue #313) — the
roster reachability checker the project runs before publishing
`reliable-nodes.json` and periodically against the published copy.

Two things here are deliberately tested against the **real** node-side
code rather than against fixtures, and for the same reason. The checker
lives in `services/` and cannot import `netbbs` — it runs on the web
host, where the package is not installed — so both the HTTP signature it
keys on and its copy of the roster rules are duplicated, and duplication
is this module's real risk. Every place the two drift apart produces a
*false green*: a roster this gate approves that the network then rejects
or silently truncates, which is precisely the blind spot the checker was
written to close. So the reachability tests drive a real `LinkServer`,
and `test_validator_agrees_with_the_real_node_parser` cross-checks the
validator against the real `parse_reliable_nodes` over a corpus.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
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
    _build_arg_parser,
    load_roster,
    main,
    normalize_entry,
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
    actually produces — see this module's docstring for why this is
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
    """An ordinary HTTP server that is not a Link node — a stale DNS
    record now pointing at someone's web server, or a reverse proxy in
    front of a node that is itself down."""
    responses: dict[str, object] = {"status": 404, "body": b"not found"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming
            # Drain the request body before replying. Closing a socket
            # with unread data still buffered makes Windows answer with
            # an RST, which the client sees as a connection error rather
            # than the HTTP status this fixture exists to serve — a
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
    """A 2xx to an unparseable hello is never a Link node — catching it
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


def test_probe_treats_link_s_own_rate_limiter_as_up(plain_http_server):
    """The Link server's rate-limit middleware answers 429 before the
    hello handler runs — still proof a Link node is there, and what a
    checker run too often would legitimately see."""
    url, responses = plain_http_server
    responses["status"] = 429
    responses["body"] = json.dumps({"error": "rate limit exceeded"}).encode()
    result = probe_link_node(url, timeout=5.0)
    assert result.status == OK
    assert "429" in result.detail


def test_probe_does_not_trust_a_429_from_something_other_than_link(plain_http_server):
    """A CDN or reverse proxy fronting a dead node can rate-limit on its
    own. 429 alone proves nothing about what is behind it, so treating
    it as healthy would recreate the false positive this tool exists to
    catch."""
    url, responses = plain_http_server
    responses["status"] = 429
    responses["body"] = b"<html>Too Many Requests</html>"
    result = probe_link_node(url, timeout=5.0)
    assert result.status == NOT_LINK
    assert "proxy or CDN" in result.detail


@pytest.fixture
def non_http_server():
    """A TCP service that is not HTTP at all — a stale roster URL now
    pointing at an SSH or SMTP port."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        try:
            conn, _ = server.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        server.close()
        thread.join(timeout=5)


def test_probe_reports_a_non_http_service_instead_of_raising(non_http_server):
    """`http.client.HTTPException` is not an `OSError`, so it escapes the
    connection-error handler. Unhandled, one stale roster entry pointing
    at a non-HTTP port would abort the whole run with a traceback and
    leave every later entry unchecked."""
    result = probe_link_node(non_http_server, timeout=5.0)
    assert result.status in (NOT_LINK, DOWN)
    assert result.detail


def test_probe_reports_an_unencodable_hostname_instead_of_raising():
    """A host label over 63 characters makes urllib raise UnicodeError
    from IDNA encoding. It is a ValueError, so no connection-error
    handler matches it, and unhandled it aborts the entire run -- while
    both roster parsers accept the URL as structurally valid, so an
    entry like this really can be published."""
    url = "http://" + "d" * 120 + ".example:7862"
    assert normalize_entry({"name": "Long", "url": url}) is not None, (
        "the roster parsers accept this URL, so the probe must cope with it"
    )
    result = probe_link_node(url, timeout=2.0)
    assert result.status in (DOWN, NOT_LINK)
    assert result.detail


# Documents whose interpretation must be identical on both sides. The
# interesting ones are the divergences the first cut of this checker had:
# a whitespace-only name, an unstripped URL, a trailing-slash duplicate,
# a deliberately empty roster, and an over-cap document.
_ROSTER_CORPUS = [
    {"version": 1, "nodes": [{"name": "Reliable Link", "url": "http://relink.netbbs.org:7862"}]},
    {"version": 1, "nodes": []},
    {"version": 1, "nodes": [{"name": "   ", "url": "http://a.example"}]},
    {"version": 1, "nodes": [{"name": "  Padded  ", "url": "  http://a.example/  "}]},
    {"version": 1, "nodes": [{"name": "a", "url": "http://a.example"},
                             {"name": "b", "url": "http://a.example/"}]},
    {"version": 1, "nodes": [{"name": "a", "url": "http://a.example"},
                             {"name": "b", "url": "HTTP://A.EXAMPLE"}]},
    {"version": 1, "nodes": [{"name": "n", "url": "ftp://a.example"}]},
    {"version": 1, "nodes": [{"name": "n", "url": "http://"}]},
    {"version": 1, "nodes": [{"name": "n", "url": "http://a.example:0"}]},
    {"version": 1, "nodes": [{"name": "n", "url": "http://[unbalanced"}]},
    {"version": 1, "nodes": [{"name": "x" * 65, "url": "http://a.example"}]},
    {"version": 1, "nodes": [{"name": "n\x07", "url": "http://a.example"}]},
    {"version": 1, "nodes": [{"url": "http://a.example"}]},
    {"version": 1, "nodes": ["not-an-object"]},
    {"version": 1, "nodes": [{"name": str(i), "url": f"http://{i}.example"} for i in range(40)]},
    {"version": 1, "nodes": [{"name": str(i), "url": f"http://{i}.example"} for i in range(257)]},
    # A version mismatch discards the whole document, however good the
    # entries look -- the corpus was all version 1 before, so nothing
    # pinned that.
    {"version": 2, "nodes": [{"name": "Reliable Link", "url": "http://relink.example:7862"}]},
    {"version": 2, "nodes": []},
    {"nodes": [{"name": "n", "url": "http://a.example"}]},
]


@pytest.mark.parametrize("document", _ROSTER_CORPUS, ids=range(len(_ROSTER_CORPUS)))
def test_validator_agrees_with_the_real_node_parser(document):
    """The checker cannot import `netbbs`, so its copy of the roster
    rules is the thing most likely to drift — and every divergence is a
    false green. Pin the two together against the real parser rather
    than trusting inspection to keep them in step."""
    from netbbs.link.reliable_nodes import ReliableNodesError, parse_reliable_nodes

    raw = json.dumps(document)
    try:
        expected = [(node.name, node.url) for node in parse_reliable_nodes(raw)]
        node_rejected_outright = False
    except ReliableNodesError:
        expected, node_rejected_outright = [], True

    entries, problems = validate_roster(document)
    assert entries == expected, f"checker keeps {entries}, node keeps {expected}"
    if node_rejected_outright:
        assert problems, "a document every node discards must never pass the gate"
    if len(entries) < len(document.get("nodes", [])):
        assert problems, "anything a node silently drops has to surface as a problem"


def test_normalize_entry_strips_like_the_node_does():
    """The specific divergence that mattered: a node strips before
    testing emptiness, so a whitespace-only name is skipped there. If it
    were the sole entry, nodes would cache an empty list and retire
    their built-in fallback while this gate reported a healthy roster."""
    assert normalize_entry({"name": "   ", "url": "http://a.example"}) is None
    assert normalize_entry({"name": " N ", "url": " http://a.example/ "}) == ("N", "http://a.example")


def test_validator_accepts_a_deliberately_empty_roster():
    """A fetched empty roster is the supported way to retire every
    built-in entry — `get_cached_reliable_nodes` preserves `[]` rather
    than falling back. The gate must not block that mechanism."""
    entries, problems = validate_roster({"version": 1, "nodes": []})
    assert entries == [] and problems == []


def test_main_exits_zero_for_a_deliberately_empty_roster(tmp_path, capsys):
    roster = tmp_path / "reliable-nodes.json"
    roster.write_text(json.dumps({"version": 1, "nodes": []}), encoding="utf-8")
    assert main([str(roster)]) == 0
    assert "empty" in capsys.readouterr().out


def test_validate_roster_accepts_the_shipped_roster():
    """The roster this repository actually publishes must be valid on
    its own terms — a structural regression in it is exactly what a
    pre-publish check is for."""
    import pathlib

    roster = json.loads(
        (pathlib.Path(__file__).resolve().parents[1]
         / "services" / "reliable_nodes" / "reliable-nodes.json").read_text(encoding="utf-8")
    )
    entries, problems = validate_roster(roster)
    assert problems == []
    assert entries, "the shipped roster must list at least one node"


def test_validate_roster_still_probes_the_entries_a_node_would_keep():
    """One bad entry must not suppress checking the good ones — a node
    skips malformed entries individually, so the checker does too."""
    entries, problems = validate_roster(
        {"version": 1, "nodes": [{"name": "bad"}, {"name": "good", "url": "http://good.example"}]}
    )
    assert entries == [("good", "http://good.example")]
    assert len(problems) == 1


@pytest.mark.parametrize(
    "document, expected_fragment",
    [
        ({"version": 2, "nodes": [{"name": "n", "url": "http://a.example"}]}, "version is 2"),
        ({"version": 1}, "'nodes' is missing"),
        ({"version": 1, "nodes": [{"name": "  ", "url": "http://a.example"}]}, "malformed"),
        ({"version": 1, "nodes": [{"name": str(i), "url": f"http://{i}.e"} for i in range(257)]},
         "rejects the whole document"),
    ],
)
def test_validate_roster_reports_what_a_node_would_reject(document, expected_fragment):
    _, problems = validate_roster(document)
    assert any(expected_fragment in problem for problem in problems), problems


def test_load_roster_rejects_an_oversized_document(tmp_path):
    """Larger than the node parser's cap means every node discards it
    and keeps its previous roster — a publish that looks fine and is
    inert."""
    roster = tmp_path / "big.json"
    roster.write_text(
        json.dumps({"version": 1, "padding": "x" * (64 * 1024), "nodes": []}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="larger than"):
        load_roster(str(roster))


def test_main_exits_two_for_an_oversized_document(tmp_path, capsys):
    roster = tmp_path / "big.json"
    roster.write_text(
        json.dumps({"version": 1, "padding": "x" * (64 * 1024), "nodes": []}), encoding="utf-8"
    )
    assert main([str(roster)]) == 2
    assert "could not read roster" in capsys.readouterr().err


def test_main_exits_non_zero_for_an_unreachable_roster(tmp_path, capsys):
    """The exit code is the whole point of the pre-publish/cron use —
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
        json.dumps({"version": 1, "nodes": [{"name": "Reliable Link", "url": "http://relink.example"}]}),
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


def test_output_wraps_to_the_terminal_width(tmp_path, capsys, monkeypatch):
    """AGENTS.md: CLI prose wraps, measuring the stream it goes to. A
    64-character name plus a 256-character URL are both within the
    roster's own limits and would otherwise run past 300 columns,
    clipping the status word that matters."""
    from services.reliable_nodes import check_roster as module

    monkeypatch.setenv("COLUMNS", "60")
    long_url = "http://" + "d" * 200 + ".example"
    monkeypatch.setattr(
        module, "probe_link_node",
        lambda url, timeout=None: module.ProbeResult("", url, DOWN, "could not connect: timed out"),
    )
    roster = tmp_path / "reliable-nodes.json"
    roster.write_text(
        json.dumps({"version": 1, "nodes": [{"name": "N" * 64, "url": long_url}]}), encoding="utf-8"
    )
    main([str(roster)])
    captured = capsys.readouterr()
    overlong = [
        line for line in (captured.out + captured.err).splitlines()
        if len(line) > 60 and " " in line.strip()
    ]
    assert not overlong, overlong


def test_published_flag_actually_selects_the_published_roster():
    """argparse assigns a `nargs="?"` positional's default *after*
    processing optionals, so sharing a dest made `--published` alone
    resolve to the local file — the documented periodic check would have
    reported on the copy it was about to publish and never fetched the
    live one. This is the regression test for that silent no-op."""
    from services.reliable_nodes.check_roster import (
        DEFAULT_ROSTER_PATH, DEFAULT_ROSTER_URL, _build_arg_parser, _resolve_roster,
    )

    parse = _build_arg_parser().parse_args
    assert _resolve_roster(parse(["--published"])) == DEFAULT_ROSTER_URL
    assert _resolve_roster(parse([])) == DEFAULT_ROSTER_PATH
    assert _resolve_roster(parse(["other.json"])) == "other.json"
    with pytest.raises(ValueError, match="mutually exclusive"):
        _resolve_roster(parse(["--published", "other.json"]))


def test_main_reports_a_malformed_remote_roster_instead_of_raising(non_http_server):
    """`http.client.HTTPException` is not an `OSError`, and `load_roster`
    reaches `urlopen` by a different path than the probe — so fixing
    only the probe left this one escaping as a traceback."""
    assert main([non_http_server, "--timeout", "2"]) == 2


def test_output_escapes_control_characters_from_a_roster(tmp_path, capsys):
    """Neither parser rejects control characters in a URL (`_parse_entry`
    checks only the name), so a roster can carry one. Printed verbatim, a
    newline forges extra result lines in the output of the tool an
    operator reads to learn what is OK."""
    from services.reliable_nodes.check_roster import sanitize

    forged = "http://a.example\nOK       Fake <http://fake> -- answered a Link hello"
    assert "\n" not in sanitize(forged)
    assert "\\x1b" in sanitize("http://a\x1b[2J")

    roster = tmp_path / "reliable-nodes.json"
    roster.write_text(
        json.dumps({"version": 1, "nodes": [{"name": "Sneaky", "url": forged}]}), encoding="utf-8"
    )
    main([str(roster), "--timeout", "2"])
    captured = capsys.readouterr()
    assert not any(
        line.startswith("OK") for line in (captured.out + captured.err).splitlines()
    ), "a roster must not be able to forge an OK line"


def test_wrapping_splits_tokens_wider_than_the_terminal():
    """A 256-character URL is within the roster's own limits, so leaving
    over-width tokens intact still overflows. AGENTS.md requires an
    over-width row to wrap rather than run off the screen."""
    from services.reliable_nodes.check_roster import _wrap_to_width, display_width

    text = "DOWN     " + "N" * 64 + " <http://" + "d" * 200 + ".example> -- timed out"
    lines = _wrap_to_width(text, 60)
    assert lines and all(display_width(line) <= 60 for line in lines)
    # Wrapping must not lose or reorder content, only add line breaks.
    assert "".join(lines).replace(" ", "") == text.replace(" ", "")


def test_display_width_counts_terminal_columns_not_characters():
    """Roster names are SysOp-chosen and may be CJK, where one character
    occupies two columns — `textwrap` alone would let them overflow."""
    from services.reliable_nodes.check_roster import display_width, _wrap_to_width

    assert display_width("東京ノード") == 10
    assert display_width("Tokyo") == 5
    assert all(display_width(line) <= 12 for line in _wrap_to_width("東京ノード" * 6, 12))


def test_an_unsupported_version_stops_validation_entirely(tmp_path, capsys):
    """`parse_reliable_nodes` raises on the version before it looks at
    `nodes`, so probing those entries would report on nodes nothing will
    ever dial — and with an empty list the old code printed the
    retirement message when installed nodes actually keep their
    previous roster."""
    entries, problems = validate_roster(
        {"version": 2, "nodes": [{"name": "Live", "url": "http://a.example"}]}
    )
    assert entries == []
    assert any("version is 2" in problem for problem in problems)

    roster = tmp_path / "reliable-nodes.json"
    roster.write_text(json.dumps({"version": 2, "nodes": []}), encoding="utf-8")
    assert main([str(roster)]) == 1
    assert "retire its built-in fallback" not in capsys.readouterr().out


def test_load_roster_accepts_a_bom_exactly_as_a_node_does(tmp_path):
    """`json.loads` on bytes detects the encoding and a BOM, which is
    what `parse_reliable_nodes` gets. Decoding UTF-8 first left a
    leading U+FEFF, so the gate rejected a document — a common result of
    editing on Windows — that every installed node accepts."""
    from netbbs.link.reliable_nodes import parse_reliable_nodes

    raw = json.dumps({"version": 1, "nodes": []}).encode("utf-8-sig")
    roster = tmp_path / "bom.json"
    roster.write_bytes(raw)
    assert parse_reliable_nodes(raw) == []          # the node accepts it
    assert load_roster(str(roster)) == {"version": 1, "nodes": []}


def test_timeout_option_rejects_values_a_socket_cannot_use(capsys):
    """0, a negative, nan or inf would make every probe fail and report
    a healthy roster as entirely DOWN, or raise OverflowError from
    inside urllib. A wrong verdict is worse than a usage error."""
    parser = _build_arg_parser()
    for bad in ["0", "-1", "nan", "inf"]:
        with pytest.raises(SystemExit):
            parser.parse_args(["--timeout", bad])
    assert parser.parse_args(["--timeout", "2.5"]).timeout == 2.5


def test_read_bounded_stops_at_its_deadline():
    """`urlopen`'s timeout bounds each socket operation, not the whole
    exchange, so a server dripping bytes below it never reaches the byte
    cap and holds the read open indefinitely — one entry silently ending
    a cron run."""
    import time as _time
    from services.reliable_nodes.check_roster import _read_bounded

    class Dripping:
        def read(self, size):
            _time.sleep(0.05)
            return b"x" * min(size, 8)

    with pytest.raises(TimeoutError):
        _read_bounded(Dripping(), deadline=_time.monotonic() + 0.2)


def test_read_bounded_deadline_survives_a_sub_chunk_drip():
    """`read(n)` blocks until it has n bytes or EOF, so a deadline
    checked only between `read` calls is never reached during exactly
    the slow drip it exists to catch. `read1` returns after one
    underlying socket read, which is what makes it observable."""
    import time as _time
    from services.reliable_nodes.check_roster import _read_bounded

    class Dripping:
        """Never returns a full chunk, and never reaches EOF."""

        def read1(self, size):
            _time.sleep(0.02)
            return b"x"

        def read(self, size):  # pragma: no cover - read1 is preferred
            raise AssertionError("must not block on a full-size read")

    started = _time.monotonic()
    with pytest.raises(TimeoutError):
        _read_bounded(Dripping(), deadline=started + 0.3)
    assert _time.monotonic() - started < 5, "must give up near its deadline"


def test_wrapping_terminates_when_a_glyph_is_wider_than_the_terminal():
    """At width 1 a double-width glyph can never fit; a zero-length cut
    would append "" and reassign the same chunk forever, hanging the
    checker instead of printing a verdict."""
    from services.reliable_nodes.check_roster import _wrap_to_width

    lines = _wrap_to_width("東京ノード", 1)
    assert lines and "".join(lines) == "東京ノード"


def test_load_error_text_from_a_server_is_sanitized(tmp_path, capsys):
    """An HTTP reason phrase is server-controlled and reaches stderr on
    this path, so it gets the same treatment as any other roster-derived
    string bound for a terminal."""
    from services.reliable_nodes import check_roster as module

    def exploding(source, timeout=None):
        raise OSError("HTTP 500 \x1b[2Jcleared your screen")

    original = module.load_roster
    module.load_roster = exploding
    try:
        assert main([str(tmp_path / "any.json")]) == 2
    finally:
        module.load_roster = original
    captured = capsys.readouterr()
    assert "\x1b" not in captured.err
    assert "\\x1b" in captured.err


@pytest.fixture
def header_dripping_server():
    """Sends header bytes forever, each well inside the socket timeout.
    No individual socket operation ever times out, so `urlopen` never
    returns and a per-operation timeout bounds nothing."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()

    def serve():
        try:
            conn, _ = server.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n")
                while not stop.is_set():
                    conn.sendall(b"X-Pad: x\r\n")
                    stop.wait(0.02)
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        server.close()
        thread.join(timeout=5)


def test_probe_gives_up_on_a_server_that_drips_headers(header_dripping_server):
    """A socket timeout bounds each operation, never the exchange, and
    reading the body in small pieces does not help because `urlopen` has
    not returned yet. Without a wall-clock bound one roster entry stalls
    the whole cron run — the checker itself silently stopping partway
    through, which is precisely the failure it exists to detect."""
    import time as _time

    started = _time.monotonic()
    result = probe_link_node(header_dripping_server, timeout=2.0)
    elapsed = _time.monotonic() - started
    assert result.status == DOWN
    assert elapsed < 15, f"probe took {elapsed:.1f}s -- the deadline did not bound it"


def test_a_dripping_entry_does_not_stop_the_rest_of_the_run(
    header_dripping_server, tmp_path, capsys
):
    """The point of bounding it: later entries still get checked."""
    roster = tmp_path / "reliable-nodes.json"
    roster.write_text(
        json.dumps({"version": 1, "nodes": [
            {"name": "Dripping", "url": header_dripping_server},
            {"name": "Dead", "url": f"http://127.0.0.1:{_free_port()}"},
        ]}),
        encoding="utf-8",
    )
    assert main([str(roster), "--timeout", "2"]) == 1
    err = capsys.readouterr().err
    assert "Dripping" in err and "Dead" in err, err


def test_the_checker_process_exits_after_a_drip_timeout(tmp_path, header_dripping_server):
    """The timeout is worthless if the process then hangs on the way
    out. `ThreadPoolExecutor` workers are non-daemon and
    `concurrent.futures` registers an atexit hook that joins them, so a
    wedged worker would let the checker print its verdict and then never
    exit — the same stalled cron run, moved to process shutdown. Only a
    real subprocess can prove it terminates.
    """
    import subprocess
    import sys as _sys

    roster = tmp_path / "reliable-nodes.json"
    roster.write_text(
        json.dumps({"version": 1, "nodes": [{"name": "Dripping", "url": header_dripping_server}]}),
        encoding="utf-8",
    )
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(repo_root), str(repo_root / "src")]))
    completed = subprocess.run(
        [_sys.executable, "-m", "services.reliable_nodes.check_roster",
         str(roster), "--timeout", "2"],
        cwd=str(repo_root), env=env, capture_output=True, text=True, timeout=45,
    )
    assert completed.returncode == 1, completed.stderr
    assert "DOWN" in completed.stderr
