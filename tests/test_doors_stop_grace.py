"""How long a door gets to exit before it is killed.

The grace was a fixed 0.5 s: long enough for a process which exits on SIGTERM,
far too short for one which flushes anything first. A DOS game writing its
scores through the emulator is the case that matters, and it is reached on
every caller disconnect, not only at shutdown.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

from netbbs.doors.profiles import DoorProfile, ProfileError
from netbbs.doors.runtime import DOOR_STOP_GRACE_SECONDS, _stop_process


def test_the_default_replaces_the_old_fixed_half_second():
    assert DoorProfile().stop_grace_seconds == DOOR_STOP_GRACE_SECONDS == 5


def test_a_profile_saved_before_this_existed_picks_up_the_new_default(tmp_path):
    """The upgrade property: an already-registered door is fixed by upgrading,
    without a SysOp opening and re-saving its Compatibility screen."""
    stored = json.dumps({"version": 1, "adapter": "dosbox", "endpoint": "socketpair",
                         "encoding": "cp437", "install_dir": str(tmp_path),
                         "width": 80, "height": 25, "options": {"command": "GAME.EXE"}})

    assert "stop_grace_seconds" not in stored
    assert DoorProfile.from_json(stored).stop_grace_seconds == 5


def test_the_grace_is_bounded_at_both_ends():
    DoorProfile(stop_grace_seconds=1).validate()
    DoorProfile(stop_grace_seconds=60).validate()
    for bad in (0, 61):
        with pytest.raises(ProfileError, match="stop_grace_seconds"):
            DoorProfile(stop_grace_seconds=bad).validate()


def test_it_survives_a_json_round_trip():
    profile = DoorProfile(stop_grace_seconds=20).validate()
    assert DoorProfile.from_json(profile.to_json()).stop_grace_seconds == 20


def _spawn(body):
    return asyncio.create_subprocess_exec(
        sys.executable, "-c", body,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        **({"start_new_session": True} if os.name == "posix" else {}))


def test_a_door_which_exits_promptly_never_waits_out_the_grace():
    """Why raising the default is safe: the wait ends when the door does.

    Only a door which refuses to exit pays for a longer grace, and that is
    exactly the door the longer grace exists for.
    """
    async def scenario():
        proc = await _spawn("pass")
        while proc.returncode is None:
            await asyncio.sleep(0.01)

        started = time.monotonic()
        await _stop_process(proc, grace=30)
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 5, f"stopping an already-exited door took {elapsed:.1f}s of a 30s grace"


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal handling")
def test_a_door_which_ignores_sigterm_is_given_its_grace_then_killed():
    async def scenario():
        proc = await _spawn(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(300)\n")
        await asyncio.sleep(0.3)  # let the handler be installed

        started = time.monotonic()
        await _stop_process(proc, grace=2)
        elapsed = time.monotonic() - started
        return proc.returncode, elapsed

    returncode, elapsed = asyncio.run(scenario())
    assert returncode is not None, "SIGKILL did not follow the grace"
    assert elapsed >= 1.5, f"killed after {elapsed:.1f}s -- the grace was not honoured"
    assert elapsed < 12, f"took {elapsed:.1f}s for a 2s grace"


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal handling")
def test_a_door_which_flushes_on_sigterm_gets_time_to_finish(tmp_path):
    """The case this exists for: 0.5 s was not enough to write anything."""
    marker = tmp_path / "scores.saved"

    async def scenario():
        proc = await _spawn(
            "import signal, sys, time\n"
            "def flush(*unused):\n"
            "    time.sleep(1.5)\n"
            f"    open({str(marker)!r}, 'w').write('saved')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, flush)\n"
            "time.sleep(300)\n")
        await asyncio.sleep(0.3)
        await _stop_process(proc, grace=10)

    asyncio.run(scenario())

    assert marker.exists(), "the door was killed before it could write its game data"
