"""
Platform-specific local-terminal I/O for the SysOp admin CLI tool
(design doc -- SysOp foundation round).

Deliberately the smallest possible platform-specific sliver:
`raw_terminal()` puts the controlling terminal into character-mode
input for the duration of an admin CLI session (see
`netbbs.net.local_cli.LocalCLISession`), and the two blocking byte-read
functions below are what it reads with. Everything else -- line
editing, echo, history, Tab completion -- comes from
`netbbs.net.char_input`, shared with Telnet/SSH, and needs no
platform-specific code at all.

This module is genuinely hard to exercise automatically on every
platform this project cares about: the current dev sandbox is Windows,
while the deployment target (per CLAUDE.md) is NetBSD. A pty-based test
(tests/test_local_terminal_raw_mode.py, POSIX-only, skipped on Windows)
covers the POSIX branch; the Windows branch has no equivalent automated
coverage here and is flagged for manual verification, consistent with
how this project already treats a few other genuinely-unverifiable-in-
this-sandbox things (SSH/Zmodem/xterm.js interop -- see
docs/NetBBS-design-doc.md).
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import select
    import termios
    import tty


@contextlib.contextmanager
def raw_terminal() -> Iterator[None]:
    """
    Put the controlling terminal into character-mode input (no line
    buffering, no local echo) for the duration of the `with` block, and
    always restore the previous mode on exit.

    Windows: a no-op. `msvcrt.getch()` (used by the read functions
    below) already reads one unbuffered character per call regardless
    of any console mode -- there's no global mode to set or restore the
    way POSIX's termios has.
    """
    if sys.platform == "win32":
        yield
        return

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def read_byte_blocking() -> bytes:
    """
    Block until one byte is available on stdin and return it, or
    return b"" at EOF (stdin closed).

    Reads via `os.read` directly on POSIX, not `sys.stdin.buffer` --
    deliberately bypassing Python's own buffered-stream layer so this
    can't desync from `select.select`'s kernel-level readiness checks
    in `read_byte_blocking_with_timeout` below (a buffered reader can
    silently hold bytes `select` has no visibility into).
    """
    if sys.platform == "win32":
        return msvcrt.getch()
    return os.read(sys.stdin.fileno(), 1)


def read_byte_blocking_with_timeout(timeout: float) -> bytes | None:
    """Like `read_byte_blocking`, but give up and return `None` after
    `timeout` seconds if nothing arrives."""
    if sys.platform == "win32":
        return _win32_read_byte_with_timeout(timeout)
    fd = sys.stdin.fileno()
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return None
    return os.read(fd, 1)


def _win32_read_byte_with_timeout(timeout: float) -> bytes | None:
    """`msvcrt` has no blocking-read-with-timeout primitive, so this
    polls `kbhit()` -- acceptable here since this branch only ever runs
    on the Windows dev sandbox, never the NetBSD deployment target this
    project actually ships to."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            return msvcrt.getch()
        time.sleep(0.01)
    return None
