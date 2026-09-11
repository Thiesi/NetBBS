"""Per-door CPU and wall-clock ceilings (issue #467).

The defaults reproduce the original fixed constants, so these tests are as
much about what did *not* change for an existing profile as about the new
explicit opt-outs.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from netbbs.doors import create_door
from netbbs.doors.profiles import DoorProfile, ProfileError, limit_advisories
from netbbs.doors.runtime import (DOOR_CPU_LIMIT_SECONDS, WALL_TIME_LIMIT_SECONDS,
                                  effective_wall_limit, run_door)
from tests.test_doors_runtime import FakeSession, _run, _write_script, db, lane, player


def test_defaults_reproduce_the_original_fixed_constants():
    profile = DoorProfile()
    assert profile.time_limit == WALL_TIME_LIMIT_SECONDS
    assert profile.cpu_seconds == DOOR_CPU_LIMIT_SECONDS


def test_unprofiled_door_keeps_the_original_ceiling():
    assert effective_wall_limit(None) == WALL_TIME_LIMIT_SECONDS


def test_profile_may_exceed_the_old_one_hour_maximum():
    assert effective_wall_limit(DoorProfile(time_limit=7200)) == 7200


def test_zero_time_limit_is_no_ceiling_rather_than_an_instant_timeout():
    # The obvious min() folds 0 in as the smallest bound and times the door
    # out immediately; the opt-out has to be filtered out, not minimised.
    assert effective_wall_limit(DoorProfile(time_limit=0)) is None


def test_a_call_site_bound_still_wins_when_it_is_tighter():
    # The DOSBox capability probe relies on this: it passes 12 seconds and
    # must not inherit the profile's hour, nor be defeated by an opt-out.
    assert effective_wall_limit(DoorProfile(time_limit=3600), 12) == 12
    assert effective_wall_limit(DoorProfile(time_limit=0), 12) == 12
    assert effective_wall_limit(DoorProfile(time_limit=60), 3600) == 60


@pytest.mark.parametrize("field", ["time_limit", "cpu_seconds"])
def test_ceilings_accept_zero_and_reject_out_of_range(field):
    DoorProfile(**{field: 0}).validate()
    DoorProfile(**{field: 86400}).validate()
    for bad in (-1, 86401):
        with pytest.raises(ProfileError, match=field):
            DoorProfile(**{field: bad}).validate()


def test_cpu_seconds_survives_a_json_round_trip():
    profile = DoorProfile(cpu_seconds=1800, time_limit=0).validate()
    assert DoorProfile.from_json(profile.to_json()) == profile


def test_advisories_explain_removed_ceilings_and_stay_quiet_by_default():
    assert limit_advisories(DoorProfile()) == []
    assert limit_advisories(None) == []
    assert any("node lease" in note for note in limit_advisories(DoorProfile(time_limit=0)))
    assert any("CPU-seconds" in note for note in limit_advisories(DoorProfile(cpu_seconds=0)))
    assert any("7200" in note for note in limit_advisories(DoorProfile(time_limit=7200)))


def test_door_with_no_wall_limit_runs_to_its_own_exit(db, lane, player, tmp_path):
    """The regression this guards: an unbounded door ending instantly."""
    script = _write_script(tmp_path, "quick.py", "import sys; sys.stdout.write('done')")
    door = create_door(db, "Unbounded", sys.executable, args=(str(script),), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path), time_limit=0))

    result = asyncio.run(_run(FakeSession(), lane, door, player))

    assert result.reason == "exited"
    assert result.exit_code == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX resource limits")
@pytest.mark.parametrize("cpu_seconds", [77, 0])
def test_profile_cpu_seconds_reaches_the_door_process(cpu_seconds, db, lane, player, tmp_path):
    script = _write_script(tmp_path, "report_cpu.py",
                           "import resource,sys; sys.stdout.write(str(resource.getrlimit(resource.RLIMIT_CPU)[0]))")
    door = create_door(db, f"CPU {cpu_seconds}", sys.executable, args=(str(script),), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path), cpu_seconds=cpu_seconds))

    session = FakeSession()
    result = asyncio.run(_run(session, lane, door, player, wall_time_limit_seconds=30))

    assert result.reason == "exited"
    reported = session.written.decode()
    if cpu_seconds:
        assert reported == "77"
    else:
        # Zero must leave the inherited limit alone, not set a limit of zero.
        assert reported != "0"
