"""
Minimal Telnet transport (RFC 854 basics), plus NAWS (RFC 1073) window-
size negotiation.

Deliberately stays in the client's default line mode — the client's own
OS-level terminal driver handles local echo and local line editing
(backspace, etc.), and we just read complete CRLF/LF-terminated lines.
This is a scoping decision, not an oversight: character-at-a-time mode
(needed for the fullscreen editor and any per-keystroke TUI screens) is
the "TUI half" of the hybrid rendering framework, deliberately deferred
until a real screen needs it (see design doc phasing sign-off notes) —
this module still only needs to prove the connectivity/rendering-support
layer works, not build a TUI.

Terminal width/height (via NAWS) is the one piece of the rendering
framework that lives here rather than in `netbbs.rendering`, since
detecting it is inherently transport-specific (Telnet: NAWS; SSH: its own
PTY window-size channel request; a future web terminal: JS reporting the
xterm.js viewport) — see `netbbs.net.session.Session` for the
transport-agnostic `terminal_width`/`terminal_height` attributes every
transport populates however it can.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from netbbs.net.session import Session, SessionClosedError

# Telnet protocol constants (RFC 854, plus NAWS from RFC 1073).
IAC = 0xFF  # "Interpret As Command" — introduces every negotiation sequence
WILL = 0xFB
WONT = 0xFC
DO = 0xFD
DONT = 0xFE
SB = 0xFA  # begin subnegotiation
SE = 0xF0  # end subnegotiation
ECHO = 0x01
SUPPRESS_GO_AHEAD = 0x03
NAWS = 0x1F  # Negotiate About Window Size (RFC 1073)

_logger = logging.getLogger(__name__)


class TelnetSession(Session):
    """A single Telnet client connection."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        # Conservative defaults (also the Session base class defaults);
        # updated in place by _handle_subnegotiation if/when the client
        # actually reports its real size via NAWS. A client that doesn't
        # support NAWS at all — or resizes later without sending an
        # updated subnegotiation — simply keeps these values.
        self.terminal_width = 80
        self.terminal_height = 24

    async def negotiate_initial_options(self) -> None:
        """
        Ask the client to suppress "go ahead" turn-taking signaling, and
        request window-size reporting (NAWS).

        Both fire-and-forget — we don't wait for or require a response.
        For NAWS specifically: the client's WILL/WONT reply and (if
        supported) its actual width/height subnegotiation will naturally
        be consumed and processed by `_read_raw_line`'s existing IAC-
        handling the next time we call `read_line()` (i.e., when
        prompting for a username) — see `_handle_subnegotiation`. This
        deliberately avoids a separate blocking wait-for-negotiation-
        response step, which would need to distinguish "the client's
        negotiation reply" from "the client already sending real data"
        with no reliable way to un-read a byte from an
        `asyncio.StreamReader` if that distinction is guessed wrong. The
        cost of this simpler approach: the very first thing we send (the
        welcome banner) is always written before we know the real
        terminal width, using the 80-column default. Accepted — the
        banner is short, static text that doesn't meaningfully benefit
        from reflow, unlike board post bodies shown well after login, by
        which point NAWS negotiation has always already resolved.
        """
        self._writer.write(bytes([IAC, WILL, SUPPRESS_GO_AHEAD]))
        self._writer.write(bytes([IAC, DO, NAWS]))
        await self._writer.drain()

    async def write(self, text: str) -> None:
        # Normalize all line endings to CRLF (RFC 854's correct Telnet
        # line terminator) here, at the transport boundary — not the
        # caller's job. Rendering utilities like netbbs.rendering.reflow
        # correctly use plain '\n' internally (they're transport-
        # agnostic; hardcoding '\r\n' into a text-wrapping utility would
        # be a layering mistake), but that means multi-line text reaching
        # this method may only have bare '\n' between its internal lines.
        # Session.write_line() only appends '\r\n' once, at the very end
        # — without this normalization, every internal line break in
        # something like a reflowed post body would go out as a bare LF,
        # which most modern terminals tolerate (they auto-CR on LF as a
        # convenience) but which isn't actually correct Telnet, and could
        # misrender on a stricter/older client. Idempotent regardless of
        # whether the input already used '\r\n', bare '\n', or a mix.
        normalized = text.replace("\r\n", "\n").replace("\n", "\r\n")

        # No IAC-escaping needed here: text is UTF-8 encoded, and byte
        # value 0xFF (== IAC) never appears in valid UTF-8 output — bytes
        # 0xF5-0xFF are unused by the UTF-8 encoding scheme entirely.
        # (Worth stating explicitly rather than leaving as an unexplained
        # absence — an earlier draft of this method escaped IAC
        # defensively, which turned out to be dead code once traced
        # through: it could never actually trigger.)
        data = normalized.encode("utf-8", errors="replace")
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
                    await self._handle_subnegotiation()
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

    async def _handle_subnegotiation(self) -> None:
        """
        Read a full subnegotiation (option byte + body, up to the
        terminating IAC SE) and act on it if it's one we understand —
        currently just NAWS (window size).

        The body is read via `_read_subnegotiation_body`, which correctly
        un-escapes IAC-doubling within the body — this matters
        specifically for NAWS, whose width/height bytes are arbitrary
        16-bit values: a terminal that's, say, 255 columns wide would
        have a literal 0xFF byte in its NAWS payload, which per RFC 854
        must arrive doubled (IAC IAC) same as any other data byte. Naively
        scanning for the first "IAC SE" without un-escaping first would
        misparse that case.
        """
        option = (await self._reader.readexactly(1))[0]
        body = await self._read_subnegotiation_body()

        if option == NAWS and len(body) >= 4:
            width = (body[0] << 8) | body[1]
            height = (body[2] << 8) | body[3]
            # A width/height of 0 is a real value some clients send
            # (meaning "unknown"/"not applicable"), not useful to act on
            # — keep whatever default or previously-known value instead
            # of overwriting a real size with 0.
            if width > 0:
                self.terminal_width = width
            if height > 0:
                self.terminal_height = height

    async def _read_subnegotiation_body(self) -> bytes:
        """
        Read and return the raw, un-escaped body of a subnegotiation,
        stopping at (and consuming) the terminating IAC SE.

        A literal 0xFF byte within the body arrives doubled (IAC IAC) per
        RFC 854 and is un-escaped back to a single 0xFF here — without
        this, a subnegotiation body that happens to contain 0xFF (e.g.
        NAWS reporting a 255-column-wide terminal) could be misparsed.
        """
        body = bytearray()
        while True:
            b = (await self._reader.readexactly(1))[0]
            if b == IAC:
                nxt = (await self._reader.readexactly(1))[0]
                if nxt == SE:
                    return bytes(body)
                if nxt == IAC:
                    body.append(0xFF)
                    continue
                # IAC followed by something else inside a subnegotiation
                # is technically malformed, but a real client shouldn't
                # produce it — rather than raising and killing the whole
                # connection over a protocol oddity, discard the
                # unexpected byte and keep reading the body.
                continue
            body.append(b)


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
