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
import json
import os
import re
import sys
import subprocess
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from queue import Queue, Empty

import pytest
from pathlib import Path

_WAR_DIALER_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "bundled" / "war_dialer.py"
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
CLEAR = "\x1b[2J\x1b[H"
_ANSI_BYTES = re.compile(rb"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([AB0-2]|\x1b[78HDM]")

# The switchboard's own prompt, as a caller reads it: the marker these tests wait
# for to know the screen is drawn and the cursor is waiting. In one place because
# it is presentation, and the presentation is rebuilt (issue #494).
DIAL = "dial \u203a ".encode()


def _plain_with_offsets(raw) -> tuple[bytes, list[int]]:
    """The bytes a caller reads, and where each one sat in the styled stream."""
    raw = bytes(raw)
    plain, offsets, index = bytearray(), [], 0
    while index < len(raw):
        escape = _ANSI_BYTES.match(raw, index)
        if escape:
            index = escape.end()
            continue
        plain.append(raw[index])
        offsets.append(index)
        index += 1
    return bytes(plain), offsets


def _plain(raw) -> bytes:
    """Output with its styling removed, for a substring assertion."""
    return _ANSI_BYTES.sub(b"", bytes(raw))


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


def test_draw_help_never_overflows_its_own_declared_width(monkeypatch):
    monkeypatch.setattr(wd, "read_menu_choice", lambda valid: "B")
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
        original_width = wd._OUTPUT_WIDTH
        try:
            wd._OUTPUT_WIDTH = width
            wd.out = written.append
            wd.draw_help(wd.Palette(truecolor=False), width, height=200)
        finally:
            wd.out = original_out
            wd._OUTPUT_WIDTH = original_width
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


@pytest.mark.parametrize("sequence", [
    b"\x1b[C", b"\x1bOC", b"\x1b[1;5C", b"\x1b[15~",
    b"\x1bc", b"\x1b(B", b"\x1b]0;CCC\x07",
    b"\x1bPCCC\x1b\\", b"\x1b[200~CCCJTRXA\x1b[201~",
    b"CCCJTRXA", b"\x1b",
])
def test_menu_decoder_never_accepts_control_sequences_or_paste(monkeypatch, sequence):
    read_fd, write_fd = os.pipe()
    stdin = _buffered_stdin(read_fd)
    try:
        monkeypatch.setattr(sys, "stdin", stdin)
        os.write(write_fd, sequence)
        assert wd.read_input_key() in ("", wd.ESC)
    finally:
        stdin.close()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("sequence", [
    b"\x1b[", b"\x1bO", b"\x1b[1;", b"\x1b(",
    b"\x1b[200~CCC", b"\x1b]CCC", b"\x1b[M C",
    b"\x1b[" + b"1" * 80, b"C" * 4200,
])
def test_incomplete_or_excessive_input_fails_closed_in_bounded_time(monkeypatch, sequence):
    read_fd, write_fd = os.pipe()
    stdin = _buffered_stdin(read_fd)
    try:
        monkeypatch.setattr(sys, "stdin", stdin)
        # Oversized input can exceed the OS pipe capacity; stream it while
        # the decoder reads instead of blocking the test before it starts.
        writer = threading.Thread(target=lambda: os.write(write_fd, sequence), daemon=True)
        writer.start()
        started = time.monotonic()
        with pytest.raises(wd.InputSequenceError):
            wd.read_input_key()
        assert time.monotonic() - started < 2
        writer.join(timeout=2)
        assert not writer.is_alive()
    finally:
        stdin.close()
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("future_schema", [False, True])
def test_unusable_world_has_readable_exit_without_replacement(tmp_path, future_schema):
    path = tmp_path / "unusable.db"
    if future_schema:
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version={wd.WORLD_SCHEMA_VERSION + 1}")
        conn.close()
    else:
        path.write_bytes(b"Damaged world data; must not be replaced.")
    before = path.read_bytes()
    env = dict(os.environ, WAR_DIALER_DB_PATH=str(path), PYTHONIOENCODING="utf-8")
    env.pop("NETBBS_DOOR_INFO", None)
    result = subprocess.run(
        [sys.executable, "-u", str(_WAR_DIALER_PATH)], input=b"",
        capture_output=True, env=env, timeout=10,
    )
    assert result.returncode == 1
    message = b"not supported" if future_schema else b"storage is unavailable"
    assert message in result.stdout
    assert b"Traceback" not in result.stderr
    assert b"FIRST VISIT" not in result.stdout
    assert path.read_bytes() == before


#: The frame's corners and rules, in both the heavy set the rebuilt screens use
#: (issue #494) and the ASCII substitutes the `plain` preset gets.
_FRAME_EDGES = "┏┗┣┓┛┫╔╚╠+"
_FRAME_FILL = set("━═─-=+| ┏┓┗┛┣┫")


def _rows(screen: str, *, keep_style: bool = False) -> list[str]:
    """The rows a terminal would be showing after `screen` was written.

    A row rewritten in place -- the carrier sweep that plays while a committed
    result comes back (issue #494) -- shows whatever follows its last carriage
    return, and the door pads each frame to a constant width so that is also
    the widest one. Measuring the whole sequence as one row would report a
    width no caller ever saw.
    """
    text = screen if keep_style else _ANSI_RE.sub("", screen)
    return [row.split("\r")[-1] for row in text.rstrip("\r\n").split("\r\n")]


def _last_screen(written) -> str:
    """The screen a caller is looking at, with its styling removed.

    A screen is everything after the last clear. Styling comes off because rows
    and bars are styled segment by segment now, so `[A] Act` is a hotkey in
    amber followed by a label in mint and is not a contiguous run of characters
    (issue #494) -- a stub that drives the door by reading its own output has to
    read what a caller reads.
    """
    raw = written if isinstance(written, str) else "".join(written)
    return _ANSI_RE.sub("", raw.split(CLEAR)[-1])


def _screen_text(written) -> str:
    """What a caller reads, with the door's frame taken off.

    Screens draw inside the frame again (issue #487), so a sentence that wraps
    has a border between its halves: joining the raw rows would look for
    "season 2 has started" in "season 2 has | | started". Works the same on an
    unframed narrow screen, where there is nothing to take off.

    A border row is dropped entirely, including the card heading written into
    it, so a title assertion reads `_screen_titles` instead.
    """
    # A list here is what the door wrote, chunk by chunk, not a list of rows:
    # join it the way `out` did, and let the row split below do the rest.
    raw = written if isinstance(written, str) else "".join(written)
    words: list[str] = []
    for row in _ANSI_RE.sub("", raw).replace("\r\n", "\n").split("\n"):
        row = row.strip()
        if not row or row[0] in _FRAME_EDGES or set(row) <= _FRAME_FILL:
            continue
        words += row.strip("║┃|").split()
    return " ".join(words)


def _screen_titles(written) -> str:
    """The titles and card headings, which live in the frame's own borders."""
    raw = written if isinstance(written, str) else "".join(written)
    rows = []
    for row in _ANSI_RE.sub("", raw).replace("\r\n", "\n").split("\n"):
        row = row.strip()
        if row and row[0] in _FRAME_EDGES:
            rows.append(" ".join(row.strip(_FRAME_EDGES + "━═─-= ▚").split()))
    return " | ".join(rows)


@contextmanager
def _running_door(tmp_path, *, new_player=False, event=False):
    """Actual standalone launch, real input pipe, continuously drained output."""
    path = tmp_path / "process-world.db"
    conn = wd.connect(path)
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    if not new_player:
        wd.load_or_create_player(conn, 0, "Guest", now, 1)
    if event:
        wd.record_event(conn, 0, "Rival", "A retained offline receipt", now)
    conn.close()
    env = dict(os.environ, WAR_DIALER_DB_PATH=str(path), PYTHONIOENCODING="utf-8")
    env.pop("NETBBS_DOOR_INFO", None)
    process = subprocess.Popen(
        [sys.executable, "-u", str(_WAR_DIALER_PATH)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    chunks = Queue()
    output = bytearray()

    def drain():
        while chunk := os.read(process.stdout.fileno(), 4096):
            output.extend(chunk)
            chunks.put(chunk)
        chunks.put(None)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    pending = bytearray()

    def wait_for(marker):
        """Wait for text to reach the screen, ignoring the styling around it.

        Every row and bar is styled segment by segment now, so `[A] Act` is a
        hotkey in amber followed by a label in mint and is not a contiguous run
        of bytes on the wire (issue #494). Matching therefore runs against the
        output with its SGR removed -- which is what a caller actually reads --
        while the raw bytes consumed are still tracked exactly, so a later
        marker in the same chunk is never thrown away.
        """
        deadline = time.monotonic() + 8
        while True:
            plain, offsets = _plain_with_offsets(pending)
            at = plain.find(marker)
            if at >= 0:
                del pending[:offsets[at + len(marker) - 1] + 1]
                return
            try:
                chunk = chunks.get(timeout=max(0.01, deadline - time.monotonic()))
            except Empty:
                pytest.fail(f"Door did not display {marker!r}: {bytes(output)!r}")
            if chunk is None:
                pytest.fail(f"Door exited before {marker!r}: {bytes(output)!r}")
            pending.extend(chunk)

    def send(data):
        process.stdin.write(data)
        process.stdin.flush()

    def screen() -> bytes:
        """What is on the terminal now, with its styling removed."""
        return _plain(bytes(output).split(CLEAR.encode())[-1])

    def reach(marker, *, limit=12):
        """Press [N] until `marker` is on the screen.

        A preview's [A] Act bar appears only on its last page -- reading to the
        end is the contract (issue #282) -- and a rebuilt preview has more pages
        than the sentences it replaced, so a walk turns pages rather than
        assuming the stakes fit on one.
        """
        for _ in range(limit):
            wait_for(b"[B] Back")  # whatever page is showing has this in its bar
            if marker in screen():
                return
            send(b"n")
        pytest.fail(f"never reached {marker!r}: {bytes(output)!r}")

    try:
        yield process, path, wait_for, send, output, reach, screen
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        reader.join(timeout=5)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize("sequence", [
    b"\x1b[C", b"\x1bOC", b"\x1b[1;5C",
    b"\x1b[200~CCCJTRXA\x1b[201~", b"CCCJTRXA", b"\x1b",
])
def test_real_process_main_menu_input_does_not_spend_turns(tmp_path, sequence):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        send(sequence)
        # Deliberately separate the later real key from the input burst. This
        # interval exercises the decoder's timeout, not a process-start guess.
        time.sleep(0.25)
        send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""

        conn = wd.connect(path)
        player = wd.read_player(conn, 0)
        assert (player.cash, player.crew, player.turns_used) == (300, 3, 0)
        conn.close()


@pytest.mark.parametrize("sequence", [b"\x1b[A", b"\x1bOA", b"\x1b[200~A\x1b[201~"])
def test_real_process_target_menu_does_not_select_arrow_or_paste(tmp_path, sequence):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        send(b"x")
        wait_for(b"Cancel")
        send(sequence)
        time.sleep(0.25)
        send(b"q")
        wait_for(DIAL)
        send(b"q")
        assert process.wait(timeout=5) == 0
        conn = wd.connect(path)
        assert wd.read_player(conn, 0).turns_used == 0
        assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
        conn.close()


def test_real_process_incomplete_escape_exits_without_action(tmp_path):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        send(b"\x1b[")
        assert process.wait(timeout=5) == 1
        assert process.stderr.read() == b""
        conn = wd.connect(path)
        assert wd.read_player(conn, 0).turns_used == 0
        conn.close()


@pytest.mark.parametrize("stage", ["onboarding", "receipt", "menu", "recruit", "root"])
def test_real_process_disconnect_preserves_only_committed_actions(tmp_path, stage):
    with _running_door(
        tmp_path, new_player=stage == "onboarding", event=stage == "receipt",
    ) as (process, path, wait_for, send, output, reach, screen):
        if stage in ("onboarding", "receipt"):
            wait_for(b"Press any key to continue...")
        else:
            wait_for(DIAL)
            if stage == "recruit":
                send(b"c")
                wait_for(b"[A] Act [B] Back")
                send(b"a")
                wait_for(b"A new member joins")
            elif stage == "root":
                send(b"x")
                wait_for(b"Cancel")
                send(b"1")
                wait_for(b"[A] Act [B] Back")
                send(b"a")
                wait_for(b"It's yours now.")
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""
        conn = wd.connect(path)
        player = wd.read_player(conn, 0)
        assert player.turns_used == (1 if stage in ("recruit", "root") else 0)
        if stage == "recruit":
            assert (player.cash, player.crew) == (225, 4)
        if stage == "root":
            assert wd.list_exchanges(conn)[0].controller_user_id == 0
        if stage == "receipt":
            assert len(wd.unseen_events(conn, 0)) == 1
        conn.close()


@pytest.mark.parametrize("action", ["trade", "recruit", "job", "raid", "root"])
def test_action_is_durable_before_success_output_fails(tmp_path, monkeypatch, action):
    path = tmp_path / "output-world.db"
    conn = wd.connect(path)
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    player = wd.load_or_create_player(conn, 1, "Alpha", now, 1)
    wd.load_or_create_player(conn, 2, "Beta", now - timedelta(days=3), 1)
    conn.execute("UPDATE players SET cash=1000 WHERE user_id=2")
    conn.execute("UPDATE players SET turns_used=14 WHERE user_id=1")
    monkeypatch.setattr(wd, "read_menu_choice", lambda valid: next(key for key in "1A" if key in valid))
    original_pages = wd.show_text_pages
    monkeypatch.setattr(wd, "show_text_pages", lambda *args, **kwargs: "A" if kwargs.get("accept") else original_pages(*args, **kwargs))
    monkeypatch.setattr(wd, "now_utc", lambda: now)
    observer = wd.connect(path)

    class SuccessRandom:
        def random(self):
            return 0.0

        def randint(self, lo, hi):
            return lo

        def choice(self, choices):
            return choices[0]

    marker = {
        "trade": "You move some warez", "recruit": "A new member",
        "job": "Job:", "raid": "You hit", "root": "You root",
    }[action]

    def fail_at_result(text=""):
        if marker in text:
            assert wd.read_player(observer, 1).turns_used == 15
            if action == "raid":
                assert wd.read_player(observer, 2).cash == 850
                assert len(wd.unseen_events(observer, 2)) == 1
            if action == "root":
                assert wd.list_exchanges(observer)[0].controller_user_id == 1
            raise BrokenPipeError("output disconnected after commit")

    monkeypatch.setattr(wd, "out_line", fail_at_result)
    palette = wd.Palette(False)
    rng = SuccessRandom()
    with pytest.raises(BrokenPipeError, match="after commit"):
        if action == "trade":
            wd.do_trade_warez(palette, conn, player, now, rng)
        elif action == "recruit":
            wd.do_recruit(palette, conn, player, now)
        elif action == "job":
            wd.do_job(palette, conn, player, now, rng)
        elif action == "raid":
            wd.do_raid(palette, conn, player, now, rng, 78)
        else:
            wd.do_root_exchange(palette, conn, player, now, rng, 78)
    assert wd.read_player(observer, 1).turns_used == 15
    observer.close()
    conn.close()


def test_real_process_disconnect_does_not_restore_an_incoming_raid(tmp_path):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        conn = wd.connect(path)
        now = wd.now_utc()
        attacker = wd.load_or_create_player(conn, 1, "Alpha", now, 1)
        conn.execute("UPDATE players SET created_at=? WHERE user_id=0", (wd.to_iso(now - wd.GRACE),))

        class Win:
            def random(self):
                return 0.0

        wd.resolve_raid(conn, attacker, 0, now, Win())
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert conn.execute("SELECT cash FROM players WHERE user_id=0").fetchone()[0] == 255
        assert len(wd.unseen_events(conn, 0)) == 1
        conn.close()


@pytest.mark.parametrize("stage", ["onboarding", "receipt", "menu", "target"])
def test_linux_console_function_key_never_leaks_an_action(tmp_path, stage):
    with _running_door(
        tmp_path, new_player=stage == "onboarding", event=stage == "receipt",
    ) as (process, path, wait_for, send, output, reach, screen):
        if stage in ("onboarding", "receipt"):
            wait_for(b"Press any key to continue...")
        else:
            wait_for(DIAL)
            if stage == "target":
                send(b"x")
                wait_for(b"Cancel")
        send(b"\x1b[[C")  # Linux-console F3, not Crew Recruit / exchange C.
        if stage in ("onboarding", "receipt"):
            wait_for(DIAL)
        time.sleep(0.25)
        conn = wd.connect(path)
        try:
            assert wd.read_player(conn, 0).turns_used == 0
            assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
        finally:
            conn.close()
        send(b"q")
        if stage == "target":
            wait_for(DIAL)
            send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""


@pytest.mark.parametrize("width,height", [(39, 24), (80, 11), (20, 10), (40, 5), (40, 1)])
def test_a_terminal_below_the_floor_is_refused_without_touching_the_world(
    tmp_path, monkeypatch, width, height
):
    """One layout, one floor (issue #495).

    Below 40x12 the door says so and stops, before the world database is
    opened -- and it stops with a *zero* exit, because the supervisor reads
    every nonzero exit as a crash and would tell the caller the door died.
    """
    class Output(io.StringIO):
        def reconfigure(self, **kwargs):
            pass

    out = Output()
    monkeypatch.setattr(wd.sys, "stdout", out)
    monkeypatch.setattr(wd, "_load_door_info", lambda: {
        "user_id": 0, "handle": "Guest", "terminal_width": width, "terminal_height": height})
    monkeypatch.setattr(wd, "_resolve_db_path",
                        lambda: pytest.fail("a refused launch opened the world"))
    assert wd.main() == 0
    raw = _ANSI_RE.sub("", out.getvalue())
    said = " ".join(raw.split())
    assert "needs at least 40 columns by 12 rows" in said or "needs 40x12" in said
    assert f"{width}x{height}" in said, "the caller is told what their terminal reports"
    # The reason has to survive the terminal it is about (issue #495 review).
    assert raw.count("\n") <= max(0, height - 1), f"the refusal scrolls itself off: {raw!r}"


def test_idle_zero_turn_menu_accepts_action_after_refill(tmp_path, monkeypatch):
    path = tmp_path / "idle-world.db"
    now = wd.now_utc()
    conn = wd.connect(path)
    wd.ensure_schema(conn)
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 0, "Guest", now, 1)
    conn.execute("UPDATE players SET turns_used=15, turn_day_start=heat_updated_at")
    conn.close()

    clock = [now]
    choices = iter(("C", "Q"))

    def choose(valid):
        choice = next(choices)
        if choice == "C":
            clock[0] += wd.DAY
        assert choice in valid
        return choice

    class Output(io.StringIO):
        def reconfigure(self, **kwargs):
            pass

    monkeypatch.setattr(wd.sys, "stdout", Output())
    monkeypatch.setattr(wd, "_load_door_info", lambda: {"user_id": 0, "handle": "Guest"})
    monkeypatch.setattr(wd, "_resolve_db_path", lambda: path)
    monkeypatch.setattr(wd, "now_utc", lambda: clock[0])
    monkeypatch.setattr(wd, "read_menu_choice", choose)
    monkeypatch.setattr(wd, "press_any_key", lambda palette: None)
    monkeypatch.setattr(wd, "show_text_pages", lambda *args, **kwargs: "A")
    assert wd.main() == 0
    conn = wd.connect(path)
    player = wd.read_player(conn, 0)
    assert (player.cash, player.crew, player.turns_used) == (225, 4, 1)
    assert player.turn_day_start == wd.to_iso(now + wd.DAY)
    conn.close()


@pytest.mark.parametrize("stage", ["onboarding", "receipt", "menu", "target"])
def test_fragmented_x10_mouse_report_never_spends_a_turn(tmp_path, stage):
    with _running_door(
        tmp_path, new_player=stage == "onboarding", event=stage == "receipt",
    ) as (process, path, wait_for, send, output, reach, screen):
        if stage in ("onboarding", "receipt"):
            wait_for(b"Press any key to continue...")
        else:
            wait_for(DIAL)
            if stage == "target":
                send(b"x")
                wait_for(b"Cancel")
        send(b"\x1b[M")
        for byte in (b" ", b"C", b"C"):
            time.sleep(0.05)  # Beyond burst detection, within sequence lookahead.
            send(byte)
        if stage in ("onboarding", "receipt"):
            wait_for(DIAL)
        time.sleep(0.15)
        conn = wd.connect(path)
        try:
            assert wd.read_player(conn, 0).turns_used == 0
            assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
        finally:
            conn.close()
        send(b"q")
        if stage == "target":
            wait_for(DIAL)
            send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""


@pytest.mark.parametrize("stage", ["onboarding", "receipt", "menu", "target"])
def test_extended_x10_mouse_encoding_stops_without_spending_a_turn(tmp_path, stage):
    with _running_door(
        tmp_path, new_player=stage == "onboarding", event=stage == "receipt",
    ) as (process, path, wait_for, send, output, reach, screen):
        if stage in ("onboarding", "receipt"):
            wait_for(b"Press any key to continue...")
        else:
            wait_for(DIAL)
            if stage == "target":
                send(b"x")
                wait_for(b"Cancel")
        send(b"\x1b[M \xc4\x80C")  # UTF-8 coordinate, then an ASCII coordinate.
        time.sleep(0.25)
        conn = wd.connect(path)
        try:
            assert wd.read_player(conn, 0).turns_used == 0
            assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
        finally:
            conn.close()
        assert process.wait(timeout=5) == 1
        assert b"Unsupported mouse encoding" in output
        assert process.stderr.read() == b""


def test_open_session_can_continue_after_season_refresh(tmp_path, monkeypatch):
    path = tmp_path / 'season-world.db'
    now = wd.now_utc()
    conn = wd.connect(path)
    wd.ensure_schema(conn)
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 0, 'Guest', now, 1)
    wd.load_or_create_player(conn, 1, 'Dormant', now, 1)
    conn.execute('UPDATE players SET cash=50000 WHERE user_id=1')
    conn.close()
    clock = [now]
    choices = iter(('C', 'C', 'Q'))

    def choose(valid):
        choice = next(choices)
        assert choice in valid
        return choice

    class Output(io.StringIO):
        def reconfigure(self, **kwargs):
            pass

    output = Output()
    monkeypatch.setattr(wd.sys, 'stdout', output)
    monkeypatch.setattr(wd, '_load_door_info', lambda: {'user_id': 0, 'handle': 'Guest'})
    monkeypatch.setattr(wd, '_resolve_db_path', lambda: path)
    monkeypatch.setattr(wd, 'now_utc', lambda: clock[0])
    monkeypatch.setattr(wd, 'read_menu_choice', choose)
    monkeypatch.setattr(wd, 'press_any_key', lambda palette: None)
    def accept_preview(*args, **kwargs):
        for text in args[2]:
            wd.out_line(text)
        for _, card in kwargs.get('cards') or []:
            for row in card:
                wd.out_line(row)
        if clock[0] == now:
            clock[0] += wd.SEASON
        return "A"
    monkeypatch.setattr(wd, 'show_text_pages', accept_preview)
    assert wd.main() == 0
    conn = wd.connect(path)
    player = wd.read_player(conn, 0)
    assert (player.season_number, player.cash, player.crew, player.turns_used) == (2, 225, 4, 1)
    assert wd.read_player(conn, 1).cash == wd.STARTING_CASH
    assert 'Season changed.' in _ANSI_RE.sub('', output.getvalue())
    assert 'season 2 has started' in _screen_text(output.getvalue())
    conn.close()


def test_board_read_rolls_world_before_displaying_ownership(tmp_path, monkeypatch):
    conn = wd.connect(tmp_path / 'board-season.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 1, 'OldBoss', now, 1)
    conn.execute('UPDATE exchanges SET controller_user_id=1, garrison=30')
    lines = []
    monkeypatch.setattr(wd, 'out_line', lambda text='': lines.append(text))
    monkeypatch.setattr(wd, 'now_utc', lambda: now + wd.SEASON)
    monkeypatch.setattr(wd, "read_menu_choice", lambda valid: "B")
    wd.show_territory(wd.Palette(False), conn, 78, 24)
    assert wd.read_player(conn, 1).season_number == 2
    assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
    assert 'OldBoss' not in ''.join(lines)
    conn.close()


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
@pytest.mark.parametrize("unseen_only", [False, True])
def test_history_pages_fit_terminal_and_preserve_long_unicode_records(tmp_path, monkeypatch, width, height, unseen_only):
    conn = wd.connect(tmp_path / "history.db")
    wd.ensure_schema(conn)
    summary = "\x1b[31m" + "界e\u0301" * 600 + "\x1b[0m END"
    wd.record_event(conn, 1, None, summary, wd.now_utc())
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", width)
    if unseen_only:
        monkeypatch.setattr(wd, "read_input_key", lambda: " ")
    else:
        def choose(valid):
            if "END" in _last_screen(written):
                return "B"
            return "N"
        monkeypatch.setattr(wd, "read_menu_choice", choose)
    wd.show_event_history(wd.Palette(False), conn, 1, width, height, unseen_only=unseen_only)
    screens = "".join(written).split(CLEAR)[1:-1]
    assert len(screens) > 1
    body = []
    for screen in screens:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
        # The frame is not part of a record: take the borders off the row.
        body.extend(line.strip("║┃ ") for line in lines if "界" in line or "END" in line)
    assert "".join(body).replace(" ", "") == "界e\u0301" * 600 + "END"
    assert len(wd.unseen_events(conn, 1)) == (0 if unseen_only else 1)
    conn.close()


def test_history_ack_skips_partial_records_and_new_arrivals(tmp_path, monkeypatch):
    conn = wd.connect(tmp_path / "history.db")
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.record_event(conn, 1, None, "Long receipt " * 100, now)
    original_id = wd.unseen_events(conn, 1)[0].id
    monkeypatch.setattr(wd, "out", lambda text: None)
    choices = iter(["A", "B"])
    monkeypatch.setattr(wd, "read_menu_choice", lambda valid: next(choices))
    wd.show_event_history(wd.Palette(False), conn, 1, 40, 12)
    assert [e.id for e in wd.unseen_events(conn, 1)] == [original_id]
    conn.execute("DELETE FROM events")
    for index in range(10):
        wd.record_event(conn, 1, None, f"Receipt {index}", now)
    ids = [e.id for e in wd.history_events(conn, 1)]
    choices = iter(["A", "B"])
    def choose(valid):
        key = next(choices)
        if key == "A":
            wd.record_event(conn, 1, None, "Arrived while reading", now)
        return key
    monkeypatch.setattr(wd, "read_menu_choice", choose)
    wd.show_event_history(wd.Palette(False), conn, 1, 40, 12)
    read_ids = [e.id for e in wd.history_events(conn, 1) if e.seen_at]
    # How many records a page holds is a layout decision; what matters is that a
    # page acknowledges a prefix of what it showed, never a partial record and
    # never one that arrived while it was being read.
    assert read_ids and read_ids == ids[:len(read_ids)] and len(read_ids) < len(ids)
    assert wd.history_events(conn, 1)[0].seen_at is None
    conn.close()


def test_real_process_history_is_replayable_and_free(tmp_path):
    with _running_door(tmp_path, event=True) as (process, path, wait_for, send, output, reach, screen):
        wait_for(b"Press any key to continue")
        send(b" ")
        wait_for(DIAL)
        send(b"h")
        wait_for(b"[A] Ack page [B] Back")
        assert b"EVENT LOG" in output and b"READ" in screen()
        send(b"b")
        wait_for(DIAL)
        send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""
        conn = wd.connect(path)
        player = wd.read_player(conn, 0)
        assert (player.cash, player.crew, player.turns_used) == (300, 3, 0)
        assert len(wd.history_events(conn, 0)) == 1
        assert wd.unseen_events(conn, 0) == []
        conn.close()


def test_offline_summary_disconnect_only_acknowledges_completed_pages(tmp_path, monkeypatch):
    conn = wd.connect(tmp_path / "history.db")
    wd.ensure_schema(conn)
    for index in range(10):
        wd.record_event(conn, 1, None, f"Receipt {index}", wd.now_utc())
    ids = [e.id for e in wd.unseen_events(conn, 1)]
    monkeypatch.setattr(wd, "out", lambda text: None)
    calls = 0
    def read():
        nonlocal calls
        calls += 1
        if calls == 1:
            return " "
        raise EOFError
    monkeypatch.setattr(wd, "read_input_key", read)
    with pytest.raises(EOFError):
        wd.show_event_history(wd.Palette(False), conn, 1, 40, 12, unseen_only=True)
    # Only the records the first page showed completely are acknowledged; how
    # many that is belongs to the layout, not to this boundary.
    remaining = [e.id for e in wd.unseen_events(conn, 1)]
    assert remaining and remaining == ids[len(ids) - len(remaining):]
    conn.close()


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_dashboard_pages_fit_with_long_names_and_large_resources(tmp_path, monkeypatch, width, height):
    conn = wd.connect(tmp_path / "dashboard.db")
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    name = "\x1b[31m" + "界e\u0301" * 50
    wd.load_or_create_player(conn, 1, name, now, 1)
    conn.execute("UPDATE players SET cash=999999999999, crew=999999999, crew_recruited_total=1000000")
    state = wd.dashboard_state(conn, 1, now)
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", width)
    index, count = wd.draw_dashboard(wd.Palette(False), state, now, width, height)
    for index in range(1, count):
        wd.draw_dashboard(wd.Palette(False), state, now, width, height, index)
    screens = "".join(written).split(CLEAR)[1:]
    for screen in screens:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    text = _ANSI_RE.sub("", "".join(written))
    assert "999,999,999" in text
    assert "Season end:" in text  # the absolute deadline, on the SEASON card
    assert "top tier" in text  # Already at the highest tier; no next-tier chip.
    conn.close()


def test_dashboard_explains_unstarted_and_running_turn_windows(tmp_path):
    conn = wd.connect(tmp_path / "dashboard.db")
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    player = wd.load_or_create_player(conn, 1, "Owner", now, 1)
    state = wd.dashboard_state(conn, 1, now)
    text = "\n".join(wd.dashboard_lines(state, now))
    assert "Turn window starts with your next action" in text
    # The rank ladder and the protection clock are gauges and chips on the
    # switchboard's first card now, not sentences in the fact list.
    written: list[str] = []
    original_out, wd.out = wd.out, written.append
    try:
        wd.draw_dashboard(wd.Palette(False), state, now, 80, 24)
    finally:
        wd.out = original_out
    card = _screen_text(written)
    assert "rank 0" in card and "next Wannabe" in card
    assert "SHIELD newcomer 2d 0h 0m" in card
    wd.resolve_recruit(conn, player, now)
    later = now + timedelta(hours=1)
    state = wd.dashboard_state(conn, 1, later)
    assert "Turn refill at " in "\n".join(wd.dashboard_lines(state, later))
    written = []
    original_out, wd.out = wd.out, written.append
    try:
        wd.draw_dashboard(wd.Palette(False), state, later, 80, 24)
    finally:
        wd.out = original_out
    card = _screen_text(written)
    assert "TURNS" in card and "14/15" in card
    assert "refill 23h 0m" in card
    assert "rank 10" in card and "next Wannabe" in card
    conn.close()


def test_real_process_dashboard_keeps_action_result_until_acknowledged(tmp_path):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        assert b"SWITCHBOARD" in output and b"NEW" in output and b"0" in output
        send(b"c")
        wait_for(b"[A] Act [B] Back")
        send(b"a")
        wait_for(b"Press any key to continue...")
        after_result = len(output)
        # Acknowledgement is a real input boundary: no automatic dashboard redraw.
        assert b"SWITCHBOARD" not in output[output.index(b"A new member joins"):]
        conn = wd.connect(path)
        assert wd.read_player(conn, 0).turns_used == 1
        conn.close()
        send(b" ")
        wait_for(DIAL)
        assert b"SWITCHBOARD" in output[after_result:]
        send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_garrison_flow_fits_and_commits_only_after_final_preview(tmp_path, monkeypatch, width, height):
    conn = wd.connect(tmp_path / "garrison.db")
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, "Owner", now, 1)
    wd.resolve_root_exchange(conn, actor, 1, now, __import__("random").Random(1))
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(wd, "now_utc", lambda: now)
    def select(valid):
        screen = _last_screen(written)
        # Every picker/preview key is driven by the currently displayed choices.
        assert wd.read_player(conn, actor.user_id).turns_used == 1
        if "[A] Act" in screen:
            assert "A" in valid
            return "A"
        if "1" in valid:
            return "1"
        assert "N" in valid
        return "N"
    monkeypatch.setattr(wd, "read_menu_choice", select)
    monkeypatch.setattr(wd, "read_input_key", lambda: " ")
    assert wd.do_garrison(wd.Palette(False), conn, actor, width, height)
    assert (actor.crew, wd.assigned_crew(conn, actor.user_id), actor.turns_used) == (1, 2, 2)
    for screen in "".join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize("disconnect", [False, True])
def test_real_process_garrison_preview_cancel_and_disconnect_preserve_assignment(tmp_path, disconnect):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        conn = wd.connect(path)
        actor = wd.read_player(conn, 0)
        wd.resolve_root_exchange(conn, actor, 1, wd.now_utc(), __import__("random").Random(1))
        conn.close()
        send(b"g")
        wait_for(b"[B] Back [Q] Cancel")
        send(b"1")
        wait_for(b"[B] Back [Q] Cancel")
        send(b"1")
        wait_for(b"[A] Act [B] Back")
        if disconnect:
            process.stdin.close()
        else:
            send(b"b")
            wait_for(DIAL)
            send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""
        conn = wd.connect(path)
        actor = wd.read_player(conn, 0)
        assert (actor.crew, wd.assigned_crew(conn, 0), actor.turns_used) == (2, 1, 1)
        conn.close()


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_text_screen_pages_preserve_content_with_clear_back_path(monkeypatch, width, height):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", width)
    def choose(valid):
        assert "B" in valid
        screen = _last_screen(written)
        return "B" if "LAST RECORD" in screen else "N"
    monkeypatch.setattr(wd, "read_menu_choice", choose)
    wd.show_text_pages(wd.Palette(False), "RIVAL DIRECTORY", ["界e\u0301" * 200, "LAST RECORD"], width, height)
    screens = "".join(written).split(CLEAR)[1:]
    for screen in screens:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
        assert lines[-1] == "[N] Next [P] Prev [B] Back"
    assert "".join(written).count("界") == 200


@pytest.mark.parametrize("key,heading", [(b"b", b"SEASON STANDINGS"), (b"e", b"THE SCENE"),
                                         (b"v", b"RIVAL DIRECTORY"), (b"?", b"HOW TO PLAY")])
def test_real_process_browsing_screens_are_free_and_do_not_ack_events(tmp_path, key, heading):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        conn = wd.connect(path)
        wd.record_event(conn, 0, "Rival", "Arrived in session", wd.now_utc())
        conn.close()
        send(key)
        wait_for(b"[B] Back")
        assert heading in output
        send(b"b")
        wait_for(DIAL)
        send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""
        conn = wd.connect(path)
        player = wd.read_player(conn, 0)
        assert (player.cash, player.crew, player.turns_used) == (300, 3, 0)
        assert len(wd.unseen_events(conn, 0)) == 1
        conn.close()


@pytest.mark.parametrize("key", [b"t", b"c", b"j", b"x"])
@pytest.mark.parametrize("disconnect", [False, True])
def test_real_process_preview_cancel_or_disconnect_spends_nothing(tmp_path, key, disconnect):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        send(key)
        if key == b"x":
            wait_for(b"Cancel")
            send(b"1")
        if key == b"j":
            wait_for(b"Cancel")
            send(b"1")
            wait_for(b"Cancel")
            send(b"1")
        wait_for(b"[A] Act [B] Back")
        assert b"Cost: 1 turn" in _plain(output)
        if disconnect:
            process.stdin.close()
        else:
            send(b"b")
            wait_for(DIAL)
            send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""
        conn = wd.connect(path)
        player = wd.read_player(conn, 0)
        assert (player.cash, player.crew, player.turns_used) == (300, 3, 0)
        assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
        conn.close()


def test_preview_uses_known_odds_and_explicit_private_uncertainty(tmp_path):
    conn = wd.connect(tmp_path / "preview.db")
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    player = wd.load_or_create_player(conn, 1, "Owner", now, 1)
    rival = wd.load_or_create_player(conn, 2, "Rival", now, 1)
    rival.cash = 1234567
    rival.crew = 23456
    raid = "\n".join(wd.action_preview_lines("raid", player, rival))
    assert "unknown (10%-90%)" in raid
    assert "1234567" not in raid and "23456" not in raid
    exchange = wd.list_exchanges(conn)[0]
    root = "\n".join(wd.action_preview_lines("root", player, exchange))
    assert "Success: 100%" in root
    player.heat = 80
    trade = "\n".join(wd.action_preview_lines("trade", player))
    assert "bust risk 4.0%" in trade
    recruit = "\n".join(wd.action_preview_lines("recruit", player))
    assert "No Heat or bust roll" in recruit
    conn.close()


def test_preview_requires_reading_to_last_page_before_act(monkeypatch):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    states = []
    def choose(valid):
        states.append(valid)
        return "A" if "A" in valid else "N"
    monkeypatch.setattr(wd, "read_menu_choice", choose)
    result = wd.show_text_pages(wd.Palette(False), "PREVIEW", ["Risk details " * 40], 40, 12, accept=True)
    assert result == "A" and len(states) > 1
    assert all("A" not in state for state in states[:-1])
    assert "A" in states[-1]


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_picker_pages_fit_and_only_select_complete_visible_records(monkeypatch, width, height):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", width)
    states = []
    def choose(valid):
        states.append(valid)
        return "2" if "2" in valid else "N"
    monkeypatch.setattr(wd, "read_menu_choice", choose)
    records = [(["Protected crew", "Newcomer shield"], False), (["界e\u0301" * 150, "Eligible"], True)]
    assert wd.pick_record_page(wd.Palette(False), "RAID TARGETS", records, width, height) == "2"
    assert all("1" not in state for state in states)
    assert all("2" not in state for state in states[:-1])
    screens = "".join(written).split(CLEAR)[1:]
    for screen in screens:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    assert "".join(written).count("界") == 150


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_result_pages_keep_all_net_changes_readable(monkeypatch, width, height):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(wd, "read_input_key", lambda: " ")
    delta = wd.ActionDelta(cash=-1234567890123, crew=0, heat=-90, rank=500, turns=1)
    wd.show_action_result(wd.Palette(False), ["You root " + "界e\u0301" * 150], delta, True, width, height)
    screens = "".join(written).split(CLEAR)[1:]
    for screen in screens:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    text = _ANSI_RE.sub("", "".join(written))
    assert text.count("界") == 150
    assert "CREW +0" in text
    assert "turns spent 1" in _screen_text(text)  # the frame sits between the words


def test_raid_picker_can_choose_an_eligible_crew_after_fifty_others(tmp_path, monkeypatch):
    conn = wd.connect(tmp_path / "rivals.db")
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    for user in range(1, 57):
        wd.load_or_create_player(conn, user, f"Crew {user}", now, 1)
    conn.execute("UPDATE players SET created_at=? WHERE user_id=56", (wd.to_iso(now - wd.GRACE),))
    player = wd.read_player(conn, 1)
    monkeypatch.setattr(wd, "out", lambda text: None)
    monkeypatch.setattr(wd, "read_menu_choice", lambda valid: "5" if "5" in valid else "N")
    target = wd.choose_rival(wd.Palette(False), conn, player, 40, 12)
    assert target.user_id == 56
    assert wd.read_player(conn, 1).turns_used == 0
    conn.close()


def test_next_steps_explain_depleted_resources_without_spending(tmp_path):
    conn = wd.connect(tmp_path / "guidance.db")
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 1, "Owner", now, 1)
    conn.execute("UPDATE players SET cash=0, crew=1, heat=90")
    state = wd.dashboard_state(conn, 1, now)
    text = "\n".join(wd.next_steps(state, now))
    assert "Need $75 more" in text
    assert "Trade needs no cash" in text
    assert "one-member floor" in text
    assert "2h 24m" in text
    assert "Recruitment adds no Heat" in text
    assert wd.read_player(conn, 1).turns_used == 0
    state.player.turns_used = 15
    text = "\n".join(wd.next_steps(state, now))
    assert "No turns" in text and "free" in text and "refill" in text
    conn.close()


@pytest.mark.parametrize("key,heading", [(b"r", b"RAID UNAVAILABLE"), (b"x", b"ROOT UNAVAILABLE")])
def test_real_process_no_turns_explains_refill_before_target_selection(tmp_path, key, heading):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        conn = wd.connect(path)
        conn.execute("UPDATE players SET turns_used=15, turn_day_start=heat_updated_at")
        conn.close()
        send(key)
        wait_for(b"[B] Back")
        assert heading in output
        assert b"No turns. Refill at" in output
        send(b"b")
        wait_for(DIAL)
        send(b"q")
        assert process.wait(timeout=5) == 0
        conn = wd.connect(path)
        assert wd.read_player(conn, 0).turns_used == 15
        conn.close()


def test_new_player_gets_short_first_visit_then_switchboard(tmp_path):
    with _running_door(tmp_path, new_player=True) as (process, path, wait_for, send, output, reach, screen):
        wait_for(b"Press any key to continue...")
        assert b"FIRST VISIT" in output
        assert b"[E] Map first" in _plain(output)
        send(b" ")
        wait_for(DIAL)
        assert b"SWITCHBOARD" in output
        send(b"q")
        assert process.wait(timeout=5) == 0


def test_recruit_preview_lists_cash_shortfall_even_when_turns_are_exhausted(tmp_path):
    conn = wd.connect(tmp_path / "both-blockers.db")
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    player = wd.load_or_create_player(conn, 1, "Owner", now, 1)
    player.turns_used = 15
    player.turn_day_start = wd.to_iso(now)
    player.cash = 25
    text = "\n".join(wd.action_preview_lines("recruit", player))
    assert "Refill at" in text
    assert "Need $50 more cash" in text
    conn.close()


@pytest.mark.parametrize("screen", ["raid", "root", "preview"])
def test_target_refresh_announces_rollover_before_cancel(tmp_path, monkeypatch, screen):
    conn = wd.connect(tmp_path / "picker-rollover.db")
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    player = wd.load_or_create_player(conn, 1, "Owner", now, 1)
    monkeypatch.setattr(wd, "now_utc", lambda: now + wd.SEASON)
    monkeypatch.setattr(wd, "read_menu_choice", lambda valid: "B")
    monkeypatch.setattr(wd, "read_input_key", lambda: " ")
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    if screen == "raid":
        assert wd.choose_rival(wd.Palette(False), conn, player, 40, 12) is None
    elif screen == "root":
        assert wd.do_root_exchange(wd.Palette(False), conn, player, now + wd.SEASON, None, 40, 12) is False
    else:
        assert wd.confirm_action(wd.Palette(False), conn, player, "recruit", 40, 12) is False
    output = _screen_text(written)
    assert "season 2 has started" in output
    assert (player.season_number, player.turns_used) == (2, 0)
    conn.close()


def test_disabled_picker_entry_has_no_apparent_digit_hotkey(monkeypatch):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "read_menu_choice", lambda valid: "B")
    wd.pick_record_page(wd.Palette(False), "RAID TARGETS", [(["Protected", "Newcomer shield"], False)], 40, 12)
    output = "".join(written)
    assert "Newcomer shield" in output
    assert "[1]" not in output


@pytest.mark.parametrize("owner", [None, "b" * 32])
def test_bound_world_refuses_guest_or_different_node_process(tmp_path, owner):
    path = tmp_path / "bound.db"
    conn = wd.connect(path)
    wd.ensure_schema(conn)
    wd.bind_world_owner(conn, "a" * 32)
    conn.close()
    info = tmp_path / "info.json"
    import json
    info.write_text(json.dumps({"user_id": 1, "handle": "Other", "war_dialer_owner": owner}), encoding="utf-8")
    env = dict(os.environ, WAR_DIALER_DB_PATH=str(path), NETBBS_DOOR_INFO=str(info), PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-u", str(_WAR_DIALER_PATH)], input=b"", capture_output=True,
                            env=env, timeout=10)
    assert result.returncode == 1
    message = b"launch metadata is invalid" if owner is None else b"belongs to another node"
    assert message in result.stdout
    assert b"Traceback" not in result.stderr
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("metadata", ["missing", "", "[]", "{}", '{"user_id":0,"handle":"Guest"}',
                                       '{"user_id":1,"handle":123}', '{"user_id":1,"handle":"ok","terminal_width":null}',
                                       '{"user_id":9223372036854775808,"handle":"ok"}'])
def test_bad_host_metadata_does_not_create_guest_world(tmp_path, metadata):
    path = tmp_path / "never-created.db"
    info = tmp_path / "info.json"
    if metadata != "missing":
        info.write_text(metadata, encoding="utf-8")
    env = dict(os.environ, WAR_DIALER_DB_PATH=str(path), NETBBS_DOOR_INFO=str(info), PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-u", str(_WAR_DIALER_PATH)], input=b"", capture_output=True,
                            env=env, timeout=10)
    assert result.returncode == 1
    assert b"launch metadata is invalid" in result.stdout
    assert b"Traceback" not in result.stderr
    assert not path.exists()


def test_world_in_maintenance_returns_clear_message_without_player_creation(tmp_path):
    path = tmp_path / "closed.db"
    conn = wd.connect(path)
    conn.execute("INSERT INTO meta (key,value) VALUES ('maintenance','on')")
    conn.close()
    env = dict(os.environ, WAR_DIALER_DB_PATH=str(path), PYTHONIOENCODING="utf-8")
    env.pop("NETBBS_DOOR_INFO", None)
    result = subprocess.run([sys.executable, "-u", str(_WAR_DIALER_PATH)], input=b"", capture_output=True,
                            env=env, timeout=10)
    assert result.returncode == 1
    assert b"closed for SysOp maintenance" in result.stdout
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("owner", [None, "invalid"])
def test_incomplete_host_owner_does_not_initialize_world(tmp_path, owner):
    import json
    path = tmp_path / "must-not-create.db"
    info = tmp_path / "info.json"
    metadata = {"user_id": 1, "handle": "Caller"}
    if owner is not None:
        metadata["war_dialer_owner"] = owner
    info.write_text(json.dumps(metadata), encoding="utf-8")
    env = dict(os.environ, WAR_DIALER_DB_PATH=str(path), NETBBS_DOOR_INFO=str(info), PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-u", str(_WAR_DIALER_PATH)], input=b"", capture_output=True,
                            env=env, timeout=10)
    assert result.returncode == 1
    assert b"launch metadata is invalid" in result.stdout
    assert not path.exists()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
def test_rival_shield_expiry_is_public_and_private_resources_stay_hidden(tmp_path, monkeypatch, width, height):
    conn = wd.connect(tmp_path / 'shield.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 1, 'Viewer', now, 1)
    wd.load_or_create_player(conn, 2, 'Rival', now, 1)
    conn.execute('UPDATE players SET cash=87654321, crew=7654321, created_at=?, raid_shield_until=? WHERE user_id=2',
                 (wd.to_iso(now - wd.GRACE), wd.to_iso(now + wd.RAID_SHIELD)))
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    def choose(valid):
        page = re.search(r'page (\d+)/(\d+)', _last_screen(written))
        return 'N' if page and page.group(1) != page.group(2) else 'B'
    monkeypatch.setattr(wd, 'read_menu_choice', choose)
    wd.show_player_directory(wd.Palette(False), conn, 1, width, height)
    text = _ANSI_RE.sub('', ''.join(written))
    assert 'Raid shield until' in ' '.join(text.split())
    assert (now + wd.RAID_SHIELD).strftime('%Y-%m-%d') in text
    assert '87654321' not in text and '87,654,321' not in text
    assert '7654321' not in text and '7,654,321' not in text
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
@pytest.mark.parametrize('approach', [0, 1, 2])
def test_contract_flow_reaches_chosen_job_and_commits_only_after_preview(tmp_path, monkeypatch, width, height, approach):
    conn = wd.connect(tmp_path / 'contracts.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    calls = 0
    def select(valid):
        nonlocal calls
        calls += 1
        assert calls < 100
        assert (wd.read_player(conn, 1).cash, wd.read_player(conn, 1).turns_used) == (300, 0)
        screen = _last_screen(written)
        if 'CONTRACT BOARD' in screen:
            return '5' if '5' in valid else 'N'
        if 'CHOOSE APPROACH' in screen:
            key = str(approach + 1)
            return key if key in valid else 'N'
        assert 'JOB PREVIEW' in screen
        return 'A' if 'A' in valid else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    monkeypatch.setattr(wd, 'read_input_key', lambda: ' ')
    class SuccessRoll:
        def random(self): return 0
        def randint(self, low, high): return low
    assert wd.do_job(wd.Palette(False), conn, actor, now, SuccessRoll(), width, height)
    terms = wd.job_terms(wd.JobChoice(4, approach))
    assert (actor.cash, actor.heat, actor.turns_used, actor.successful_jobs) == (300 + terms[2][0], terms[4], 1, 1)
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('stage', ['board', 'approach', 'preview'])
def test_contract_browsing_cancel_and_reconnect_preserve_offers_without_random_draws(tmp_path, monkeypatch, stage):
    path = tmp_path / 'offers.db'
    conn = wd.connect(path)
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    conn.execute('UPDATE players SET turns_used=15, turn_day_start=?', (wd.to_iso(now),))
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    class NoDraws:
        def __getattr__(self, name):
            raise AssertionError('Browsing must not draw randomness: ' + name)
    captured = []
    for _ in range(2):
        written = []
        monkeypatch.setattr(wd, 'out', written.append)
        def select(valid):
            screen = _last_screen(written)
            if 'CONTRACT BOARD' in screen:
                return 'B' if stage == 'board' else '1' if '1' in valid else 'N'
            if 'CHOOSE APPROACH' in screen:
                return 'B' if stage == 'approach' else '1' if '1' in valid else 'N'
            assert 'JOB PREVIEW' in screen and 'A' not in valid
            return 'B'
        monkeypatch.setattr(wd, 'read_menu_choice', select)
        before = list(conn.iterdump())
        assert not wd.do_job(wd.Palette(False), conn, actor, now, NoDraws())
        assert list(conn.iterdump()) == before
        captured.append(''.join(written))
        conn.close()
        conn = wd.connect(path)
        actor = wd.read_player(conn, 1)
    assert captured[0] == captured[1]
    conn.close()


@pytest.mark.parametrize('stage', ['board', 'approach'])
def test_real_process_disconnect_from_contract_picker_spends_nothing(tmp_path, stage):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        send(b'j')
        wait_for(b'Cancel')
        if stage == 'approach':
            send(b'1')
            wait_for(b'Cancel')
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b''
        conn = wd.connect(path)
        player = wd.read_player(conn, 0)
        assert (player.cash, player.crew, player.turns_used, player.successful_jobs) == (300, 3, 0, 0)
        conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
@pytest.mark.parametrize('index', range(5))
def test_crew_screen_previews_every_purchase_before_committing(tmp_path, monkeypatch, width, height, index):
    conn = wd.connect(tmp_path / 'crew.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    calls = 0
    def select(valid):
        nonlocal calls
        calls += 1
        assert calls < 100
        assert wd.read_player(conn, 1).cash == 300
        screen = _last_screen(written)
        if 'CREW DEVELOPMENT' in screen:
            key = str(index + 1)
            return key if key in valid else 'N'
        assert 'CREW PREVIEW' in screen
        return 'A' if 'A' in valid else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    monkeypatch.setattr(wd, 'read_input_key', lambda: ' ')
    assert wd.do_crew(wd.Palette(False), conn, actor, width, height)
    item, _, price, _ = wd.CREW_ITEMS[index]
    assert actor.cash == 300 - price and actor.turns_used == 1
    assert item in (actor.specialty, actor.support)
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


def test_crew_purchase_retains_selection_after_stale_preview(tmp_path, monkeypatch):
    conn = wd.connect(tmp_path / 'draft.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    acts = 0
    def select(valid):
        nonlocal acts
        screen = _last_screen(written)
        if 'CREW DEVELOPMENT' in screen: return '1' if '1' in valid else 'N'
        assert 'CREW PREVIEW' in screen
        if 'A' not in valid: return 'N'
        acts += 1
        assert acts <= 2
        if acts == 1: conn.execute('UPDATE players SET cash=299 WHERE user_id=1')
        return 'A'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    monkeypatch.setattr(wd, 'read_input_key', lambda: ' ')
    assert wd.do_crew(wd.Palette(False), conn, actor, 80, 24)
    assert acts == 2 and actor.specialty == 'phreakers'
    assert (actor.cash, actor.turns_used) == (149, 1)
    assert 'selection is retained' in ''.join(written)
    conn.close()


@pytest.mark.parametrize('stage', ['board', 'preview', 'committed'])
def test_real_process_crew_disconnect_boundaries(tmp_path, stage):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        send(b's')
        wait_for(b'Cancel')
        if stage != 'board':
            send(b'1')
            reach(b'[A] Act')
        if stage == 'committed':
            send(b'a')
            wait_for(b'Phreakers ready.')
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b''
        conn = wd.connect(path)
        player = wd.read_player(conn, 0)
        assert (player.cash, player.turns_used, player.specialty) == ((150, 1, 'phreakers') if stage == 'committed' else (300, 0, ''))
        conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
def test_operations_hub_completes_three_previewed_steps_at_compact_sizes(tmp_path, monkeypatch, width, height):
    conn = wd.connect(tmp_path / 'operations.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    monkeypatch.setattr(wd, 'read_input_key', lambda: ' ')
    class Success:
        def random(self): return 0
        def randint(self, lo, hi): return lo
    for phase in range(3):
        calls = 0
        def select(valid):
            nonlocal calls
            calls += 1
            assert calls < 150 and wd.read_player(conn, 1).turns_used == phase
            screen = _last_screen(written)
            if 'PREVIEW' in screen: return 'A' if 'A' in valid else 'N'
            return '1' if '1' in valid else 'N'
        monkeypatch.setattr(wd, 'read_menu_choice', select)
        assert wd.do_operations_hub(wd.Palette(False), conn, actor, Success(), width, height)
    assert (actor.operation_stage, actor.successful_operations, actor.cash, actor.turns_used) == (0, 1, 334, 3)
    text = _ANSI_RE.sub('', ''.join(written))
    assert all(title in text for title in ('CASE PREVIEW', 'PREPARE PREVIEW', 'EXECUTE PREVIEW'))
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
def test_recon_reaches_rival_beyond_fifty_without_disclosing_resources_before_act(tmp_path, monkeypatch, width, height):
    conn = wd.connect(tmp_path / 'recon.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    for uid in range(2, 63): wd.load_or_create_player(conn, uid, 'Rival' + str(uid), now, 1)
    conn.execute('UPDATE players SET cash=87654321, crew=7654321 WHERE user_id=62')
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    monkeypatch.setattr(wd, 'read_input_key', lambda: ' ')
    calls = 0
    def select(valid):
        nonlocal calls
        calls += 1
        assert calls < 1000 and wd.read_player(conn, 1).turns_used == 0
        assert conn.execute('SELECT COUNT(*) FROM recon').fetchone()[0] == 0
        text = ''.join(written)
        assert '87,654,321' not in text and '7,654,321' not in text
        screen = _last_screen(text)
        if 'RECON PREVIEW' in screen: return 'A' if 'A' in valid else 'N'
        return '1' if 'Rival62' in text and '1' in valid else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    assert wd.do_recon(wd.Palette(False), conn, actor, width, height)
    dossiers = wd.read_dossiers(conn, 1, now)
    assert len(dossiers) == 1 and dossiers[0]['target'] == 62
    assert 'Last-known' in _ANSI_RE.sub('', ''.join(written))
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('keys,marker,stage,turns', [
    ([b'o'], b'OPERATIONS / RECON', 0, 0),
    ([b'o', b'1', b'1', b'1'], b'CASE PREVIEW', 0, 0),
    ([b'o', b'1', b'1', b'1', b'a'], b'Casing saved.', 1, 1),
    ([b'o', b'2', b'1'], b'RECON PREVIEW', 0, 0),
])
def test_real_process_recon_operation_disconnect_boundaries(tmp_path, keys, marker, stage, turns):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        conn = wd.connect(path)
        now = wd.now_utc()
        wd.load_or_create_player(conn, 2, 'Rival', now, 1)
        conn.close()
        for index, key in enumerate(keys):
            send(key)
            if index == len(keys) - 1:
                wait_for(marker)
            elif keys[index + 1] == b'a':
                reach(b'[A] Act')
            else:
                wait_for(b'Cancel')
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b''
        conn = wd.connect(path)
        player = wd.read_player(conn, 0)
        assert (player.operation_stage, player.turns_used, player.cash) == (stage, turns, 300)
        assert conn.execute('SELECT COUNT(*) FROM recon').fetchone()[0] == 0
        conn.close()


def test_operation_inspection_and_abandon_are_free_with_no_turns(tmp_path, monkeypatch):
    conn = wd.connect(tmp_path / 'free-operation.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    conn.execute('UPDATE players SET operation_contract=4, operation_approach=2, operation_stage=2, turns_used=15, turn_day_start=?', (wd.to_iso(now),))
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    def cancel(valid):
        screen = _last_screen(written)
        if 'ACTIVE OPERATION' in screen: return '1' if '1' in valid else 'N'
        assert 'EXECUTE PREVIEW' in screen and 'A' not in valid
        return 'B'
    monkeypatch.setattr(wd, 'read_menu_choice', cancel)
    class NoDraws:
        def __getattr__(self, name): raise AssertionError(name)
    before = list(conn.iterdump())
    assert not wd.do_operation(wd.Palette(False), conn, actor, NoDraws(), 80, 24)
    assert list(conn.iterdump()) == before
    def abandon(valid):
        screen = _last_screen(written)
        if 'ACTIVE OPERATION' in screen: return '2' if '2' in valid else 'N'
        return 'A' if 'A' in valid else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', abandon)
    monkeypatch.setattr(wd, 'read_input_key', lambda: ' ')
    assert wd.do_operation(wd.Palette(False), conn, actor, NoDraws(), 80, 24)
    assert actor.operation_stage == 0 and actor.turns_used == 15 and actor.cash == 300
    conn.close()


@pytest.mark.parametrize('stage,used,cash,expected', [(0, 0, 300, '3 turns and $50'), (1, 3, 30, '2 turns and $50'), (1, 14, 30, '2 turns and $50'), (2, 14, 0, '1 turn and $0'), (2, 15, 0, '1 turn and $0')])
def test_saved_operation_advice_names_remaining_budget_and_preserves_progress(tmp_path, stage, used, cash, expected):
    conn = wd.connect(tmp_path / 'visit.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    conn.execute('UPDATE players SET operation_contract=0, operation_approach=0, operation_stage=?, turns_used=?, cash=?, turn_day_start=?', (stage, used, cash, wd.to_iso(now)))
    state = wd.dashboard_state(conn, 1, now)
    before = list(conn.iterdump())
    advice = ' '.join(wd.next_steps(state, now))
    assert expected in advice and '[O] Ops' in advice
    if used == 15: assert 'No turns' in advice and 'Progress waits safely' in advice
    if stage == 1: assert 'Need $20 more before Prepare' in advice and 'Progress waits safely' in advice
    assert list(conn.iterdump()) == before
    conn.close()



def test_operations_hub_does_not_advertise_an_inactive_ops_hotkey(tmp_path, monkeypatch):
    conn = wd.connect(tmp_path / 'hub-key.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    output = []
    monkeypatch.setattr(wd, 'out', output.append)
    monkeypatch.setattr(wd, 'read_menu_choice', lambda valid: 'B')
    assert not wd.do_operations_hub(wd.Palette(False), conn, actor, None, 80, 24)
    assert '[O] Ops' not in _ANSI_RE.sub('', ''.join(output))
    assert '3 turns and $50' in _ANSI_RE.sub('', ''.join(output))
    conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
@pytest.mark.parametrize('role', ['pbx', 'carrier', 'hub'])
def test_owner_services_reachable_through_garrison_at_compact_sizes(tmp_path, monkeypatch, width, height, role):
    conn = wd.connect(tmp_path / 'services.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Owner', now, 1)
    exchange = next(e for e in wd.list_exchanges(conn) if e.role == role)
    conn.execute('UPDATE exchanges SET controller_user_id=1, garrison=1, controlled_since=? WHERE id=?', (wd.to_iso(now), exchange.id))
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, 'read_input_key', lambda: ' ')
    stage, calls = 0, 0
    service_key = wd.PICK_KEYS[len(wd.garrison_options(actor, next(e for e in wd.list_exchanges(conn) if e.id == exchange.id)))]
    def select(valid):
        nonlocal stage, calls
        calls += 1
        assert calls < 200 and wd.read_player(conn, 1).turns_used == 0
        key = '1' if stage == 0 else service_key if stage == 1 else 'A'
        if key in valid:
            stage += 1
            return key
        assert 'N' in valid
        return 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    assert wd.do_garrison(wd.Palette(False), conn, actor, width, height, rng=__import__('random').Random(1))
    assert actor.turns_used == 1 and stage == 3
    assert (actor.cash == 235) if role == 'carrier' else (actor.cash >= 300)
    assert wd.assigned_crew(conn, 1) == 1
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('commit', [False, True])
def test_real_process_owner_service_cancel_or_commit(tmp_path, commit):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        conn = wd.connect(path)
        conn.execute('UPDATE exchanges SET controller_user_id=0, garrison=1, controlled_since=? WHERE id=1', (wd.to_iso(wd.now_utc()),))
        actor = wd.read_player(conn, 0)
        exchange = wd.list_exchanges(conn)[0]
        service_key = wd.PICK_KEYS[len(wd.garrison_options(actor, exchange))].encode()
        conn.close()
        send(b'g')
        wait_for(b'Cancel')
        send(b'1')
        wait_for(b'Cancel')
        send(service_key)
        reach(b'[A] Act')
        send(b'a' if commit else b'b')
        if commit:
            wait_for(b'Carrier recruitment:')
            send(b' ')
        wait_for(DIAL)
        send(b'q')
        assert process.wait(timeout=5) == 0 and process.stderr.read() == b''
        conn = wd.connect(path)
        actor = wd.read_player(conn, 0)
        assert (actor.cash, actor.crew, actor.turns_used) == ((235, 4, 1) if commit else (300, 3, 0))
        conn.close()


def test_exchange_map_shows_actual_ring_security_services_and_viewer_price(tmp_path, monkeypatch):
    conn = wd.connect(tmp_path / 'map.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 1, 'Owner', now, 1)
    conn.execute('UPDATE exchanges SET controller_user_id=1, garrison=1, controlled_since=? WHERE id=10', (wd.to_iso(now),))
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', 80)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    # Inspect every exchange in turn: the ring and the table carry ownership,
    # defence and income, and each exchange's own card carries its links, its
    # role's service and the price this viewer would pay (issue #494).
    wanted, calls = list(wd.PICK_KEYS), 0
    def select(valid):
        nonlocal calls
        calls += 1
        assert calls < 300
        page = re.search(r'page (\d+)/(\d+)', _last_screen(written))
        if page and page.group(1) != page.group(2):
            return 'N'
        if wanted and wanted[0] in valid:
            return wanted.pop(0)
        return 'B'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    wd.show_territory(wd.Palette(False), conn, 80, 24, viewer_id=1)
    rows = [row for screen in ''.join(written).split(CLEAR)[1:] for row in _rows(screen)]
    text = ' '.join(' '.join(' '.join(rows).split('┃')).split())
    # Node 1 neighbours node 10, which this viewer holds, so its Warez Hub
    # capture is discounted from $75 to $65 and its ring links name both sides.
    assert 'links #10, #2' in text and 'capture $40' in text and 'base $50' in text
    assert 'neighbour discount $10' in text
    assert 'security +2' in text and 'total 3' in text
    assert all(role in text for role in ('⟦PUBLIC PBX⟧', '⟦CARRIER SWITCH⟧',
                                         '⟦WAREZ HUB⟧', 'Lay Low', 'Recruit:',
                                         'Warez outlet'))
    conn.close()



def test_small_cash_balance_can_reach_affordable_pbx_capture(tmp_path, monkeypatch):
    conn = wd.connect(tmp_path / 'cheap-capture.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    conn.execute('UPDATE players SET cash=25 WHERE user_id=1')
    target_index = next(i for i, e in enumerate(wd.list_exchanges(conn)) if e.role == 'pbx')
    target_key = wd.PICK_KEYS[target_index]
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', 80)
    monkeypatch.setattr(wd, 'read_input_key', lambda: ' ')
    monkeypatch.setattr(wd, 'out', lambda text: None)
    selected, calls = False, 0
    def select(valid):
        nonlocal selected, calls
        calls += 1
        assert calls < 100 and wd.read_player(conn, 1).turns_used == 0
        if selected: return 'A' if 'A' in valid else 'N'
        if target_key in valid:
            selected = True
            return target_key
        assert 'N' in valid
        return 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    assert wd.do_root_exchange(wd.Palette(False), conn, actor, now, __import__('random').Random(1), 80, 24)
    assert (actor.cash, actor.turns_used, actor.crew, actor.heat) == (0, 1, 2, 4)
    conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
def test_neutral_map_paginates_names_defense_and_return_deadline(tmp_path, monkeypatch, width, height):
    conn = wd.connect(tmp_path / 'npc-map.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    deadline = wd.to_iso(now + wd.DAY)
    conn.execute("UPDATE exchanges SET npc_key='', garrison=0, controlled_since=NULL, npc_return_at=? WHERE id=5", (deadline,))
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    # The ring and the table name every exchange; an operator's biography, its
    # return deadline and its defence live on the exchange's own card, which is
    # the digit in its first column away (issue #494). The walk opens each of
    # the three NPC homes, turning every page of each.
    wanted, calls = ['5', '6', '7'], 0
    def select(valid):
        nonlocal calls
        calls += 1
        assert calls < 300 and 'B' in valid
        page = re.search(r'page (\d+)/(\d+)', _last_screen(written))
        if page and page.group(1) != page.group(2):
            return 'N'
        if wanted and wanted[0] in valid:
            return wanted.pop(0)
        return 'B'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    wd.show_territory(wd.Palette(False), conn, width, height, viewer_id=1)
    body = []
    for screen in ''.join(written).split(CLEAR)[1:]:
        body.extend(_rows(screen)[1:-1])
    normalized = ' '.join(' '.join(' '.join(body).split('┃')).split())
    assert 'Returns if still unclaimed at' in normalized
    assert 'Night Relay Union' in normalized and 'Spool Archive Collective' in normalized
    assert wd.read_player(conn, 1).turns_used == 0
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('stage', ['cancel', 'disconnect', 'committed'])
def test_real_process_neutral_capture_preview_boundaries(tmp_path, stage):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        send(b'x')
        for _ in range(10):
            wait_for(b'Cancel')
            # The digit has to be in the hint, not only in an entry's marker:
            # an entry whose last row is on the next page is not selectable.
            if b'[5]' in screen().split(b'pick ')[-1]: break
            send(b'n')
        else: pytest.fail('NPC home never became selectable')
        send(b'5')
        reach(b'[A] Act')
        assert b'Patch Panel Society' in output and b'Success: 60%' in _plain(output)
        if stage == 'cancel':
            send(b'b')
            wait_for(DIAL)
            send(b'q')
        else:
            if stage == 'committed':
                send(b'a')
                wait_for(b'RESULT')
            process.stdin.close()
        assert process.wait(timeout=5) == 0 and process.stderr.read() == b''
        conn = wd.connect(path)
        actor, exchange = wd.read_player(conn, 0), wd.list_exchanges(conn)[4]
        assert (actor.cash, actor.turns_used) == ((275, 1) if stage == 'committed' else (300, 0))
        assert (exchange.controller_user_id == 0) != bool(exchange.npc_key)
        assert conn.execute('SELECT COUNT(*) FROM players').fetchone()[0] == 1
        conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
@pytest.mark.parametrize('entry', ['1', '2', '3'])
def test_scene_and_insignia_are_free_and_fit_small_terminals(tmp_path, monkeypatch, width, height, entry):
    conn = wd.connect(tmp_path / 'scene.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    conn.execute('UPDATE players SET cash=0,turns_used=15,turn_day_start=? WHERE user_id=1', (wd.to_iso(now),))
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    monkeypatch.setattr(wd, 'read_input_key', lambda: ' ')
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    stage, calls = 0, 0
    def select(valid):
        nonlocal stage, calls
        calls += 1
        assert calls < 150
        if stage == 0:
            if entry in valid:
                stage += 1
                return entry
            return 'N'
        if entry == '1':
            key = '4' if stage == 1 else 'A'
            if key in valid:
                stage += 1
                return key
            return 'N'
        screen = _last_screen(written)
        page = re.search(r'page (\d+)/(\d+)', screen)
        # A single-page screen has no counter at all now: there is nothing to turn.
        return 'B' if page is None or page.group(1) == page.group(2) else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    wd.do_scene(wd.Palette(False), conn, actor, width, height)
    assert (actor.cash, actor.turns_used, actor.crew, actor.heat) == (0, 15, 3, 0)
    assert actor.insignia == ('archive' if entry == '1' else 'modem')
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('stage', ['scene', 'preview', 'committed'])
def test_real_process_scene_disconnect_preserves_only_selected_insignia(tmp_path, stage):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        send(b'i')
        wait_for(b'Cancel')
        assert b'BBS SCENE' in output
        if stage != 'scene':
            send(b'1')
            wait_for(b'Cancel')
            send(b'4')
            reach(b'[A] Act')
        if stage == 'committed':
            send(b'a')
            wait_for(b'Archive insignia selected.')
        process.stdin.close()
        assert process.wait(timeout=5) == 0 and process.stderr.read() == b''
        conn = wd.connect(path)
        actor = wd.read_player(conn, 0)
        assert actor.insignia == ('archive' if stage == 'committed' else 'modem')
        assert (actor.cash, actor.turns_used, actor.crew) == (300, 0, 3)
        conn.close()


@pytest.mark.parametrize('population', [1, 3, 80])
@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
def test_complete_visit_stays_productive_without_pvp_at_every_world_size(tmp_path, monkeypatch, population, width, height):
    conn = wd.connect(tmp_path / 'world-size.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    for uid in range(1, population + 1):
        wd.load_or_create_player(conn, uid, 'Caller' + str(uid), now, 1)
    actor = wd.read_player(conn, 1)
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    monkeypatch.setattr(wd, 'read_input_key', lambda: ' ')
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    # Newcomer rivals are protected; the visit must never require a raid.
    monkeypatch.setattr(wd, 'read_menu_choice', lambda valid: 'B')
    assert wd.choose_rival(wd.Palette(False), conn, actor, width, height) is None
    if population == 1:
        # At minimum width guidance can span pages; inspect its content directly.
        lines = []
        with monkeypatch.context() as scoped:
            scoped.setattr(wd, 'show_text_pages', lambda p, title, content, *args, **kw: lines.extend(content))
            assert wd.choose_rival(wd.Palette(False), conn, actor, width, height) is None
        assert '[J] Jobs and [O] Operations need no rival' in ' '.join(lines)
        guidance = ' '.join(lines)
        assert guidance.index('[B] Back to the switchboard') < guidance.index('[J] Jobs')
    class Success:
        def random(self): return .01
        def randint(self, low, high): return low
    for step in range(3):
        calls = 0
        def select(valid):
            nonlocal calls
            calls += 1
            assert calls < 100 and wd.read_player(conn, 1).turns_used == step
            if 'A' in valid: return 'A'
            return '1' if '1' in valid else 'N'
        monkeypatch.setattr(wd, 'read_menu_choice', select)
        assert wd.do_operations_hub(wd.Palette(False), conn, actor, Success(), width, height)
    assert (actor.turns_used, actor.successful_operations, wd.rank_score(actor), actor.cash) == (3, 1, 30, 334)
    for _ in range(12): wd.resolve_trade_warez(conn, actor, now, Success())
    assert actor.turns_used == 15
    before = list(conn.iterdump())
    monkeypatch.setattr(wd, 'read_menu_choice', lambda valid: 'B')
    wd.do_scene(wd.Palette(False), conn, actor, width, height)
    wd.show_territory(wd.Palette(False), conn, width, height, viewer_id=1)
    wd.show_player_directory(wd.Palette(False), conn, 1, width, height, standings=True)
    assert list(conn.iterdump()) == before
    standings = wd.read_player_page(conn, 1, now, offset=70, standings=True)
    assert standings.total == population
    assert all(p.handle.startswith('Caller') for p in standings.entries)
    if population == 80: assert standings.entries[-1].user_id == 80
    assert conn.execute('SELECT SUM(successful_raids) FROM players').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM players').fetchone()[0] == population
    assert len(wd.read_scene(conn)) == 3 and all(r['kind'] == 'neutral' for r in wd.read_scene(conn))
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
def test_season_rules_and_archived_results_are_readable_and_free(tmp_path, monkeypatch, width, height):
    conn = wd.connect(tmp_path / 'season-view.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'HistoricalCaller', now, 1)
    wd.resolve_recruit(conn, actor, now)
    state = wd.dashboard_state(conn, 1, now)
    assert wd.SEASON_AWARDS in wd.dashboard_lines(state, now)
    assert any('Season end:' in line for line in wd.dashboard_lines(state, now))
    later = now + wd.SEASON * 3
    actor = wd.load_or_create_player(conn, 1, 'RenamedCaller', later, 4)
    monkeypatch.setattr(wd, 'now_utc', lambda: later)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    selected, calls = False, 0
    def select(valid):
        nonlocal selected, calls
        calls += 1
        assert calls < 150
        if not selected:
            if '4' in valid:
                selected = True
                return '4'
            return 'N'
        screen = _last_screen(written)
        page = re.search(r'page (\d+)/(\d+)', screen)
        # A single-page screen has no counter at all now: there is nothing to turn.
        return 'B' if page is None or page.group(1) == page.group(2) else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    before = list(conn.iterdump())
    wd.do_scene(wd.Palette(False), conn, actor, width, height)
    assert list(conn.iterdump()) == before
    body = []
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
        if 'SEASON RESULTS' in lines[0]: body.extend(lines[1:-1])
    normalized = ' '.join(' '.join(body).split())
    # A medal is a badge beside the handle that earned it, not a sentence.
    assert '⟦GOLD⟧ HistoricalCaller' in normalized and 'rank 10' in normalized
    assert 'inactive' in normalized and 'your result #1' in normalized
    conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
@pytest.mark.parametrize('choice', ['5', '6'])
@pytest.mark.parametrize('played', [False, True])
def test_season_recognition_from_scene_is_historical_free_and_bounded(tmp_path, monkeypatch, width, height, choice, played):
    conn = wd.connect(tmp_path / 'recognition.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    actor = wd.load_or_create_player(conn, 1, 'HistoricalCaller', now, 1)
    if played:
        wd.set_insignia(conn, actor, 'archive', now)
        wd.resolve_recruit(conn, actor, now)
        now += wd.SEASON
        actor = wd.load_or_create_player(conn, 1, 'RenamedCaller', now, 2)
    monkeypatch.setattr(wd, 'now_utc', lambda: now)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    selected, calls = False, 0
    def select(valid):
        nonlocal selected, calls
        calls += 1
        assert calls < 150
        if not selected:
            if choice in valid:
                selected = True
                return choice
            return 'N'
        screen = _last_screen(written)
        page = re.search(r'page (\d+)/(\d+)', screen)
        # A single-page screen has no counter at all now: there is nothing to turn.
        return 'B' if page is None or page.group(1) == page.group(2) else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    before = list(conn.iterdump())
    wd.do_scene(wd.Palette(False), conn, actor, width, height)
    assert list(conn.iterdump()) == before
    body = []
    title = 'YOUR SEASON REPORTS' if choice == '5' else 'HALL OF FAME'
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
        # Past the title and the counter, and stopping where the bar starts:
        # a sentence that runs onto the next page must not have the bar
        # spliced into the middle of it. `_screen_text` drops the frame.
        if title in lines[0]:
            rows = lines[1:]
            bar = next((index for index, row in enumerate(rows)
                        if row.strip().startswith(("Press any key", "[N] Next", "[B] Back"))), len(rows))
            body.append(_screen_text("\r\n".join(rows[:bar])))
    normalized = ' '.join(' '.join(body).split())
    if played:
        assert '⟦{##}⟧ HistoricalCaller' in normalized and 'RenamedCaller' not in normalized
        assert '⟦GOLD⟧' in normalized and 'rank 10' in normalized and 'place #1 of 1' in normalized
        if choice == '5':
            assert 'Gold 1' in normalized and 'Silver 0' in normalized
            assert 'best rank 10' in normalized
    else:
        assert ('No completed-season result' if choice == '5' else 'No medals awarded') in normalized
    conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
def test_late_join_and_fresh_season_dashboard_explains_actual_reset(tmp_path, monkeypatch, width, height):
    conn = wd.connect(tmp_path / 'late-join.db')
    wd.ensure_schema(conn)
    start = wd.now_utc()
    wd.get_or_create_season_anchor(conn, start)
    wd.ensure_exchanges_seeded(conn, 1, start)
    late = start + wd.SEASON - wd.DAY
    actor = wd.load_or_create_player(conn, 1, 'LateCaller', late, 1)
    identity_age = actor.created_at
    state = wd.dashboard_state(conn, 1, late)
    text = ' '.join(wd.dashboard_lines(state, late))
    assert 'Joining late?' in text and 'Cautious' in text
    assert 'even without a medal' in text and 'all saved operation progress (cased or prepared)' in text
    assert 'All competitive progress and resources reset' in text and 'assigned crew, exchanges, Rank' in text
    wd.resolve_recruit(conn, actor, late)
    after = start + wd.SEASON
    state = wd.dashboard_state(conn, 1, after)
    assert (state.player.cash, state.player.crew, state.player.turns_used) == (300, 3, 0)
    assert state.player.created_at == identity_age
    assert wd.is_in_grace(state.player, after)
    assert not wd.is_in_grace(state.player, after + wd.DAY)
    assert conn.execute('SELECT rank,medal FROM season_results WHERE user_id=1').fetchone()['rank'] == 10
    text = ' '.join(wd.dashboard_lines(state, after))
    assert 'Ready to play: $300, 3 available crew and 15 turns' in text
    assert 'medals give no resource or protection bonus' in text
    wd.load_or_create_player(conn, 2, 'FirstVisitInSeasonTwo', after, 2)
    first_visit = ' '.join(wd.dashboard_lines(wd.dashboard_state(conn, 2, after), after))
    assert 'any retained results' in first_visit and 'Past results remain' not in first_visit
    assert conn.execute('SELECT COUNT(*) FROM season_results WHERE user_id=2').fetchone()[0] == 0
    conn.execute('UPDATE players SET cash=328, crew=2 WHERE user_id=2')
    class TradeRandom:
        def randint(self, low, high): return low
        def random(self): return .99
    trading = wd.read_player(conn, 2)
    wd.resolve_trade_warez(conn, trading, after, TradeRandom())
    assert trading.turns_used == 1 and wd.rank_score(trading) == 0
    refill = ' '.join(wd.dashboard_lines(wd.dashboard_state(conn, 2, after + wd.DAY), after + wd.DAY))
    assert f'Ready to play: ${trading.cash}, 2 available crew and 15 turns' in refill
    assert 'Ready to play: $300' not in refill
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    before = list(conn.iterdump())
    for stamp in (late, after):
        # Refresh only forward; the saved late state is used for its own display.
        shown = state if stamp == after else wd.DashboardState(actor, [], 0, after, None)
        page_start = len(written)
        _, count = wd.draw_dashboard(wd.Palette(False), shown, stamp, width, height)
        if stamp == late:
            assert 'Season reset in' in _ANSI_RE.sub('', ''.join(written[page_start:]))
        for page in range(1, count): wd.draw_dashboard(wd.Palette(False), shown, stamp, width, height, page)
    assert list(conn.iterdump()) == before
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('width,height', [(40, 12), (80, 24)])
@pytest.mark.parametrize('setting', ['1', '2', '3'])
def test_display_toggles_are_free_paginated_and_survive_seasons(tmp_path, monkeypatch, width, height, setting):
    conn = wd.connect(tmp_path / 'display.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    actor = wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    monkeypatch.setattr(wd, '_ASCII_DECOR', False)
    monkeypatch.setattr(wd, '_MONOCHROME', False)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    selected, calls = False, 0
    def select(valid):
        nonlocal selected, calls
        calls += 1
        assert calls < 60
        screen = ' '.join(_ANSI_RE.sub('', ''.join(written).split(CLEAR)[-1]).split())
        for digit, label in zip('123', ('ASCII decorations', 'Monochrome', 'Fast mode')):
            if digit in valid:
                assert label in screen
        if selected: return 'B'
        if setting in valid:
            selected = True
            return setting
        return 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    palette = wd.Palette(False)
    wd.do_display(palette, conn, 1, width, height)
    key = wd.DISPLAY_KEYS[int(setting) - 1]
    assert wd.read_display(conn, 1) == {key: True}
    assert wd.read_display(conn, 2) == {}
    assert wd.read_player(conn, 1) == actor
    assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
    before = list(conn.iterdump())
    wd.do_display(palette, conn, 1, width, height)
    assert list(conn.iterdump()) == before
    wd.settle_world(conn, now + wd.SEASON)
    assert wd.read_display(conn, 1) == {key: True}
    for screen in ''.join(written).split(CLEAR)[1:]:
        lines = _rows(screen)
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


def test_ascii_monochrome_output_preserves_controls_names_and_noncolor_results(monkeypatch, capsys):
    monkeypatch.setattr(wd, '_ASCII_DECOR', True)
    monkeypatch.setattr(wd, '_MONOCHROME', True)
    palette = wd.Palette(True)
    palette.monochrome = True
    # Glyphs are substituted where a row is built, not by translating a finished
    # one (issue #494): `gl()` is the only spelling a screen ever prints, so no
    # preset can miss a glyph -- and a caller's own name, which may itself
    # contain a box-drawing character, is never rewritten.
    frame = "".join(wd.gl(name) for name in ('tl', 'h', 'v', 'tr'))
    wd.out('\x1b[2J' + wd.sty(palette.phosphor, frame) + ' Caller Jos\u00e9 A\u2502B: +10 Rank')
    assert capsys.readouterr().out == '\x1b[2J+-|+ Caller Jos\u00e9 A\u2502B: +10 Rank'
    assert palette.phosphor == ''  # monochrome removes colour at the source


def test_fast_mode_skips_only_optional_flavor_and_art(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, '_ASCII_DECOR', False)
    monkeypatch.setattr(wd, '_MONOCHROME', False)
    rendered = []
    def capture(p, title, lines, *a, **k):
        # A rebuilt screen hands over styled cards, not paragraphs (issue #494).
        rows = [row for _, card in k.get('cards') or [('', lines)] for row in card]
        rendered.append((title, [_ANSI_RE.sub('', row) for row in rows]))
    monkeypatch.setattr(wd, 'show_text_pages', capture)
    palette = wd.Palette(False)
    delta = wd.ActionDelta(cash=-25, crew=-1, heat=4, rank=0, turns=1)
    wd.show_action_result(palette, ['Attempt failed.'], delta, True, 40, 12)
    normal = rendered[-1][1]
    palette.fast = True
    wd.show_action_result(palette, ['Attempt failed.'], delta, True, 40, 12)
    fast = rendered[-1][1]
    flavour = 'Sirens cut through the carrier tone.'
    assert flavour in ' '.join(normal) and flavour not in ' '.join(fast)
    assert any('BUSTED' in line for line in fast)
    # Every stake and net change survives; only the flavour and the frame go.
    for figure in ('turns spent 1', '-$25', 'CREW -1', 'HEAT +4.0', 'RANK +0'):
        assert figure in ' '.join(fast), figure
    conn = wd.connect(tmp_path / 'art.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    # The neutral-operator dossiers keep the only static ASCII art the door still
    # draws: exchange roles are badges on the ring's own table now, and the ring
    # is the screen's content, not optional flavour (issue #494).
    actor = wd.read_player(conn, 1)
    for fast in (False, True):
        palette.fast = fast
        rendered.clear()
        # The dossiers entry may sit on a later page of the scene picker at forty
        # columns; turn pages until the digit it is keyed to is selectable.
        monkeypatch.setattr(wd, 'read_menu_choice',
                            lambda valid: '2' if '2' in valid else 'N' if 'N' in valid else 'B')
        wd.do_scene(palette, conn, actor, 40, 12)
        dossiers = next(rows for title, rows in rendered if title == 'NEUTRAL DOSSIERS')
        drawn = any(art in line for line in dossiers for art in wd.NPC_ART.values())
        assert drawn is not fast
        assert any('Patch Panel Society' in line for line in dossiers)
    # Fast mode is the one deliberately unframed layout; the ring survives it.
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', 40)
    def turn(valid):
        page = re.search(r'page (\d+)/(\d+)', _last_screen(written))
        return 'N' if page and page.group(1) != page.group(2) else 'B'
    monkeypatch.setattr(wd, 'read_menu_choice', turn)
    for fast, framed in ((False, True), (True, False)):
        palette.fast = fast
        written.clear()
        wd.show_territory(palette, conn, 40, 12, viewer_id=1)
        screen = _ANSI_RE.sub('', ''.join(written))
        assert ('┃' in screen) is framed
        assert 'TEN EXCHANGES' in screen and 'Rain City' in screen
    conn.close()


@pytest.mark.parametrize('toggle', [False, True])
def test_real_process_display_disconnect_preserves_only_chosen_toggle(tmp_path, toggle):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        send(b'i')
        for _ in range(10):
            wait_for(b'Cancel')
            # The digit has to be in the hint, not only in an entry's marker:
            # an entry whose last row is on the next page is not selectable.
            if b'[7]' in screen().split(b'pick ')[-1]: break
            send(b'n')
        else: pytest.fail('Display entry was not reachable')
        send(b'7')
        wait_for(b'Cancel')
        assert b'DISPLAY' in output
        if toggle:
            send(b'1')
            # A state tag is a badge beside the label, not a bracketed word
            # appended to it -- and turning ASCII decorations on is exactly the
            # toggle that respells the badge's own brackets (issue #494).
            wait_for(b'[ON]')
            assert b'ASCII decorations' in screen()
        process.stdin.close()
        assert process.wait(timeout=5) == 0 and process.stderr.read() == b''
        conn = wd.connect(path)
        assert wd.read_display(conn, 0) == ({'ascii_art': True} if toggle else {})
        assert wd.read_player(conn, 0).turns_used == 0
        conn.close()


@pytest.mark.parametrize('default_ascii', [False, True])
def test_display_local_override_wins_over_host_default_without_affecting_other_toggles(tmp_path, monkeypatch, default_ascii):
    conn = wd.connect(tmp_path / 'host-display.db')
    wd.ensure_schema(conn)
    palette = wd.Palette(False)
    palette.default_ascii = default_ascii
    monkeypatch.setattr(wd, '_ASCII_DECOR', False)
    monkeypatch.setattr(wd, '_MONOCHROME', False)
    monkeypatch.setattr(wd, 'out', lambda text='': None)
    wd.apply_display(palette, {})
    assert palette.ascii_art is default_ascii
    choices = iter(['2', 'B'])
    monkeypatch.setattr(wd, 'pick_record_page', lambda *a, **k: next(choices))
    wd.do_display(palette, conn, 1, 80, 24)
    assert wd.read_display(conn, 1) == {'monochrome': True}
    assert palette.ascii_art is default_ascii
    choices = iter(['1', 'B'])
    wd.do_display(palette, conn, 1, 80, 24)
    assert wd.read_display(conn, 1) == {'monochrome': True, 'ascii_art': not default_ascii}
    palette.default_ascii = not default_ascii
    wd.apply_display(palette, wd.read_display(conn, 1))
    assert palette.ascii_art is (not default_ascii)
    conn.close()


@pytest.mark.parametrize('host_unicode,local_ascii', [(None, None), (True, None), (False, None), (False, False), (True, True)])
def test_real_process_unicode_metadata_defaults_and_local_override(tmp_path, host_unicode, local_ascii):
    import json
    path = tmp_path / 'host-world.db'
    conn = wd.connect(path)
    wd.ensure_schema(conn)
    wd.bind_world_owner(conn, 'a' * 32)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    if local_ascii is not None:
        conn.execute("INSERT INTO meta(key,value) VALUES ('display:1',?)", (json.dumps({'ascii_art': local_ascii}),))
    conn.close()
    info = {'user_id': 1, 'handle': 'Caller', 'war_dialer_owner': 'a' * 32}
    if host_unicode is not None: info['unicode_style'] = host_unicode
    metadata = tmp_path / 'door_info.json'
    metadata.write_text(json.dumps(info), encoding='utf-8')
    env = dict(os.environ, WAR_DIALER_DB_PATH=str(path), NETBBS_DOOR_INFO=str(metadata), PYTHONIOENCODING='utf-8')
    result = subprocess.run([sys.executable, '-u', str(_WAR_DIALER_PATH)], input=b'q', capture_output=True, env=env, timeout=10)
    assert result.returncode == 0 and result.stderr == b''
    ascii_expected = local_ascii if local_ascii is not None else host_unicode is False
    assert ('\u250f'.encode('utf-8') not in result.stdout) is ascii_expected
    assert b'SWITCHBOARD' in result.stdout


@pytest.mark.parametrize('action', ['prepare', 'execute', 'abandon', 'recon'])
@pytest.mark.parametrize('commit', [False, True])
def test_real_process_operation_and_recon_disconnect_at_final_act(tmp_path, action, commit):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        conn = wd.connect(path)
        now = wd.now_utc()
        wd.load_or_create_player(conn, 2, 'Rival', now, 1)
        stage = 1 if action == 'prepare' else 2
        conn.execute("UPDATE players SET operation_contract=0, operation_approach=0, operation_stage=?, support='burner' WHERE user_id=0", (stage,))
        before = wd.read_player(conn, 0)
        conn.close()
        send(b'o')
        wait_for(b'Cancel')
        send(b'2' if action == 'recon' else b'1')
        wait_for(b'Cancel')
        send(b'2' if action == 'abandon' else b'1')
        reach(b'[A] Act')
        if commit:
            send(b'a')
            wait_for(b'OPERATION ABANDONED' if action == 'abandon' else b'ACTION RESULT')
        process.stdin.close()
        assert process.wait(timeout=5) == 0 and process.stderr.read() == b''
        conn = wd.connect(path)
        actor = wd.read_player(conn, 0)
        if not commit:
            assert (actor.cash, actor.crew, actor.turns_used, actor.operation_stage, actor.support) == (300, 3, 0, stage, 'burner')
        elif action == 'prepare':
            assert (actor.cash, actor.turns_used, actor.operation_stage, actor.support) == (250, 1, 2, 'burner')
        elif action == 'abandon':
            assert (actor.cash, actor.turns_used, actor.operation_stage, actor.support) == (300, 0, 0, 'burner')
        elif action == 'execute':
            assert actor.turns_used == 1 and actor.operation_stage in (0, 1)
            assert actor.support == '' and actor.crew == 3
            assert wd.rank_score(actor) == (30 if actor.operation_stage == 0 else 0)
        else:
            assert (actor.cash, actor.turns_used, actor.operation_stage, actor.support) == (300, 1, 2, 'burner')
        assert conn.execute('SELECT COUNT(*) FROM recon').fetchone()[0] == int(commit and action == 'recon')
        conn.close()


@pytest.mark.parametrize('exchange_id', [1, 4, 5])
@pytest.mark.parametrize('commit', [False, True])
def test_real_process_every_owner_service_disconnect_preserves_commit_boundary(tmp_path, exchange_id, commit):
    with _running_door(tmp_path) as (process, path, wait_for, send, output, reach, screen):
        wait_for(DIAL)
        conn = wd.connect(path)
        now = wd.now_utc()
        conn.execute("UPDATE exchanges SET controller_user_id=0,garrison=1,npc_key='',controlled_since=? WHERE id=?", (wd.to_iso(now), exchange_id))
        conn.execute('UPDATE players SET heat=30,heat_updated_at=? WHERE user_id=0', (wd.to_iso(now),))
        actor = wd.read_player(conn, 0)
        exchange = next(e for e in wd.list_exchanges(conn) if e.id == exchange_id)
        service_key = wd.PICK_KEYS[len(wd.garrison_options(actor, exchange))].encode()
        conn.close()
        send(b'g')
        wait_for(b'Cancel')
        send(b'1')
        wait_for(b'Cancel')
        send(service_key)
        reach(b'[A] Act')
        if commit:
            send(b'a')
            wait_for(b'ACTION RESULT')
        process.stdin.close()
        assert process.wait(timeout=5) == 0 and process.stderr.read() == b''
        conn = wd.connect(path)
        actor = wd.read_player(conn, 0)
        assert actor.turns_used == int(commit)
        if not commit:
            assert actor.cash == 300 and actor.crew == 3
        elif exchange.role == 'carrier':
            assert actor.cash == 235 and actor.crew == 4
        elif exchange.role == 'hub':
            assert 330 <= actor.cash <= 370 and actor.crew == 3
        else:
            assert actor.cash == 300 and actor.crew == 3 and 14 <= actor.heat <= 15
        assert wd.assigned_crew(conn, 0) == 1
        conn.close()



def test_real_process_lost_output_pipe_exits_cleanly_without_spending(tmp_path):
    path = tmp_path / 'lost-output.db'
    conn = wd.connect(path)
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 0, 'Guest', now, 1)
    conn.close()
    env = dict(os.environ, WAR_DIALER_DB_PATH=str(path), PYTHONIOENCODING='utf-8')
    env.pop('NETBBS_DOOR_INFO', None)
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run([sys.executable, '-u', str(_WAR_DIALER_PATH)], input=b'q',
            stdout=write_fd, stderr=subprocess.PIPE, env=env, timeout=10)
    finally:
        os.close(write_fd)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert result.stderr == b''
    conn = wd.connect(path)
    actor = wd.read_player(conn, 0)
    assert (actor.cash, actor.crew, actor.turns_used) == (300, 3, 0)
    conn.close()



def test_real_process_output_loss_after_commit_retains_paid_action(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'committed-output.db'
    conn = wd.connect(path)
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 0, 'Guest', now, 1)
    conn.close()
    code = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('game', sys.argv[1])
game = importlib.util.module_from_spec(spec)
sys.modules['game'] = game
spec.loader.exec_module(game)
original = game.show_action_result
def pause_before_result(*args, **kwargs):
    print('COMMITTED', flush=True)
    sys.stdin.buffer.read(1)
    return original(*args, **kwargs)
game.show_action_result = pause_before_result
sys.exit(game.main())
"""
    env = dict(os.environ, WAR_DIALER_DB_PATH=str(path), PYTHONIOENCODING='utf-8')
    env.pop('NETBBS_DOOR_INFO', None)
    process = subprocess.Popen([sys.executable, '-u', '-c', code, str(_WAR_DIALER_PATH)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    pool = ThreadPoolExecutor(1)
    def until(marker):
        def read():
            data = bytearray()
            while marker not in _plain(data):
                part = process.stdout.read(1)
                assert part, bytes(data)
                data.extend(part)
        pool.submit(read).result(timeout=20)
    def send(key):
        process.stdin.write(key)
        process.stdin.flush()
    try:
        until(DIAL)
        send(b'c')
        until(b'[A] Act')
        send(b'a')
        until(b'COMMITTED')
        process.stdout.close()
        send(b'X')
        assert process.wait(timeout=10) == 0
        assert process.stderr.read() == b''
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=5)
        pool.shutdown(wait=True)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
    conn = wd.connect(path)
    actor = wd.read_player(conn, 0)
    assert (actor.cash, actor.crew, actor.turns_used, wd.rank_score(actor)) == (225, 4, 1, 10)
    conn.close()



def test_fast_goodbye_retains_rank_without_a_decorative_frame(tmp_path, monkeypatch):
    conn = wd.connect(tmp_path / 'fast-goodbye.db')
    wd.ensure_schema(conn)
    actor = wd.load_or_create_player(conn, 1, 'Caller', wd.now_utc(), 1)
    palette = wd.Palette(False)
    palette.fast = True
    lines = []
    monkeypatch.setattr(wd, 'out_line', lines.append)
    wd.draw_goodbye(palette, actor, 20)
    assert lines == ['Carrier lost. Rank 0 - Newbie']
    lines.clear()
    wd.draw_title(palette, {'node_name': 'TestNode', 'handle': 'Caller'}, 2, 20)
    assert lines == ['WAR DIALER - Season 2', 'Node: TestNode; Handle: Caller']
    conn.close()


# ---------------------------------------------------------------------------
# The presentation contract (issue #494). These are the tests that can fail
# because a screen is grey, because two things that mean different things are
# the same colour, or because a table's columns wander from row to row. The
# suite could only ever assert that a screen *fits* before, which is how an
# entire visual design was lost with every slice passing review.
# ---------------------------------------------------------------------------

_GLYPH_VOCABULARY = None  # filled in lazily from the door's own table


def _unicode_glyphs() -> set[str]:
    """Every glyph the design system draws, in its Unicode spelling."""
    glyphs = {rich for rich, _ in wd._GLYPHS.values()} | set(wd.SPARK)
    glyphs |= {"╔", "╗", "╚", "╝", "═", "║", "│", "…"}
    return glyphs


def _colour_before(raw: str, marker: str) -> str:
    """The last colour introduced before `marker` first appears in `raw`."""
    at = raw.index(marker)
    colours = [escape for escape in _ANSI_RE.findall(raw[:at]) if "38;" in escape]
    return colours[-1] if colours else ""


def _body_rows(raw_screen: str) -> list[str]:
    """Every row's content between the frame's sides, styling intact.

    The frame's own SGR is left outside: a screen whose rows are grey inside a
    green box passes any test that looks at the whole row (issue #494).
    """
    edge = wd.gl("v")
    rows = []
    for row in raw_screen.split("\r\n"):
        plain = _ANSI_RE.sub("", row)
        if not plain.strip():
            continue
        if plain.startswith(edge) and plain.rstrip().endswith(edge):
            rows.append(edge.join(row.split(edge)[1:-1]))
        elif plain[0] not in _FRAME_EDGES:
            rows.append(row)
    return rows


def _painted_world(tmp_path, name="paint.db"):
    """A world with one holding, one rival holding, an NPC home and two receipts."""
    conn = wd.connect(tmp_path / name)
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 1, "Thiesi", now, 1)
    wd.load_or_create_player(conn, 2, "Kilobaud", now, 1)
    conn.execute("UPDATE players SET cash=4820, crew=3, heat=72, turns_used=7, "
                 "crew_recruited_total=24, successful_raids=4, successful_jobs=12, "
                 "specialty='phreakers', support='burner' WHERE user_id=1")
    conn.execute("UPDATE players SET crew_recruited_total=40, successful_raids=9 WHERE user_id=2")
    conn.execute("UPDATE exchanges SET controller_user_id=1, garrison=2 WHERE id=1")
    conn.execute("UPDATE exchanges SET controller_user_id=2, garrison=3 WHERE id=3")
    conn.commit()
    wd.record_event(conn, 1, "Kilobaud", "Kilobaud raided you and got away with $340!", now)
    wd.record_event(conn, 1, None, "Rooted 212-555 Uptown Exchange; +$120/hour.", now, seen=True)
    return conn, now


def _walk_screens(conn, palette, width, height, monkeypatch, *, keys=()):
    """Every screen a shallow walk of the door reaches, as raw written chunks."""
    written: list[str] = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(wd, "read_input_key", lambda: " ")
    wanted = list(keys)

    def select(valid):
        page = re.search(r"page (\d+)/(\d+)", _last_screen(written))
        if page and page.group(1) != page.group(2):
            key = "N"
        elif wanted and wanted[0] in valid:
            key = wanted.pop(0)
        else:
            key = "B"
        # The real reader echoes the key, which is what ends the bar's own row
        # (issue #487); a stub that does not would glue the next screen onto it.
        wd.out_line(key)
        return key

    monkeypatch.setattr(wd, "read_menu_choice", select)
    now = wd.now_utc()
    player = wd.refresh_player(conn, 1, now)
    state = wd.dashboard_state(conn, 1, now)
    wd.draw_title(palette, {"node_name": "ReLink", "handle": "Thiesi"}, 1, width)
    _, pages = wd.draw_dashboard(palette, state, now, width, height)
    for page in range(1, pages):
        wd.draw_dashboard(palette, state, now, width, height, page)
    wd.show_territory(palette, conn, width, height, player=player)
    wd.show_player_directory(palette, conn, 1, width, height, standings=True)
    wd.show_player_directory(palette, conn, 1, width, height)
    wd.show_event_history(palette, conn, 1, width, height, own_handle=player.handle)
    wd.draw_help(palette, width, height)
    wd.draw_help(palette, width, height, onboarding=True)
    wd.confirm_action(palette, conn, player, "root", width, height,
                      wd.list_exchanges(conn, 1)[3])
    wd.confirm_action(palette, conn, player, "raid", width, height,
                      wd.refresh_player(conn, 2, now))
    wd.show_action_result(palette, ["You root 415-555 Bay Exchange."],
                          wd.ActionDelta(cash=420, crew=-1, heat=12.0, rank=30, turns=1, assigned=1),
                          False, width, height)
    wd.do_crew(palette, conn, player, width, height)
    wd.do_operations_hub(palette, conn, player, __import__("random").Random(1), width, height)
    wd.do_display(palette, conn, 1, width, height)
    wd.show_season_results(palette, conn, 1, width, height)
    wd.show_season_recognition(palette, conn, 1, width, height)
    # The goodbye card is deliberately not part of the walk: it scrolls under
    # whatever screen the caller quit from, the way the host's own three epilogue
    # rows do, so it is not a screen of its own to measure.
    return "".join(written).split(CLEAR)


@pytest.mark.parametrize("width,height", [(40, 12), (80, 24)])
def test_every_screen_colours_the_rows_a_caller_reads(tmp_path, monkeypatch, width, height):
    """Acceptance criterion 1: colour reaches the body.

    This is the test that would have caught the regression. Every screen's rows
    used to be wrapped through a plain-text flattener and then coloured one
    colour from outside, so `p.white` on the row was the only styling that
    survived and the whole game read as one grey block inside a green frame.
    """
    conn, _ = _painted_world(tmp_path)
    palette = wd.Palette(True)
    screens = _walk_screens(conn, palette, width, height, monkeypatch, keys=["1"])
    assert len(screens) > 12, "the walk did not reach the door's screens"
    grey = []
    for screen in screens:
        for row in _body_rows(screen):
            if "\x1b[" not in row:
                grey.append(row)
    assert not grey, f"{len(grey)} body rows printed with no styling: {grey[:4]!r}"
    conn.close()


def test_hotkeys_labels_values_and_the_frame_are_four_different_colours():
    """Acceptance criterion 2: roles are distinct."""
    palette = wd.Palette(True)
    roles = {name: palette.role(name) for name in wd.Palette.ROLES}
    assert len(set(roles.values())) == len(roles), "two roles share a colour"
    # The four roles a caller has to tell apart on every screen.
    assert len({palette.amber, palette.grey, palette.ink, palette.phosphor}) == 4
    bar = wd.key_bar(palette, (("T", "Trade", "Trade"),), 40, 1)[0]
    assert bar.startswith(palette.amber + wd.BOLD + "[T]")
    assert _colour_before(bar, "Trade") == palette.mint
    chip = wd.label_value(palette, "CASH", "$4,820", style=palette.amber)
    assert _colour_before(chip, "CASH") == palette.grey
    assert _colour_before(chip, "$4,820") == palette.amber
    # 256-colour terminals get a deliberate fallback index per role, not a guess.
    fallback = wd.Palette(False)
    indexes = {fallback.role(name) for name in wd.Palette.ROLES}
    assert len(indexes) == len(wd.Palette.ROLES)
    assert all("38;5;" in escape for escape in indexes)


def test_an_exchange_reads_the_same_colour_on_the_map_the_table_and_the_feed(tmp_path, monkeypatch):
    """Acceptance criterion 3: owner colour is consistent."""
    conn, now = _painted_world(tmp_path, "owner.db")
    palette = wd.Palette(True)
    player = wd.refresh_player(conn, 1, now)
    exchanges = wd.list_exchanges(conn, 1)
    mine, rival = exchanges[0], exchanges[2]
    assert wd.owner_node(palette, mine, 1)[1] == palette.phosphor
    assert wd.owner_node(palette, rival, 1)[1] == palette.magenta
    ring = wd.scene_map(palette, exchanges, 1, 72)[0]
    assert _colour_before(ring, f"{wd.gl('mine')}") == palette.phosphor
    assert _colour_before(ring, f"{wd.gl('rival')}") == palette.magenta
    # [0] is the header row; the ten exchanges follow it in world order.
    rows = wd.territory_cards(palette, exchanges, 1, player, 72)[1][1][1:]
    assert _colour_before(rows[0], wd.gl("mine")) == palette.phosphor
    assert _colour_before(rows[2], wd.gl("rival")) == palette.magenta
    # A rival's own move against you is the same magenta in the feed.
    events = wd.history_events(conn, 1)
    feed = wd.feed(palette, events, 72, own_handle="Thiesi")
    raided = next(row for row in feed if "Kilobaud" in row)
    assert _colour_before(raided, wd.gl("bullet")) == palette.magenta
    assert _colour_before(raided, "Kilobaud") == palette.magenta
    mine_row = next(row for row in feed if "Rooted" in row)
    assert _colour_before(mine_row, wd.gl("bullet")) == palette.phosphor
    conn.close()


@pytest.mark.parametrize("width", [40, 64, 80])
def test_table_columns_start_at_the_same_display_column_on_every_row(width):
    """Acceptance criterion 4: columns align."""
    palette = wd.Palette(True)
    rows = [[f"a{'x' * index}", f"b{'y' * (9 - index)}", wd.dots(palette, index, 4, cap=4),
             f"{index * 137}"] for index in range(5)]
    drawn = wd.table(palette, ["KEY", "NAME", "DEFENCE", "RANK"], rows, "<<<>", width)
    columns = None
    for row in drawn[1:]:
        plain = _ANSI_RE.sub("", row)
        starts = tuple(sum(wd._char_width(ch) for ch in plain[:plain.index(marker)])
                       for marker in ("a", "b"))
        assert columns in (None, starts), f"columns moved at width {width}: {drawn}"
        columns = starts
    for row in drawn:
        assert sum(wd._char_width(ch) for ch in _ANSI_RE.sub("", row)) <= width


@pytest.mark.parametrize("preset", ["default", "ascii_art", "monochrome", "fast"])
@pytest.mark.parametrize("width,height", [(40, 12), (64, 20), (80, 24)])
def test_every_display_preset_renders_every_screen_deliberately(tmp_path, monkeypatch, preset,
                                                                width, height):
    """Acceptance criteria 5 and 6: it still fits, and every preset is deliberate."""
    conn, _ = _painted_world(tmp_path, f"preset-{preset}-{width}.db")
    palette = wd.Palette(True)
    # The preset goes where the door reads it from, exactly as its own Display
    # screen writes it: the walk opens that screen, and `apply_display` would
    # otherwise reset a palette flag a test had set by hand.
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     ("display:1", json.dumps({preset: True} if preset != "default" else {})))
    wd.apply_display(palette, wd.read_display(conn, 1))
    monkeypatch.setattr(wd, "_ASCII_DECOR", preset == "ascii_art")
    monkeypatch.setattr(wd, "_MONOCHROME", preset == "monochrome")
    screens = _walk_screens(conn, palette, width, height, monkeypatch, keys=["1"])
    for screen in screens:
        rows = _rows(screen)
        assert len(rows) <= height, (f"{preset} at {width}x{height}: "
                                     f"{len(rows)} rows " + " | ".join(rows))
        for row in rows:
            assert sum(wd._char_width(ch) for ch in row) <= width, repr(row)
    text = "".join(screens)
    if preset == "ascii_art":
        # Every glyph in the vocabulary has an ASCII substitute, and the ASCII
        # preset is the one place that can prove none was forgotten.
        leaked = sorted(glyph for glyph in _unicode_glyphs() if glyph in text)
        assert not leaked, f"no ASCII substitute reached the screen for {leaked}"
    if preset == "monochrome":
        # Monochrome is removed at the source: a role returns no SGR at all, so
        # no screen can depend on a colour it is not going to get.
        assert all(palette.role(name) == "" for name in wd.Palette.ROLES)
        assert "38;" not in text
    if preset == "fast":
        assert wd.gl("v") not in text  # the one deliberately unframed layout
        assert "SWITCHBOARD" in _ANSI_RE.sub("", text)
    conn.close()


def test_motion_is_skippable_absent_in_fast_and_mono_and_changes_no_screen(monkeypatch):
    """Acceptance criterion 7: motion is skippable and never blocks input."""
    palette = wd.Palette(True)
    assert wd.motion_enabled(palette)
    for preset in ("fast", "monochrome", "ascii_art"):
        muted = wd.Palette(True)
        setattr(muted, preset, True)
        assert not wd.motion_enabled(muted), preset

    rows = [f"row {index}" for index in range(8)]
    written: list[str] = []
    monkeypatch.setattr(wd, "out", written.append)
    beats = []
    monkeypatch.setattr(wd, "_read_key_with_timeout", lambda timeout: beats.append(timeout) or None)
    wd.reveal(palette, rows)
    animated = "".join(written)
    assert len(beats) == len(rows), "a reveal waits once per row"
    assert sum(beats) <= wd.MOTION_BUDGET_SECONDS + 1e-9, "motion stays inside its budget"

    # A key skips the rest: the same screen, written without waiting again.
    written.clear()
    beats.clear()
    monkeypatch.setattr(wd, "_read_key_with_timeout", lambda timeout: beats.append(timeout) or " ")
    wd.reveal(palette, rows)
    assert len(beats) == 1 and "".join(written) == animated

    # And a preset without motion writes exactly the same rows, with no waiting.
    written.clear()
    beats.clear()
    still = wd.Palette(True)
    still.fast = True
    wd.reveal(still, rows)
    assert not beats and "".join(written) == animated


def test_the_carrier_sweep_plays_after_the_commit_and_ends_on_the_real_figure(monkeypatch):
    palette = wd.Palette(True)
    written: list[str] = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(wd, "_read_key_with_timeout", lambda timeout: None)
    wd.resolve_sweep(palette, 72, amount=420)
    text = "".join(written)
    # It owns its own screen: a sweep drawn under the screen the caller pressed a
    # key at would push that screen off a twelve-row terminal.
    assert text.startswith(CLEAR)
    frames = [frame for frame in _ANSI_RE.sub("", text).split("\r") if frame.strip()]
    assert len(frames) > 4 and "$420" in frames[-1]
    assert all(sum(wd._char_width(ch) for ch in frame) <= 80 for frame in frames)
    written.clear()
    muted = wd.Palette(True)
    muted.fast = True
    wd.resolve_sweep(muted, 72, amount=420)
    assert written == []


def test_the_switchboard_reads_at_a_glance_with_gauges_not_sentences(tmp_path, monkeypatch):
    """The §1 target: the facts a caller scans for are gauges, chips and a map."""
    conn, now = _painted_world(tmp_path, "glance.db")
    palette = wd.Palette(True)
    state = wd.dashboard_state(conn, 1, now)
    written: list[str] = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", 80)
    wd.draw_dashboard(palette, state, now, 78, 24)
    first = _screen_text(written)
    for fact in ("CASH $4,820", "HEAT", "CREW", "TURNS", "HOLD", "SHIELD"):
        assert fact in first, fact
    # Gauges, pips, crew dots and the ring, all on the first page.
    assert wd.gl("meter_on") in first and wd.gl("meter_off") in first
    assert wd.gl("turn_on") in first and wd.gl("crew_on") in first
    assert wd.gl("link_h") * 2 in first and wd.gl("mine") in first
    assert _screen_titles(written).startswith("SWITCHBOARD")
    conn.close()


# ---------------------------------------------------------------------------
# Defects the review of the rebuild found (PR #503). Each one is a screen saying
# something that is not true of the action behind it, or a keystroke reaching a
# screen the caller never pressed it on.
# ---------------------------------------------------------------------------


def test_a_key_that_skips_the_masthead_does_not_reach_the_next_screen(monkeypatch):
    """The masthead's reveal has no reader of its own.

    Handing its skip key on would acknowledge a page of unread receipts, or skip
    a page of the first-visit guide, that the caller never pressed anything on.
    A result screen's reveal is the opposite case: an acknowledgement follows it
    immediately, so one press should do both.
    """
    palette = wd.Palette(True)
    monkeypatch.setattr(wd, "out", lambda text: None)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(wd, "_read_key_with_timeout", lambda timeout: " ")
    wd._PENDING_INPUT.clear()
    wd.draw_title(palette, {"node_name": "ReLink", "handle": "Thiesi"}, 1, 78)
    assert wd._PENDING_INPUT == [], "the masthead handed its skip key to the next screen"
    wd.reveal(palette, ["one", "two"])
    assert wd._PENDING_INPUT == [" "], "a result reveal must not eat the acknowledgement"
    wd._PENDING_INPUT.clear()


def test_the_log_tones_your_own_receipts_the_way_the_dashboard_does(tmp_path, monkeypatch):
    conn, now = _painted_world(tmp_path, "tone.db")
    palette = wd.Palette(True)
    # A receipt the caller caused records their own handle; one a rival caused
    # records the rival's. Both screens have to agree about which is which.
    wd.record_event(conn, 1, "Thiesi", "Reinforced 212-555 Uptown Exchange with 1.", now,
                    seen=True)
    events = wd.history_events(conn, 1)
    feed = wd.feed(palette, events, 72, own_handle="Thiesi")
    own_feed = next(row for row in feed if "Reinforced" in row)
    assert _colour_before(own_feed, wd.gl("bullet")) == palette.phosphor
    written: list[str] = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(wd, "read_menu_choice", lambda valid: "B")
    wd.show_event_history(palette, conn, 1, 78, 24, own_handle="Thiesi")
    own_log = next(row for row in "".join(written).split("\r\n") if "Reinforced" in row)
    assert palette.magenta not in own_log, "your own receipt read as hostile in the log"
    assert palette.ink in own_log
    conn.close()


def test_the_feed_wraps_wide_glyphs_without_losing_the_tail(tmp_path, monkeypatch):
    conn, now = _painted_world(tmp_path, "wide.db")
    palette = wd.Palette(True)
    # A handle of CJK glyphs is twice as wide as it is long; a row measured in
    # characters would be clipped by the frame and lose the end of the receipt.
    wd.record_event(conn, 1, "界" * 12, "界" * 12 + " raided you and got away with $5.",
                    now)
    rows = wd.feed(palette, wd.history_events(conn, 1), 60, own_handle="Thiesi", limit=1)
    assert len(rows) > 1, "a wide receipt has to carry onto another row"
    for row in rows:
        assert wd._dlen(row) <= 60, (wd._dlen(row), row)
    assert "$5." in "".join(rows), "the tail of the receipt was dropped"
    conn.close()


@pytest.mark.parametrize("role,expect_roll", [("pbx", False), ("carrier", False), ("hub", True)])
def test_an_owner_service_preview_shows_only_the_risk_that_service_takes(tmp_path, role,
                                                                        expect_roll):
    conn, now = _painted_world(tmp_path, f"service-{role}.db")
    palette = wd.Palette(True)
    exchange = next(e for e in wd.list_exchanges(conn, 1) if e.role == role)
    conn.execute("UPDATE exchanges SET controller_user_id=1, garrison=1, controlled_since=? "
                 "WHERE id=?", (wd.to_iso(now), exchange.id))
    conn.execute("UPDATE players SET heat=78 WHERE user_id=1")
    conn.commit()
    player = wd.refresh_player(conn, 1, now)
    exchange = next(e for e in wd.list_exchanges(conn, 1) if e.id == exchange.id)
    cards = wd.stakes_cards(palette, "service", player, exchange, 72)
    text = _ANSI_RE.sub("", " ".join(row for _, rows in cards for row in rows))
    # Only a Warez Hub adds Heat and rolls for a bust. At 78 Heat the other two
    # would otherwise advertise a bust chance directly above terms that say there
    # is no roll. ("NO BUST ROLL" contains "BUST", so the badge is matched whole.)
    assert ("NO BUST ROLL" in text) is not expect_roll, text
    assert ("⟦BUST " in text) is expect_roll, text
    assert (wd.preview_heat("service", player, exchange) > 0) is expect_roll
    conn.close()


@pytest.mark.parametrize("step,cost", [("case", "$0"), ("prepare", "$50"), ("execute", "$0")])
def test_an_operation_step_previews_its_own_cost_and_its_own_risk(tmp_path, step, cost):
    conn, now = _painted_world(tmp_path, f"step-{step}.db")
    palette = wd.Palette(True)
    # No specialty and no support, so the execution's Heat actually lands and its
    # bust badge is the thing being compared against the other two steps.
    conn.execute("UPDATE players SET heat=78, specialty='', support='' WHERE user_id=1")
    conn.commit()
    player = wd.refresh_player(conn, 1, now)
    cards = wd.operation_step_cards(palette, player, step, wd.JobChoice(1, 1), 72)
    head = _ANSI_RE.sub("", " ".join(cards[1][1]))
    stakes = _ANSI_RE.sub("", " ".join(rows for heading, rows in cards
                                      if heading == "STAKES" for rows in rows))
    assert f"cash {cost}" in head, head
    # Casing and preparing roll for nothing and add no Heat; only executing does.
    assert ("ODDS" in stakes) is (step == "execute"), stakes
    assert ("NO BUST ROLL" in stakes) is (step != "execute"), stakes
    assert wd.progress_chain(palette, list(wd.OPERATION_STAGES),
                             wd.OPERATION_STAGES.index(step)) in cards[0][1][0]
    conn.close()


def test_your_own_garrisons_are_not_described_as_targets(tmp_path):
    conn, now = _painted_world(tmp_path, "held.db")
    palette = wd.Palette(True)
    player = wd.refresh_player(conn, 1, now)
    held = next(e for e in wd.list_exchanges(conn, 1) if e.controller_user_id == 1)
    rows = _ANSI_RE.sub("", " ".join(wd.garrison_entry_rows(palette, held, player, 68)))
    # A holding has posted crew, defence, income and a service; a capture price,
    # root Heat or odds against its own defence would mean nothing here.
    for absent in ("capture", "odds", "heat"):
        assert absent not in rows.lower(), rows
    for present in ("posted", "defence", "income", "Service:"):
        assert present in rows, rows
    conn.close()


def test_the_scene_table_advertises_no_hotkey_the_screen_ignores(tmp_path):
    conn, now = _painted_world(tmp_path, "verbs.db")
    palette = wd.Palette(True)
    player = wd.refresh_player(conn, 1, now)
    exchanges = wd.list_exchanges(conn, 1)
    table = wd.territory_cards(palette, exchanges, 1, player, 72)[1][1]
    text = _ANSI_RE.sub("", " ".join(table))
    # The scene screen inspects and never acts, so its table names the verb and
    # the exchange's own card says where the key that does it lives.
    for verb in ("garrison", "raid", "root"):
        assert verb in text
    for key in ("[G]", "[R]", "[X]"):
        assert key not in text, f"{key} is printed on a screen whose dispatch ignores it"
    card = _ANSI_RE.sub("", " ".join(
        row for _, rows in wd.exchange_detail_cards(palette, exchanges[0], player, 72)
        for row in rows))
    assert "Back on the switchboard" in card and "[G] Garrison" in card
    conn.close()


@pytest.mark.parametrize("action,target_role,rolls", [
    ("trade", None, True), ("job", None, True), ("raid", None, True), ("root", None, True),
    ("service", "hub", True),
    ("recruit", None, False), ("crew", None, False),
    ("service", "pbx", False), ("service", "carrier", False),
])
def test_a_preview_promises_a_bust_roll_only_where_one_happens(tmp_path, action, target_role,
                                                              rolls):
    """Only the five resolvers that call `apply_heat` roll against Heat.

    The first round of this fix made `preview_heat` return zero for the two
    services that take none, but the chance was still computed from the caller's
    existing Heat -- so above 80 Heat a Lay Low, a Carrier recruitment and every
    kit purchase still advertised `BUST n%` above terms saying there is no roll.
    """
    conn, now = _painted_world(tmp_path, f"roll-{action}-{target_role}.db")
    palette = wd.Palette(True)
    conn.execute("UPDATE players SET heat=95, specialty='', support='' WHERE user_id=1")
    conn.commit()
    player = wd.refresh_player(conn, 1, now)
    if action == "service":
        exchange = next(e for e in wd.list_exchanges(conn, 1) if e.role == target_role)
        conn.execute("UPDATE exchanges SET controller_user_id=1, garrison=1, controlled_since=? "
                     "WHERE id=?", (wd.to_iso(now), exchange.id))
        conn.commit()
        target = next(e for e in wd.list_exchanges(conn, 1) if e.id == exchange.id)
    elif action == "root":
        target = wd.list_exchanges(conn, 1)[3]
    elif action == "raid":
        target = wd.refresh_player(conn, 2, now)
    elif action == "job":
        target = wd.JobChoice(1, 1)
    elif action == "crew":
        target = wd.CrewChoice("stash")
    else:
        target = None
    assert wd.rolls_for_bust(action, target) is rolls
    cards = wd.stakes_cards(palette, action, player, target, 72)
    stakes = _ANSI_RE.sub("", " ".join(row for heading, rows in cards
                                      if heading == "STAKES" for row in rows))
    if action == "recruit":
        assert "BUST" not in stakes, stakes  # recruiting has no Heat row at all
    else:
        assert ("NO BUST ROLL" in stakes) is not rolls, stakes
        assert ("⟦BUST " in stakes) is rolls, stakes


def test_a_bracketed_word_is_never_coloured_like_a_hotkey():
    """Every hotkey this door has is one character.

    `[ON]`, `[HELD]` or `[SPECIALTY]` in amber and bold beside `[1]` reads as a
    second key to press. A state tag is a badge or neutral data, never a key.
    """
    palette = wd.Palette(True)
    row = wd.prose_rows(palette, "Press [T] to trade; the slot shows [HELD] when taken.", 72)[0]
    assert _colour_before(row, "[T]") == palette.amber
    assert wd.BOLD in row.split("[T]")[0]
    assert _colour_before(row, "[HELD]") == palette.cyan
    assert _colour_before(row, "[HELD]") != palette.amber


def test_no_screen_prints_a_hotkey_its_own_dispatch_ignores(tmp_path):
    """The rival directory reads; it does not raid.

    `show_player_directory` delegates input to `show_text_pages`, which accepts
    only Next/Prev/Back/Quit, so `[R] raid` in its table was a key that did
    nothing -- the same defect as the scene table's action column.
    """
    conn, now = _painted_world(tmp_path, "verdicts.db")
    palette = wd.Palette(True)
    conn.execute("UPDATE players SET created_at=? WHERE user_id=2",
                 (wd.to_iso(now - wd.GRACE * 2),))
    conn.commit()
    page = wd.read_player_page(conn, 1, now)
    cards = wd.rivals_cards(palette, page, 1, 72, now)
    table = _ANSI_RE.sub("", " ".join(
        row for heading, rows in cards if heading == "RIVAL CREWS" for row in rows))
    assert "eligible" in table, table
    assert "[R]" not in table, "a key the screen's dispatch ignores"
    # The head card says where the key that does raid actually lives.
    head = _ANSI_RE.sub("", " ".join(cards[0][1]))
    assert "Back on the switchboard" in head and "[R] Raid" in head
    conn.close()


def test_the_unread_receipt_page_shows_how_to_keep_it_unread(tmp_path, monkeypatch):
    """Any key acknowledges the page; Back is the only way not to.

    The login view's footer said only "Press any key to continue...", which both
    hides the documented escape and implies Back acknowledges like anything else.
    """
    conn, now = _painted_world(tmp_path, "unread.db")
    palette = wd.Palette(True)
    written: list[str] = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", 40)
    monkeypatch.setattr(wd, "read_input_key", lambda: "B")
    wd.show_event_history(palette, conn, 1, 40, 12, unseen_only=True, own_handle="Thiesi")
    screen = _ANSI_RE.sub("", "".join(written))
    assert "Press any key to continue" in screen
    assert "[B] Back keeps this page unread" in screen
    # Back left them unread, which is the behaviour the footer now documents.
    assert wd.unseen_events(conn, 1), "Back acknowledged the page"
    for row in _rows("".join(written).split(CLEAR)[-1]):
        assert sum(wd._char_width(ch) for ch in row) <= 40, repr(row)
    conn.close()


def test_skipping_the_masthead_consumes_the_whole_input_unit(monkeypatch):
    """An arrow key is several bytes, and all of them belong to the skip.

    Dropping only the leading ESC left `[A` for the next reader, where `A` was
    taken as the "any key" that advances the first-visit guide or marks a page of
    receipts read -- an action on a screen the caller never pressed anything on.
    """
    palette = wd.Palette(True)
    monkeypatch.setattr(wd, "out", lambda text: None)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", 80)
    stream = list("\x1b[A")  # one press of the up arrow
    monkeypatch.setattr(wd, "_read_key_with_timeout",
                        lambda timeout: wd.read_key() if wd._PENDING_INPUT
                        else (stream.pop(0) if stream else None))
    wd._PENDING_INPUT.clear()
    wd.draw_title(palette, {"node_name": "ReLink", "handle": "Thiesi"}, 1, 78)
    assert stream == [], "the rest of the sequence was left for the next screen"
    assert wd._PENDING_INPUT == []
    wd._PENDING_INPUT.clear()


def test_one_width_rule_measures_every_row():
    """`_dlen` and the wrapper have to agree, or a budget is a guess.

    A Hangul choseong is two columns wide and sits *below* U+2E80, and a combining
    accent is zero; the old rule called both one. A handle of them was budgeted as
    one row, wrapped into two by `out_line`, and scrolled the footer off a
    twelve-row terminal.
    """
    for sample in ("ᄀ" * 6, "界é" * 6, "plain ascii", "　ᅠ"):
        assert wd._dlen(sample) == wd._visible_width(sample), sample
        wrapped = wd._wrap_output(sample, 20).split("\r\n")
        assert all(wd._dlen(row) <= 20 for row in wrapped), wrapped
    # And a truncation measures the same way, so a fitted cell really fits.
    for width in (4, 9, 20):
        assert wd._dlen(wd._fit("ᄀ" * 30, width)) <= width


def test_turning_fast_mode_off_inside_display_redraws_at_the_frame_width(tmp_path, monkeypatch):
    """Fast mode is the one preset that changes how wide a row may be.

    Caching the width across the toggle composed the next screen's rows for an
    unframed terminal, and the frame then clipped the setting descriptions.
    """
    conn, _ = _painted_world(tmp_path, "fast-toggle.db")
    palette = wd.Palette(True)
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     ("display:1", json.dumps({"fast": True})))
    written: list[str] = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", 40)
    # Fast mode is the third entry, which may be on the picker's second page at
    # forty columns; page forward until it is offered, then turn it off and leave.
    state = {"off": False}

    def select(valid):
        if state["off"]:
            return "B"
        if "3" in valid:
            state["off"] = True
            return "3"
        return "N" if "N" in valid else "B"

    monkeypatch.setattr(wd, "read_menu_choice", select)
    wd.do_display(palette, conn, 1, 40, 12)
    screens = "".join(written).split(CLEAR)[1:]
    assert len(screens) > 1, "the toggle did not redraw"
    framed = screens[-1]
    assert wd.gl("v") in _ANSI_RE.sub("", framed), "Fast mode was not turned off"
    for row in _rows(framed):
        assert sum(wd._char_width(ch) for ch in row) <= 40, repr(row)
    # Nothing was clipped away by the frame on the redraw.
    assert "caller names are untouched." in _screen_text(framed)
    conn.close()


def test_raid_authorization_stays_above_the_ui_boundary():
    """`resolve_raid` reaches `raid_block` through `is_eligible_raid_target`.

    A helper the domain depends on cannot live below the file's UI-layer marker,
    or a presentation-only edit can change whether raids are permitted.
    """
    source = _WAR_DIALER_PATH.read_text(encoding="utf-8")
    boundary = source.index("# UI layer -- everything below touches")
    # `rolls_for_bust` is deliberately *not* here: nothing but a preview card
    # consults it, so it belongs with the screens.
    for name in ("def raid_block", "def raid_eligibility_reason",
                 "def is_eligible_raid_target"):
        assert name in source, name
        assert source.index(name) < boundary, f"{name} is below the UI-layer boundary"
    # And the direction of the dependency: the domain helper takes no palette.
    assert "p: Palette" not in source[source.index("def raid_block"):
                                     source.index("def raid_eligibility_reason")]


def test_lay_low_previews_the_heat_it_will_leave(tmp_path):
    """A PBX's service removes Heat, which is the reason a caller opens it.

    Reporting it as no change left the prominent gauge at the Heat the terms
    immediately below promised to reduce: 78 above "remove up to 15 Heat".
    """
    conn, now = _painted_world(tmp_path, "laylow.db")
    palette = wd.Palette(True)
    pbx = next(e for e in wd.list_exchanges(conn, 1) if e.role == "pbx")
    conn.execute("UPDATE exchanges SET controller_user_id=1, garrison=1, controlled_since=? "
                 "WHERE id=?", (wd.to_iso(now), pbx.id))
    conn.execute("UPDATE players SET heat=78 WHERE user_id=1")
    conn.commit()
    player = wd.refresh_player(conn, 1, now)
    pbx = next(e for e in wd.list_exchanges(conn, 1) if e.id == pbx.id)
    assert wd.preview_heat("service", player, pbx) == -15
    stakes = next(rows for heading, rows in wd.stakes_cards(palette, "service", player, pbx, 72)
                  if heading == "STAKES")
    text = _ANSI_RE.sub("", " ".join(stakes))
    assert "78 -15 = 63" in text, text
    # A reduction is not an alarm.
    heat_row = next(row for row in stakes if "HEAT" in _ANSI_RE.sub("", row))
    assert _colour_before(heat_row, "-15") == palette.phosphor
    # Less Heat than the caller has, on the gauge itself.
    assert heat_row.count(wd.gl("meter_on")) < wd.meter(palette, 78, 100, 18).count(wd.gl("meter_on"))
    conn.close()


def test_capped_dots_keep_their_proportion():
    """A gauge that lights every dot for half a pool is worse than no gauge."""
    palette = wd.Palette(True)
    lit = wd.gl("crew_on")
    half = wd.dots(palette, 10, 20, cap=6)
    assert half.count(lit) == 3, _ANSI_RE.sub("", half)
    assert wd.dots(palette, 20, 20, cap=6).count(lit) == 6  # all of it is all of it
    assert wd.dots(palette, 0, 20, cap=6).count(lit) == 0  # and none is none
    assert wd.dots(palette, 1, 40, cap=6).count(lit) == 1  # some is never none
    assert wd.dots(palette, 39, 40, cap=6).count(lit) == 5  # and not-all is never all
    # A transfer at the top of the range still moves the gauge.
    before = wd.dots(palette, 10, 20, cap=6).count(lit)
    after = wd.dots(palette, 14, 20, cap=6).count(lit)
    assert after > before


@pytest.mark.parametrize("heat,expected", [(0, ""), (72, "near bust"), (79, "bust risk"),
                                           (95, "bust risk")])
def test_the_heat_chip_warns_about_what_a_trade_would_leave(tmp_path, heat, expected):
    """At 79 Heat a trade crosses the threshold and rolls.

    Judging the chip on where Heat stands called that "near bust" while the
    advice on the same screen told the caller to wait for a risk-free trade.
    """
    conn, now = _painted_world(tmp_path, f"chip-{heat}.db")
    conn.execute("UPDATE players SET heat=?, specialty='', support='' WHERE user_id=1", (heat,))
    conn.commit()
    palette = wd.Palette(True)
    player = wd.refresh_player(conn, 1, now)
    chip = _ANSI_RE.sub("", wd.heat_chip(palette, player))
    assert (expected in chip if expected else chip == ""), (heat, chip)
    if expected == "bust risk":
        # The same screen's advice agrees: it offers a wait, not a free trade.
        state = wd.dashboard_state(conn, 1, now)
        assert any("without a bust roll" in line for line in wd.next_steps(state, now))
    conn.close()
