"""
Minimal Telnet transport (RFC 854 basics only).

Deliberately stays in the client's default line mode — the client's own
OS-level terminal driver handles local echo and local line editing
(backspace, etc.), and we just read complete CRLF/LF-terminated lines.
This is a scoping decision, not an oversight: character-at-a-time mode
(needed for the fullscreen editor and any per-keystroke TUI screens) is
explicitly part of the hybrid ANSI/TUI rendering framework, a separate,
not-yet-built piece — this module only needs to prove the connectivity
layer itself works.

Same reasoning applies to window-size negotiation (NAWS): knowing a
client's terminal dimensions matters for the "not horrible above 40x24"
rendering requirement, but that's the rendering framework's job to
request and use, not bare connectivity's. Negotiation sequences we don't
actively use (NAWS included) are consumed and silently ignored here
rather than causing a hang or a parse error.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from netbbs.net.session import Session, SessionClosedError

# Telnet protocol constants (RFC 854).
IAC = 0xFF  # "Interpret As Command" — introduces every negotiation sequence
WILL = 0xFB
WONT = 0xFC
DO = 0xFD
DONT = 0xFE
SB = 0xFA  # begin subnegotiation
SE = 0xF0  # end subnegotiation
ECHO = 0x01
SUPPRESS_GO_AHEAD = 0x03

_logger = logging.getLogger(__name__)


class TelnetSession(Session):
    """A single Telnet client connection."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    async def negotiate_initial_options(self) -> None:
        """
        Ask the client to suppress "go ahead" turn-taking signaling.

        Universally supported by every modern client; we're operating in
        full-duplex line mode, not the half-duplex "go ahead" model
        Telnet originally assumed. We don't wait for or require the
        client's response — if an ancient client ignores this, nothing
        else here depends on it having taken effect.
        """
        self._writer.write(bytes([IAC, WILL, SUPPRESS_GO_AHEAD]))
        await self._writer.drain()

    async def write(self, text: str) -> None:
        # No IAC-escaping needed here: text is UTF-8 encoded, and byte
        # value 0xFF (== IAC) never appears in valid UTF-8 output — bytes
        # 0xF5-0xFF are unused by the UTF-8 encoding scheme entirely.
        # (Worth stating explicitly rather than leaving as an unexplained
        # absence — an earlier draft of this method escaped IAC
        # defensively, which turned out to be dead code once traced
        # through: it could never actually trigger.)
        data = text.encode("utf-8", errors="replace")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise SessionClosedError("client disconnected during write") from exc

    async def read_line(self, echo: bool = True) -> str:
        if not echo:
            self._writer.write(bytes([IAC, WILL, ECHO]))
            await self._writer.drain()
        try:
            try:
                line_bytes = await self._read_raw_line()
            except asyncio.IncompleteReadError as exc:
                raise SessionClosedError("client disconnected during read") from exc
        finally:
            if not echo:
                self._writer.write(bytes([IAC, WONT, ECHO]))
                await self._writer.drain()
        return line_bytes.decode("utf-8", errors="replace")

    async def close(self) -> None:
        if not self._writer.is_closing():
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    # -- byte-level reading -------------------------------------------------

    async def _read_raw_line(self) -> bytes:
        line = bytearray()
        while True:
            b = (await self._reader.readexactly(1))[0]

            if b == IAC:
                next_byte = (await self._reader.readexactly(1))[0]
                if next_byte == IAC:
                    # Client sent an escaped literal 0xFF as actual data
                    # (the RFC 854 escaping rule, mirroring what write()
                    # would do if it ever needed to — see the note there
                    # on why our own output never needs it).
                    line.append(0xFF)
                    continue
                if next_byte in (WILL, WONT, DO, DONT):
                    await self._reader.readexactly(1)  # option byte, ignored
                    continue
                if next_byte == SB:
                    await self._consume_subnegotiation()
                    continue
                # Other bare commands (NOP, AYT, GA, etc.) carry no
                # further bytes — nothing more to consume.
                continue

            if b == 0x0D:  # CR
                # Standard Telnet line ending is CRLF; a following NUL is
                # also an RFC 854-permitted variant. Consume either if
                # present.
                peek = await self._reader.read(1)
                if peek and peek[0] not in (0x0A, 0x00):
                    # A real client sending something other than LF/NUL
                    # immediately after a bare CR would be very unusual.
                    # asyncio.StreamReader has no "unread" operation, so
                    # the byte is lost from this line rather than
                    # mis-attributed to it — an accepted, narrow edge
                    # case rather than a silent correctness bug.
                    pass
                break

            if b == 0x0A:  # bare LF also terminates a line
                break

            line.append(b)

        return bytes(line)

    async def _consume_subnegotiation(self) -> None:
        """Consume bytes up to and including the terminating IAC SE."""
        while True:
            b = (await self._reader.readexactly(1))[0]
            if b == IAC:
                nxt = (await self._reader.readexactly(1))[0]
                if nxt == SE:
                    return


SessionHandler = Callable[[Session], Awaitable[None]]


class TelnetServer:
    """
    Asyncio TCP server producing `TelnetSession` objects and handing each
    to a caller-supplied `session_handler` coroutine.

    Deliberately knows nothing about logins, menus, or boards — the
    handler callback is where all of that lives (see
    `netbbs.net.login_flow` for the current Phase 1 handler). Keeps this
    module a pure transport concern, matching the modular-package
    decision.
    """

    def __init__(self, host: str, port: int, session_handler: SessionHandler):
        self._host = host
        self._port = port
        self._session_handler = session_handler
        self._server: asyncio.base_events.Server | None = None

    @property
    def port(self) -> int:
        """
        Actual bound port.

        Useful when constructed with `port=0` to let the OS assign a free
        port — the pattern the test suite uses to avoid hardcoding (and
        potentially colliding on) a fixed port number.
        """
        if self._server is None:
            raise RuntimeError("server has not been started yet")
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_connection, self._host, self._port)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        session = TelnetSession(reader, writer)
        try:
            await session.negotiate_initial_options()
            await self._session_handler(session)
        except SessionClosedError:
            pass  # client disconnected mid-session — expected, not an error
        except Exception:
            _logger.exception("unhandled error in session handler for %s", peer)
        finally:
            await session.close()
