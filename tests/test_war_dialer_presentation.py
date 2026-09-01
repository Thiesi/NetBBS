"""Tests for the War Dialer door's presentation layer (netbbs.doors.
bundled.war_dialer) -- deliberately narrow, unlike test_war_dialer_
domain.py's broad domain-formula coverage. This door has no other
presentation-layer tests (matches Retro Trivia's own established
boundary: domain logic gets real regression coverage, the terminal-
driving `main()`/rendering glue doesn't) -- these exist specifically
because each pins a real bug Codex caught across PR #239/#240/#241/#242's
review rounds, not a routine rendering check.

Loaded directly from its file path, same reasoning as test_war_dialer_
domain.py: this is the exact file NetBBS launches as a standalone
subprocess, not an ordinarily-imported library module.
"""

from __future__ import annotations

import importlib.util
import io
import os
import re
import sys
from pathlib import Path

_WAR_DIALER_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "bundled" / "war_dialer.py"
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _load_war_dialer():
    spec = importlib.util.spec_from_file_location("war_dialer_presentation_under_test", _WAR_DIALER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wd = _load_war_dialer()


def _buffered_stdin(read_fd: int) -> io.TextIOWrapper:
    """Wraps a raw pipe fd the same way real `sys.stdin` is wrapped
    (TextIOWrapper over a BufferedReader over a FileIO), not a bare
    unbuffered FileIO. Codex review (PR #242): a bare unbuffered FileIO
    has no `.buffer` attribute, so a test built on one would fail with
    an unrelated `AttributeError` -- not a meaningful assertion failure
    -- if `read_key()` ever regressed back to `sys.stdin.buffer.read(1)`
    (the exact PR #241 bug these tests exist to guard against). Using
    the same layered shape as production stdin means a regression there
    still fails these tests for the right reason."""
    return io.TextIOWrapper(io.BufferedReader(io.FileIO(read_fd, closefd=False)))


def test_draw_help_never_overflows_its_own_declared_width():
    # Codex review (PR #239): draw_help's body text used to be hand-
    # wrapped assuming a fixed ~78-column terminal, overflowing into
    # extra rows exactly at the narrow widths main() explicitly
    # supports (down to 40 columns) -- defeating the "one page" intent
    # right where it matters most. Checked across the full supported
    # range, not just one width, since the fix (_wrap) is a general
    # mechanism, not a special case for any one column count.
    for width in (40, 60, 78):
        written: list[str] = []
        original_out = wd.out
        try:
            wd.out = written.append
            wd.draw_help(wd.Palette(truecolor=False), width)
        finally:
            wd.out = original_out
        text = _ANSI_RE.sub("", "".join(written))
        for line in text.split("\r\n"):
            assert len(line) <= width, f"line exceeds width={width}: {line!r}"


def test_press_any_key_consumes_a_full_arrow_key_sequence():
    # Codex review (PR #239), a real bug: press_any_key() used to
    # consume only the leading ESC byte of a multi-byte arrow-key
    # sequence (ESC [ <letter>), leaving the rest in the input buffer
    # for the *next* read. read_menu_choice() silently ignored the
    # stray '[' but accepted the trailing letter as a real hotkey --
    # right-arrow's trailing 'C' silently spent cash and a turn on
    # Crew Recruit, an action the caller never chose to take. Confirms
    # the fix by scripting a right-arrow press (\x1b[C) at
    # press_any_key()'s own prompt and asserting nothing extra is
    # readable afterward.
    inputs = iter([wd.ESC, "[", "C", "b"])  # right-arrow, then a real "back-ish" byte

    class _FakeSession:
        def __init__(self):
            self.calls = 0

        def read(self):
            self.calls += 1
            return next(inputs)

    session = _FakeSession()
    original_read_key = wd.read_key
    original_lookahead = wd._read_key_with_timeout
    try:
        wd.read_key = session.read
        # A real CSI sequence's bytes are already sitting in the input
        # buffer by the time press_any_key() checks (PR #240 fix below)
        # -- true here since this FakeSession's whole sequence is
        # scripted up front, not arriving byte-by-byte from a real
        # socket, so the lookahead always finds the next byte
        # immediately.
        wd._read_key_with_timeout = lambda timeout: session.read()
        written: list[str] = []
        original_out = wd.out
        try:
            wd.out = written.append
            wd.press_any_key(wd.Palette(truecolor=False))
        finally:
            wd.out = original_out
    finally:
        wd.read_key = original_read_key
        wd._read_key_with_timeout = original_lookahead

    # The whole 3-byte sequence (ESC, '[', 'C') must be consumed by
    # press_any_key() itself -- exactly 3 read_key() calls, leaving the
    # 4th scripted byte ('b') untouched for whatever reads next, not
    # already silently consumed as if it were a menu choice.
    assert session.calls == 3


def test_press_any_key_does_not_block_on_a_standalone_escape():
    # Codex review (PR #240), a real bug in the PR #239 fix above: a
    # standalone Escape press is an ordinary way to dismiss "Press any
    # key to continue..." -- but the unconditional second read_key()
    # call that fix added blocked waiting for a byte that was never
    # coming, then silently consumed whatever the caller typed *next*
    # (their real following menu choice) as if it might be the '[' of a
    # CSI sequence. Confirms the fix: when no further byte is available
    # (_read_key_with_timeout stubbed to return None, matching a real
    # standalone Escape with nothing queued behind it), press_any_key()
    # must return after exactly one read_key() call, never attempting a
    # second.
    key_calls = 0
    lookahead_calls = 0

    def _read_key():
        nonlocal key_calls
        key_calls += 1
        return wd.ESC

    def _lookahead(timeout):
        nonlocal lookahead_calls
        lookahead_calls += 1
        return None

    original_read_key = wd.read_key
    original_lookahead = wd._read_key_with_timeout
    try:
        wd.read_key = _read_key
        wd._read_key_with_timeout = _lookahead
        written: list[str] = []
        original_out = wd.out
        try:
            wd.out = written.append
            wd.press_any_key(wd.Palette(truecolor=False))
        finally:
            wd.out = original_out
    finally:
        wd.read_key = original_read_key
        wd._read_key_with_timeout = original_lookahead

    assert key_calls == 1
    assert lookahead_calls == 1


def test_read_key_does_not_over_consume_from_a_real_pipe():
    # Codex review (PR #241), the real root cause of the P1 finding
    # above: the *previous* read_key() went through sys.stdin.buffer
    # (a BufferedReader), which can pull more than the one requested
    # byte from the OS pipe into its own internal buffer -- invisible
    # to a readiness check that only sees the raw fd. This is what the
    # stubbed-lookahead tests above can't catch (Codex called that out
    # specifically): they prove press_any_key()'s own branching logic is
    # right, but not that read_key() itself leaves unread bytes actually
    # unread. Exercises the real read_key() against a real OS pipe,
    # wrapped the same layered (buffered) way real stdin is -- Codex's
    # own follow-up finding on the *previous* version of this test: a
    # bare unbuffered fixture can't actually demonstrate the prefetch
    # bug this test exists to guard against, since `read_key()`
    # regressing to `sys.stdin.buffer.read(1)` would just crash with an
    # unrelated AttributeError instead of failing the real assertion
    # below. os.pipe()/os.read() behave the same on Windows and POSIX,
    # so this test needs no platform guard.
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"AB")
    real_stdin = sys.stdin
    try:
        sys.stdin = _buffered_stdin(read_fd)
        key = wd.read_key()
    finally:
        sys.stdin = real_stdin
        os.close(write_fd)

    assert key == "A"
    # The second byte must still be sitting unread in the pipe -- proof
    # read_key() took exactly the one byte it asked for, nothing more.
    remaining = os.read(read_fd, 10)
    os.close(read_fd)
    assert remaining == b"B"


def test_read_key_with_timeout_survives_a_windows_style_oserror():
    # Codex review (PR #241): select.select() only accepts sockets on
    # Windows, not the pipe/console handle sys.stdin actually is for
    # this door -- raising OSError there instead of just failing to
    # detect readiness, which used to crash the whole door the moment a
    # caller dismissed a pause with a standalone Escape. Confirms the
    # guard: with select.select() forced to behave the way it really
    # does on Windows, _read_key_with_timeout() must fall through to its
    # own Windows poll path (not propagate the exception) and still find
    # a byte that's genuinely there to read.
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"X")

    def _raise(*args, **kwargs):
        raise OSError("select() only accepts sockets on Windows")

    original_select = wd.select.select
    real_stdin = sys.stdin
    try:
        wd.select.select = _raise
        sys.stdin = _buffered_stdin(read_fd)
        result = wd._read_key_with_timeout(1.0)
    finally:
        wd.select.select = original_select
        sys.stdin = real_stdin
        os.close(write_fd)
        os.close(read_fd)

    assert result == "X"


def test_read_key_with_timeout_returns_none_when_nothing_arrives():
    # Companion to the above: with select() forced to fail the same way
    # (simulating Windows) and genuinely nothing written to the pipe,
    # the Windows poll fallback must give up after its own timeout
    # budget and return None -- not block forever, and not raise.
    read_fd, write_fd = os.pipe()

    def _raise(*args, **kwargs):
        raise OSError("select() only accepts sockets on Windows")

    original_select = wd.select.select
    real_stdin = sys.stdin
    try:
        wd.select.select = _raise
        sys.stdin = _buffered_stdin(read_fd)
        result = wd._read_key_with_timeout(0.05)
    finally:
        wd.select.select = original_select
        sys.stdin = real_stdin
        os.close(write_fd)
        os.close(read_fd)

    assert result is None


def test_press_any_key_consumes_full_csi_sequence_over_a_real_pipe():
    # Codex review (PR #241/#242): the strongest form of this regression
    # test -- exercises the REAL read_key() and REAL
    # _read_key_with_timeout() together (no stubbing at all) against a
    # real OS pipe, with a full right-arrow CSI sequence written in one
    # shot the way an actual terminal delivers it. This is the only way
    # to actually catch a mismatch between what the readiness check
    # reports and what read_key() has already consumed -- the exact gap
    # the previous (BufferedReader-based) read_key() had. Runs
    # unconditionally, including on Windows: _read_key_with_timeout()'s
    # own Windows poll fallback (verified directly against a real
    # Windows pipe fd) means this no longer needs a platform skip the
    # way its select()-only predecessor did.
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"\x1b[Cb")  # right-arrow, then a real next keystroke
    real_stdin = sys.stdin
    try:
        sys.stdin = _buffered_stdin(read_fd)
        written: list[str] = []
        original_out = wd.out
        try:
            wd.out = written.append
            wd.press_any_key(wd.Palette(truecolor=False))
        finally:
            wd.out = original_out
    finally:
        sys.stdin = real_stdin
        os.close(write_fd)

    # The whole 3-byte CSI sequence must be consumed by press_any_key()
    # itself, leaving only the caller's real next keystroke ('b') for
    # whatever reads next -- not leaked into it.
    remaining = os.read(read_fd, 10)
    os.close(read_fd)
    assert remaining == b"b"
