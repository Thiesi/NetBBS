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

# Control byte values relevant to character-mode line building.
_CR = 0x0D
_LF = 0x0A
_NUL = 0x00
_BS = 0x08  # Backspace
_DEL = 0x7F  # Delete — many terminals send this for the Backspace key
_ESC = 0x1B

# Bounded waits used when we need to check for a byte that might not be
# coming (a following LF after a lone CR; the rest of an escape
# sequence) — short enough to be imperceptible when the byte does
# arrive (which happens essentially instantly for a real client sending
# a CRLF pair or a real escape sequence in one write), long enough to
# never falsely time out on a real, if slightly slow, connection.
_FOLLOWUP_BYTE_TIMEOUT = 0.05

# Defensive cap on a single line's length. Not a meaningful limit for any
# real use (post subjects/bodies, chat messages, usernames are all far
# shorter), just cheap insurance against a broken or malicious client
# sending unbounded data with no Enter — without this, the line buffer
# would grow without bound. Once hit, further characters are silently
# not appended (but Backspace and Enter still work normally).
_MAX_LINE_LENGTH = 4096

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

    async def read_line(self, echo: bool = True) -> str:
        """
        Read one line of input, character by character, echoing (or
        masking, if `echo=False`) each character ourselves as it
        arrives — see the module docstring for why this replaced relying
        on the client's own local line editing.

        `echo=False` no longer means "no visual feedback at all" the way
        it originally did; it means each typed character is masked with
        `*` instead of shown as typed, matching common modern password-
        field UX (revealing length, not content) rather than the more
        conservative "reveal nothing" alternative.
        """
        line: list[str] = []
        try:
            while True:
                b = await self._read_byte()
                if b is None:
                    continue  # pure negotiation action, no data produced

                if b in (_CR, _LF):
                    if b == _CR:
                        await self._consume_optional_lf_or_nul()
                    break

                if b in (_BS, _DEL):
                    if line:
                        line.pop()
                        await self.write("\b \b")
                    continue

                if b == _ESC:
                    await self._discard_escape_sequence()
                    continue

                if b < 0x20:
                    # Any other control byte (Tab, Ctrl+C, Ctrl+D, etc.)
                    # — not supported in this pass; discard rather than
                    # corrupt the line or echo something meaningless.
                    continue

                if b < 0x80:
                    char = chr(b)
                else:
                    char = await self._read_utf8_continuation(b)
                    if char is None:
                        continue  # malformed/interrupted multi-byte sequence

                if len(line) < _MAX_LINE_LENGTH:
                    line.append(char)
                    await self.write(char if echo else "*")
                # else: silently drop the character but keep reading —
                # Backspace and Enter still work normally past the cap.
        except asyncio.IncompleteReadError as exc:
            raise SessionClosedError("client disconnected during read") from exc

        await self.write("\r\n")
        return "".join(line)

    async def read_key(self, echo: bool = True) -> str:
        """
        Read a single character and return immediately — see the
        `Session.read_key` docstring for the intended use (menu hotkeys,
        not free-text input).

        Control bytes with no meaning as a standalone "key" — Backspace/
        Delete, CR/LF, unsupported escape sequences — are silently
        skipped and reading continues, rather than being returned as a
        key in their own right: there's no line being built here to
        backspace within, and Enter doesn't mean anything special when
        we're already responding to the very next keystroke, immediately.
        """
        try:
            while True:
                b = await self._read_byte()
                if b is None:
                    continue  # pure negotiation action, no data produced

                if b in (_CR, _LF, _BS, _DEL):
                    continue

                if b == _ESC:
                    await self._discard_escape_sequence()
                    continue

                if b < 0x20:
                    continue

                if b < 0x80:
                    char = chr(b)
                else:
                    char = await self._read_utf8_continuation(b)
                    if char is None:
                        continue

                await self.write(char if echo else "*")
                return char
        except asyncio.IncompleteReadError as exc:
            raise SessionClosedError("client disconnected during read") from exc

    async def close(self) -> None:
        if not self._writer.is_closing():
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    # -- byte-level reading -------------------------------------------------

    async def _read_byte(self) -> int | None:
        """
        Read and return the next actual DATA byte from the client, or
        `None` if what was read was purely a Telnet negotiation action
        (option negotiation, NAWS subnegotiation, etc.) with no data
        significance — callers should just loop and call this again.

        Centralizes all IAC/negotiation handling in one place, used by
        every higher-level read in this class.
        """
        b = (await self._reader.readexactly(1))[0]

        if b == IAC:
            next_byte = (await self._reader.readexactly(1))[0]
            if next_byte == IAC:
                # Client sent an escaped literal 0xFF as actual data
                # (the RFC 854 escaping rule).
                return 0xFF
            if next_byte in (WILL, WONT, DO, DONT):
                await self._reader.readexactly(1)  # option byte, ignored
                return None
            if next_byte == SB:
                await self._handle_subnegotiation()
                return None
            # Other bare commands (NOP, AYT, GA, etc.) carry no further
            # bytes — nothing more to consume.
            return None

        return b

    async def _read_utf8_continuation(self, lead_byte: int) -> str | None:
        """
        Given a UTF-8 multi-byte lead byte already read, read the
        appropriate number of continuation bytes (per the UTF-8 encoding
        scheme's lead-byte ranges) and decode the complete character.

        Matters concretely for this project: umlauts and other non-ASCII
        characters are everyday input, not an edge case, and a naive
        byte-at-a-time decode would corrupt every one of them. Returns
        `None` (discarding the partial character) if the sequence is
        malformed or interrupted by a negotiation action rather than
        risking a wrong decode.
        """
        if 0xC2 <= lead_byte <= 0xDF:
            extra = 1
        elif 0xE0 <= lead_byte <= 0xEF:
            extra = 2
        elif 0xF0 <= lead_byte <= 0xF4:
            extra = 3
        else:
            return None  # not a valid UTF-8 lead byte

        raw = bytearray([lead_byte])
        for _ in range(extra):
            cb = await self._read_byte()
            if cb is None or not (0x80 <= cb <= 0xBF):
                return None
            raw.append(cb)

        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    async def _consume_optional_lf_or_nul(self) -> None:
        """
        After a CR, consume a following LF or NUL if present — both are
        valid Telnet line-ending continuations (CRLF or CR-NUL).

        Bounded by a short timeout rather than an unbounded read: a
        client in true character mode may send a lone CR with nothing
        immediately following it, and an earlier version of this method
        (inherited from the original line-mode reader) could have hung
        indefinitely waiting for a byte that wasn't coming. A real CRLF
        pair, sent together in one client-side write, arrives well within
        this window regardless of connection latency, since it's one
        send hitting our receive buffer, not a further round trip.
        """
        try:
            peek = await asyncio.wait_for(self._reader.read(1), timeout=_FOLLOWUP_BYTE_TIMEOUT)
        except asyncio.TimeoutError:
            return
        if peek and peek[0] not in (_LF, _NUL):
            # Not part of the line terminator. asyncio.StreamReader has
            # no "unread" operation, so this byte is lost from this read
            # rather than mis-attributed to it — an accepted, narrow edge
            # case rather than a silent correctness bug.
            pass

    async def _discard_escape_sequence(self) -> None:
        """
        Consume and discard a terminal escape sequence following an ESC
        byte (arrow keys, function keys, Home/End, etc.) as a complete
        unit — not supported in this pass (see module docstring).
        Handles the two common shapes real terminals use for special
        keys:

        - CSI sequences: ESC [ ... <final byte in 0x40-0x7E> (the vast
          majority of arrow/function/navigation keys)
        - SS3 sequences: ESC O <single letter> (some terminals'
          "application cursor key mode" encoding for arrow keys)

        Anything else after a lone ESC (a real Escape keypress with
        nothing following, or a shape we don't recognize) is left alone
        after discarding just the ESC itself, on a short timeout — so we
        can never hang waiting for bytes that aren't coming, the same
        reasoning as `_consume_optional_lf_or_nul`.
        """
        try:
            next_byte = await asyncio.wait_for(self._reader.read(1), timeout=_FOLLOWUP_BYTE_TIMEOUT)
        except asyncio.TimeoutError:
            return
        if not next_byte:
            return
        nb = next_byte[0]

        if nb == 0x5B:  # '[' — CSI sequence
            while True:
                try:
                    b = (
                        await asyncio.wait_for(
                            self._reader.readexactly(1), timeout=_FOLLOWUP_BYTE_TIMEOUT
                        )
                    )[0]
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    return
                if 0x40 <= b <= 0x7E:
                    return  # final byte of the CSI sequence
        elif nb == 0x4F:  # 'O' — SS3 sequence, always exactly one more byte
            try:
                await asyncio.wait_for(
                    self._reader.readexactly(1), timeout=_FOLLOWUP_BYTE_TIMEOUT
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return
        # else: some other/unrecognized shape — just the ESC itself was
        # consumed; nothing more to do.

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
