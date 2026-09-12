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
