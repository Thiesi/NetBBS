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
        deadline = time.monotonic() + 8
        while marker not in pending:
            try:
                chunk = chunks.get(timeout=max(0.01, deadline - time.monotonic()))
            except Empty:
                pytest.fail(f"Door did not display {marker!r}: {bytes(output)!r}")
            if chunk is None:
                pytest.fail(f"Door exited before {marker!r}: {bytes(output)!r}")
            pending.extend(chunk)
        end = pending.index(marker) + len(marker)
        del pending[:end]

    def send(data):
        process.stdin.write(data)
        process.stdin.flush()

    try:
        yield process, path, wait_for, send, output
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
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b">\x1b[0m ")
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
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b">\x1b[0m ")
        send(b"x")
        wait_for(b"cancel")
        send(sequence)
        time.sleep(0.25)
        send(b"q")
        wait_for(b">\x1b[0m ")
        send(b"q")
        assert process.wait(timeout=5) == 0
        conn = wd.connect(path)
        assert wd.read_player(conn, 0).turns_used == 0
        assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
        conn.close()


def test_real_process_incomplete_escape_exits_without_action(tmp_path):
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b">\x1b[0m ")
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
    ) as (process, path, wait_for, send, output):
        if stage in ("onboarding", "receipt"):
            wait_for(b"Press any key to continue...")
        else:
            wait_for(b">\x1b[0m ")
            if stage == "recruit":
                send(b"c")
                wait_for(b"[A]Act [B]ack")
                send(b"a")
                wait_for(b"A new member joins")
            elif stage == "root":
                send(b"x")
                wait_for(b"cancel")
                send(b"1")
                wait_for(b"[A]Act [B]ack")
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
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b">\x1b[0m ")
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
    ) as (process, path, wait_for, send, output):
        if stage in ("onboarding", "receipt"):
            wait_for(b"Press any key to continue...")
        else:
            wait_for(b">\x1b[0m ")
            if stage == "target":
                send(b"x")
                wait_for(b"cancel")
        send(b"\x1b[[C")  # Linux-console F3, not Crew Recruit / exchange C.
        if stage in ("onboarding", "receipt"):
            wait_for(b">\x1b[0m ")
        time.sleep(0.25)
        conn = wd.connect(path)
        try:
            assert wd.read_player(conn, 0).turns_used == 0
            assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
        finally:
            conn.close()
        send(b"q")
        if stage == "target":
            wait_for(b">\x1b[0m ")
            send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""


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
    ) as (process, path, wait_for, send, output):
        if stage in ("onboarding", "receipt"):
            wait_for(b"Press any key to continue...")
        else:
            wait_for(b">\x1b[0m ")
            if stage == "target":
                send(b"x")
                wait_for(b"cancel")
        send(b"\x1b[M")
        for byte in (b" ", b"C", b"C"):
            time.sleep(0.05)  # Beyond burst detection, within sequence lookahead.
            send(byte)
        if stage in ("onboarding", "receipt"):
            wait_for(b">\x1b[0m ")
        time.sleep(0.15)
        conn = wd.connect(path)
        try:
            assert wd.read_player(conn, 0).turns_used == 0
            assert all(e.controller_user_id is None for e in wd.list_exchanges(conn))
        finally:
            conn.close()
        send(b"q")
        if stage == "target":
            wait_for(b">\x1b[0m ")
            send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""


@pytest.mark.parametrize("stage", ["onboarding", "receipt", "menu", "target"])
def test_extended_x10_mouse_encoding_stops_without_spending_a_turn(tmp_path, stage):
    with _running_door(
        tmp_path, new_player=stage == "onboarding", event=stage == "receipt",
    ) as (process, path, wait_for, send, output):
        if stage in ("onboarding", "receipt"):
            wait_for(b"Press any key to continue...")
        else:
            wait_for(b">\x1b[0m ")
            if stage == "target":
                send(b"x")
                wait_for(b"cancel")
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
        if clock[0] == now:
            clock[0] += wd.SEASON
        return "A"
    monkeypatch.setattr(wd, 'show_text_pages', accept_preview)
    assert wd.main() == 0
    conn = wd.connect(path)
    player = wd.read_player(conn, 0)
    assert (player.season_number, player.cash, player.crew, player.turns_used) == (2, 225, 4, 1)
    assert wd.read_player(conn, 1).cash == wd.STARTING_CASH
    assert 'Season changed.' in output.getvalue()
    assert 'season 2 has started' in output.getvalue()
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


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
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
            if "END" in _ANSI_RE.sub("", "".join(written).split("\x1b[2J\x1b[H")[-1]):
                return "B"
            return "N"
        monkeypatch.setattr(wd, "read_menu_choice", choose)
    wd.show_event_history(wd.Palette(False), conn, 1, width, height, unseen_only=unseen_only)
    screens = "".join(written).split("\x1b[2J\x1b[H")[1:-1]
    assert len(screens) > 1
    body = []
    for screen in screens:
        lines = _ANSI_RE.sub("", screen).rstrip("\r\n").split("\r\n")
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
        body.extend(line for line in lines if "界" in line or "END" in line)
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
    wd.show_event_history(wd.Palette(False), conn, 1, 20, 10)
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
    assert read_ids == ids[:3]
    assert wd.history_events(conn, 1)[0].seen_at is None
    conn.close()


def test_real_process_history_is_replayable_and_free(tmp_path):
    with _running_door(tmp_path, event=True) as (process, path, wait_for, send, output):
        wait_for(b"Press any key to continue")
        send(b" ")
        wait_for(b">\x1b[0m ")
        send(b"h")
        wait_for(b"[A]ck page [B]ack")
        assert b"EVENT HISTORY" in output and b"[READ]" in output
        send(b"b")
        wait_for(b">\x1b[0m ")
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
    assert [e.id for e in wd.unseen_events(conn, 1)] == ids[3:]
    conn.close()


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
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
    screens = "".join(written).split("\x1b[2J\x1b[H")[1:]
    for screen in screens:
        lines = _ANSI_RE.sub("", screen).split("\r\n")
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    text = _ANSI_RE.sub("", "".join(written))
    assert "999,999,999" in text
    assert "Season end:" in text
    assert "Next:" not in text  # Already at the highest tier.
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
    assert "Next: Wannabe in 100 Rank" in text
    assert "newcomer, 2d 0h 0m remaining" in text
    wd.resolve_recruit(conn, player, now)
    state = wd.dashboard_state(conn, 1, now + timedelta(hours=1))
    text = "\n".join(wd.dashboard_lines(state, now + timedelta(hours=1)))
    assert "Turns left: 14/15" in text
    assert "Turn refill in 23h 0m" in text
    assert "Next: Wannabe in 90 Rank" in text
    conn.close()


def test_real_process_dashboard_keeps_action_result_until_acknowledged(tmp_path):
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b">\x1b[0m ")
        assert b"SWITCHBOARD" in output and b"New events: 0" in output
        send(b"c")
        wait_for(b"[A]Act [B]ack")
        send(b"a")
        wait_for(b"Press any key to continue...")
        after_result = len(output)
        # Acknowledgement is a real input boundary: no automatic dashboard redraw.
        assert b"SWITCHBOARD" not in output[output.index(b"A new member joins"):]
        conn = wd.connect(path)
        assert wd.read_player(conn, 0).turns_used == 1
        conn.close()
        send(b" ")
        wait_for(b">\x1b[0m ")
        assert b"SWITCHBOARD" in output[after_result:]
        send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
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
        screen = "".join(written).split("\x1b[2J\x1b[H")[-1]
        # Every picker/preview key is driven by the currently displayed choices.
        assert wd.read_player(conn, actor.user_id).turns_used == 1
        if "[A]Act" in screen:
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
    for screen in "".join(written).split("\x1b[2J\x1b[H")[1:]:
        lines = _ANSI_RE.sub("", screen).split("\r\n")
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize("disconnect", [False, True])
def test_real_process_garrison_preview_cancel_and_disconnect_preserve_assignment(tmp_path, disconnect):
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b">\x1b[0m ")
        conn = wd.connect(path)
        actor = wd.read_player(conn, 0)
        wd.resolve_root_exchange(conn, actor, 1, wd.now_utc(), __import__("random").Random(1))
        conn.close()
        send(b"g")
        wait_for(b"[B]ack (Q cancel)")
        send(b"1")
        wait_for(b"[B]ack (Q cancel)")
        send(b"1")
        wait_for(b"[A]Act [B]ack")
        if disconnect:
            process.stdin.close()
        else:
            send(b"b")
            wait_for(b">\x1b[0m ")
            send(b"q")
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""
        conn = wd.connect(path)
        actor = wd.read_player(conn, 0)
        assert (actor.crew, wd.assigned_crew(conn, 0), actor.turns_used) == (2, 1, 1)
        conn.close()


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_text_screen_pages_preserve_content_with_clear_back_path(monkeypatch, width, height):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", width)
    def choose(valid):
        assert "B" in valid
        screen = "".join(written).split("\x1b[2J\x1b[H")[-1]
        return "B" if "LAST RECORD" in screen else "N"
    monkeypatch.setattr(wd, "read_menu_choice", choose)
    wd.show_text_pages(wd.Palette(False), "RIVAL DIRECTORY", ["界e\u0301" * 200, "LAST RECORD"], width, height)
    screens = "".join(written).split("\x1b[2J\x1b[H")[1:]
    for screen in screens:
        lines = _ANSI_RE.sub("", screen).split("\r\n")
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
        assert lines[-1] == "[B]ack"
    assert "".join(written).count("界") == 200


@pytest.mark.parametrize("key,heading", [(b"b", b"SEASON STANDINGS"), (b"e", b"EXCHANGE TERRITORY"),
                                         (b"v", b"RIVAL DIRECTORY"), (b"?", b"HOW TO PLAY")])
def test_real_process_browsing_screens_are_free_and_do_not_ack_events(tmp_path, key, heading):
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b">\x1b[0m ")
        conn = wd.connect(path)
        wd.record_event(conn, 0, "Rival", "Arrived in session", wd.now_utc())
        conn.close()
        send(key)
        wait_for(b"[B]ack")
        assert heading in output
        send(b"b")
        wait_for(b">\x1b[0m ")
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
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b">\x1b[0m ")
        send(key)
        if key == b"x":
            wait_for(b"cancel")
            send(b"1")
        if key == b"j":
            wait_for(b"cancel")
            send(b"1")
            wait_for(b"cancel")
            send(b"1")
        wait_for(b"[A]Act [B]ack")
        assert b"Cost: 1 turn" in output
        if disconnect:
            process.stdin.close()
        else:
            send(b"b")
            wait_for(b">\x1b[0m ")
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
    result = wd.show_text_pages(wd.Palette(False), "PREVIEW", ["Risk details " * 40], 20, 10, accept=True)
    assert result == "A" and len(states) > 1
    assert all("A" not in state for state in states[:-1])
    assert "A" in states[-1]


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
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
    screens = "".join(written).split("\x1b[2J\x1b[H")[1:]
    for screen in screens:
        lines = _ANSI_RE.sub("", screen).split("\r\n")
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    assert "".join(written).count("界") == 150


@pytest.mark.parametrize("width,height", [(20, 10), (40, 12), (80, 24)])
def test_result_pages_keep_all_net_changes_readable(monkeypatch, width, height):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd, "_OUTPUT_WIDTH", width)
    monkeypatch.setattr(wd, "read_input_key", lambda: " ")
    delta = wd.ActionDelta(cash=-1234567890123, crew=0, heat=-90, rank=500, turns=1)
    wd.show_action_result(wd.Palette(False), ["You root " + "界e\u0301" * 150], delta, True, width, height)
    screens = "".join(written).split("\x1b[2J\x1b[H")[1:]
    for screen in screens:
        lines = _ANSI_RE.sub("", screen).split("\r\n")
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    text = _ANSI_RE.sub("", "".join(written))
    assert text.count("界") == 150
    assert "crew: +0" in text
    assert "turns spent: 1" in " ".join(text.split())


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
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b">\x1b[0m ")
        conn = wd.connect(path)
        conn.execute("UPDATE players SET turns_used=15, turn_day_start=heat_updated_at")
        conn.close()
        send(key)
        wait_for(b"[B]ack")
        assert heading in output
        assert b"No turns. Refill at" in output
        send(b"b")
        wait_for(b">\x1b[0m ")
        send(b"q")
        assert process.wait(timeout=5) == 0
        conn = wd.connect(path)
        assert wd.read_player(conn, 0).turns_used == 15
        conn.close()


def test_new_player_gets_short_first_visit_then_switchboard(tmp_path):
    with _running_door(tmp_path, new_player=True) as (process, path, wait_for, send, output):
        wait_for(b"Press any key to continue...")
        assert b"FIRST VISIT" in output
        assert b"[E]Map first" in output
        send(b" ")
        wait_for(b">\x1b[0m ")
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
    output = " ".join(_ANSI_RE.sub("", "".join(written)).split())
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


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
        page = next(re.search(r'Page (\d+)/(\d+)', text) for text in reversed(written) if 'Page ' in text)
        return 'N' if page[1] != page[2] else 'B'
    monkeypatch.setattr(wd, 'read_menu_choice', choose)
    wd.show_player_directory(wd.Palette(False), conn, 1, width, height)
    text = _ANSI_RE.sub('', ''.join(written))
    assert 'Raid shield until' in ' '.join(text.split())
    assert (now + wd.RAID_SHIELD).strftime('%Y-%m-%d') in text
    assert '87654321' not in text and '87,654,321' not in text
    assert '7654321' not in text and '7,654,321' not in text
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
        screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
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
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
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
            screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
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
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b'>\x1b[0m ')
        send(b'j')
        wait_for(b'cancel')
        if stage == 'approach':
            send(b'1')
            wait_for(b'cancel')
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b''
        conn = wd.connect(path)
        player = wd.read_player(conn, 0)
        assert (player.cash, player.crew, player.turns_used, player.successful_jobs) == (300, 3, 0, 0)
        conn.close()


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
        screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
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
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
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
        screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
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
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b'>\x1b[0m ')
        send(b's')
        wait_for(b'cancel')
        if stage != 'board':
            send(b'1')
            wait_for(b'[A]Act')
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


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
            screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
            if 'PREVIEW' in screen: return 'A' if 'A' in valid else 'N'
            return '1' if '1' in valid else 'N'
        monkeypatch.setattr(wd, 'read_menu_choice', select)
        assert wd.do_operations_hub(wd.Palette(False), conn, actor, Success(), width, height)
    assert (actor.operation_stage, actor.successful_operations, actor.cash, actor.turns_used) == (0, 1, 334, 3)
    text = _ANSI_RE.sub('', ''.join(written))
    assert all(title in text for title in ('CASE PREVIEW', 'PREPARE PREVIEW', 'EXECUTE PREVIEW'))
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
        screen = text.split('\x1b[2J\x1b[H')[-1]
        if 'RECON PREVIEW' in screen: return 'A' if 'A' in valid else 'N'
        return '1' if 'Rival62' in text and '1' in valid else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    assert wd.do_recon(wd.Palette(False), conn, actor, width, height)
    dossiers = wd.read_dossiers(conn, 1, now)
    assert len(dossiers) == 1 and dossiers[0]['target'] == 62
    assert 'Last-known' in ''.join(written)
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
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
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b'>\x1b[0m ')
        conn = wd.connect(path)
        now = wd.now_utc()
        wd.load_or_create_player(conn, 2, 'Rival', now, 1)
        conn.close()
        for index, key in enumerate(keys):
            send(key)
            if index == len(keys) - 1:
                wait_for(marker)
            elif keys[index + 1] == b'a':
                wait_for(b'[A]Act')
            else:
                wait_for(b'cancel')
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
        screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
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
        screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
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
    assert expected in advice and '[O]Ops' in advice
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
    assert '[O]Ops' not in ''.join(output)
    assert '3 turns and $50' in ''.join(output)
    conn.close()


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('commit', [False, True])
def test_real_process_owner_service_cancel_or_commit(tmp_path, commit):
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b'>\x1b[0m ')
        conn = wd.connect(path)
        conn.execute('UPDATE exchanges SET controller_user_id=0, garrison=1, controlled_since=? WHERE id=1', (wd.to_iso(wd.now_utc()),))
        actor = wd.read_player(conn, 0)
        exchange = wd.list_exchanges(conn)[0]
        service_key = wd.PICK_KEYS[len(wd.garrison_options(actor, exchange))].encode()
        conn.close()
        send(b'g')
        wait_for(b'cancel')
        send(b'1')
        wait_for(b'cancel')
        send(service_key)
        wait_for(b'[A]Act')
        send(b'a' if commit else b'b')
        if commit:
            wait_for(b'Carrier recruitment:')
            send(b' ')
        wait_for(b'>\x1b[0m ')
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
    captured = []
    monkeypatch.setattr(wd, 'show_text_pages', lambda p, title, lines, *args: captured.extend(lines))
    wd.show_territory(wd.Palette(False), conn, 80, 24, viewer_id=1)
    text = '\n'.join(captured)
    assert 'Ring links: #1 -- #2' in text and '#10 -- #1' in text
    assert 'Links: #10, #2' in text and 'Capture $40 (discount $10)' in text
    assert 'garrison 1; security +2; total defense 3' in text
    assert all(role in text for role in ('Public PBX', 'Carrier Switch', 'Warez Hub', 'Lay Low', 'Recruit:', 'Warez outlet'))
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


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
    calls = 0
    def select(valid):
        nonlocal calls
        calls += 1
        assert calls < 200 and 'B' in valid
        screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
        page = re.search(r'Page (\d+)/(\d+)', screen)
        return 'B' if page.group(1) == page.group(2) else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    wd.show_territory(wd.Palette(False), conn, width, height, viewer_id=1)
    body = []
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        body.extend(_ANSI_RE.sub('', screen).split('\r\n')[2:-2])
    normalized = ' '.join(' '.join(body).split())
    assert 'NPC returns at' in normalized and 'if still unclaimed.' in normalized
    assert 'NPC: Night Relay' in normalized and 'NPC: Spool Archive' in normalized
    assert wd.read_player(conn, 1).turns_used == 0
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('stage', ['cancel', 'disconnect', 'committed'])
def test_real_process_neutral_capture_preview_boundaries(tmp_path, stage):
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b'>\x1b[0m ')
        send(b'x')
        for _ in range(10):
            wait_for(b'cancel')
            screen = bytes(output).split(b'\x1b[2J\x1b[H')[-1].decode('utf-8', errors='replace')
            match = re.search(r'\[([0-9]+)\]Pick', screen)
            if match and '5' in match.group(1): break
            send(b'n')
        else: pytest.fail('NPC home never became selectable')
        send(b'5')
        wait_for(b'[A]Act')
        assert b'NPC: Patch Panel Society' in output and b'Success: 60%' in output
        if stage == 'cancel':
            send(b'b')
            wait_for(b'>\x1b[0m ')
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


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
        screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
        page = re.search(r'Page (\d+)/(\d+)', screen)
        return 'B' if page.group(1) == page.group(2) else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    wd.do_scene(wd.Palette(False), conn, actor, width, height)
    assert (actor.cash, actor.turns_used, actor.crew, actor.heat) == (0, 15, 3, 0)
    assert actor.insignia == ('archive' if entry == '1' else 'modem')
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('stage', ['scene', 'preview', 'committed'])
def test_real_process_scene_disconnect_preserves_only_selected_insignia(tmp_path, stage):
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b'>\x1b[0m ')
        send(b'i')
        wait_for(b'cancel')
        assert b'BBS SCENE' in output
        if stage != 'scene':
            send(b'1')
            wait_for(b'cancel')
            send(b'4')
            wait_for(b'[A]Act')
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
@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
        assert '[J]Jobs and [O]Operations need no rival' in ' '.join(lines)
        guidance = ' '.join(lines)
        assert guidance.index('[B]ack to the switchboard') < guidance.index('[J]Jobs')
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
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
        screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
        page = re.search(r'Page (\d+)/(\d+)', screen)
        return 'B' if page.group(1) == page.group(2) else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    before = list(conn.iterdump())
    wd.do_scene(wd.Palette(False), conn, actor, width, height)
    assert list(conn.iterdump()) == before
    body = []
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
        if 'SEASON RESULTS' in lines[0]: body.extend(lines[2:-2])
    normalized = ' '.join(' '.join(body).split())
    assert 'Gold: HistoricalCaller, Rank 10' in normalized
    assert 'inactive' in normalized and 'Your result: #1, Rank 10.' in normalized
    conn.close()


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
        screen = ''.join(written).split('\x1b[2J\x1b[H')[-1]
        page = re.search(r'Page (\d+)/(\d+)', screen)
        return 'B' if page.group(1) == page.group(2) else 'N'
    monkeypatch.setattr(wd, 'read_menu_choice', select)
    before = list(conn.iterdump())
    wd.do_scene(wd.Palette(False), conn, actor, width, height)
    assert list(conn.iterdump()) == before
    body = []
    title = 'YOUR SEASON REPORTS' if choice == '5' else 'HALL OF FAME'
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
        if title in lines[0]: body.extend(lines[2:-2])
    normalized = ' '.join(' '.join(body).split())
    if played:
        assert '{##} HistoricalCaller' in normalized and 'RenamedCaller' not in normalized
        assert 'Gold; final Rank 10; #1 of 1.' in normalized
        if choice == '5':
            assert 'Gold 1, Silver 0, Bronze 0' in normalized
            assert 'Best retained Rank: 10' in normalized
    else:
        assert ('No completed-season result' if choice == '5' else 'No medals awarded') in normalized
    conn.close()


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
    assert 'even without a medal' in text and 'prepared operations reset' in text
    wd.resolve_recruit(conn, actor, late)
    after = start + wd.SEASON
    state = wd.dashboard_state(conn, 1, after)
    assert (state.player.cash, state.player.crew, state.player.turns_used) == (300, 3, 0)
    assert state.player.created_at == identity_age
    assert wd.is_in_grace(state.player, after)
    assert not wd.is_in_grace(state.player, after + wd.DAY)
    assert conn.execute('SELECT rank,medal FROM season_results WHERE user_id=1').fetchone()['rank'] == 10
    text = ' '.join(wd.dashboard_lines(state, after))
    assert 'Fresh competition: $300, 3 available crew and 15 turns' in text
    assert 'medals give no resource or protection bonus' in text
    wd.load_or_create_player(conn, 2, 'FirstVisitInSeasonTwo', after, 2)
    first_visit = ' '.join(wd.dashboard_lines(wd.dashboard_state(conn, 2, after), after))
    assert 'any retained results' in first_visit and 'Past results remain' not in first_visit
    assert conn.execute('SELECT COUNT(*) FROM season_results WHERE user_id=2').fetchone()[0] == 0
    monkeypatch.setattr(wd, '_OUTPUT_WIDTH', width)
    written = []
    monkeypatch.setattr(wd, 'out', written.append)
    before = list(conn.iterdump())
    for stamp in (late, after):
        # Refresh only forward; the saved late state is used for its own display.
        shown = state if stamp == after else wd.DashboardState(actor, [], 0, after, None)
        _, count = wd.draw_dashboard(wd.Palette(False), shown, stamp, width, height)
        for page in range(1, count): wd.draw_dashboard(wd.Palette(False), shown, stamp, width, height, page)
    assert list(conn.iterdump()) == before
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


@pytest.mark.parametrize('width,height', [(20, 10), (40, 12), (80, 24)])
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
    for screen in ''.join(written).split('\x1b[2J\x1b[H')[1:]:
        lines = _ANSI_RE.sub('', screen).split('\r\n')
        assert len(lines) <= height
        assert all(sum(wd._char_width(ch) for ch in line) <= width for line in lines)
    conn.close()


def test_ascii_monochrome_output_preserves_controls_names_and_noncolor_results(monkeypatch, capsys):
    monkeypatch.setattr(wd, '_ASCII_DECOR', True)
    monkeypatch.setattr(wd, '_MONOCHROME', True)
    wd.out('\x1b[2J\x1b[38;5;51m\u2554\u2550\u2551\u2557\x1b[0m Caller Jos\u00e9: +10 Rank')
    assert capsys.readouterr().out == '\x1b[2J+-|+ Caller Jos\u00e9: +10 Rank'


def test_fast_mode_skips_only_optional_flavor_and_art(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, '_ASCII_DECOR', False)
    monkeypatch.setattr(wd, '_MONOCHROME', False)
    rendered = []
    monkeypatch.setattr(wd, 'show_text_pages', lambda p, title, lines, *a, **k: rendered.append((title, lines)))
    palette = wd.Palette(False)
    delta = wd.ActionDelta(cash=-25, crew=-1, heat=4, rank=0, turns=1)
    wd.show_action_result(palette, ['Attempt failed.'], delta, True, 40, 12)
    normal = rendered[-1][1]
    palette.fast = True
    wd.show_action_result(palette, ['Attempt failed.'], delta, True, 40, 12)
    fast = rendered[-1][1]
    assert len(normal) == len(fast) + 1
    assert all(line in normal for line in fast)
    assert any('BUSTED' in line for line in fast)
    assert any('turns spent: 1' in line for line in fast)
    conn = wd.connect(tmp_path / 'art.db')
    wd.ensure_schema(conn)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, 1, 'Caller', now, 1)
    palette.fast = False
    wd.show_territory(palette, conn, 40, 12, viewer_id=1)
    assert all(art in rendered[-1][1] for art in wd.ROLE_ART.values())
    palette.fast = True
    wd.show_territory(palette, conn, 40, 12, viewer_id=1)
    assert not any(art in rendered[-1][1] for art in wd.ROLE_ART.values())
    assert any('Owner:' in line for line in rendered[-1][1])
    conn.close()


@pytest.mark.parametrize('toggle', [False, True])
def test_real_process_display_disconnect_preserves_only_chosen_toggle(tmp_path, toggle):
    with _running_door(tmp_path) as (process, path, wait_for, send, output):
        wait_for(b'>\x1b[0m ')
        send(b'i')
        for _ in range(10):
            wait_for(b'cancel')
            screen = bytes(output).split(b'\x1b[2J\x1b[H')[-1]
            if b'[7]Pick' in screen: break
            send(b'n')
        else: pytest.fail('Display entry was not reachable')
        send(b'7')
        wait_for(b'cancel')
        assert b'DISPLAY' in output
        if toggle:
            send(b'1')
            wait_for(b'ASCII decorations: ON')
        process.stdin.close()
        assert process.wait(timeout=5) == 0 and process.stderr.read() == b''
        conn = wd.connect(path)
        assert wd.read_display(conn, 0) == ({'ascii_art': True} if toggle else {})
        assert wd.read_player(conn, 0).turns_used == 0
        conn.close()
