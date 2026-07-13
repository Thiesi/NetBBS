"""
Web transport (design doc round 22/25): `aiohttp`-based, serving both
the xterm.js terminal page and a websocket endpoint from one process.

Structured JSON wire protocol confirmed in round 22, not raw byte
passthrough — see that round's sign-off note for the full reasoning.
A browser has already resolved the raw-terminal-byte ambiguity
`netbbs.net.char_input` exists to handle (a `keydown`/paste event
already delivers decoded Unicode characters, not a byte stream needing
UTF-8 reconstruction), so `WebSession` implements `read_line`/`read_key`
directly against decoded characters — reusing `char_input`'s
byte-oriented `ByteSource` protocol here would mean forcing something
that was never bytes back into a byte-shaped hole.

Cursor-addressable editing and history recall (design doc round 47/
Track 5f) reuse `char_input.move_cursor`/`redraw_tail`/`InputHistory`
directly, though — those are pure `list[str]`/cursor-integer/`WriteFunc`
manipulation with no dependency on bytes, so only the byte-vs-already-
decoded-character *reading* half stays genuinely separate between the
two transports, same split round 25 already established.

**File transfer is not available over this transport.** Real Zmodem
interop (`netbbs.net.zmodem`) depends on the *terminal client*
auto-detecting and driving the protocol — a property of native terminal
emulators (SyncTERM, lrzsz) that a JS widget running in a browser tab
doesn't have. `WebSession.read_byte`/`write_raw` exist only to satisfy
the `Session` ABC and raise `NotImplementedError` if ever called;
`netbbs.net.file_flow` already handles that gracefully (same as any
other failed transfer) rather than crashing the session.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import WSCloseCode, web

from netbbs.net.char_input import Completer, InputHistory, apply_tab_completion, move_cursor, redraw_tail
from netbbs.net.session import Session, SessionClosedError

_logger = logging.getLogger(__name__)

# netbbs/web/static/, a sibling top-level package rather than living
# under netbbs/net/ (design doc round 22, point 8) — asset files aren't
# a transport-layer concern the way this module's actual code is.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"

_CR = "\r"
_LF = "\n"
_BS = "\x08"
_DEL = "\x7f"
_ESC = "\x1b"
_TAB = "\t"

# Same reasoning as netbbs.net.char_input._MAX_LINE_LENGTH — cheap
# insurance against unbounded input from a broken or malicious client,
# not a meaningful limit for any real use.
_MAX_LINE_LENGTH = 4096

# Bound every buffering layer exposed before authentication. The websocket
# frame limit includes JSON overhead; individual key events and the decoded
# character queue are intentionally smaller. Queue saturation is treated as
# a protocol violation and closes the connection rather than moving the
# unbounded buffer into aiohttp or the application task.
_MAX_WS_MESSAGE_SIZE = 16 * 1024
_MAX_KEY_EVENT_LENGTH = 4096
_MAX_QUEUED_CHARS = 8192

# Recognized escape sequences, mirroring netbbs.net.char_input's
# _CSI_FINAL_TO_KEY/_CSI_TILDE_TO_KEY/_SS3_TO_KEY exactly (same key set,
# same reasoning) but keyed by already-decoded characters rather than
# raw byte values, since that's what a browser's onData event delivers.
_CSI_FINAL_TO_KEY = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END"}
_CSI_TILDE_TO_KEY = {"1": "HOME", "4": "END", "3": "DELETE", "2": "INSERT"}
_SS3_TO_KEY = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END"}


@dataclass(frozen=True)
class _SpecialKey:
    """Distinguishes a recognized escape sequence (e.g. the two literal
    characters "U" and "P" typed by a user) from the *symbolic* key
    "UP" produced by parsing `ESC[A` — both would otherwise be
    indistinguishable plain strings once queued."""

    name: str


def _parse_input_events(data: str) -> list[str | _SpecialKey]:
    """
    Splits a raw `onData` string into plain characters and recognized
    `_SpecialKey`s (arrow keys, Home/End, Delete, Insert — design doc
    round 47/Track 5f), discarding anything else escape-sequence-shaped
    as a complete unit — the same "recognize a few, discard the rest"
    scope `netbbs.net.char_input._read_escape_sequence` documents for
    Telnet/SSH, applied here at the point a whole keystroke event
    arrives rather than via a peek-with-timeout: xterm.js's `onData`
    already delivers a complete escape sequence in one event for a
    single keypress, unlike a raw byte stream where bytes can arrive
    split across separate reads, so there's nothing to peek for.
    """
    out: list[str | _SpecialKey] = []
    i = 0
    while i < len(data):
        if data[i] == _ESC and i + 1 < len(data):
            nxt = data[i + 1]
            if nxt == "[":  # CSI sequence: ESC [ ... <final char 0x40-0x7E>
                j = i + 2
                while j < len(data) and not ("\x40" <= data[j] <= "\x7e"):
                    j += 1
                if j < len(data):
                    params, final = data[i + 2 : j], data[j]
                    key = _CSI_TILDE_TO_KEY.get(params) if final == "~" else (
                        _CSI_FINAL_TO_KEY.get(final) if not params else None
                    )
                    if key is not None:
                        out.append(_SpecialKey(key))
                    i = j + 1
                else:
                    i = len(data)
                continue
            if nxt == "O" and i + 2 < len(data):  # SS3: ESC O <char>
                key = _SS3_TO_KEY.get(data[i + 2])
                if key is not None:
                    out.append(_SpecialKey(key))
                i += 3
                continue
        out.append(data[i])
        i += 1
    return out


class WebSession(Session):
    """A single browser client's terminal session, over a websocket."""

    def __init__(self, ws: web.WebSocketResponse, peer_address: str | None = None):
        self._ws = ws
        self._char_queue: asyncio.Queue[str | _SpecialKey | None] = asyncio.Queue(
            maxsize=_MAX_QUEUED_CHARS
        )
        self._input_error = "client disconnected"
        self.peer_address = peer_address
        self._reader_task = asyncio.create_task(self._read_loop())

    def _signal_input_closed(self, message: str) -> None:
        self._input_error = message
        try:
            self._char_queue.put_nowait(None)
        except asyncio.QueueFull:
            # Guarantee that a blocked reader is woken even when hostile input
            # filled the queue completely. Dropping one queued character is
            # irrelevant because this session is being terminated.
            self._char_queue.get_nowait()
            self._char_queue.put_nowait(None)

    async def _reject_input(self, message: str) -> None:
        self._signal_input_closed(message)
        if not self._ws.closed:
            await self._ws.close(
                code=WSCloseCode.MESSAGE_TOO_BIG,
                message=message.encode("utf-8"),
            )
        raise SessionClosedError(message)

    async def _read_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        event = json.loads(msg.data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(event, dict):
                        await self._handle_event(event)
                elif msg.type in (
                    web.WSMsgType.CLOSE,
                    web.WSMsgType.CLOSING,
                    web.WSMsgType.ERROR,
                ):
                    break
        except SessionClosedError:
            pass
        finally:
            # Sentinel: wakes up any read_line/read_key blocked on the
            # queue so it can raise SessionClosedError, the same role
            # asyncio.IncompleteReadError plays for TelnetSession.
            self._signal_input_closed(self._input_error)

    async def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "key":
            data = event.get("data")
            if isinstance(data, str):
                if len(data) > _MAX_KEY_EVENT_LENGTH:
                    await self._reject_input("web terminal key event is too large")
                for item in _parse_input_events(data):
                    try:
                        self._char_queue.put_nowait(item)
                    except asyncio.QueueFull:
                        await self._reject_input("web terminal input queue is full")
        elif event_type == "resize":
            cols, rows = event.get("cols"), event.get("rows")
            if isinstance(cols, int) and cols > 0:
                self.terminal_width = cols
            if isinstance(rows, int) and rows > 0:
                self.terminal_height = rows
        # Unknown event types are ignored rather than treated as an
        # error — a forward-compatible client sending a message type
        # this version doesn't understand yet shouldn't break the
        # session over it.

    async def _read_item(self) -> str | _SpecialKey:
        """Every queued item, plain character or recognized special
        key alike — used by `read_line`'s cursor-aware path, which
        needs to tell them apart. `_read_char` (below) is the
        char-only view `read_key` and masked reads still want."""
        item = await self._char_queue.get()
        if item is None:
            raise SessionClosedError(self._input_error)
        return item

    async def _read_char(self) -> str:
        """Plain characters only -- a recognized special key has no
        meaning for a masked read or a single-keystroke menu choice
        (same reasoning `netbbs.net.char_input.read_key` documents for
        Telnet/SSH), so it's silently skipped here rather than
        surfaced."""
        while True:
            item = await self._read_item()
            if isinstance(item, str):
                return item

    async def write(self, text: str) -> None:
        # Same CRLF normalization TelnetSession.write/SSHSession.write
        # perform, and the same reasoning: xterm.js is a real terminal
        # emulator, not a browser textarea — it needs an explicit CR to
        # return to column 0, same as any other terminal.
        normalized = text.replace("\r\n", "\n").replace("\n", "\r\n")
        try:
            await self._ws.send_json({"type": "output", "data": normalized})
        except (ConnectionResetError, RuntimeError) as exc:
            raise SessionClosedError("client disconnected during write") from exc

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError(
            "file transfer is not available over the web transport — real Zmodem "
            "interop depends on a native terminal client auto-detecting the "
            "protocol, which a browser tab can't do; use Telnet or SSH instead"
        )

    async def read_byte(self) -> int | None:
        raise NotImplementedError(
            "raw byte I/O is not available over the web transport — see write_raw"
        )

    async def read_line(
        self,
        echo: bool = True,
        history: InputHistory | None = None,
        completer: Completer | None = None,
    ) -> str:
        """
        Read one line, with the same cursor-addressable editing,
        `history` recall (design doc round 47/Track 5f), and `completer`-
        driven Tab completion (design doc round 49/Track 5g)
        `netbbs.net.char_input.read_line` provides for Telnet/SSH --
        reusing that module's `move_cursor`/`redraw_tail`/
        `apply_tab_completion` helpers directly rather than re-deriving
        the same escape-sequence/redraw arithmetic a second time.
        `echo=False` (password prompts) keeps the original simple
        append/Backspace-from-the-end-only behavior, same scope boundary
        as the Telnet/SSH path and for the same reason -- see that
        module's `read_line` docstring.
        """
        if not echo:
            return await self._read_line_masked()
        return await self._read_line_editable(history, completer)

    async def _read_line_masked(self) -> str:
        line: list[str] = []
        while True:
            char = await self._read_char()
            if char in (_CR, _LF):
                break
            if char in (_BS, _DEL):
                if line:
                    line.pop()
                    await self.write("\b \b")
                continue
            if ord(char) < 0x20:
                continue
            if len(line) < _MAX_LINE_LENGTH:
                line.append(char)
                await self.write("*")
        await self.write("\r\n")
        return "".join(line)

    async def _read_line_editable(
        self, history: InputHistory | None, completer: Completer | None = None
    ) -> str:
        line: list[str] = []
        cursor = 0
        overwrite = False
        history_index = 0
        saved_in_progress: list[str] | None = None

        while True:
            item = await self._read_item()

            if isinstance(item, _SpecialKey):
                key = item.name
                if key == "LEFT":
                    if cursor > 0:
                        cursor -= 1
                        await self.write(move_cursor(1, forward=False))
                elif key == "RIGHT":
                    if cursor < len(line):
                        cursor += 1
                        await self.write(move_cursor(1, forward=True))
                elif key == "HOME":
                    if cursor > 0:
                        await self.write(move_cursor(cursor, forward=False))
                        cursor = 0
                elif key == "END":
                    if cursor < len(line):
                        await self.write(move_cursor(len(line) - cursor, forward=True))
                        cursor = len(line)
                elif key == "DELETE":
                    if cursor < len(line):
                        terminal_col = cursor
                        del line[cursor]
                        await redraw_tail(
                            self.write, terminal_col=terminal_col, edit_pos=cursor,
                            line=line, new_cursor=cursor,
                        )
                elif key == "INSERT":
                    overwrite = not overwrite
                elif key in ("UP", "DOWN") and history is not None:
                    recalled = None
                    if key == "UP" and history_index < len(history):
                        if history_index == 0:
                            saved_in_progress = list(line)
                        history_index += 1
                        recalled = list(history.entry(history_index))
                    elif key == "DOWN" and history_index > 0:
                        history_index -= 1
                        recalled = list(saved_in_progress) if history_index == 0 else list(
                            history.entry(history_index)
                        )
                    if recalled is not None:
                        terminal_col = cursor
                        line = recalled
                        cursor = len(line)
                        await redraw_tail(
                            self.write, terminal_col=terminal_col, edit_pos=0,
                            line=line, new_cursor=cursor,
                        )
                continue

            char = item
            if char in (_CR, _LF):
                break

            if char in (_BS, _DEL):
                if cursor > 0:
                    terminal_col = cursor
                    del line[cursor - 1]
                    cursor -= 1
                    await redraw_tail(
                        self.write, terminal_col=terminal_col, edit_pos=cursor,
                        line=line, new_cursor=cursor,
                    )
                continue

            if char == _TAB:
                if completer is not None:
                    cursor = await apply_tab_completion(self.write, completer, line, cursor)
                continue

            if ord(char) < 0x20:
                continue

            if overwrite and cursor < len(line):
                line[cursor] = char
                cursor += 1
                await self.write(char)
                continue

            if len(line) >= _MAX_LINE_LENGTH:
                continue

            terminal_col = cursor
            line.insert(cursor, char)
            cursor += 1
            if cursor == len(line):
                await self.write(char)
            else:
                await redraw_tail(
                    self.write, terminal_col=terminal_col, edit_pos=terminal_col,
                    line=line, new_cursor=cursor,
                )

        await self.write("\r\n")
        result = "".join(line)
        if history is not None:
            history.record(result)
        return result

    async def read_key(self, echo: bool = True) -> str:
        while True:
            char = await self._read_char()
            if char in (_CR, _LF, _BS, _DEL):
                continue
            if ord(char) < 0x20:
                continue
            await self.write(char if echo else "*")
            return char

    async def close(self) -> None:
        self._reader_task.cancel()
        try:
            await self._reader_task
        except asyncio.CancelledError:
            pass
        if not self._ws.closed:
            await self._ws.close()


SessionHandler = Callable[[Session], Awaitable[None]]


class WebServer:
    """
    Web server producing `WebSession` objects and handing each to a
    caller-supplied `session_handler` coroutine — same shape and
    intended usage as `netbbs.net.telnet.TelnetServer`/
    `netbbs.net.ssh.SSHServer`.

    Supplied browser `Origin` headers must be approved. By default, HTTP and
    HTTPS origins whose authority exactly matches the request Host are
    accepted. Deployments needing a different public origin (for example,
    because a reverse proxy rewrites Host) can pass an explicit allowlist.
    Requests without Origin are accepted deliberately for non-browser clients;
    browsers send Origin for websocket handshakes, so this does not weaken the
    cross-site protection the check is intended to provide.
    """

    def __init__(
        self,
        host: str,
        port: int,
        session_handler: SessionHandler,
        *,
        allowed_origins: set[str] | None = None,
    ):
        self._host = host
        self._port = port
        self._session_handler = session_handler
        self._allowed_origins = (
            {origin.rstrip("/") for origin in allowed_origins}
            if allowed_origins is not None
            else None
        )
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def port(self) -> int:
        if self._site is None:
            raise RuntimeError("server has not been started yet")
        return self._site.port

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_websocket)
        app.router.add_static("/static/", _STATIC_DIR)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def serve_forever(self) -> None:
        if self._site is None:
            await self.start()
        # aiohttp's runner already drives the server in the background
        # once started; there's nothing further to await except staying
        # alive until stop() tears it down — matches TelnetServer/
        # SSHServer's serve_forever contract (a coroutine that doesn't
        # return until the server is stopped) with a simple sleep loop.
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(_STATIC_DIR / "index.html")

    def _origin_is_allowed(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        normalized = origin.rstrip("/")
        if self._allowed_origins is not None:
            return normalized in self._allowed_origins
        parsed = urlsplit(normalized)
        return parsed.scheme in {"http", "https"} and parsed.netloc == request.host

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        if not self._origin_is_allowed(request):
            raise web.HTTPForbidden(text="WebSocket Origin is not allowed")

        ws = web.WebSocketResponse(max_msg_size=_MAX_WS_MESSAGE_SIZE)
        await ws.prepare(request)
        session = WebSession(ws, request.remote)
        try:
            await self._session_handler(session)
        except SessionClosedError:
            pass  # client disconnected mid-session — expected, not an error
        except Exception:
            _logger.exception("unhandled error in web session handler")
        finally:
            await session.close()
        return ws
