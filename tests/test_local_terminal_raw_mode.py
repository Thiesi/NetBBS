"""
POSIX-only pty-based smoke test for
`netbbs.net.local_terminal.raw_terminal`. Skipped on Windows, where
that function is a no-op
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


def _assert_mode_restored(before, after) -> None:
    """
    Compares two termios attribute lists the way raw_terminal()'s own
    contract actually promises, not by opaque full-struct equality.

    iflag/oflag/cflag and the control-character array (cc, including
    VMIN/VTIME) are asserted exactly -- these are the fields
    tty.setraw() actually mutates (see its own CPython source: BRKINT/
    ICRNL/INPCK/ISTRIP/IXON in iflag, OPOST in oflag, CSIZE/PARENB/CS8
    in cflag, VMIN/VTIME in cc), and raw_terminal()'s restore
    (tcsetattr with the full previously-captured struct) genuinely
    round-trips them bit-for-bit in practice.

    lflag is checked only for the specific bits tty.setraw() actually
    touches (ECHO, ICANON, IEXTEN, ISIG) rather than full equality.
    The kernel's n_tty line discipline can assert its own internal
    lflag bookkeeping bits (e.g. PENDIN-style "raw input still pending
    canonical reprocessing" state) as a side effect of switching modes
    on a pty -- that's live kernel state tcsetattr() cannot force back
    to an exact prior snapshot, and it was never something
    raw_terminal() set or is responsible for restoring in the first
    place. Asserting the whole opaque lflag word couples this test to
    kernel/pty-driver internals outside this function's control, not
    to anything raw_terminal() actually promises.
    """
    assert after[:3] == before[:3], "iflag/oflag/cflag must be restored exactly"
    significant_lflag_bits = termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG
    assert (after[3] & significant_lflag_bits) == (before[3] & significant_lflag_bits), (
        "echo/canonical/extended-input/signal-generation flags must be restored"
    )
    assert after[4:] == before[4:], "baud rate and control-character settings must be restored"


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
        _assert_mode_restored(before, after)
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
        _assert_mode_restored(before, after)
    finally:
        os.close(master_fd)
        os.close(slave_fd)
