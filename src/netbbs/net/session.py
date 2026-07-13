"""
Session: the transport-agnostic abstraction every connection type
implements.

Design doc — Telnet, SSH, and a web-based terminal emulator (xterm.js)
are all supported connection methods, landing on this one interface so
the login/menu/command layer never needs to know or care which transport
a given user connected through.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


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
    async def read_line(self, echo: bool = True) -> str:
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
