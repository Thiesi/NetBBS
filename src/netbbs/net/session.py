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

    async def write_line(self, text: str = "") -> None:
        """
        Send text followed by a line terminator.

        Concrete implementation here, not abstract — always `\\r\\n`
        regardless of transport. That's the correct line ending for
        Telnet (RFC 854) and is also universally accepted by SSH and web
        terminal clients, so there's no reason for subclasses to
        override this.
        """
        await self.write(text + "\r\n")

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

    @abstractmethod
    async def read_editor_key(self) -> EditorKey:
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
