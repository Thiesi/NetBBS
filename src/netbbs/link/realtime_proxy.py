"""
Every real-time Link socket is opened here (design doc §8.10; decisions under
§16, issue #628).

Real-time Link is Noise over raw TCP, which a network that lets traffic out
only through an HTTP proxy cannot carry. When the environment names a proxy
for the target, the socket is a `CONNECT` tunnel through it instead, and the
attach preamble, the Noise handshake and the session run over the tunnel
unchanged: Noise authenticates the remote node end to end, so the proxy learns
the dialled address and nothing inside the session.

The rules, each a decision recorded under issue #628:

- **The same proxy asynchronous Link uses.** A peer's Link endpoint is
  advertised as `http`, so `aiohttp` (under `trust_env=True`) sends that
  peer's boards and mail through `HTTP_PROXY`; the tunnel uses the same, then
  `HTTPS_PROXY`, with `NO_PROXY` honoured, all through the standard library's
  own lookup (`urllib.request.getproxies` / `proxy_bypass`, which is what
  `aiohttp` calls). Loopback targets always go direct, the precedent
  `netbbs.managed_dns.client.outbound_session` set.
- **Only `http://` proxies.** Any other proxy URL that applies fails the dial;
  it is never treated as absent, since a direct dial would bypass the
  operator's egress policy wherever direct traffic happens to work.
- **The tunnel is the only attempt** when a proxy applies.
- **Basic authentication** from the proxy URL's userinfo, else the netrc entry
  for the proxy host -- the sources `aiohttp` reads.
- **The target is validated strictly** before it reaches the proxy: it comes
  from a peer's signed descriptor, whose addresses nothing else validates, and
  a CR or LF in it would inject request lines into the operator's
  authenticated proxy.
- **Bounded, and cleaned up.** One timeout covers connecting to the proxy and
  reading its answer, the answer has a size ceiling, and on every path that
  does not return the stream the proxy connection is closed and awaited.
- **Outcomes are recorded here**, on `REALTIME_PROXY_STATUS`, because the
  anchor connector swallows every dial exception and a caller's "unreachable"
  message deliberately carries no reason. The Link status screen reads it.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from netbbs.net.nodeconfig import is_loopback_host

_logger = logging.getLogger(__name__)

PROXY_TIMEOUT_SECONDS = 10.0
"""Connecting to the proxy and reading its answer, together."""

MAX_PROXY_ANSWER_BYTES = 8192
"""The most a proxy's answer to `CONNECT` may be, status line and headers."""

_HOSTNAME_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


class RealtimeProxyError(ConnectionError):
    """A tunnel could not be opened. A `ConnectionError`, so every caller that
    already tolerates a failed `asyncio.open_connection` tolerates this."""


class RealtimeTargetError(ConnectionError):
    """A peer advertised a real-time address that is not a valid authority."""


@dataclass(frozen=True)
class ProxyEndpoint:
    host: str
    port: int
    authorization: str | None

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class RealtimeConnection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    proxy: ProxyEndpoint | None
    """The proxy the stream tunnels through, or `None` for a direct socket."""


class RealtimeProxyStatus:
    """The last tunnel outcome, for the Link status screen. One per process,
    which is one node. A change of outcome is logged once at WARNING, not on
    every retry of the same failure."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.proxy: str | None = None
        self.outcome: str | None = None
        self.ok: bool = False
        self.at: float | None = None

    def record(self, proxy: ProxyEndpoint, outcome: str, *, ok: bool) -> None:
        changed = (proxy.label, outcome) != (self.proxy, self.outcome)
        self.proxy, self.outcome, self.ok, self.at = proxy.label, outcome, ok, time.time()
        if changed:
            _logger.log(
                logging.INFO if ok else logging.WARNING,
                "real-time Link through proxy %s: %s", proxy.label, outcome,
            )


REALTIME_PROXY_STATUS = RealtimeProxyStatus()


def validate_authority(host: object, port: object) -> tuple[str, int]:
    """`(host, port)` if they form a strict authority: a DNS hostname, an IPv4
    literal or an IPv6 literal, and a port in range. Raises
    `RealtimeTargetError` otherwise."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise RealtimeTargetError(f"invalid real-time port {port!r}")
    if not isinstance(host, str) or not host:
        raise RealtimeTargetError(f"invalid real-time host {host!r}")
    try:
        ipaddress.ip_address(host)
        return host, port
    except ValueError:
        pass
    name = host[:-1] if host.endswith(".") else host
    if len(name) > 253 or not all(_HOSTNAME_LABEL.match(label) for label in name.split(".")):
        raise RealtimeTargetError(f"invalid real-time host {host!r}")
    return host, port


def _connect_authority(host: str, port: int) -> str:
    try:
        if ipaddress.ip_address(host).version == 6:
            return f"[{host}]:{port}"
    except ValueError:
        pass
    return f"{host}:{port}"


def _netrc_authorization(proxy_host: str) -> str | None:
    try:
        from aiohttp.helpers import netrc_from_env

        netrc_obj = netrc_from_env()
        auth = netrc_obj.authenticators(proxy_host) if netrc_obj is not None else None
    except Exception:  # a malformed netrc is aiohttp's to report, not a dial failure
        return None
    if not auth:
        return None
    login, _account, password = auth
    return _basic(login or "", password or "")


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")


def proxy_for(host: str) -> ProxyEndpoint | None:
    """The proxy a tunnel to `host` goes through, or `None` for a direct
    socket. Raises `RealtimeProxyError` for a proxy URL that applies but is
    not usable, rather than falling back to direct."""
    if is_loopback_host(host):
        return None
    proxies = urllib.request.getproxies()
    url = proxies.get("http") or proxies.get("https")
    if not url:
        return None
    if urllib.request.proxy_bypass(host):
        return None
    parts = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    if parts.scheme != "http" or not parts.hostname:
        raise RealtimeProxyError(
            f"proxy {url!r} is not an http:// proxy, which is all real-time Link can tunnel through"
        )
    try:
        port = parts.port or 80
    except ValueError as exc:
        raise RealtimeProxyError(f"proxy {url!r} has an invalid port") from exc
    if parts.username is not None:
        authorization = _basic(
            urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or "")
        )
    else:
        authorization = _netrc_authorization(parts.hostname)
    return ProxyEndpoint(parts.hostname, port, authorization)


async def open_realtime_connection(host: str, port: int, *, handshake_follows: bool = True) -> RealtimeConnection:
    """Open the socket for one real-time connection to `host`/`port`: direct,
    or a `CONNECT` tunnel when a proxy applies.

    With `handshake_follows` (every caller but the relay's raw upstream leg),
    a tunnel's success is recorded by `record_handshake_outcome` once the
    handshake settles, so a proxy that opens tunnels and then breaks what runs
    through them reads as one standing failure rather than alternating."""
    host, port = validate_authority(host, port)
    proxy = proxy_for(host)
    if proxy is None:
        reader, writer = await asyncio.open_connection(host, port)
        return RealtimeConnection(reader, writer, None)
    reader, writer = await _open_tunnel(proxy, host, port, record_success=not handshake_follows)
    return RealtimeConnection(reader, writer, proxy)


async def _open_tunnel(
    proxy: ProxyEndpoint, host: str, port: int, *, record_success: bool
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(PROXY_TIMEOUT_SECONDS):
            try:
                reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
            except OSError as exc:
                raise RealtimeProxyError(f"proxy unreachable ({exc})") from exc
            authority = _connect_authority(host, port)
            request = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
            if proxy.authorization is not None:
                request += f"Proxy-Authorization: {proxy.authorization}\r\n"
            writer.write((request + "\r\n").encode("ascii"))
            await writer.drain()
            answer = await _read_answer(reader)
    except TimeoutError:
        error = RealtimeProxyError(f"no answer from the proxy within {PROXY_TIMEOUT_SECONDS:.0f}s")
        REALTIME_PROXY_STATUS.record(proxy, str(error), ok=False)
        await _close(writer)
        raise error from None
    except RealtimeProxyError as exc:
        REALTIME_PROXY_STATUS.record(proxy, str(exc), ok=False)
        await _close(writer)
        raise
    except BaseException:
        # Cancellation, or anything unexpected: nothing else holds the writer.
        await _close(writer)
        raise
    status, reason = answer
    if not 200 <= status < 300:
        error = RealtimeProxyError(f"tunnel refused: {status} {reason}".rstrip())
        REALTIME_PROXY_STATUS.record(proxy, str(error), ok=False)
        await _close(writer)
        raise error
    if record_success:
        REALTIME_PROXY_STATUS.record(proxy, "tunnel open", ok=True)
    return reader, writer


async def _read_answer(reader: asyncio.StreamReader) -> tuple[int, str]:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError as exc:
        raise RealtimeProxyError("the proxy's answer was too large") from exc
    except asyncio.IncompleteReadError as exc:
        raise RealtimeProxyError("the proxy closed the connection without answering") from exc
    if len(head) > MAX_PROXY_ANSWER_BYTES:
        raise RealtimeProxyError("the proxy's answer was too large")
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
    match = re.match(r"^HTTP/1\.[01] (\d{3})(?: (.*))?$", status_line)
    if match is None:
        raise RealtimeProxyError(f"the proxy's answer was not HTTP: {status_line[:80]!r}")
    return int(match.group(1)), (match.group(2) or "").strip()


async def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # the original failure is what the caller needs to see
        pass


def record_handshake_outcome(connection: RealtimeConnection, exc: BaseException | None) -> None:
    """Settle a tunnel's outcome once the Noise handshake over it has run. A
    tunnel that opened but whose handshake then failed is its own outcome --
    the shape a TLS-inspecting proxy produces -- and never reads as a
    success. A cancellation (a caller's own timeout) says nothing about the
    proxy and is not recorded."""
    if connection.proxy is None or isinstance(exc, asyncio.CancelledError):
        return
    if exc is None:
        REALTIME_PROXY_STATUS.record(connection.proxy, "tunnel open", ok=True)
    else:
        REALTIME_PROXY_STATUS.record(connection.proxy, "tunnel opened, handshake failed", ok=False)


def describe_proxy_status() -> tuple[str, bool | None] | None:
    """The Link status screen's "Live proxy" line: the proxy and the last
    tunnel outcome, or the configured proxy if no live connection has used it
    yet, or `None` when there is no proxy at all. `ok` is `None` for "not
    tried". Never shows credentials."""
    status = REALTIME_PROXY_STATUS
    if status.proxy is not None and status.outcome is not None:
        return f"{status.proxy} -- {status.outcome}", status.ok
    proxies = urllib.request.getproxies()
    url = proxies.get("http") or proxies.get("https")
    if not url:
        return None
    parts = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    try:
        where = f"{parts.hostname}:{parts.port or 80}" if parts.hostname else url
    except ValueError:
        where = parts.hostname or "?"
    return f"{where} -- configured, not used by a live connection yet", None
