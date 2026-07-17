"""
LocalCLISession: a `Session` implementation over the local controlling
terminal's stdin/stdout (design doc -- SysOp foundation round), used by
the standalone `python -m netbbs.admin` CLI tool
(`netbbs.admin.__main__`) so it can share the exact same
`netbbs.net.admin_flow.admin_menu` code the in-BBS Admin option uses.

Modeled directly on `netbbs.net.telnet.TelnetSession`: `read_line`/
`read_key` simply delegate to `netbbs.net.char_input`, which does every
bit of the actual character-mode-input work (echo, backspace, UTF-8
decoding, history, Tab completion) already shared with Telnet/SSH --
this class only needs to supply raw bytes in and normalized text out.

The two byte-read functions are constructor-injectable specifically so
this class is unit-testable (tests/test_local_cli_session.py) via a
fake byte source, with no real terminal involved -- the one genuinely
platform-specific, hard-to-test-here piece lives entirely in
`netbbs.net.local_terminal` instead.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from typing import Callable

from netbbs.net import char_input, local_terminal
from netbbs.net.char_input import Completer, InputHistory, LiveInputBuffer
from netbbs.net.session import Session, SessionClosedError


class LocalCLISession(Session):
    """A single local-terminal "connection" -- there's exactly one per
    `python -m netbbs.admin` process, for as long as it runs."""

    def __init__(
        self,
        *,
        read_byte_fn: Callable[[], bytes] = local_terminal.read_byte_blocking,
        read_byte_with_timeout_fn: Callable[[float], bytes | None] = (
            local_terminal.read_byte_blocking_with_timeout
        ),
    ) -> None:
        self._read_byte_fn = read_byte_fn
        self._read_byte_with_timeout_fn = read_byte_with_timeout_fn
        size = shutil.get_terminal_size(fallback=(80, 24))
        self.terminal_width = size.columns
        self.terminal_height = size.lines
        self.peer_address = None

    async def write(self, text: str) -> None:
        # Same CRLF normalization TelnetSession.write performs, and for
        # the same reason: raw/cbreak mode
        # (netbbs.net.local_terminal.raw_terminal) disables the
        # terminal driver's own NL->CRLF translation.
        normalized = text.replace("\r\n", "\n").replace("\n", "\r\n")
        sys.stdout.write(normalized)
        sys.stdout.flush()

    async def write_raw(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    async def read_line(
        self,
        echo: bool = True,
        history: InputHistory | None = None,
        completer: Completer | None = None,
        *,
        live_buffer: LiveInputBuffer | None = None,
        lock: asyncio.Lock | None = None,
        list_candidates: char_input.CandidateListPrinter | None = None,
    ) -> str:
        # live_buffer/lock/list_candidates (design doc round 79) are
        # never actually passed by this session's one caller (the
        # standalone `python -m netbbs.admin` CLI has no chat feature) --
        # accepted anyway purely for signature consistency with the rest
        # of the Session implementations.
        return await char_input.read_line(
            self, self.write, echo, history, completer,
            live_buffer=live_buffer, lock=lock, list_candidates=list_candidates,
        )

    async def read_key(self, echo: bool = True) -> str:
        return await char_input.read_key(self, self.write, echo)

    async def read_editor_key(self) -> char_input.EditorKey:
        return await char_input.read_editor_key(self)

    async def close(self) -> None:
        # Nothing to close -- stdin/stdout live for the process's whole
        # lifetime, unlike a network socket. Restoring the terminal mode
        # is raw_terminal()'s job (a context manager the CLI entry point
        # wraps the whole session in), not this method's.
        pass

    # -- char_input.ByteSource ------------------------------------------

    async def read_byte(self) -> int | None:
        data = await asyncio.to_thread(self._read_byte_fn)
        if not data:
            raise SessionClosedError("stdin closed")
        return data[0]

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        data = await asyncio.to_thread(self._read_byte_with_timeout_fn, timeout)
        return data[0] if data else None
