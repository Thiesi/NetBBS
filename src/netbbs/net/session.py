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
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Awaitable, Callable

from netbbs.rendering.reflow import wrap_terminal_text

if TYPE_CHECKING:
    # Deferred/type-checking-only: netbbs.net.char_input itself imports
    # SessionClosedError from this module, so a real top-level import
    # here would be circular. `from __future__ import annotations`
    # already makes every annotation in this file a lazily-evaluated
    # string at runtime; this block exists only so type checkers/IDEs
    # can resolve `InputHistory` by name.
    from netbbs.net.char_input import CandidateListPrinter, Completer, EditorKey, InputHistory, LiveInputBuffer


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

    @abstractmethod
    async def write(self, text: str) -> None:
        """Send raw text to the client, no trailing newline added."""

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
    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
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
        """

    async def discard_buffered_enter(self) -> None:
        """Discard an Enter already buffered behind a completed hotkey.

        Confirmation prompts use this after accepting Y/N so callers who
        habitually type ``y`` plus Enter do not accidentally apply that Enter
        to the following prompt. Interactive transports override this with a
        bounded, pushback-safe peek. The no-op default preserves compatibility
        for non-interactive and lightweight Session adapters.
        """

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

    @abstractmethod
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
        """

    @abstractmethod
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
        """


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
