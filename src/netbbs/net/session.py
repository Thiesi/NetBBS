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

        `echo=False` suppresses the client's own local echo for the
        duration of this call (used for password prompts). *How* it's
        suppressed is transport-specific — Telnet: IAC WILL/WONT ECHO;
        SSH: the pty's own echo flag; web: JS-side masking — which is
        exactly why this is abstract rather than shared logic here.
        """

    @abstractmethod
    async def close(self) -> None:
        """Close the underlying connection."""
