"""
Session: the transport-agnostic abstraction every connection type
implements.

Design doc — Telnet, SSH, and a web-based terminal emulator (xterm.js)
are all supported connection methods, landing on this one interface so
the login/menu/command layer never needs to know or care which transport
a given user connected through.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import TYPE_CHECKING, Awaitable, Callable

from netbbs.rendering.charset import CP437, UTF8, Charset, map_text
from netbbs.rendering.pipe_codes import PastedColor
from netbbs.rendering.reflow import wrap_terminal_text
from netbbs.rendering.terminal_emulator import TerminalEmulator

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Deferred/type-checking-only: netbbs.net.char_input itself imports
    # SessionClosedError from this module, so a real top-level import
    # here would be circular. `from __future__ import annotations`
    # already makes every annotation in this file a lazily-evaluated
    # string at runtime; this block exists only so type checkers/IDEs
    # can resolve `InputHistory` by name.
    from netbbs.net.char_input import CandidateListPrinter, Completer, EditorKey, InputHistory, LiveInputBuffer
    from netbbs.net.throttle import LoginThrottle


# Same numbers as netbbs.rendering.screen_buffer.ScreenBuffer's own
# defensive ceiling, deliberately (GitHub issue #33) -- comfortably
# exceeds any real terminal while keeping width*height a small, fixed
# number of cells regardless of what a client reports.
_MAX_TERMINAL_WIDTH = 500
_MAX_TERMINAL_HEIGHT = 200


async def wait_until_drained(
    is_drained: Callable[[], bool], timeout: float, *, poll_interval: float = 0.05
) -> bool:
    """Wait up to `timeout` seconds for `is_drained()` to become true.

    Shared by `TelnetServer.stop`/`SSHServer.stop`: each listener tracks
    the connections it admitted itself and waits on *that* set, because
    `asyncio.Server.wait_closed()` is the wrong signal on every supported
    interpreter -- on Python 3.11 it returns immediately after `close()`
    even with clients still attached (so nothing would ever be aborted),
    while on 3.12+ it blocks until every client has dropped (the
    nine-minute dead-peer hang). Polling a plain set is deliberately
    simpler than threading an Event through two transports' connection
    callbacks; at this interval the added latency is invisible next to
    the seconds-scale bound.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not is_drained():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(poll_interval)
    return True


def clamp_terminal_size(width: int, height: int) -> tuple[int, int]:
    """
    Clamp a client-reported terminal size to a sane operational range
    (GitHub issue #33).

    A reported width/height is untrusted display metadata from the
    remote peer -- Telnet NAWS and SSH's PTY window-size channel are
    each bounded to 16 bits, but the web transport accepts any positive
    Python integer in its `resize` event, and none of the three should
    be treated as a resource-allocation authorization. Every transport
    should call this before assigning to `Session.terminal_width`/
    `terminal_height`, so a downstream consumer like the fullscreen
    editors' `ScreenBuffer` allocation never sees an absurd size in the
    first place -- `ScreenBuffer` itself also clamps defensively, but
    that's a backstop, not a substitute for clamping at the boundary
    where the untrusted value actually enters the system.
    """
    return (
        max(1, min(width, _MAX_TERMINAL_WIDTH)),
        max(1, min(height, _MAX_TERMINAL_HEIGHT)),
    )


class SessionClosedError(Exception):
    """
    Raised when the client disconnects while a read or write is in
    progress.

    Transport-agnostic on purpose: Telnet, SSH, and a websocket-based web
    terminal all have their own underlying "the pipe broke" exceptions
    (`asyncio.IncompleteReadError`, `ConnectionResetError`, a closed
    websocket, etc.) — every `Session` implementation is expected to
    catch its own transport-specific version and re-raise this instead,
    so anything built on top of `Session` (login flow, menus, later
    boards/chat) only ever needs to handle one exception type regardless
    of transport.
    """


#: The socket errors a caller's hang-up surfaces as (issue #834). A reset
#: can reach a *read* as well as a write: asyncio hands the transport's
#: error to the stream reader, so `readexactly` raises it directly rather
#: than `IncompleteReadError`. Every transport maps these to
#: `SessionClosedError` at its read and write boundaries.
CLIENT_DISCONNECT_ERRORS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


class Session(ABC):
    """A single connected user's read/write channel, transport-agnostic."""

    #: Best-known terminal dimensions for this session, for reflow (see
    #: `netbbs.rendering.reflow`) and any other width-aware output.
    #: Every transport implementation initializes these to a conservative
    #: default (80x24 — also the design doc's "must degrade gracefully
    #: above 40x24 minimum" floor is well below this) and updates them if
    #: it learns the client's actual size: Telnet via NAWS negotiation
    #: (see `netbbs.net.telnet`), SSH via its own PTY window-size channel
    #: request, a future web terminal via JS reporting the xterm.js
    #: viewport. Screens/output code should read these rather than
    #: assuming a fixed width.
    terminal_width: int = 80
    terminal_height: int = 24

    #: Whether this session's client is known to support 24-bit
    #: truecolor (`CSI 38;2;r;g;bm`), for `netbbs.rendering.gradient.
    #: gradient_text` and any other truecolor-aware output. Conservative
    #: default `False` — every transport either derives this
    #: synchronously at construction (SSH, from a `COLORTERM` env value;
    #: Web, hardcoded `True` since NetBBS controls the xterm.js client
    #: end-to-end) or updates it in place once negotiation resolves
    #: (Telnet NEW-ENVIRON, mirroring `terminal_width`/`terminal_height`'s
    #: own NAWS lazy-resolution precedent above — see
    #: `netbbs.net.telnet.TelnetSession`). A per-user manual override can
    #: supersede this post-login — see
    #: `netbbs.net.color_depth_preference.effective_truecolor`.
    supports_truecolor: bool = False

    #: Whether this transport can hand its raw byte stream to a Zmodem
    #: transfer (issue #475). Zmodem is a protocol *inside* the
    #: terminal stream, so this is a property of the client at the far
    #: end as much as of the transport -- but the transport is the only
    #: place the answer is ever knowable: `netbbs.net.web`'s browser
    #: terminal cannot do it at all, while Telnet and SSH can, provided
    #: the caller's own emulator implements the protocol (many do not,
    #: which is why the HTTP path exists). `True` by default because
    #: every transport but the web one carries bytes; a caller whose
    #: *emulator* has no Zmodem is discovered the only way anyone can
    #: discover it, by the transfer not starting.
    supports_zmodem: bool = True

    #: Which transport carries this session (issue #469): "telnet", "ssh",
    #: "web" or "local". A class attribute set by each transport, the same
    #: shape as `supports_zmodem` above, rather than something inferred from
    #: the class name. Doors are told this because key decoding and latency
    #: assumptions genuinely differ -- the browser terminal in particular.
    #: "unknown" is what a direct test construction gets.
    transport_name: str = "unknown"

    #: Human-readable provenance for ``supports_truecolor``. Shown in the
    #: caller profile so a failed/missing capability report is diagnosable
    #: rather than inferred from appearance. Transport implementations replace
    #: this conservative default with their own exact negotiation path.
    truecolor_diagnostic: str = "transport did not report truecolor capability; using 256-color"

    #: This node's own display name (`netbbs.config.get_node_display_
    #: name`), shown as the root breadcrumb segment on every post-login
    #: screen. Unlike `terminal_width`/`supports_truecolor` above, not
    #: transport-negotiated -- no transport ever sets this itself;
    #: `netbbs.net.login_flow.run_authenticated_session` resolves it
    #: once, right after authentication (when `db` first becomes
    #: available), the same "conservative class default, reassigned in
    #: place once the real value is known" shape those two already use,
    #: just from node_config instead of client negotiation. The
    #: class-level default ("NetBBS") is what every screen rendered
    #: before login (or by a test/direct call site that never reaches
    #: `run_authenticated_session`) still sees.
    node_display_name: str = "NetBBS"

    #: Preset gradient name (`netbbs.rendering.gradient.GRADIENTS`) to
    #: recolor `node_display_name` with wherever it's shown as a
    #: breadcrumb segment, or `None` for a flat `header_color` (GitHub
    #: issue #175). Same resolve-once-at-login lifecycle as
    #: `node_display_name` itself -- `netbbs.net.login_flow.
    #: run_authenticated_session` sets both from `netbbs.net.node_theme.
    #: effective_node_name_gradient` in the same place, right after
    #: `db` first becomes available. The class-level default (`None`)
    #: is what every screen rendered before login, or by a test/direct
    #: call site that never reaches `run_authenticated_session`, still
    #: sees -- and renders byte-for-byte as before this field existed.
    node_name_gradient: str | None = None

    #: Best-known remote address (host only, no port) for this
    #: connection, or `None` if a transport genuinely has no such
    #: concept. Used for per-source login throttling (see
    #: `netbbs.net.throttle.LoginThrottle`) — not meant for any identity
    #: or trust decision, since it's trivially spoofable/shared (NAT).
    peer_address: str | None = None

    #: The node's shared `netbbs.net.throttle.LoginThrottle`, or `None`
    #: for a session that never went through a real entry point (a
    #: direct test call, the local admin CLI). Set once by
    #: `netbbs.net.login_flow.run_authenticated_session`, the same
    #: resolve-at-login lifecycle as `node_display_name` above, so that
    #: a password check made *inside* an authenticated session -- the
    #: "current password" prompt of a self-service password change
    #: (issue #611) -- charges the same per-source/per-username budgets
    #: the login prompt does, rather than being a second, unthrottled
    #: place to try passwords.
    login_throttle: LoginThrottle | None = None

    #: `time.monotonic()` of the last data byte or key the client sent,
    #: or `None` before the first one -- the SysOp monitor's idle time
    #: (issue #762). Stamped by `note_input` at each transport's lowest
    #: input point, below every read method, so a keystroke counts
    #: whichever screen, editor or door consumes it. Transport-level
    #: traffic (Telnet negotiation, SSH resize) is not input: a client's
    #: keepalive must not make an idle caller look active.
    last_input_at: float | None = None

    #: Where this caller is, as a trail of place names (issue #762). Set
    #: only through `netbbs.net.session_activity`, which restores it when
    #: a screen ends; see that module for what may and may not go in it.
    activity: tuple[str, ...] = ()

    #: Hook a screen can install so an out-of-band system notice (a
    #: node-shutdown broadcast, `netbbs.net.session_registry.
    #: ActiveSessionRegistry.broadcast_to_all`) reaches this session
    #: safely instead of assuming a plain scrolling prompt. `None` for
    #: every screen that doesn't need anything special — the overwhelming
    #: majority, which is exactly why `broadcast_to_all` falls back to a
    #: plain `write_line` when this is unset. `netbbs.net.chat_flow.
    #: _chat_loop` is currently the only screen that ever sets it: a
    #: raw `write_line` while chat's pinned status/input rows are active
    #: lands wherever the real cursor happens to sit (often the pinned
    #: input row, mid-keystroke), and a subsequent Backspace then edits
    #: text the session's own input-editing state never knew was
    #: written — chat installs its already-correct pinned-row-aware
    #: delivery path here instead (the same one kick/ban notices use),
    #: and clears it again on exit so a stale closure never lingers past
    #: the chat session that captured it.
    pinned_notice_hook: Callable[[str], Awaitable[None]] | None = None

    #: True while a binary protocol (Zmodem) owns the byte stream: its
    #: frames are not terminal output and must not reach the screen copy.
    #: Set through `binary_transfer()`.
    binary_transfer_active: bool = False

    #: True while a door owns the terminal (`netbbs.doors.runtime`), so a
    #: notice from another caller -- a direct-chat invitation (issue #843)
    #: -- is not written into the door's screen.
    door_active: bool = False

    #: What this caller's terminal reads (design doc §3.2, "Character set
    #: per session", issue #929): UTF-8, CP437 or 7-bit ASCII. `write` maps
    #: composed text to it, and the transport encodes in it; raw byte
    #: output (a door's stream) is in it too. Stays UTF-8 until the
    #: caller's terminal or preference says otherwise.
    output_charset: Charset = UTF8
    #: Whether `output_charset` came from something the terminal said for
    #: certain. False for a Telnet terminal that reported no known type
    #: (it gets ASCII) or only `ansi`, and for an SSH terminal type on
    #: neither list: such a caller is asked after login which sample line
    #: looks right (`netbbs.net.terminal_detect`).
    charset_certain: bool = True
    #: The terminal types the client reported, in order (Telnet TTYPE, or
    #: the SSH PTY request's terminal type).
    terminal_types: tuple[str, ...] = ()

    #: The server-side copy of this caller's screen (issue #764), created
    #: on the first write. See `screen_copy`.
    _screen_copy: TerminalEmulator | None = None
    _raw_decoder: codecs.IncrementalDecoder | None = None
    #: Bumped on every write that reaches the copy, so a repaint from it
    #: can tell whether output arrived while it was being sent.
    _copy_generation: int = 0

    #: Issue #765, a SysOp's break-in chat. While `_break_in_input` is set,
    #: every byte the caller types goes there instead of to whatever read
    #: their own screen is waiting in, and while `_output_held` is set,
    #: what their screen writes updates the copy but is not sent. The
    #: caller's task keeps running untouched; see `begin_break_in`.
    _break_in_input: asyncio.Queue[int] | None = None
    _output_held: bool = False
    _break_in_over: asyncio.Event | None = None
    #: Keystrokes a break-in chat had to drop because its queue was full.
    break_in_dropped: int = 0

    #: True while this session is reading masked input (a password): a
    #: break-in is refused then, and any key diverted to a chat while it is
    #: set is shown as `*`. Set through `secret_input`.
    reading_secret: bool = False

    #: How many times `end_break_in` repaints before it releases anyway,
    #: when output keeps arriving during every repaint (a busy door).
    _RESTORE_ATTEMPTS = 3

    def note_input(self) -> None:
        """Record that the client just sent input; see `last_input_at`."""
        self.last_input_at = time.monotonic()

    # -- output: one shared layer below every transport (issue #764) ------
    #
    # `write` and `write_raw` are concrete here and final in spirit: they
    # feed the screen copy, then hand the bytes to the transport's
    # `_send_text`/`_send_raw`. A transport implements those two, never
    # `write` itself, so no transport can send output the copy misses.
    # (A test double may still override `write` outright; it has no
    # caller behind it to copy.)

    async def write(self, text: str) -> None:
        """Send text to the client, no trailing newline added. Bare `\\n`
        becomes `\\r\\n` on every transport."""
        if self._raw_decoder is not None:
            # Raw output ended mid-character (a door that died partway):
            # the terminal shows a replacement for it before this text, so
            # the copy must too.
            self._copy_output(self._raw_decoder.decode(b"", final=True))
            self._raw_decoder = None
        text = map_text(text, self.output_charset)
        self._copy_output(_normalize_newlines(text))
        if self._output_held:
            return
        await self._send_text(text)

    async def write_raw(self, data: bytes) -> None:
        """
        Send raw bytes to the client exactly as given — no CRLF
        normalization, no UTF-8 encoding (the caller already has bytes),
        no line terminator added.

        Deliberately separate from `write`, which exists for human-
        readable text and performs both of those transforms — a binary
        protocol like ZMODEM (`netbbs.net.zmodem`) needs bytes to arrive
        completely unmodified, including any 0x0A/0x0D/0xFF values that
        happen to appear in a ZDLE-escaped frame or raw file content,
        which `write` would otherwise corrupt.

        Outside a binary transfer, raw output is a door's terminal stream
        in this session's `output_charset` (`netbbs.doors.runtime.
        DoorTerminal` transcodes to it), and the screen copy decodes it as
        such.
        """
        if not self.binary_transfer_active:
            if self._raw_decoder is None:
                codec = "cp437" if self.output_charset == CP437 else "utf-8"
                self._raw_decoder = codecs.getincrementaldecoder(codec)("replace")
            self._copy_output(self._raw_decoder.decode(data))
        elif self._output_held:
            # A transfer is refused a break-in (#765); should one start
            # during one anyway, its frames must still reach the wire.
            await self._send_raw(data)
            return
        if self._output_held:
            return
        await self._send_raw(data)

    async def _send_text(self, text: str) -> None:
        """The transport's own text send; see `write`."""
        raise NotImplementedError

    async def _send_raw(self, data: bytes) -> None:
        """The transport's own raw send; see `write_raw`."""
        raise NotImplementedError

    def screen_copy(self) -> TerminalEmulator:
        """What this caller's terminal shows now, as far as NetBBS's own
        output can tell (issue #764): the SysOp's snoop view reads it, and
        a break-in chat repaints the caller from it. Follows the
        terminal's reported size."""
        width, height = self.terminal_width, self.terminal_height
        if self._screen_copy is None:
            self._screen_copy = TerminalEmulator(width, height)
        else:
            self._screen_copy.resize(width, height)
        return self._screen_copy

    def _copy_output(self, text: str) -> None:
        if not text:
            return
        self._copy_generation += 1
        try:
            self.screen_copy().feed(text)
        except Exception:  # pragma: no cover - a copy bug must never cost a caller their session
            _logger.exception("screen copy failed; starting a fresh one")
            self._screen_copy = None

    # -- input: the other half of the shared layer (issue #765) -----------
    #
    # Byte-stream transports (Telnet, SSH, the local CLI) implement
    # `_receive_byte`/`_receive_byte_with_timeout`; `read_byte` and
    # `read_byte_with_timeout` here are what every reader calls, and the
    # one place a break-in can take the caller's keystrokes. The web
    # transport receives input as websocket events instead and diverts
    # them where they arrive (`WebSession._handle_event`).

    async def read_byte(self) -> int | None:
        """
        Read and return the next raw data byte from the client, blocking
        until one arrives, or `None` if what was read was a pure
        transport-level action with no data significance (a Telnet
        negotiation sequence, an SSH terminal-resize notification) —
        callers should just loop and call this again. Raises
        `SessionClosedError` if the connection closes while waiting.

        The lower-level primitive `read_line`/`read_key` are built on
        (see `netbbs.net.char_input`), also usable directly by anything
        that needs genuinely raw bytes rather than character-mode
        line/key semantics — currently `netbbs.net.zmodem`, which
        ZDLE-decodes its own framing and has no use for backspace/UTF-8/
        escape-sequence handling built for human keyboard input.

        During a break-in the caller's own pending read never sees a
        keystroke: each one goes to the chat, and this keeps waiting.
        """
        while True:
            value = await self._receive_byte()
            if value is not None and self._divert(value):
                continue
            return value

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        """A bounded peek (escape-sequence lookahead, typeahead discard):
        the next byte within `timeout` seconds, or `None`."""
        value = await self._receive_byte_with_timeout(timeout)
        if value is not None and self._divert(value):
            return None
        return value

    async def _receive_byte(self) -> int | None:
        """The transport's own blocking byte read; see `read_byte`."""
        raise NotImplementedError

    async def _receive_byte_with_timeout(self, timeout: float) -> int | None:
        """The transport's own bounded peek; see `read_byte_with_timeout`."""
        raise NotImplementedError

    def _divert(self, value: int) -> bool:
        """Hand `value` to a break-in chat if one is running."""
        queue = self._break_in_input
        if queue is None:
            return False
        try:
            queue.put_nowait(value)
        except asyncio.QueueFull:
            # A runaway paste into a chat: dropped rather than buffered
            # without bound, and said so -- in the node log once per chat,
            # and on the SysOp's chat screen (`break_in_dropped`).
            if not self.break_in_dropped:
                _logger.warning("break-in chat input overflowed; dropping the caller's excess keystrokes")
            self.break_in_dropped += 1
        return True

    # -- break-in (issue #765) --------------------------------------------

    @property
    def in_break_in(self) -> bool:
        return self._break_in_input is not None

    def begin_break_in(self) -> asyncio.Queue[int]:
        """Take over this session's terminal for a SysOp's chat: from now
        on the caller's keystrokes arrive on the returned queue, and their
        own screen's output is held (kept in the copy, not sent). Draw the
        chat with `write_through`. `end_break_in` gives it all back."""
        if self._break_in_input is not None:
            raise RuntimeError("this session is already in a break-in chat")
        self._break_in_input = asyncio.Queue(maxsize=4096)
        self._output_held = True
        self._break_in_over = asyncio.Event()
        self.break_in_dropped = 0
        return self._break_in_input

    async def break_in_began(self) -> None:
        """Transport housekeeping once a break-in has taken the terminal;
        nothing by default (see `WebSession`)."""

    async def wait_for_break_in_end(self) -> None:
        """Return once no break-in holds this session. A binary transfer
        waits here before claiming the byte stream: the chat would divert
        its peer's replies and interleave its own drawing with the frames
        (`netbbs.net.zmodem`)."""
        while self._break_in_over is not None:
            await self._break_in_over.wait()

    async def write_through(self, text: str) -> None:
        """Write past a break-in's hold, and past the screen copy: the
        chat is drawn over the caller's screen, and the copy keeps what is
        underneath, to be put back."""
        await self._send_text(map_text(text, self.output_charset))

    def _held_raw_prefix(self) -> bytes:
        """The start of a multi-byte character a door sent while output was
        held, whose remaining bytes are still to come: the copy's decoder
        is waiting on it, and the caller's terminal must be too."""
        decoder = self._raw_decoder
        return decoder.getstate()[0] if decoder is not None else b""

    async def _send_held_prefix(self) -> None:
        prefix = self._held_raw_prefix()
        if not prefix:
            return
        try:
            await self._send_raw(prefix)
        except NotImplementedError:
            # The door already ended (a web session has no raw stream
            # outside door mode): the character will never be completed, so
            # the copy shows what the terminal would, a replacement.
            self._drop_held_prefix()

    def _drop_held_prefix(self) -> None:
        """Give up on a held partial character: the copy shows the
        replacement a terminal would, and whatever continuation arrives
        later is equally a stray byte on both sides."""
        if self._raw_decoder is not None:
            self._copy_output(self._raw_decoder.decode(b"", final=True))
            self._raw_decoder = None

    async def end_break_in(self) -> None:
        """Repaint the caller's screen as their own program left it,
        output that arrived during the chat included, then give input and
        output back. Nothing is awaited between the last repaint and the
        release, so no write can fall between the two."""
        try:
            for _attempt in range(self._RESTORE_ATTEMPTS):
                # Stable means: no output reached the copy and the terminal
                # kept its size while the repaint -- and the held prefix after
                # it -- were on their way.
                before = (self._copy_generation, self.terminal_width, self.terminal_height)
                await self.write_through(self.screen_copy().restore_ansi())
                await self._send_held_prefix()
                if before == (self._copy_generation, self.terminal_width, self.terminal_height):
                    break
            else:
                # Output kept arriving during every repaint (a busy door on a
                # slow line). Release first, then repaint once more: the
                # repaint is queued on the wire before anything the caller's
                # screen writes after the release, so nothing is lost. A held
                # partial character can't be ordered safely against output
                # released alongside it, so it is given up on both sides.
                self._drop_held_prefix()
                self._output_held = False
                await self.write_through(self.screen_copy().restore_ansi())
        finally:
            self._output_held = False
            self._break_in_input = None
            over, self._break_in_over = self._break_in_over, None
            if over is not None:
                over.set()

    @contextmanager
    def binary_transfer(self):
        """Mark a binary protocol's span on the byte stream (Zmodem), so
        its frames stay out of the screen copy."""
        previous = self.binary_transfer_active
        self.binary_transfer_active = True
        try:
            yield
        finally:
            self.binary_transfer_active = previous
            self._raw_decoder = None

    async def enter_door_mode(self, *, encoding: str = "utf-8", width: int | None = None,
                              height: int | None = None) -> None:
        """Temporarily give a door ownership of terminal input and output."""

    async def leave_door_mode(self) -> None:
        """Restore ordinary input after the owning door has stopped."""

    async def write_line(self, text: str = "") -> None:
        """
        Send text followed by a line terminator.

        Concrete implementation here, not abstract — always `\\r\\n`
        regardless of transport. That's the correct line ending for
        Telnet (RFC 854) and is also universally accepted by SSH and web
        terminal clients, so there's no reason for subclasses to
        override this.
        """
        await self.write(wrap_terminal_text(text, self.terminal_width) + "\r\n")

    @abstractmethod
    async def read_line(
        self,
        echo: bool = True,
        history: InputHistory | None = None,
        completer: Completer | None = None,
        *,
        live_buffer: LiveInputBuffer | None = None,
        lock: asyncio.Lock | None = None,
        list_candidates: CandidateListPrinter | None = None,
        initial: str = "",
        cancellable: bool = False,
        viewport: int | Callable[[], int] | None = None,
        viewport_owns_row: bool = False,
        pasted_color: PastedColor | None = None,
    ) -> str:
        """
        Read one line of input from the client.

        `echo=False` masks each typed character (e.g. with `*`) instead
        of showing it as typed — used for password prompts. This reveals
        length but not content, a deliberate choice over showing nothing
        at all. *How* characters are echoed/masked is transport-specific
        — for Telnet (see `netbbs.net.telnet`), the server takes over
        echoing entirely and handles this itself, character by character;
        other transports may differ — which is exactly why this is
        abstract rather than shared logic here.

        `history` enables Up/Down command recall for this read —
        optional, and ignored entirely for masked (`echo=False`) reads, which keep
        simple append-only editing (see `netbbs.net.char_input.
        read_line`'s docstring for why). Most callers don't pass one;
        currently only `netbbs.net.chat_flow`'s chat input loop does,
        with one `InputHistory` constructed per connected session (see
        `netbbs.net.login_flow.handle_session`) so recall persists
        across a `/join` channel switch.

        `completer` enables Tab completion for this read, also ignored
        for masked reads — see
        `netbbs.net.char_input.apply_tab_completion`'s docstring for its
        exact behavior. Built fresh per call by callers that need it
        (`netbbs.net.chat_flow`'s command/username completer,
        `netbbs.net.picker.pick_item`'s name-based one for its
        `"Search: "` prompt), not threaded through a session-lifetime
        object the way `history` is — a completer's candidate set
        depends on exactly where it's called from, so there's nothing
        to persist between calls the way recalled history lines are.

        `live_buffer`/`lock`/`list_candidates` are pinned-input-row hooks
        that only `netbbs.net.chat_flow`'s chat loop uses — every other
        caller leaves all three at their default
        `None`, a complete no-op. See `netbbs.net.char_input.read_line`'s
        docstring for what each does.

        `pasted_color` (issue #754) turns a pasted SGR color sequence
        into the pipe codes a post editor shows, typed at the cursor.
        Only the post editors pass one; every other read drops a pasted
        SGR, as it always has.
        """

    @abstractmethod
    async def read_key(self, echo: bool = True) -> str:
        """
        Read a single character and return immediately — no Enter
        required. The character-mode equivalent of a classic BBS hotkey
        menu: intended for genuine single-choice menu selections (e.g.
        "[B]oards [C]hat [Q]uit"), not free-text input (board names,
        post subjects, chat messages), which should keep using
        `read_line`.

        Only meaningful once a transport has taken over character-mode
        input itself (see `netbbs.net.telnet`) — a transport relying on
        client-side line buffering has no way to return before the user
        presses Enter, since the whole line arrives as one chunk only
        after that.
        """

    async def read_any_key(self, echo: bool = True) -> str:
        """
        Wait for literally one keystroke — Enter included — to dismiss a
        "Press any key to continue..." pause (dogfood report: `read_key`
        deliberately treats CR/LF as meaningless noise, correct for a
        hotkey menu but not for this different context, where Enter is
        arguably the single most natural key to reach for).

        Concrete, not abstract, with a `read_key`-delegating default —
        every existing `Session` subclass (every real transport's own
        test double included) keeps working unchanged; a transport
        overrides this only where it actually implements character-mode
        input itself (see `netbbs.net.telnet`'s own override, which
        routes to `netbbs.net.char_input.read_any_key`).
        """
        return await self.read_key(echo=echo)

    @abstractmethod
    async def read_editor_key(
        self, *, distinguish_ctrl_h: bool = False, pasted_color: PastedColor | None = None
    ) -> EditorKey:
        """
        Read one structured key event for a full-screen editor (design
        doc -- welcome banner, `netbbs.net.ansi_editor`).

        Unlike `read_key` (which discards every escape sequence
        outright -- there's no line for a cursor to move within in a
        single-keystroke menu) or `read_line` (line-oriented, returns
        a finished `str` only on Enter), this surfaces arrows, Home/
        End, Page Up/Down, and a real standalone Escape press as
        first-class `netbbs.net.char_input.EditorKey` events, alongside
        ordinary characters, Enter, Backspace, Delete, Tab, and
        Ctrl+letter combos -- everything a screen editor needs that
        neither of the other two read methods has a use for.

        `distinguish_ctrl_h` -- `False` by default, so every existing
        caller (the fullscreen ANSI/prose editors, which genuinely need
        0x08 to keep meaning real character-deleting Backspace) is
        unaffected. See `netbbs.net.char_input.read_editor_key`'s own
        docstring for the full rationale and why it's safe only for a
        caller whose own dispatch never needs a real Backspace.

        `pasted_color` -- as for `read_line`: a pasted SGR arrives as
        its pipe codes, one `CHAR` event each.
        """

    async def discard_buffered_enter(self) -> None:
        """Discard an Enter already buffered behind a completed hotkey.

        Confirmation prompts use this after accepting Y/N so callers who
        habitually type ``y`` plus Enter do not accidentally apply that Enter
        to the following prompt. Interactive transports override this with a
        bounded, pushback-safe peek. The no-op default preserves compatibility
        for non-interactive and lightweight Session adapters.
        """

    def arm_word_guard(self) -> None:
        """After a one-key answer, drop the rest of a word typed after it
        and the Enter that ends it (issue #840, F114; see
        `netbbs.net.char_input.WORD_GUARD_SECONDS`). Interactive transports
        override it; the no-op default suits every other adapter."""

    async def discard_buffered_input(self) -> None:
        """Discard *every* byte/keystroke currently buffered ahead of the
        next real read -- a wider-scoped sibling of
        ``discard_buffered_enter`` (which only ever looks for one trailing
        Enter). Used when this session is about to be evicted mid-
        keystroke from whatever it was doing (a moderation kick/ban):
        without this, whatever the caller had already typed but not yet
        submitted silently leaks into whatever screen the eviction lands
        them on next, one keystroke at a time, invisibly navigating them
        through unrelated screens with no indication why (dogfood follow-
        up). Interactive transports override this with a bounded loop of
        the same pushback-safe peek ``discard_buffered_enter`` already
        uses, repeated until nothing more arrives. The no-op default
        preserves compatibility for non-interactive and lightweight
        Session adapters, same reasoning as ``discard_buffered_enter``'s
        own default.
        """

    @abstractmethod
    async def close(self) -> None:
        """Close the underlying connection."""




@contextmanager
def secret_input(session: object):
    """Mark `session` as reading masked input for the span of the read
    (issue #765): a SysOp's break-in must never show a password. Tolerates
    sources that are not a `Session` (test doubles, stand-ins)."""
    previous = getattr(session, "reading_secret", False)
    try:
        session.reading_secret = True  # type: ignore[attr-defined]
    except AttributeError:
        yield
        return
    try:
        yield
    finally:
        session.reading_secret = previous  # type: ignore[attr-defined]


def _normalize_newlines(text: str) -> str:
    """What every transport puts on the wire for `write`: a bare LF
    becomes CRLF. The screen copy is fed the same, or it would see a line
    feed without its carriage return."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


async def write_prompt(session: Session, text: str) -> None:
    """Write a width-safe interactive prompt without a trailing newline.

    Two columns are reserved for the first input character, covering the
    maximum width of one supported East Asian Wide/Fullwidth character, so a
    prompt never makes that first keystroke disappear into an implicit
    soft-wrap.  Callers should use this instead of raw ``Session.write``
    whenever the output is human-readable prompt text; ``write`` remains the
    low-level primitive for cursor controls, incremental echo, screen-buffer
    diffs, bells, and raw door-style output.
    """
    width = max(1, getattr(session, "terminal_width", 80) - 2)
    await session.write(wrap_terminal_text(text, width))


async def write_preformatted_line(session: Session, text: str) -> None:
    """Write trusted terminal art while preserving authored line breaks.

    SysOp-authored ANSI banners and mastheads keep their original rows whenever
    those rows fit.  Cursor positioning is preserved but modeled so it
    participates in width measurement.  An over-width row still wraps as the
    bounded fallback, so trusted art cannot hide content beyond a narrow
    terminal's right edge.  Ordinary human-readable text must use ``write_line``
    or ``write_prompt``.
    """
    width = max(1, getattr(session, "terminal_width", 80))
    await session.write(wrap_terminal_text(text, width) + "\r\n")
