"""Supervised long-lived companion processes (issue #466).

Real subprocesses throughout, like tests/test_doors_runtime.py: the whole
point of this subsystem is process lifetime, which a mock cannot exercise.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import time
from dataclasses import replace

import pytest

from netbbs.doors import create_door
from netbbs.doors.profiles import DoorProfile, ProfileError
from netbbs.doors.services import (BACKOFF, FAILED, RUNNING, STOPPED, DoorService, DoorServiceManager,
                                   ServiceStatus, service_spec)
from tests.test_doors_runtime import _write_script, db, lane, player


def _profile(tmp_path, **service):
    service.setdefault("argv", ["-c", "import time; time.sleep(60)"])
    return DoorProfile(install_dir=str(tmp_path), service=service)


def _door(db, player, tmp_path, name="Serviced", **service):
    return create_door(db, name, sys.executable, args=(), creator=player,
                       profile=_profile(tmp_path, **service))


# -- profile validation ---------------------------------------------------

def test_a_profile_without_a_service_declares_none():
    assert service_spec(DoorProfile()) is None
    assert service_spec(None) is None


def test_service_defaults_match_the_documented_ones(tmp_path):
    spec = service_spec(_profile(tmp_path))
    assert (spec.start, spec.stop_grace_seconds, spec.memory_mb) == ("with_node", 10, 512)
    assert spec.health_kind == "pid"


def test_install_dir_substitution_reaches_argv_and_health_path(tmp_path):
    spec = service_spec(DoorProfile(install_dir=str(tmp_path), service={
        "argv": ["-m", "game", "--dir", "{install_dir}"],
        "health": {"kind": "socket", "path": "{install_dir}/run/game.sock"}}))
    assert spec.argv[-1] == str(tmp_path.resolve())
    assert spec.health_path == f"{tmp_path.resolve()}/run/game.sock"


@pytest.mark.parametrize("service, message", [
    ({"argv": []}, "1-32"),
    ({"argv": ["ok"], "start": "sometimes"}, "with_node"),
    ({"argv": ["ok"], "stop_grace_seconds": 0}, "stop_grace_seconds"),
    ({"argv": ["ok"], "stop_grace_seconds": 61}, "stop_grace_seconds"),
    ({"argv": ["ok"], "service_memory_mb": 8}, "service_memory_mb"),
    ({"argv": ["ok"], "health": {"kind": "http"}}, "pid or socket"),
    ({"argv": ["ok"], "health": {"kind": "socket"}}, "socket health needs a path"),
    ({"argv": ["ok"], "health": {"kind": "pid", "path": 123}}, "pid health takes no path"),
    ({"argv": ["ok"], "health": {"kind": "pid", "path": "{unknown}"}}, "pid health takes no path"),
    ({"argv": ["ok"], "nonsense": 1}, "unknown service fields"),
    ({"argv": ["{node_dir}"]}, "install_dir"),
])
def test_invalid_service_blocks_are_rejected(service, message, tmp_path):
    with pytest.raises(ProfileError, match=message):
        DoorProfile(install_dir=str(tmp_path), service=service).validate()


def test_a_service_requires_an_installation_directory():
    with pytest.raises(ProfileError, match="installation directory"):
        DoorProfile(service={"argv": ["ok"]}).validate()


def test_service_survives_a_json_round_trip(tmp_path):
    profile = DoorProfile(install_dir=str(tmp_path),
                          service={"argv": ["-m", "game"], "start": "on_first_caller"}).validate()
    assert DoorProfile.from_json(profile.to_json()) == profile


# -- status rendering -----------------------------------------------------

def test_status_summary_reads_as_a_sentence_in_every_state():
    assert ServiceStatus().summary() == "not running"
    running = ServiceStatus(state=RUNNING, since=time.monotonic() - 3700, restarts=2)
    assert running.summary().startswith("running, up 1h01m, 2 restart(s)")
    assert "exit code 3" in ServiceStatus(state=BACKOFF, last_exit_code=3).summary()
    assert "repeated failures" in ServiceStatus(state=FAILED, last_exit_code=1).summary()


def test_uptime_is_only_reported_while_actually_running():
    assert ServiceStatus().uptime_seconds() is None
    assert ServiceStatus(state=BACKOFF, since=time.monotonic()).uptime_seconds() is None
    assert ServiceStatus(state=RUNNING, since=time.monotonic()).uptime_seconds() >= 0


# -- real process lifecycle ----------------------------------------------

def test_service_starts_and_is_reported_running(db, lane, player, tmp_path):
    door = _door(db, player, tmp_path)

    async def scenario():
        service = DoorService(door, service_spec(door.profile))
        await service.start()
        try:
            assert await service.wait_until_running(20)
            assert service.status.state == RUNNING
            assert service.status.uptime_seconds() is not None
            assert await service.healthy()
        finally:
            await service.stop()
        assert service.status.state == STOPPED

    asyncio.run(scenario())


def test_stopping_actually_ends_the_process(db, lane, player, tmp_path):
    door = _door(db, player, tmp_path)

    async def scenario():
        service = DoorService(door, service_spec(door.profile))
        await service.start()
        assert await service.wait_until_running(20)
        proc = service._proc
        await service.stop()
        assert proc.returncode is not None, "the child outlived the service that owned it"

    asyncio.run(scenario())


def test_a_service_which_keeps_exiting_is_restarted_then_given_up_on(db, lane, player, tmp_path):
    """The circuit breaker: five failures in the window and it stops trying."""
    door = _door(db, player, tmp_path, argv=["-c", "raise SystemExit(3)"])

    async def scenario():
        service = DoorService(door, service_spec(door.profile))
        # Without shrinking the backoff this would take 1+2+4+8 seconds.
        import netbbs.doors.services as services_module
        original = services_module._BACKOFF_START_SECONDS
        services_module._BACKOFF_START_SECONDS = 0.01
        try:
            await service.start()
            deadline = time.monotonic() + 30
            while service.status.state != FAILED and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert service.status.state == FAILED, service.status.summary()
            assert service.status.last_exit_code == 3
            assert service.status.restarts >= 1
        finally:
            services_module._BACKOFF_START_SECONDS = original
            await service.stop()

    asyncio.run(scenario())


def test_stop_is_bounded_even_when_the_process_ignores_sigterm(db, lane, player, tmp_path):
    """Shutdown must never wait on a door agreeing to exit (PRs #228/#283)."""
    if os.name != "posix":
        pytest.skip("POSIX signal handling")
    script = _write_script(tmp_path, "stubborn.py", """
        import signal, sys, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sys.stdout.write("up\\n"); sys.stdout.flush()
        time.sleep(300)
    """)
    door = create_door(db, "Stubborn", sys.executable, args=(), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path),
                                           service={"argv": [str(script)], "stop_grace_seconds": 1}))

    async def scenario():
        service = DoorService(door, service_spec(door.profile))
        await service.start()
        assert await service.wait_until_running(20)
        proc = service._proc
        started = time.monotonic()
        await service.stop()
        elapsed = time.monotonic() - started
        assert proc.returncode is not None, "SIGKILL did not follow the grace period"
        assert elapsed < 10, f"stop took {elapsed:.1f}s despite a 1s grace"

    asyncio.run(scenario())


# -- manager --------------------------------------------------------------

def test_only_with_node_services_start_with_the_node(db, lane, player, tmp_path):
    eager = _door(db, player, tmp_path, name="Eager")
    lazy = _door(db, player, tmp_path / "lazy", name="Lazy", start="on_first_caller")
    (tmp_path / "lazy").mkdir(exist_ok=True)

    async def scenario():
        manager = DoorServiceManager()
        await manager.start_node_services([eager, lazy])
        try:
            assert await manager.get(eager.id).wait_until_running(20)
            assert manager.status(lazy.id).state == STOPPED
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_a_door_without_a_service_is_never_gated(db, lane, player, tmp_path):
    script = _write_script(tmp_path, "plain.py", "pass")
    door = create_door(db, "Plain", sys.executable, args=(str(script),), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path)))

    async def scenario():
        manager = DoorServiceManager()
        assert await manager.ensure_running(door) is None

    asyncio.run(scenario())


def test_first_caller_starts_a_lazy_service_and_is_let_through(db, lane, player, tmp_path):
    door = _door(db, player, tmp_path, start="on_first_caller")

    async def scenario():
        manager = DoorServiceManager()
        try:
            assert await manager.ensure_running(door, wait_seconds=20) is None
            assert manager.status(door.id).state == RUNNING
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_a_caller_is_refused_with_one_line_when_the_service_will_not_run(db, lane, player, tmp_path):
    """Driven to the circuit breaker first, which is the only terminal state.

    Asserting against a merely-restarting service would race its backoff: a
    respawn which happens to survive the settle window is legitimately
    admitted, so only the given-up state makes the refusal deterministic.
    """
    door = _door(db, player, tmp_path, argv=["-c", "raise SystemExit(1)"], start="on_first_caller")

    async def scenario():
        import netbbs.doors.services as services_module
        original = services_module._BACKOFF_START_SECONDS
        services_module._BACKOFF_START_SECONDS = 0.01
        manager = DoorServiceManager()
        try:
            service = await manager.adopt(door)
            await service.start()
            deadline = time.monotonic() + 30
            while service.status.state != FAILED and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert service.status.state == FAILED, service.status.summary()

            problem = await manager.ensure_running(door, wait_seconds=1.5)

            assert problem is not None
            assert door.name in problem and "SysOp" in problem
            assert "\n" not in problem, "the caller gets one line, not a stack of them"
        finally:
            services_module._BACKOFF_START_SECONDS = original
            await manager.stop_all()

    asyncio.run(scenario())


def test_stop_all_ends_every_service_and_forgets_them(db, lane, player, tmp_path):
    first = _door(db, player, tmp_path, name="First")
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _door(db, player, second_dir, name="Second")

    async def scenario():
        manager = DoorServiceManager()
        await manager.start_node_services([first, second])
        assert await manager.get(first.id).wait_until_running(20)
        assert await manager.get(second.id).wait_until_running(20)
        processes = [manager.get(first.id)._proc, manager.get(second.id)._proc]

        await manager.stop_all()

        assert all(proc.returncode is not None for proc in processes)
        assert manager.get(first.id) is None and manager.get(second.id) is None

    asyncio.run(scenario())


def test_editing_a_profile_stops_the_superseded_service(db, lane, player, tmp_path):
    """Replacing a spec must not leave the old process running and unowned."""
    door = _door(db, player, tmp_path)

    async def scenario():
        manager = DoorServiceManager()
        await manager.start_node_services([door])
        try:
            first = manager.get(door.id)
            assert await first.wait_until_running(20)
            old_proc = first._proc

            edited = replace(door, profile=_profile(tmp_path, argv=["-c", "import time; time.sleep(90)"]))
            second = await manager.adopt(edited)

            assert second is not first
            assert old_proc.returncode is not None, "the superseded service was orphaned"
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_changing_only_the_executable_still_replaces_the_supervisor(db, lane, player, tmp_path):
    """The service block is not the whole launch configuration."""
    door = _door(db, player, tmp_path)

    async def scenario():
        manager = DoorServiceManager()
        await manager.start_node_services([door])
        try:
            first = manager.get(door.id)
            assert await first.wait_until_running(20)

            moved = replace(door, executable_path=str(tmp_path / "other-python"))
            second = await manager.adopt(moved)
            assert second is not first, "Restart would have relaunched the old executable"
            assert second.executable == moved.executable_path
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_concurrent_reconciliation_installs_exactly_one_supervisor(db, lane, player, tmp_path):
    """Two callers racing across adopt's stop await must not each install one."""
    door = _door(db, player, tmp_path)

    async def scenario():
        manager = DoorServiceManager()
        await manager.start_node_services([door])
        try:
            first = manager.get(door.id)
            assert await first.wait_until_running(20)

            edited = replace(door, profile=_profile(tmp_path, argv=["-c", "import time; time.sleep(90)"]))
            both = await asyncio.gather(manager.adopt(edited), manager.adopt(edited))

            assert both[0] is both[1], "each caller installed its own supervisor"
            assert manager.get(door.id) is both[0], "a supervisor was left untracked"
            running = [s for s in both if s._proc is not None and s._proc.returncode is None]
            assert len(set(id(s) for s in running)) <= 1, "two companions ran against one game state"
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_changing_only_the_environment_also_replaces_the_supervisor(db, lane, player, tmp_path):
    door = _door(db, player, tmp_path)

    async def scenario():
        manager = DoorServiceManager()
        first = await manager.adopt(door)
        retuned = replace(door, profile=replace(door.profile, environment={"TERM": "ansi"}))

        assert await manager.adopt(retuned) is not first
        assert manager.get(door.id).environment == {"TERM": "ansi"}

        await manager.stop_all()

    asyncio.run(scenario())


def test_a_caller_is_gated_on_the_profile_as_it_is_now(db, lane, player, tmp_path):
    """A cached entry must not gate callers on a profile the SysOp has edited."""
    door = _door(db, player, tmp_path)

    async def scenario():
        manager = DoorServiceManager()
        await manager.start_node_services([door])
        try:
            assert await manager.get(door.id).wait_until_running(20)
            old_proc = manager.get(door.id)._proc

            plain = replace(door, profile=DoorProfile(install_dir=str(tmp_path)))
            assert await manager.ensure_running(plain) is None, "a door with no service is never gated"
            assert old_proc.returncode is not None, "the removed service kept running"
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_removing_a_service_from_a_profile_stops_it(db, lane, player, tmp_path):
    door = _door(db, player, tmp_path)

    async def scenario():
        manager = DoorServiceManager()
        await manager.start_node_services([door])
        try:
            assert await manager.get(door.id).wait_until_running(20)
            old_proc = manager.get(door.id)._proc

            plain = replace(door, profile=DoorProfile(install_dir=str(tmp_path)))
            assert await manager.adopt(plain) is None

            assert old_proc.returncode is not None
            assert manager.get(door.id) is None
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_a_relative_health_socket_anchors_to_the_installation_directory(tmp_path):
    spec = service_spec(DoorProfile(install_dir=str(tmp_path), service={
        "argv": ["-m", "game"], "health": {"kind": "socket", "path": "run/game.sock"}}))
    # The service creates it against its own cwd; healthy() must look there,
    # not relative to wherever the NetBBS process happens to be running.
    assert spec.health_path == str(pathlib.Path(tmp_path) / "run/game.sock")


def test_forgetting_a_deleted_door_stops_its_service(db, lane, player, tmp_path):
    door = _door(db, player, tmp_path)

    async def scenario():
        manager = DoorServiceManager()
        await manager.start_node_services([door])
        try:
            assert await manager.get(door.id).wait_until_running(20)
            proc = manager.get(door.id)._proc

            await manager.forget(door.id)

            assert proc.returncode is not None, "the deleted door's companion kept running"
            assert manager.get(door.id) is None
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_both_ceilings_removed_is_reported_as_such():
    from netbbs.doors.profiles import profile_advisories
    notes = " ".join(profile_advisories(DoorProfile(time_limit=0, cpu_seconds=0)))
    assert "bounded only by the wall-clock limit" not in notes, "claimed a ceiling which is gone"
    assert "both ceilings removed" in notes


def test_start_and_stop_do_not_interleave(db, lane, player, tmp_path):
    """A Halt racing a lazy start must not leave a process nobody stops."""
    door = _door(db, player, tmp_path)

    async def scenario():
        service = DoorService(door, service_spec(door.profile))
        await service.start()
        assert await service.wait_until_running(20)
        proc = service._proc

        await asyncio.gather(service.stop(), service.start(), service.stop())

        assert proc.returncode is not None, "the original process was abandoned"
        if service._proc is not None:
            await service.stop()
            assert service._proc is None

    asyncio.run(scenario())


def test_renaming_a_door_updates_its_supervisor_diagnostics(db, lane, player, tmp_path):
    """A rename keeps the supervisor, but its log lines must follow the name."""
    door = _door(db, player, tmp_path, name="Before")

    async def scenario():
        manager = DoorServiceManager()
        first = await manager.adopt(door)
        renamed = replace(door, name="After")

        assert await manager.adopt(renamed) is first, "a rename should not relaunch anything"
        assert first.door_name == "After"

        await manager.stop_all()

    asyncio.run(scenario())


def test_a_replaced_with_node_service_is_started_not_left_stopped(db, lane, player, tmp_path):
    """Otherwise a SysOp's edit refuses every caller until they press Start."""
    door = _door(db, player, tmp_path)

    async def scenario():
        manager = DoorServiceManager()
        await manager.start_node_services([door])
        try:
            assert await manager.get(door.id).wait_until_running(20)

            edited = replace(door, profile=_profile(tmp_path, argv=["-c", "import time; time.sleep(90)"]))
            replacement = await manager.adopt(edited)

            assert await replacement.wait_until_running(20), "the replacement was left stopped"
            assert await manager.ensure_running(edited, wait_seconds=20) is None
        finally:
            await manager.stop_all()

    asyncio.run(scenario())


def test_a_halted_service_is_not_revived_by_the_next_caller(db, lane, player, tmp_path):
    """The other half: a deliberate Halt must survive someone opening the door."""
    door = _door(db, player, tmp_path)

    async def scenario():
        manager = DoorServiceManager()
        await manager.start_node_services([door])
        try:
            service = manager.get(door.id)
            assert await service.wait_until_running(20)
            await service.stop()

            problem = await manager.ensure_running(door, wait_seconds=1)

            assert problem is not None, "a halted with_node service was silently restarted"
            assert manager.get(door.id) is service, "the halted supervisor was replaced"
        finally:
            await manager.stop_all()

    asyncio.run(scenario())
