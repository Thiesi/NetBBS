"""
Telnet transport (RFC 854 basics), NAWS (RFC 1073) window-size
negotiation, and server-driven character-mode input.

Originally this module stayed in the client's default line mode (client
handles its own local echo/backspace, we just read complete CRLF-
terminated lines) — deliberately deferred, documented as a scoping
decision, not an oversight. That deferral was reversed after real testing
showed client-side line editing behaving inconsistently across clients:
Backspace not working, and Enter's CR byte showing up literally as `^M`
instead of a proper line break. Both are symptoms of relying on each
client's own local line-editing implementation, which varies. The fix is
what's implemented here: the server takes over completely — every
keystroke arrives immediately, and we handle echo, Backspace/Delete, and
Enter detection ourselves, uniformly, for the whole session.

Explicitly out of scope for this pass: full cursor-addressable line
editing (arrow keys, Home/End, mid-line insertion). Escape sequences for
keys we don't support are consumed and discarded as a complete unit
(never leaking raw escape bytes into the input buffer), not implemented.
That's meaningfully more complex — arguably the actual "fullscreen
editor" territory — and not what was needed to fix the Backspace/^M
problem.

Terminal width/height (via NAWS) is the one piece of the rendering
framework that lives here rather than in `netbbs.rendering`, since
detecting it is inherently transport-specific (Telnet: NAWS; SSH: its own
PTY window-size channel request; a future web terminal: JS reporting the
xterm.js viewport) — see `netbbs.net.session.Session` for the
transport-agnostic `terminal_width`/`terminal_height` attributes every
transport populates however it can.

Character-mode line/key reading itself (backspace handling, UTF-8
continuation bytes, escape-sequence discarding, the CR/LF dance) moved to
`netbbs.net.char_input` once `netbbs.net.ssh` needed the exact same
logic against a completely different byte source — see that module's
docstring. `TelnetSession` supplies raw bytes via `read_byte`/
`read_byte_with_timeout` below (satisfying `char_input.ByteSource`); the
actual line/key-building logic isn't duplicated here anymore.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from netbbs.net import char_input
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

# Subnegotiations are control messages, not bulk data. Bound both their
# decoded body size and total completion time so a pre-authentication client
# cannot retain a session forever or grow an unbounded bytearray.
_MAX_SUBNEGOTIATION_BODY = 1024
_SUBNEGOTIATION_TIMEOUT = 1.0


class TelnetSession(Session):
    """A single Telnet client connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer_address: str | None = None,
    ):
        self._reader = reader
        self._writer = writer
        # Conservative defaults (also the Session base class defaults);
        # updated in place by _handle_subnegotiation if/when the client
        # actually reports its real size via NAWS. A client that doesn't
        # support NAWS at all — or resizes later without sending an
        # updated subnegotiation — simply keeps these values.
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = peer_address

    async def negotiate_initial_options(self) -> None:
        """
        Ask the client to suppress "go ahead" turn-taking signaling,
        take over echoing ourselves (character mode — see module
        docstring), and request window-size reporting (NAWS).

        All fire-and-forget — we don't wait for or require a response.
        WILL ECHO is now sent once, persistently, for the whole session,
        rather than toggled per-read around password prompts the way an
        earlier version of this module did: once the client has stopped
        doing its own local echo, password masking becomes purely a
        local rendering choice (echo `*` instead of the real character —
        see `read_line`), not something that needs further protocol-level
        negotiation.

        For NAWS specifically: the client's WILL/WONT reply and (if
        supported) its actual width/height subnegotiation will naturally
        be consumed and processed the next time we call `read_line()`
        (i.e., when prompting for a username) — see
        `_handle_subnegotiation`. This deliberately avoids a separate
        blocking wait-for-negotiation-response step, which would need to
        distinguish "the client's negotiation reply" from "the client
        already sending real data" with no reliable way to un-read a byte
        from an `asyncio.StreamReader` if that distinction is guessed
        wrong. The cost: the very first thing we send (the welcome
        banner) is always written before we know the real terminal
        width, using the 80-column default — accepted, since the banner
        is short, static text that doesn't meaningfully benefit from
        reflow, unlike board post bodies shown well after login, by which
        point NAWS negotiation has always already resolved.
        """
        self._writer.write(bytes([IAC, WILL, SUPPRESS_GO_AHEAD]))
        self._writer.write(bytes([IAC, WILL, ECHO]))
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
        data = normalized.encode("utf-8", errors="replace")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise SessionClosedError("client disconnected during write") from exc

    async def write_raw(self, data: bytes) -> None:
        # Unlike write(), this data isn't guaranteed UTF-8 — it's
        # arbitrary bytes (ZMODEM framing, raw file content), so 0xFF
        # (IAC) really can appear and must be doubled per RFC 854's
        # escaping rule, the same way TelnetSession.read_byte already
        # un-doubles it on the way in.
        escaped = data.replace(bytes([IAC]), bytes([IAC, IAC]))
        try:
            self._writer.write(escaped)
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise SessionClosedError("client disconnected during write") from exc

    async def read_line(self, echo: bool = True, history: char_input.InputHistory | None = None) -> str:
        """
        Read one line of input, character by character, echoing (or
        masking, if `echo=False`) each character ourselves as it
        arrives — see the module docstring for why this replaced relying
        on the client's own local line editing. Actual character-by-
        character logic, including cursor-addressable editing and
        `history` recall (design doc round 47/Track 5f), lives in
        `netbbs.net.char_input`, shared with SSH; this method just
        supplies the byte source.
        """
        return await char_input.read_line(self, self.write, echo, history)

    async def read_key(self, echo: bool = True) -> str:
        """
        Read a single character and return immediately — see the
        `Session.read_key` docstring for the intended use (menu hotkeys,
        not free-text input). See `read_line` re: where the actual logic
        now lives.
        """
        return await char_input.read_key(self, self.write, echo)

    async def close(self) -> None:
        if not self._writer.is_closing():
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    # -- char_input.ByteSource ------------------------------------------

    async def read_byte(self) -> int | None:
        """
        Read and return the next actual DATA byte from the client, or
        `None` if what was read was purely a Telnet negotiation action
        (option negotiation, NAWS subnegotiation, etc.) with no data
        significance — callers should just loop and call this again.

        Centralizes all IAC/negotiation handling in one place, used by
        every higher-level read in this class (via
        `netbbs.net.char_input`).
        """
        try:
            b = (await self._reader.readexactly(1))[0]
        except asyncio.IncompleteReadError as exc:
            raise SessionClosedError("client disconnected during read") from exc

        if b == IAC:
            try:
                next_byte = (await self._reader.readexactly(1))[0]
            except asyncio.IncompleteReadError as exc:
                raise SessionClosedError("client disconnected during read") from exc
            if next_byte == IAC:
                # Client sent an escaped literal 0xFF as actual data
                # (the RFC 854 escaping rule).
                return 0xFF
            if next_byte in (WILL, WONT, DO, DONT):
                try:
                    await self._reader.readexactly(1)  # option byte, ignored
                except asyncio.IncompleteReadError as exc:
                    raise SessionClosedError("client disconnected during read") from exc
                return None
            if next_byte == SB:
                await self._handle_subnegotiation()
                return None
            # Other bare commands (NOP, AYT, GA, etc.) carry no further
            # bytes — nothing more to consume.
            return None

        return b

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        """
        Peek a single raw byte within `timeout` seconds, or `None` if
        nothing arrives or the connection closes — used by
        `netbbs.net.char_input` for bounded lookahead (the LF half of a
        CRLF pair; the rest of an escape sequence). Deliberately doesn't
        go through the IAC/negotiation interpretation `read_byte` does —
        this is a narrow, bounded peek, not a full protocol read; a
        pre-existing limitation carried over unchanged from before this
        module's character-mode logic moved to `char_input`.
        """
        try:
            peek = await asyncio.wait_for(self._reader.read(1), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return peek[0] if peek else None

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
        must arrive doubled (IAC IAC) same as any other data byte.
        """
        try:
            option, body = await asyncio.wait_for(
                self._read_subnegotiation(), timeout=_SUBNEGOTIATION_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            raise SessionClosedError("Telnet subnegotiation timed out") from exc
        except asyncio.IncompleteReadError as exc:
            raise SessionClosedError("client disconnected during subnegotiation") from exc

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

    async def _read_subnegotiation(self) -> tuple[int, bytes]:
        option = (await self._reader.readexactly(1))[0]
        body = await self._read_subnegotiation_body()
        return option, body

    async def _read_subnegotiation_body(self) -> bytes:
        """
        Read and return the raw, un-escaped body of a subnegotiation,
        stopping at (and consuming) the terminating IAC SE.
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
                else:
                    # IAC followed by something else inside a
                    # subnegotiation is malformed. Discard the command and
                    # continue, but still enforce the same decoded-body cap.
                    continue
            else:
                body.append(b)

            if len(body) > _MAX_SUBNEGOTIATION_BODY:
                raise SessionClosedError("Telnet subnegotiation body is too large")


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
        peer_address = peer[0] if peer else None
        session = TelnetSession(reader, writer, peer_address)
        try:
            await session.negotiate_initial_options()
            await self._session_handler(session)
        except SessionClosedError:
            pass  # client disconnected mid-session — expected, not an error
        except Exception:
            _logger.exception("unhandled error in session handler for %s", peer)
        finally:
            await session.close()
