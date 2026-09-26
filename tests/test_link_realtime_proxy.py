"""
Real-time Link through an HTTP `CONNECT` proxy (issue #628; decisions under
design doc §16).

Everything here runs against a real loopback proxy -- an asyncio server that
parses `CONNECT`, insists on `Host`, optionally demands Basic credentials, and
pipes bytes -- per the project's rule about real boundaries. Loopback targets
never use a proxy, so each test dials a made-up hostname the proxy maps to the
real loopback server; that also proves the name reaches the proxy unresolved.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import pathlib
import re

import pytest

from netbbs.link import realtime_proxy
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.realtime_proxy import (
    REALTIME_PROXY_STATUS,
    RealtimeProxyError,
    RealtimeTargetError,
    open_realtime_connection,
    proxy_for,
    validate_authority,
)
from netbbs.link.transport import (
    LinkRealtimeServer,
    LinkRealtimeSessionRegistry,
    attach_relayed_session,
    decode_bridge_attach_record,
    dial_realtime_session,
    establish_noise_xx_responder,
    read_realtime_record,
)


async def _no_frames(session, frame) -> None:
    return None


class _ConnectProxy:
    """A small real HTTP proxy: `CONNECT` only. `targets` maps the authority a
    client asks for to where the proxy really connects. `mode` makes it
    misbehave: "refuse" answers 403, "silent" never answers, "hangup" closes
    without answering, "huge" answers with an oversized header block, and
    "http-only" answers 200 and then drops the tunnel at the client's first
    byte, as a proxy inspecting for TLS does."""

    def __init__(self, targets: dict[str, tuple[str, int]], *, credentials: str | None = None, mode: str = "ok"):
        self.targets = targets
        self.credentials = credentials
        self.mode = mode
        self.requests: list[str] = []
        self.eof_seen = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._pipes: set[asyncio.Task] = set()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> "_ConnectProxy":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None
        self._server.close()
        for task in list(self._pipes):
            task.cancel()
        await asyncio.gather(*self._pipes, return_exceptions=True)
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._pipes.add(task)
        try:
            await self._handle(reader, writer)
        finally:
            self._pipes.discard(task)
            writer.close()

    async def _handle(self, reader, writer) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        text = head.decode("latin-1")
        self.requests.append(text)
        lines = text.split("\r\n")
        match = re.match(r"^CONNECT (\S+) HTTP/1\.1$", lines[0])
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        if match is None or headers.get("Host") != match.group(1):
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        if self.credentials is not None and headers.get("Proxy-Authorization") != self.credentials:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            return
        if self.mode == "refuse":
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        elif self.mode == "silent":
            if await reader.read(1) == b"":
                self.eof_seen.set()
            return
        elif self.mode == "hangup":
            return
        elif self.mode == "huge":
            writer.write(b"HTTP/1.1 200 OK\r\nX-Pad: " + b"x" * 10000 + b"\r\n\r\n")
            if await reader.read(1) == b"":
                self.eof_seen.set()
            return
        target = self.targets.get(match.group(1))
        if target is None:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        up_reader, up_writer = await asyncio.open_connection(*target)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if self.mode == "http-only":
            # An inspecting proxy that sees something other than TLS in the
            # tunnel drops it.
            await reader.read(1)
            up_writer.close()
            return
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer), return_exceptions=True)


async def _pipe(reader, writer) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch, tmp_path):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    empty_netrc = tmp_path / "empty.netrc"
    empty_netrc.write_text("")
    monkeypatch.setenv("NETRC", str(empty_netrc))
    REALTIME_PROXY_STATUS.reset()
    yield
    REALTIME_PROXY_STATUS.reset()


async def _dial_bob_through(proxy_url_setter, *, proxy_kwargs=None, target="bob.example"):
    alice = bootstrap_node_identity("alice-proxy")
    bob = bootstrap_node_identity("bob-proxy")
    registry_a = LinkRealtimeSessionRegistry(own_fingerprint=alice.fingerprint)
    registry_b = LinkRealtimeSessionRegistry(own_fingerprint=bob.fingerprint)
    server = LinkRealtimeServer(host="127.0.0.1", port=0, identity=bob, registry=registry_b, on_frame=_no_frames)
    await server.start()
    try:
        async with _ConnectProxy({f"{target}:8862": ("127.0.0.1", server.port)}, **(proxy_kwargs or {})) as proxy:
            proxy_url_setter(proxy)
            try:
                session = await dial_realtime_session(
                    target, 8862, alice, on_frame=_no_frames, registry=registry_a,
                    expected_fingerprint=bob.fingerprint,
                )
            except Exception as exc:
                return proxy, exc
            try:
                assert session.remote_fingerprint == bob.fingerprint
            finally:
                await session.close(reason="test_done")
            return proxy, None
    finally:
        await registry_b.close_all(reason="test_done")
        await server.stop()


def test_a_direct_dial_runs_noise_through_the_tunnel(monkeypatch):
    def use(proxy):
        monkeypatch.setenv("HTTP_PROXY", proxy.url)

    proxy, error = asyncio.run(_dial_bob_through(use))

    assert error is None
    assert proxy.requests[0].startswith("CONNECT bob.example:8862 HTTP/1.1\r\nHost: bob.example:8862\r\n")
    assert (REALTIME_PROXY_STATUS.outcome, REALTIME_PROXY_STATUS.ok) == ("tunnel open", True)


def test_https_proxy_is_used_when_it_is_the_only_one_set(monkeypatch):
    proxy, error = asyncio.run(_dial_bob_through(lambda p: monkeypatch.setenv("HTTPS_PROXY", p.url)))
    assert error is None and len(proxy.requests) == 1


def test_basic_credentials_from_the_proxy_url_are_sent(monkeypatch):
    expected = "Basic " + base64.b64encode(b"carrier:s3cret word").decode("ascii")

    def use(proxy):
        monkeypatch.setenv("HTTP_PROXY", f"http://carrier:s3cret%20word@127.0.0.1:{proxy.port}")

    proxy, error = asyncio.run(_dial_bob_through(use, proxy_kwargs={"credentials": expected}))

    assert error is None
    assert f"Proxy-Authorization: {expected}\r\n" in proxy.requests[0]


def test_basic_credentials_fall_back_to_netrc(monkeypatch, tmp_path):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login carrier password fromnetrc\n")
    monkeypatch.setenv("NETRC", str(netrc))
    expected = "Basic " + base64.b64encode(b"carrier:fromnetrc").decode("ascii")

    proxy, error = asyncio.run(
        _dial_bob_through(lambda p: monkeypatch.setenv("HTTP_PROXY", p.url), proxy_kwargs={"credentials": expected})
    )
    assert error is None


def test_a_407_is_a_distinct_error_and_the_recorded_outcome(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="netbbs.link.realtime_proxy"):
        proxy, error = asyncio.run(
            _dial_bob_through(lambda p: monkeypatch.setenv("HTTP_PROXY", p.url), proxy_kwargs={"credentials": "Basic x"})
        )

    assert isinstance(error, RealtimeProxyError)
    assert REALTIME_PROXY_STATUS.outcome == "tunnel refused: 407 Proxy Authentication Required"
    assert REALTIME_PROXY_STATUS.ok is False
    assert [r.levelname for r in caplog.records if r.name == "netbbs.link.realtime_proxy"] == ["WARNING"]


def test_a_repeated_failure_is_logged_once(monkeypatch, caplog):
    """The anchor connector retries with backoff; the same refusal from the
    same proxy is one log line, not one per attempt."""

    async def three_times():
        async with _ConnectProxy({}, mode="refuse") as proxy:
            monkeypatch.setenv("HTTP_PROXY", proxy.url)
            for _ in range(3):
                with pytest.raises(RealtimeProxyError):
                    await open_realtime_connection("bob.example", 8862)

    with caplog.at_level(logging.WARNING, logger="netbbs.link.realtime_proxy"):
        asyncio.run(three_times())

    assert REALTIME_PROXY_STATUS.outcome == "tunnel refused: 403 Forbidden"
    assert len([r for r in caplog.records if r.name == "netbbs.link.realtime_proxy"]) == 1


def test_a_proxy_that_breaks_the_stream_records_a_failed_handshake_not_success(monkeypatch):
    """What a TLS-inspecting proxy looks like: the tunnel opens, and what runs
    through it does not survive."""
    proxy, error = asyncio.run(
        _dial_bob_through(lambda p: monkeypatch.setenv("HTTP_PROXY", p.url), proxy_kwargs={"mode": "http-only"})
    )

    assert error is not None
    assert (REALTIME_PROXY_STATUS.outcome, REALTIME_PROXY_STATUS.ok) == ("tunnel opened, handshake failed", False)


@pytest.mark.parametrize("mode", ["silent", "huge"])
def test_a_proxy_that_does_not_answer_properly_is_bounded_and_closed(monkeypatch, mode):
    monkeypatch.setattr(realtime_proxy, "PROXY_TIMEOUT_SECONDS", 0.5)

    async def scenario():
        async with _ConnectProxy({}, mode=mode) as proxy:
            monkeypatch.setenv("HTTP_PROXY", proxy.url)
            with pytest.raises(RealtimeProxyError):
                await asyncio.wait_for(open_realtime_connection("bob.example", 8862), timeout=5.0)
            # The helper closed its end: the proxy sees EOF.
            await asyncio.wait_for(proxy.eof_seen.wait(), timeout=2.0)

    asyncio.run(scenario())


def test_a_blackholed_proxy_is_bounded_by_the_same_timeout(monkeypatch):
    """A proxy address that never completes the TCP handshake: the timeout
    covers connecting, not only the answer."""
    monkeypatch.setattr(realtime_proxy, "PROXY_TIMEOUT_SECONDS", 0.3)
    never = asyncio.Event()

    async def hang(*args, **kwargs):
        await never.wait()

    monkeypatch.setattr(realtime_proxy.asyncio, "open_connection", hang)
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:3128")

    async def scenario():
        with pytest.raises(RealtimeProxyError, match="no answer from the proxy"):
            await asyncio.wait_for(open_realtime_connection("bob.example", 8862), timeout=5.0)

    asyncio.run(scenario())


def test_no_proxy_exempts_a_target(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "bob.example")
    assert proxy_for("bob.example") is None
    assert proxy_for("carol.example") is not None


def test_loopback_targets_never_use_a_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert proxy_for("127.0.0.1") is None
    assert proxy_for("localhost") is None


@pytest.mark.parametrize("url", ["socks5://127.0.0.1:1080", "https://proxy.example:3128"])
def test_an_unsupported_proxy_fails_the_dial_instead_of_dialling_direct(monkeypatch, url):
    monkeypatch.setenv("HTTP_PROXY", url)
    dialled = []

    async def direct(*args, **kwargs):
        dialled.append(args)
        raise AssertionError("must not dial direct")

    monkeypatch.setattr(realtime_proxy.asyncio, "open_connection", direct)

    async def scenario():
        with pytest.raises(RealtimeProxyError, match="not an http:// proxy"):
            await open_realtime_connection("bob.example", 8862)

    asyncio.run(scenario())
    assert dialled == []


@pytest.mark.parametrize(
    "host, port",
    [
        ("bob.example\r\nX-Injected: 1", 8862),
        ("bob example", 8862),
        ("", 8862),
        ("-bad.example", 8862),
        ("bob.example", 0),
        ("bob.example", 70000),
        ("bob.example", "8862"),
        (None, 8862),
    ],
)
def test_an_invalid_advertised_authority_is_refused_before_the_proxy_sees_it(monkeypatch, host, port):
    async def scenario():
        async with _ConnectProxy({}) as proxy:
            monkeypatch.setenv("HTTP_PROXY", proxy.url)
            with pytest.raises(RealtimeTargetError):
                await open_realtime_connection(host, port)
            await asyncio.sleep(0.05)
            assert proxy.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "host, authority",
    [("bob.example", "bob.example:8862"), ("198.51.100.7", "198.51.100.7:8862"), ("2001:db8::7", "[2001:db8::7]:8862")],
)
def test_valid_authorities_and_ipv6_bracketing(monkeypatch, host, authority):
    assert validate_authority(host, 8862) == (host, 8862)
    assert realtime_proxy._connect_authority(host, 8862) == authority


def test_a_relayed_attach_sends_its_preamble_and_handshake_through_the_tunnel(monkeypatch):
    """The attach record is plaintext ahead of Noise; through a tunnel it is
    just more bytes."""
    token = "0123456789abcdef0123456789abcdef"

    async def scenario():
        alice = bootstrap_node_identity("alice-attach")
        bob = bootstrap_node_identity("bob-attach")
        seen_tokens: list[str | None] = []

        async def fake_relay_party(reader, writer):
            seen_tokens.append(decode_bridge_attach_record(await read_realtime_record(reader)))
            await establish_noise_xx_responder(reader, writer, bob)
            await asyncio.sleep(0.2)
            writer.close()

        relay = await asyncio.start_server(fake_relay_party, "127.0.0.1", 0)
        relay_port = relay.sockets[0].getsockname()[1]
        registry = LinkRealtimeSessionRegistry(own_fingerprint=alice.fingerprint)
        try:
            async with _ConnectProxy({"relay.example:8862": ("127.0.0.1", relay_port)}) as proxy:
                monkeypatch.setenv("HTTP_PROXY", proxy.url)
                session = await attach_relayed_session(
                    "relay.example", 8862, alice, attach_token=token, role="initiator",
                    expected_fingerprint=bob.fingerprint, on_frame=_no_frames, registry=registry,
                )
                assert session.remote_fingerprint == bob.fingerprint
                await session.close(reason="test_done")
        finally:
            relay.close()
            await relay.wait_closed()
        return seen_tokens

    assert asyncio.run(scenario()) == [token]
    assert REALTIME_PROXY_STATUS.outcome == "tunnel open"


def test_a_raw_leg_with_no_handshake_records_success_on_opening(monkeypatch):
    """The relay's upstream leg (#270) never runs Noise itself."""

    async def scenario():
        echo = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = echo.sockets[0].getsockname()[1]
        try:
            async with _ConnectProxy({"up.example:8862": ("127.0.0.1", port)}) as proxy:
                monkeypatch.setenv("HTTP_PROXY", proxy.url)
                connection = await open_realtime_connection("up.example", 8862, handshake_follows=False)
                connection.writer.close()
        finally:
            echo.close()
            await echo.wait_closed()

    asyncio.run(scenario())
    assert (REALTIME_PROXY_STATUS.outcome, REALTIME_PROXY_STATUS.ok) == ("tunnel open", True)


def test_every_real_time_socket_in_netbbs_link_is_opened_by_the_helper():
    """Ratchet: a fourth `open_connection` site would reopen the proxy gap."""
    link = pathlib.Path(realtime_proxy.__file__).parent
    offenders = [
        path.name for path in link.glob("*.py")
        if path.name != "realtime_proxy.py" and "open_connection(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.parametrize("url", ["https://carrier:s3cret@proxy.example:3128", "http://carrier:s3cret@proxy.example:99999"])
def test_a_proxy_configuration_error_never_carries_the_credentials(monkeypatch, caplog, url):
    """Callers log dial failures verbatim; a misconfigured proxy URL must not
    put its password in the log -- and the status screen must say why."""
    monkeypatch.setenv("HTTP_PROXY", url)
    with caplog.at_level(logging.INFO):
        with pytest.raises(RealtimeProxyError) as raised:
            proxy_for("bob.example")

    assert "s3cret" not in str(raised.value) and "carrier" not in str(raised.value)
    assert all("s3cret" not in record.getMessage() for record in caplog.records)
    assert REALTIME_PROXY_STATUS.ok is False
    assert "proxy.example" in REALTIME_PROXY_STATUS.proxy and "s3cret" not in REALTIME_PROXY_STATUS.proxy


@pytest.mark.parametrize("url", ["http://[bad", "http://user:secret@"])
def test_a_malformed_proxy_url_does_not_break_the_status_line_or_leak(monkeypatch, url):
    monkeypatch.setenv("HTTP_PROXY", url)
    text, ok = realtime_proxy.describe_proxy_status()
    assert "secret" not in text and ok is None


def test_basic_credentials_are_encoded_the_way_aiohttp_encodes_them():
    """Latin-1, aiohttp's default, so both halves of Link send the same bytes."""
    assert realtime_proxy._basic("carrier", "sécret") == "Basic " + base64.b64encode(
        "carrier:sécret".encode("latin-1")
    ).decode("ascii")


def test_an_internationalized_host_is_dialled_by_its_a_label():
    assert validate_authority("bbs.münchen.example", 8862) == ("bbs.xn--mnchen-3ya.example", 8862)
