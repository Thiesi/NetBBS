"""
Calling a QWKnet hub (issue #1481, design doc §6.11).

A QWKnet node logs into its hub as a user whose name is the node's QWK ID,
uploads its reply packet (named for the hub: `VERT.REP` for Vertrauen) and
downloads the hub's packet (`VERT.QWK`). Synchronet nodes do it with one of
two scripts, and NetBBS speaks both, inventing nothing:

- **FTP** (`qnet-ftp`): log in, `STOR vert.rep`, `RETR vert.qwk`. The
  hub's FTP server builds the packet when it is asked for and counts it sent
  once the transfer completes. Passive mode only: an active-mode data
  connection would have the hub connect back to this node, and is never
  used. The data connection goes to the control connection's own address
  whatever the server's PASV reply names (`ftplib`'s
  `trust_server_pasv_ipv4_address`, off), so a hub can't point this node at
  a third host. **FTPS** is the same after `AUTH TLS`, with the data
  connection protected too and the certificate checked against the
  system's authorities.
- **HTTP/HTTPS** (`qnet-http`, served by Synchronet's `qwk.ssjs`): with Basic
  authentication, `POST /qwk.ssjs` with the reply packet as the body (200
  taken), `GET /qwk.ssjs` for the packet (200 with it, 204 for nothing
  waiting), then `POST /qwk.ssjs?received=<bytes>` once it has been stored
  (204), which is what makes the hub count it sent. Redirects are not
  followed: the password goes to the host the SysOp named or nowhere.

Both are bounded: the packet downloaded stops at
`netbbs.qwk.packet.MAX_PACKET_ARCHIVE_BYTES`, and every network operation
has a timeout. A BinkP path between two NetBBS systems was considered and
left out: HTTPS already connects them, and a hub's FTP or HTTP is what every
existing QWKnet node speaks.
"""

from __future__ import annotations

import asyncio
import base64
import ftplib
import io
import socket
import ssl
from dataclasses import dataclass
from typing import Awaitable, Callable

from netbbs.qwk.packet import MAX_PACKET_ARCHIVE_BYTES
from netbbs.qwknet.networks import DEFAULT_PORTS, TRANSPORT_FTP, TRANSPORT_FTPS, TRANSPORT_HTTP, QwknetNetwork

FTP_TIMEOUT = 60.0  # seconds, per network operation
HTTP_TIMEOUT = 15 * 60.0  # a whole request, a large packet on a slow line included
HTTP_READ_TIMEOUT = 120.0
_CHUNK = 64 * 1024
ENDPOINT = "/qwk.ssjs"


class TransportError(Exception):
    """A call that failed, with a reason the SysOp can act on."""


@dataclass
class CallOutcome:
    uploaded: bool = False  # the hub took the reply packet
    received: int = 0  # bytes of packet received and tossed
    confirmed: bool = False  # the hub was told it arrived (HTTP)
    toss_summary: str = ""


Toss = Callable[[bytes], Awaitable[str]]


async def call_hub(network: QwknetNetwork, rep: bytes | None, toss: Toss, *, on_uploaded=None) -> CallOutcome:
    """Upload `rep` (when given) and fetch the hub's packet, handing it to
    `toss` before the hub is told it arrived. `on_uploaded()` is awaited as
    soon as the hub has taken the reply packet, so a later failure never
    sends it twice. Raises `TransportError`."""
    if network.transport in (TRANSPORT_FTP, TRANSPORT_FTPS):
        return await _call_ftp(network, rep, toss, on_uploaded)
    return await _call_http(network, rep, toss, on_uploaded)


# --- HTTP / HTTPS (qnet-http) ----------------------------------------------------------


def hub_url(network: QwknetNetwork) -> str:
    scheme = "http" if network.transport == TRANSPORT_HTTP else "https"
    host = network.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = network.effective_port
    authority = host if port == DEFAULT_PORTS[network.transport] else f"{host}:{port}"
    return f"{scheme}://{authority}{ENDPOINT}"


async def _call_http(network: QwknetNetwork, rep: bytes | None, toss: Toss, on_uploaded) -> CallOutcome:
    try:
        import aiohttp
    except ImportError:
        raise TransportError("HTTP needs aiohttp (pip install netbbs[web])") from None
    outcome = CallOutcome()
    url = hub_url(network)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT, sock_read=HTTP_READ_TIMEOUT, sock_connect=30)
    credentials = base64.b64encode(f"{network.our_id}:{network.password}".encode("utf-8")).decode("ascii")
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={"Authorization": f"Basic {credentials}"}) as client:
            if rep:
                async with client.post(url, data=rep, allow_redirects=False,
                                       headers={"Content-Type": "application/octet-stream"}) as response:
                    if response.status != 200:
                        raise TransportError(_http_refusal("upload", response.status, await _short(response)))
                outcome.uploaded = True
                if on_uploaded is not None:
                    await on_uploaded()
            async with client.get(url, allow_redirects=False) as response:
                if response.status == 204:
                    return outcome
                if response.status != 200:
                    raise TransportError(_http_refusal("download", response.status, await _short(response)))
                data = await _read_bounded(response)
            outcome.received = len(data)
            outcome.toss_summary = await toss(data)
            async with client.post(url, params={"received": str(len(data))}, data=b"",
                                   allow_redirects=False) as response:
                outcome.confirmed = response.status == 204
                if not outcome.confirmed:
                    raise TransportError(
                        _http_refusal("confirm the download of", response.status, await _short(response))
                        + "; it was tossed, and the hub will send it again (duplicates are dropped)"
                    )
    except aiohttp.ClientError as exc:
        raise TransportError(f"HTTP to {network.host}: {exc}") from exc
    except asyncio.TimeoutError:
        raise TransportError(f"HTTP to {network.host}: timed out") from None
    return outcome


def _http_refusal(stage: str, status: int, text: str) -> str:
    if status == 401 or status == 403:
        return f"the hub refused the login ({status}): check this node's QWK ID and password"
    if status == 409:
        return "the hub still has an earlier reply packet of ours to process (409); it is sent again next call"
    return f"the hub answered {status} to the {stage} packet" + (f": {text}" if text else "")


async def _short(response) -> str:
    try:
        data = await response.content.read(300)
    except Exception:  # noqa: BLE001 -- only for the message
        return ""
    return "".join(c for c in data.decode("utf-8", errors="replace") if c.isprintable())[:200]


async def _read_bounded(response) -> bytes:
    declared = response.content_length
    if declared is not None and declared > MAX_PACKET_ARCHIVE_BYTES:
        raise TransportError(f"the hub's packet is {declared} bytes, more than {MAX_PACKET_ARCHIVE_BYTES}")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(_CHUNK):
        total += len(chunk)
        if total > MAX_PACKET_ARCHIVE_BYTES:
            raise TransportError(f"the hub's packet is larger than {MAX_PACKET_ARCHIVE_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


# --- FTP / FTPS (qnet-ftp) --------------------------------------------------------------


class _FtpCall:
    """One FTP session, run on a worker thread. `abort()` closes its sockets
    from the event loop, so a cancelled call stops at once instead of
    holding a thread until a timeout."""

    def __init__(self, network: QwknetNetwork):
        self.network = network
        self.ftp: ftplib.FTP | None = None
        self.data_conn: socket.socket | None = None

    def connect(self) -> None:
        network = self.network
        if network.transport == TRANSPORT_FTPS:
            ftp: ftplib.FTP = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=FTP_TIMEOUT)
        else:
            ftp = ftplib.FTP(timeout=FTP_TIMEOUT)
        # The data connection goes where the control connection goes, never
        # where a PASV reply says (ftplib's default, stated here because it
        # is the bounce protection).
        ftp.trust_server_pasv_ipv4_address = False
        self.ftp = ftp
        try:
            ftp.connect(network.host, network.effective_port)
            ftp.login(network.our_id, network.password)
        except ftplib.error_perm as exc:
            raise TransportError(f"the hub refused the login: {_reply(exc)}") from exc
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        ftp.set_pasv(True)

    def upload(self, rep: bytes) -> None:
        assert self.ftp is not None
        try:
            self.ftp.storbinary(f"STOR {self.network.hub_id.lower()}.rep", io.BytesIO(rep))
        except ftplib.error_perm as exc:
            raise TransportError(f"the hub refused the reply packet: {_reply(exc)}") from exc

    def download(self) -> bytes | None:
        assert self.ftp is not None
        ftp = self.ftp
        ftp.voidcmd("TYPE I")  # a packet is a ZIP: binary, never ASCII mode
        try:
            conn = ftp.transfercmd(f"RETR {self.network.hub_id.lower()}.qwk")
        except ftplib.error_perm as exc:
            if str(exc).startswith("550"):
                return None  # nothing waiting
            raise TransportError(f"the hub refused the download: {_reply(exc)}") from exc
        self.data_conn = conn
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = conn.recv(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PACKET_ARCHIVE_BYTES:
                    raise TransportError(f"the hub's packet is larger than {MAX_PACKET_ARCHIVE_BYTES} bytes")
                chunks.append(chunk)
            if isinstance(conn, ssl.SSLSocket):
                conn.unwrap()
        finally:
            conn.close()
            self.data_conn = None
        ftp.voidresp()
        data = b"".join(chunks)
        return data or None

    def close(self) -> None:
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except (ftplib.Error, OSError, EOFError):
                self.ftp.close()

    def abort(self) -> None:
        for closer in (self.data_conn, self.ftp):
            if closer is not None:
                try:
                    closer.close()
                except OSError:
                    pass


def _reply(exc: Exception) -> str:
    return "".join(c for c in str(exc) if c.isprintable())[:200]


async def _in_thread(call: _FtpCall, function, *args):
    task = asyncio.ensure_future(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        call.abort()
        await asyncio.gather(task, return_exceptions=True)
        raise


async def _call_ftp(network: QwknetNetwork, rep: bytes | None, toss: Toss, on_uploaded) -> CallOutcome:
    outcome = CallOutcome()
    call = _FtpCall(network)
    try:
        try:
            await _in_thread(call, call.connect)
            if rep:
                await _in_thread(call, call.upload, rep)
                outcome.uploaded = True
                if on_uploaded is not None:
                    await on_uploaded()
            data = await _in_thread(call, call.download)
        except (ftplib.Error, OSError, EOFError, ssl.SSLError) as exc:
            raise TransportError(f"FTP to {network.host}: {_reply(exc) or type(exc).__name__}") from exc
        finally:
            await asyncio.gather(asyncio.to_thread(call.close), return_exceptions=True)
    except asyncio.CancelledError:
        call.abort()
        raise
    if data:
        outcome.received = len(data)
        outcome.toss_summary = await toss(data)
        outcome.confirmed = True  # the hub's FTP server counts a completed RETR as received
    return outcome
