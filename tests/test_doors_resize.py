"""Forwarding a caller's terminal resize into a running door (issue #468).

The policy is checked on every platform; the real PTY ioctl and the real
SIGUSR1 delivery need POSIX and run against actual processes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from netbbs.doors import create_door
from netbbs.doors.profiles import DoorProfile, ProfileError, profile_advisories
from netbbs.doors.runtime import _republish_terminal_size, resize_mode, run_door
from tests.test_doors_runtime import FakeSession, _write_script, db, lane, player


def test_pty_doors_follow_the_caller_without_opting_in():
    assert resize_mode(DoorProfile(endpoint="pty"), "pty") == "pty"


@pytest.mark.parametrize("kind", ["stdio", "socketpair"])
def test_stdio_and_socket_doors_are_signalled_only_when_they_opt_in(kind):
    assert resize_mode(DoorProfile(), kind) is None
    assert resize_mode(DoorProfile(resize_signal=True), kind) == "signal"


def test_a_pinned_screen_is_never_resized():
    # The profile asked for exactly this geometry; following the caller would
    # contradict it, and the same rule keeps DOS's fixed 80x25 out.
    assert resize_mode(DoorProfile(endpoint="pty", width=80, height=25), "pty") is None
    assert resize_mode(DoorProfile(resize_signal=True, width=80, height=25), "stdio") is None


def test_non_native_adapters_are_never_signalled():
    dos = DoorProfile(adapter="dosbox", endpoint="socketpair", encoding="cp437", install_dir=os.getcwd(),
                      width=80, height=25, resize_signal=True, options={"command": "GAME.EXE"})
    assert resize_mode(dos, "socketpair") is None


def test_unprofiled_doors_are_never_signalled():
    assert resize_mode(None, "stdio") is None


def test_resize_signal_must_be_a_boolean():
    with pytest.raises(ProfileError, match="resize_signal"):
        DoorProfile(resize_signal="yes").validate()


def test_advisory_names_each_way_the_opt_in_silently_does_nothing():
    assert profile_advisories(DoorProfile(resize_signal=True)) == []
    assert any("PTY door" in note
               for note in profile_advisories(DoorProfile(endpoint="pty", resize_signal=True)))
    assert any("pins the terminal size" in note
               for note in profile_advisories(DoorProfile(resize_signal=True, width=80, height=25)))
    assert any("dosbox" in note for note in profile_advisories(
        DoorProfile(adapter="dosbox", endpoint="socketpair", encoding="cp437", install_dir=os.getcwd(),
                    width=80, height=25, resize_signal=True, options={"command": "GAME.EXE"})))


def test_republished_metadata_keeps_the_other_fields_and_replaces_atomically(tmp_path):
    path = tmp_path / "door_info.json"
    original = {"handle": "Carrier", "user_id": 7, "terminal_width": 80, "terminal_height": 24}
    path.write_text(json.dumps(original), encoding="utf-8")

    updated = _republish_terminal_size(path, original, 132, 50)

    assert json.loads(path.read_text(encoding="utf-8")) == updated
    assert updated["terminal_width"] == 132 and updated["terminal_height"] == 50
    assert updated["handle"] == "Carrier" and updated["user_id"] == 7
    assert original["terminal_width"] == 80, "the caller's dict must not be mutated in place"
    assert not list(tmp_path.glob("*.new")), "no temporary file may be left behind"


async def _play_until_resized(session, lane, door, player):
    """Start the door, wait for READY, resize the caller, wait for its report."""
    task = asyncio.create_task(run_door(session, lane, door, player, wall_time_limit_seconds=30))
    try:
        async with asyncio.timeout(25):
            while b"READY" not in session.written:
                if task.done():
                    pytest.fail(f"door never started: {task.result()}")
                await asyncio.sleep(0.01)
            session.terminal_width, session.terminal_height = 132, 50
            while b"SIZE" not in session.written:
                if b"NOSIGNAL" in session.written:
                    pytest.fail("the door was never woken: the resize signal did not arrive")
                if task.done():
                    pytest.fail(f"door never saw the resize: {bytes(session.written)!r}")
                await asyncio.sleep(0.01)
        await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert b"SIZE 132 50" in session.written


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_opted_in_stdio_door_is_woken_and_reads_the_new_size(db, lane, player, tmp_path):
    script = _write_script(tmp_path, "resize_door.py", """
        import json, os, signal, sys, time
        seen = []
        signal.signal(signal.SIGUSR1, lambda *unused: seen.append(1))
        sys.stdout.write("READY\\n"); sys.stdout.flush()
        deadline = time.time() + 20
        while not seen and time.time() < deadline:
            time.sleep(0.05)
        if not seen:
            # Never report a geometry we were not woken for. The metadata file
            # is rewritten either way, so printing it regardless would let a
            # removed SIGUSR1 send keep passing this test.
            sys.stdout.write("NOSIGNAL\\n"); sys.stdout.flush()
            raise SystemExit(9)
        info = json.load(open(os.environ["NETBBS_DOOR_INFO"]))
        sys.stdout.write("SIZE %s %s\\n" % (info["terminal_width"], info["terminal_height"]))
        sys.stdout.flush()
    """)
    door = create_door(db, "Resizer", sys.executable, args=(str(script),), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path), resize_signal=True))

    asyncio.run(_play_until_resized(FakeSession(), lane, door, player))


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY window-size ioctl")
def test_pty_door_sees_the_new_window_size_on_its_own_terminal(db, lane, player, tmp_path):
    script = _write_script(tmp_path, "winch_door.py", """
        import fcntl, signal, struct, sys, termios, time
        seen = []
        signal.signal(signal.SIGWINCH, lambda *unused: seen.append(1))
        sys.stdout.write("READY\\n"); sys.stdout.flush()
        deadline = time.time() + 20
        while not seen and time.time() < deadline:
            time.sleep(0.05)
        if not seen:
            # A correct size read from the terminal proves nothing on its own;
            # without the signal this test must fail, not report the geometry.
            sys.stdout.write("NOSIGNAL\\n"); sys.stdout.flush()
            raise SystemExit(9)
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\\0" * 8))[:2]
        sys.stdout.write("SIZE %d %d\\n" % (cols, rows)); sys.stdout.flush()
    """)
    door = create_door(db, "Winch", sys.executable, args=(str(script),), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path), endpoint="pty"))

    asyncio.run(_play_until_resized(FakeSession(), lane, door, player))


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_a_door_which_did_not_opt_in_is_never_signalled(db, lane, player, tmp_path):
    """SIGUSR1's default action terminates a process; silence is what protects it."""
    script = _write_script(tmp_path, "unaware.py", """
        import sys, time
        sys.stdout.write("READY\\n"); sys.stdout.flush()
        time.sleep(3)
        sys.stdout.write("SURVIVED\\n"); sys.stdout.flush()
    """)
    door = create_door(db, "Unaware", sys.executable, args=(str(script),), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path)))

    async def scenario():
        session = FakeSession()
        task = asyncio.create_task(run_door(session, lane, door, player, wall_time_limit_seconds=30))
        try:
            async with asyncio.timeout(25):
                while b"READY" not in session.written:
                    if task.done():
                        pytest.fail(f"door never started: {task.result()}")
                    await asyncio.sleep(0.01)
                session.terminal_width, session.terminal_height = 132, 50
            result = await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert result.reason == "exited"
        assert b"SURVIVED" in session.written

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# NetBBS's own doors follow a resize without being asked to (issue #645).
#
# The two showcase doors kept their launch geometry for the whole session:
# shrink the terminal and the top of every screen scrolled away, grow it and the
# game stayed forty columns wide. The host had the mechanism since #468 and the
# doors had never opted in.
# ---------------------------------------------------------------------------


def _bundled(name):
    from netbbs.doors.bundled import BUNDLED_DOORS, resolve_bundled_door_path

    entry = next(door for door in BUNDLED_DOORS if door.key == name)
    return entry, resolve_bundled_door_path(entry)


@pytest.mark.parametrize("kind", ["stdio", "socketpair"])
def test_a_vouched_for_bundled_door_is_signalled_with_or_without_a_profile(kind):
    # The gallery registers a bundled door with no profile at all.
    assert resize_mode(None, kind, bundled_follows_resize=True) == "signal"
    assert resize_mode(DoorProfile(), kind, bundled_follows_resize=True) == "signal"
    # Vouching never overrides what a profile asked for.
    assert resize_mode(DoorProfile(width=80, height=25), kind, bundled_follows_resize=True) is None
    dos = DoorProfile(adapter="dosbox", endpoint="socketpair", encoding="cp437", install_dir=os.getcwd(),
                      width=80, height=25, options={"command": "GAME.EXE"})
    assert resize_mode(dos, kind, bundled_follows_resize=True) is None
    # And nobody else's door is signalled on NetBBS's say-so.
    assert resize_mode(None, kind) is None and resize_mode(DoorProfile(), kind) is None


def test_only_this_installs_own_copy_of_a_door_is_recognised(tmp_path):
    from netbbs.doors.bundled import launched_bundled_door

    for name in ("voidrunner", "war_dialer"):
        entry, path = _bundled(name)
        assert entry.follows_resize
        assert launched_bundled_door(sys.executable, (path.as_posix(),)) is entry
        assert launched_bundled_door(sys.executable, ("-m", f"netbbs.doors.bundled.{name}")) is entry
        # A copy somewhere else may be any version: its handler is not ours to vouch for.
        fork = tmp_path / path.name
        fork.write_bytes(path.read_bytes())
        assert launched_bundled_door(sys.executable, (fork.as_posix(),)) is None
        # Relative to an install directory that really is the package directory.
        assert launched_bundled_door(sys.executable, (path.name,), install_dir=str(path.parent)) is entry
        assert launched_bundled_door(sys.executable, (path.name,)) is None
    trivia, trivia_path = _bundled("retro_trivia")
    assert not trivia.follows_resize and launched_bundled_door(sys.executable, (trivia_path.as_posix(),)) is trivia
    assert launched_bundled_door(sys.executable, ("/somewhere/else.py",)) is None


def test_the_runtime_vouches_for_a_registered_bundled_door_and_no_other(db, player, tmp_path):
    from netbbs.doors.runtime import bundled_follows_resize

    _, path = _bundled("voidrunner")
    ours = create_door(db, "Voidrunner", sys.executable, args=(path.as_posix(),), creator=player)
    theirs = create_door(db, "Other", sys.executable, args=(_write_script(tmp_path, "other.py", "print('hi')").as_posix(),), creator=player)
    assert bundled_follows_resize(ours) and not bundled_follows_resize(theirs)


def _voidrunner():
    from tests.voidrunner.support import vr

    return vr


def test_voidrunner_redraws_at_the_new_size_when_a_resize_lands_at_an_action_bar(monkeypatch, tmp_path):
    vr = _voidrunner()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 40, "terminal_height": 12}), encoding="utf-8")
    monkeypatch.setenv("NETBBS_DOOR_INFO", str(info))
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    monkeypatch.setattr(vr, "_RESIZE_PENDING", False)
    monkeypatch.setattr(vr, "read_key", lambda: "Q")
    # No signal yet: an action bar reads its key as it always did.
    assert vr.read_command_at_prompt() == "Q" and (vr._OUTPUT_WIDTH, vr._OUTPUT_HEIGHT) == (80, 24)
    vr._note_resize()   # all the handler does
    vr._note_resize()   # and it may run twice for one resize
    assert (vr._OUTPUT_WIDTH, vr._OUTPUT_HEIGHT) == (80, 24), "nothing changes inside the handler"
    assert vr.read_command_at_prompt() == vr.RESIZE_KEY
    assert (vr._OUTPUT_WIDTH, vr._OUTPUT_HEIGHT) == (40, 12)
    assert vr.read_command_at_prompt() == "Q", "one resize is one redraw"


def test_voidrunner_draws_for_the_floor_when_shrunk_below_it_and_no_screen_owns_the_resize_key(monkeypatch, tmp_path):
    vr = _voidrunner()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 20, "terminal_height": 5}), encoding="utf-8")
    monkeypatch.setenv("NETBBS_DOOR_INFO", str(info))
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    monkeypatch.setattr(vr, "_RESIZE_PENDING", True)
    assert vr.take_resize() and (vr._OUTPUT_WIDTH, vr._OUTPUT_HEIGHT) == (vr.MINIMUM_WIDTH, vr.MINIMUM_HEIGHT)
    # A screen treats it like any key it does not know. It must never read as one it does.
    assert len(vr.RESIZE_KEY) > 1 and not vr.RESIZE_KEY.isspace()
    assert vr.RESIZE_KEY not in "MYBCSHGTQKLDPWONVX<>0123456789"


def test_war_dialer_takes_a_pending_resize_once_and_never_below_the_floor(monkeypatch, tmp_path):
    from netbbs.doors.bundled import war_dialer as wd

    info = tmp_path / "door_info.json"
    monkeypatch.setenv("NETBBS_DOOR_INFO", str(info))
    monkeypatch.setattr(wd, "_RESIZE_PENDING", False)
    base = {"user_id": 7, "handle": "Tester", "war_dialer_owner": "a" * 32}
    info.write_text(json.dumps(dict(base, terminal_width=132, terminal_height=43)), encoding="utf-8")
    assert wd.take_resize() is None
    wd._note_resize(); wd._note_resize()
    assert wd.take_resize() == (132, 43) and wd.take_resize() is None
    info.write_text(json.dumps(dict(base, terminal_width=10, terminal_height=3)), encoding="utf-8")
    wd._note_resize()
    assert wd.take_resize() == (wd.MINIMUM_WIDTH, wd.MINIMUM_HEIGHT)
    # A drop file caught unreadable keeps the size the door already has.
    info.write_text("{not json", encoding="utf-8")
    wd._note_resize()
    assert wd.take_resize() is None


def _signal_real_door(script, tmp_path, env, first_marker, prelude=(), wait=20.0):
    """Launch the shipped script at 80x24, rewrite its drop file to 40x12, send
    the real signal, and return what it wrote afterwards."""
    import signal
    import subprocess
    import time

    info = tmp_path / "door_info.json"
    proc = subprocess.Popen([sys.executable, "-u", str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=dict(os.environ, NETBBS_DOOR_INFO=str(info), PYTHONIOENCODING="utf-8", **env))
    os.set_blocking(proc.stdout.fileno(), False)
    seen = bytearray()

    def drain_until(marker, start=0):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            chunk = proc.stdout.read()
            if chunk:
                seen.extend(chunk)
            if marker in bytes(seen[start:]):
                return True
            if proc.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    try:
        assert drain_until(first_marker), bytes(seen)[-400:]
        for key, marker in prelude:
            # Typed, then waited for: the signal has to land at an action bar.
            proc.stdin.write(key); proc.stdin.flush()
            assert drain_until(marker), bytes(seen)[-400:]
        mark = len(seen)
        data = json.loads(info.read_text(encoding="utf-8"))
        _republish_terminal_size(info, data, 40, 12)
        os.kill(proc.pid, signal.SIGUSR1)
        assert drain_until(b"\x1b[2J", start=mark), "the door never redrew"
        time.sleep(0.6)
        seen.extend(proc.stdout.read() or b"")
        assert proc.poll() is None, "SIGUSR1 ended the door"
        return bytes(seen[mark:])
    finally:
        proc.kill(); proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_the_real_voidrunner_survives_the_signal_and_redraws_forty_columns_wide(tmp_path):
    vr = _voidrunner()
    from tests.voidrunner.support import _VOIDRUNNER_PATH, _world_with_seed, plain_bytes

    vr.write_save(tmp_path, 77, _world_with_seed(42).save)
    (tmp_path / "door_info.json").write_text(json.dumps(
        {"user_id": 77, "handle": "Tester", "terminal_width": 80, "terminal_height": 24}), encoding="utf-8")
    after = _signal_real_door(_VOIDRUNNER_PATH, tmp_path, {"VOIDRUNNER_SAVE_DIR": str(tmp_path)}, b"STATION SERVICES")
    rows = plain_bytes(after.rsplit(b"\x1b[2J", 1)[-1]).decode("utf-8", "replace").splitlines()
    assert any("Command Deck" in row for row in rows)
    assert max(len(row) for row in rows) <= 40, rows


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_the_real_war_dialer_survives_the_signal_and_redraws_its_switchboard(tmp_path):
    from netbbs.doors.bundled import war_dialer as wd

    script = _bundled("war_dialer")[1]
    (tmp_path / "door_info.json").write_text(json.dumps(
        {"user_id": 7, "handle": "Tester", "war_dialer_owner": "a" * 32, "terminal_width": 80, "terminal_height": 24}), encoding="utf-8")
    world = tmp_path / "world.db"
    # Past the first-visit primer, whose "press any key" is not an action bar.
    after = _signal_real_door(script, tmp_path, {"WAR_DIALER_DB_PATH": str(world)}, b"Press any key",
                              prelude=[(b" ", b"SWITCHBOARD")])
    rows = wd._strip_ansi(after.rsplit(b"\x1b[2J", 1)[-1].decode("utf-8", "replace")).splitlines()
    assert any("SWITCHBOARD" in row for row in rows)
    assert max(len(row) for row in rows) <= 40, rows


def test_the_guard_is_not_claimed_where_there_is_no_such_signal(monkeypatch):
    from netbbs.doors import runtime

    monkeypatch.setattr(runtime, "_CHILDREN_IGNORE_RESIZE_SIGNAL", False)
    if not hasattr(runtime.signal, "SIGUSR1"):
        assert runtime.children_start_ignoring_resize_signal() is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_a_child_spawned_after_the_guard_survives_a_signal_it_has_no_handler_for(monkeypatch):
    """The window the guard exists for: a process that has not yet said what to
    do with SIGUSR1, signalled the moment it exists (issue #645 review)."""
    import signal
    import subprocess

    from netbbs.doors import runtime

    before = signal.getsignal(signal.SIGUSR1)
    monkeypatch.setattr(runtime, "_CHILDREN_IGNORE_RESIZE_SIGNAL", False)
    try:
        assert runtime.children_start_ignoring_resize_signal() is True
        # Through an exec, as the launcher does it, and with no handler anywhere.
        child = subprocess.Popen([sys.executable, "-c",
                                  "import os,sys; os.execv(sys.executable,[sys.executable,'-c','import time; time.sleep(3)'])"])
        try:
            os.kill(child.pid, signal.SIGUSR1)
            import time
            time.sleep(1.0)
            os.kill(child.pid, signal.SIGUSR1)
            time.sleep(0.3)
            assert child.poll() is None, "SIGUSR1 ended a child that had no handler"
        finally:
            child.kill(); child.wait(timeout=5)
    finally:
        signal.signal(signal.SIGUSR1, before)


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_something_else_owning_the_signal_is_left_alone(monkeypatch):
    import signal

    from netbbs.doors import runtime

    before = signal.getsignal(signal.SIGUSR1)
    monkeypatch.setattr(runtime, "_CHILDREN_IGNORE_RESIZE_SIGNAL", False)
    mine = lambda signum, frame: None
    try:
        signal.signal(signal.SIGUSR1, mine)
        assert runtime.children_start_ignoring_resize_signal() is False
        assert signal.getsignal(signal.SIGUSR1) is mine
    finally:
        signal.signal(signal.SIGUSR1, before)


@pytest.mark.skipif(os.name != "posix", reason="the idle wait only exists on POSIX")
def test_a_key_the_decoder_already_holds_is_not_waited_for(monkeypatch):
    """Escape, then a hotkey a moment later: the hotkey is read off the pipe while
    deciding what the Escape was and kept in `pending`. The kernel has nothing
    left to report, so a wait on the descriptor would hold that key until the
    next one arrived (issue #645 review)."""
    vr = _voidrunner()
    read_end, write_end = os.pipe()
    try:
        reader = vr._DoorInput(vr._StdioBytes(os.fdopen(read_end, "rb", buffering=0, closefd=False)))
        reader.pending = b"M"
        monkeypatch.setattr(vr, "_INPUT_READER", reader)
        monkeypatch.setattr(vr, "_RESIZE_PENDING", False)
        import time
        started = time.monotonic()
        assert vr._resized_while_idle() is False
        assert time.monotonic() - started < 0.2, "waited on an empty pipe with a key in hand"
    finally:
        os.close(read_end); os.close(write_end)


def test_pages_cut_once_on_entry_are_cut_again_when_the_terminal_changes(monkeypatch, tmp_path):
    """A dozen Voidrunner screens paginate before their loop. They kept drawing
    rows wrapped and counted for the old terminal until the caller left; two were
    fixed by hand before both reviewers named the rest (issue #645 review)."""
    vr = _voidrunner()
    info = tmp_path / "door_info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 40, "terminal_height": 12}), encoding="utf-8")
    monkeypatch.setenv("NETBBS_DOOR_INFO", str(info))
    monkeypatch.setattr(vr, "_OUTPUT_WIDTH", 80)
    monkeypatch.setattr(vr, "_OUTPUT_HEIGHT", 24)
    lines = [f"Line {index}: " + "a fairly long sentence about trade " * 2 for index in range(30)]
    paged = {
        "service": vr._service_pages(lines, "Trading Ledger", "[N] Next [P] Prev [B] Back: "),
        "trade": vr._trade_pages(lines, "Opportunities", "[N] Next [P] Prev [B] Back: "),
        "text": vr._mission_text_pages(lines, overhead=6),
    }
    before = {name: len(pages) for name, pages in paged.items()}
    last = {name: len(pages) - 1 for name, pages in paged.items()}
    vr._note_resize()
    assert vr.take_resize()
    for name, pages in paged.items():
        assert len(pages) > before[name], name                      # shorter terminal, more pages
        assert all(vr._visible_width(row) <= 40 for page in pages for row in page), name
        assert all(len(page) <= 12 for page in pages), name
    # And back up: a page number that no longer exists reads as the last page.
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester", "terminal_width": 120, "terminal_height": 50}), encoding="utf-8")
    stale = {name: len(pages) - 1 for name, pages in paged.items()}
    vr._note_resize()
    assert vr.take_resize()
    for name, pages in paged.items():
        assert len(pages) <= before[name] and pages[stale[name]] == pages[len(pages) - 1], name
    # A list nobody holds any more is not kept alive by the registry.
    import gc
    paged.clear(); del pages; gc.collect()
    vr._service_pages(["x"], "T", "[B] Back: ")
    assert sum(1 for ref in vr._LIVE_PAGES if ref() is not None) <= 1
