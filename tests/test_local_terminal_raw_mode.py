"""
POSIX-only pty-based smoke test for
`netbbs.net.local_terminal.raw_terminal` (design doc -- SysOp
foundation round). Skipped on Windows, where that function is a no-op
by design (see its own docstring) -- this test genuinely cannot run in
the current Windows dev sandbox; it's here for whenever this project's
tests next run on POSIX/NetBSD hardware, alongside this project's other
flagged-but-not-yet-hardware-verified pieces (SSH/Zmodem/xterm.js
interop -- see docs/NetBBS-design-doc.md).
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="raw_terminal() is POSIX-only; a no-op on Windows"
)

if sys.platform != "win32":
    import pty
    import termios

    from netbbs.net.local_terminal import raw_terminal


class _FakeStdin:
    """Just enough of a stdin-shaped object for raw_terminal() -- it
    only ever calls .fileno()."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


def test_raw_terminal_disables_canonical_mode_and_echo_then_restores_it(monkeypatch):
    master_fd, slave_fd = pty.openpty()
    try:
        monkeypatch.setattr(sys, "stdin", _FakeStdin(slave_fd))

        before = termios.tcgetattr(slave_fd)
        assert before[3] & termios.ICANON, "pty should start in canonical mode"
        assert before[3] & termios.ECHO, "pty should start with echo on"

        with raw_terminal():
            during = termios.tcgetattr(slave_fd)
            assert not (during[3] & termios.ICANON), "raw_terminal() should disable canonical mode"
            assert not (during[3] & termios.ECHO), "raw_terminal() should disable local echo"

        after = termios.tcgetattr(slave_fd)
        assert after == before, "terminal mode must be restored on exit"
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_raw_terminal_restores_mode_even_if_the_body_raises(monkeypatch):
    master_fd, slave_fd = pty.openpty()
    try:
        monkeypatch.setattr(sys, "stdin", _FakeStdin(slave_fd))
        before = termios.tcgetattr(slave_fd)

        with pytest.raises(ValueError):
            with raw_terminal():
                raise ValueError("boom")

        after = termios.tcgetattr(slave_fd)
        assert after == before
    finally:
        os.close(master_fd)
        os.close(slave_fd)
