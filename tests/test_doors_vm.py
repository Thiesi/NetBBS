"""Foreign-platform native doors in a VM (issue #474).

The command line, the guest-side files and the exit-status verdict are checked
directly. The lifecycle -- boot timeout, graceful power-down on hangup, the
status sentinel -- runs through the real runtime against a fake `qemu`: a
script which reads the argv NetBBS built and plays the guest's half of the
contract over the descriptors it was handed. No test here needs a hypervisor;
booting a real guest is the operator-run capability probe's job.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest

from netbbs.doors import create_door, vm
from netbbs.doors.dropfiles import drop_file_bytes
from netbbs.doors.profiles import DoorProfile, ProfileError, preflight
from netbbs.doors.runtime import run_door
from tests.test_doors_runtime import FakeSession, db, lane, player  # noqa: F401  (fixtures)

posix_only = pytest.mark.skipif(os.name != "posix", reason="VM doors are POSIX-only")


def _profile(tmp_path, **changes) -> DoorProfile:
    for name in ("vmlinux", "initrd.cpio"):
        (tmp_path / name).write_bytes(b"guest")
    install = tmp_path / "game"
    install.mkdir(exist_ok=True)
    options = {"kernel": str(tmp_path / "vmlinux"), "initrd": str(tmp_path / "initrd.cpio"),
               "command": "{install_dir}/empire -D{door32} -X", **changes.pop("options", {})}
    base = dict(adapter="vm", endpoint="socketpair", install_dir=str(install), drop_files=("DOOR32.SYS",),
                filename_case="lower", encoding="cp437", width=80, height=25, memory_mb=1024,
                options=options)
    base.update(changes)
    return DoorProfile(**base)


# -- validation --------------------------------------------------------------


@posix_only
def test_a_complete_profile_validates(tmp_path):
    assert _profile(tmp_path).validate()


@posix_only
@pytest.mark.parametrize("changes, message", [
    ({"endpoint": "pty"}, "socketpair"),
    ({"install_dir": ""}, "installation directory"),
    ({"options": {"bogus": 1}}, "unknown VM options"),
    ({"options": {"kernel": "vmlinux"}}, "absolute"),
    ({"options": {"command": ""}}, "command is required"),
    ({"options": {"command": "game; rm -rf /"}}, "shell syntax"),
    ({"options": {"command": "game $HOME"}}, "shell syntax"),
    ({"options": {"command": "game 'quoted'"}}, "shell syntax"),
    ({"options": {"command": "game {password}"}}, "substitutions"),
    ({"options": {"command": "game {node:x}"}}, "substitutions"),
    ({"options": {"accel": "hvf"}}, "tcg, nvmm or kvm"),
    ({"options": {"guest_memory_mb": 64}}, "guest_memory_mb"),
    ({"options": {"guest_memory_mb": 512}}, "at least 1536 MiB"),
    ({"memory_mb": 768}, "at least 1024 MiB"),
    ({"options": {"boot_timeout_seconds": 1}}, "boot_timeout_seconds"),
    ({"options": {"kernel_args": "init=/bin/sh; reboot"}}, "kernel_args"),
    ({"options": {"success_exit_codes": [256]}}, "success_exit_codes"),
])
def test_profiles_netbbs_could_not_launch_as_written_are_refused(tmp_path, changes, message):
    with pytest.raises(ProfileError, match=message):
        _profile(tmp_path, **changes).validate()


@posix_only
def test_preflight_names_missing_guest_files_and_an_inaccessible_accelerator(tmp_path, monkeypatch, db, player):
    profile = _profile(tmp_path, options={"accel": "nvmm"})
    (tmp_path / "vmlinux").unlink()
    monkeypatch.setitem(vm.ACCELERATOR_DEVICES, "nvmm", str(tmp_path / "no-such-device"))
    door = create_door(db, "Empire", sys.executable, creator=player, profile=profile)
    problems = preflight(door)
    assert any("Guest kernel is missing" in problem for problem in problems)
    assert any("no-such-device is missing" in problem for problem in problems)
    assert not any("initrd" in problem for problem in problems)


# -- what the guest is given --------------------------------------------------


@posix_only
def test_the_run_script_resolves_guest_paths_and_quotes_every_word(tmp_path):
    profile = _profile(tmp_path, drop_subdir="NODE", environment={"DOOR_MODE": "a b"},
                       options={"command": "{install_dir}/empire -D{door32} -N{node} {node_dir}"})
    script = vm.run_script(profile, 3)
    assert "exec /mnt/game/empire -D/mnt/node/NODE/door32.sys -N3 /mnt/node/NODE/\n" in script
    assert "export DOOR_MODE='a b'" in script
    assert "export NETBBS_DOOR_INFO=/mnt/node/door_info.json" in script
    assert "export TERM=ansi" in script
    assert "cd /mnt/game || exit 125" in script


@posix_only
def test_the_command_line_is_netbbs_owned_and_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "host_tsc_khz", lambda: 2400000)
    profile = _profile(tmp_path)
    door = replace(_door_stub(), profile=profile)
    node = tmp_path / "node"
    node.mkdir()
    argv = vm.prepare_vm(door, node, 2, 80, 25, door_fd=7, qmp_fd=9)
    joined = " ".join(argv)
    for required in ("-nodefaults", "-no-user-config", "-nic none", "-display none", "-no-reboot",
                     "-machine microvm", "-accel tcg,tb-size=64", "-m 256",
                     "socket,id=door,fd=7", "socket,id=qmp,fd=9", "virtconsole,chardev=door",
                     f"path={profile.install_dir},", f"path={node},", "tsc_early_khz=2400000"):
        assert required in joined, required
    assert (node / "run.sh").read_text().endswith("exec /mnt/game/empire -D/mnt/node/door32.sys -X\n")
    assert (node / "geometry").read_text() == "25 80\n"


@posix_only
def test_only_software_emulation_is_told_the_tsc_frequency(tmp_path, monkeypatch):
    """Under nvmm/kvm the guest has a real clock source; the hint is a TCG fix."""
    monkeypatch.setattr(vm, "host_tsc_khz", lambda: 2400000)
    node = tmp_path / "node"
    node.mkdir()
    door = replace(_door_stub(), profile=_profile(tmp_path, options={"accel": "kvm", "kernel_args": "quiet"}))
    argv = vm.prepare_vm(door, node, 1, 80, 25, door_fd=3, qmp_fd=4)
    append = argv[argv.index("-append") + 1]
    assert "tsc_early_khz" not in append and append.endswith(" quiet")
    assert argv[argv.index("-accel") + 1] == "kvm"


@posix_only
def test_a_comma_in_the_installation_path_cannot_add_qemu_options(tmp_path):
    install = tmp_path / "game,readonly=on"
    install.mkdir()
    node = tmp_path / "node"
    node.mkdir()
    door = replace(_door_stub(), profile=_profile(tmp_path, install_dir=str(install)))
    argv = vm.prepare_vm(door, node, 1, 80, 25, door_fd=3, qmp_fd=4)
    assert f"local,id=game,path={tmp_path}/game,,readonly=on,security_model=none" in argv


@posix_only
@pytest.mark.parametrize("listing, expected", [
    ("monitor-hmp monitor-qmp", ["-object", "monitor-qmp,id=control,chardev=qmp"]),
    ("memory-backend-ram", ["-mon", "chardev=qmp,mode=control"]),
])
def test_the_control_monitor_is_declared_the_way_this_qemu_wants(tmp_path, listing, expected):
    """qemu 11.1 warns about `-mon` on every launch, into Last diagnostic;
    older builds know nothing else."""
    qemu = tmp_path / "qemu"
    qemu.write_text(f"#!/bin/sh\necho {listing}\n")
    qemu.chmod(0o755)
    node = tmp_path / "node"
    node.mkdir()
    door = replace(_door_stub(), executable_path=str(qemu), profile=_profile(tmp_path))
    vm.preflight_vm(door.profile, str(qemu))
    assert vm.prepare_vm(door, node, 1, 80, 25, door_fd=3, qmp_fd=4)[-2:] == expected


@posix_only
def test_an_unknown_qemu_is_never_asked_from_the_event_loop(tmp_path, monkeypatch):
    """Without a preflight having asked, the launch falls back rather than spawning."""
    monkeypatch.setattr(vm.subprocess, "run", lambda *a, **k: pytest.fail("spawned during launch"))
    node = tmp_path / "node"
    node.mkdir()
    door = replace(_door_stub(), executable_path=sys.executable, profile=_profile(tmp_path))
    monkeypatch.setattr(vm, "host_tsc_khz", lambda: None)
    assert vm.prepare_vm(door, node, 1, 80, 25, door_fd=3, qmp_fd=4)[-2:] == ["-mon", "chardev=qmp,mode=control"]


@posix_only
def test_drop_files_name_the_guest_node_directory(tmp_path):
    profile = _profile(tmp_path, drop_files=("DOOR.SYS", "DOOR32.SYS"))
    files = drop_file_bytes(profile, {"handle": "Carrier", "user_id": 5, "node_name": "ReLink"}, 1)
    door32 = files["door32.sys"].decode("cp437").split("\r\n")
    assert door32[:2] == ["0", "0"], "a guest door speaks on its console, not an inherited socket"
    assert "/mnt/node/" in files["door.sys"].decode("cp437").split("\r\n")


@posix_only
@pytest.mark.parametrize("written, success, expected", [
    ("0\n", [0], (0, "")),
    ("255\n", [0, 255], (0, "")),
    ("3\n", [0], (3, "The door exited with status 3 inside the guest.\n")),
    ("0\n", [255], (1, "The door exited with status 0 inside the guest.\n")),
    (None, [0], (1, None)),
    ("garbage", [0], (1, None)),
])
def test_the_status_file_is_the_only_verdict(tmp_path, written, success, expected):
    if written is not None:
        (tmp_path / "exit.status").write_text(written)
    profile = _profile(tmp_path, options={"success_exit_codes": success})
    code, problem = vm.guest_exit_code(profile, tmp_path)
    assert code == expected[0]
    if expected[1] is None:
        assert "did not report the door's exit status" in problem
    else:
        assert problem == expected[1]


def _door_stub():
    from netbbs.doors import Door
    return Door(id=1, name="Empire", description="", executable_path="/usr/pkg/bin/qemu-system-x86_64",
                args=(), min_play_level=0, pinned=False, created_at="", community_id=None)


# -- the lifecycle, against a fake qemu -----------------------------------------

FAKE_QEMU = '''
    import json, os, socket, sys, time
    argv = sys.argv[1:]
    if argv == ["-object", "help"]:
        print("memory-backend-ram monitor-qmp")
        sys.exit(0)
    def after(flag, prefix):
        return next(a for i, a in enumerate(argv) if i and argv[i - 1] == flag and a.startswith(prefix))
    door = socket.socket(fileno=int(after("-chardev", "socket,id=door,").rsplit("=", 1)[1]))
    qmp = socket.socket(fileno=int(after("-chardev", "socket,id=qmp,").rsplit("=", 1)[1]))
    exports = {spec.split(",")[1][3:]: spec.split("path=")[1].rsplit(",security_model", 1)[0]
               for spec in argv if spec.startswith("local,id=")}
    node, game = exports["node"], exports["game"]
    mode = os.environ["DOOR_FAKE_MODE"]
    qmp.sendall(b'{"QMP": {"version": {}}}\\n')
    if mode == "noboot":
        time.sleep(60)
    open(os.path.join(game, "argv.json"), "w").write(json.dumps(argv))
    open(os.path.join(node, "booted"), "w").close()
    door.sendall(b"READY")
    if mode == "hangup":
        stream = qmp.makefile("rwb")
        for line in stream:
            command = json.loads(line)["execute"]
            stream.write(b'{"return": {}}\\n'); stream.flush()
            if command == "system_powerdown":
                open(os.path.join(game, "powered-down"), "w").close()
                open(os.path.join(node, "exit.status"), "w").write("129\\n")
                sys.exit(0)
        sys.exit(9)
    door.recv(1)
    if mode != "nostatus":
        open(os.path.join(node, "exit.status"), "w").write(os.environ["DOOR_FAKE_STATUS"] + "\\n")
'''


def _fake_qemu_door(db, player, tmp_path, mode, status="0", **changes):
    script = tmp_path / "qemu-system-x86_64"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(FAKE_QEMU), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    profile = _profile(tmp_path, environment={"DOOR_FAKE_MODE": mode, "DOOR_FAKE_STATUS": status}, **changes)
    return create_door(db, "Empire", str(script), creator=player, profile=profile)


def _play(lane, door, player, *, disconnect=False, limit=30):
    async def scenario():
        session = FakeSession()
        session.terminal_height = 25
        task = asyncio.create_task(run_door(session, lane, door, player, wall_time_limit_seconds=limit))
        async with asyncio.timeout(limit + 15):
            while b"READY" not in session.written and not task.done():
                await asyncio.sleep(0.01)
            if disconnect:
                session.disconnect()
            else:
                session.type_in("X")
            return await task
    return asyncio.run(scenario())


@posix_only
def test_a_guest_which_reports_success_exits_cleanly(db, lane, player, tmp_path):
    door = _fake_qemu_door(db, player, tmp_path, "play")
    result = _play(lane, door, player)
    assert (result.reason, result.exit_code) == ("exited", 0), result
    argv = (tmp_path / "game" / "argv.json").read_text()
    assert "-nic" in argv and "virtconsole,chardev=door" in argv


@posix_only
def test_a_game_failing_inside_the_guest_is_a_crash_although_qemu_exited_zero(db, lane, player, tmp_path):
    door = _fake_qemu_door(db, player, tmp_path, "play", status="3")
    result = _play(lane, door, player)
    assert (result.reason, result.exit_code) == ("crashed", 3), result
    assert "status 3 inside the guest" in result.diagnostic


@posix_only
def test_a_configured_success_code_is_a_clean_exit(db, lane, player, tmp_path):
    door = _fake_qemu_door(db, player, tmp_path, "play", status="255",
                           options={"success_exit_codes": [0, 255]})
    assert _play(lane, door, player).reason == "exited"


@posix_only
def test_a_guest_which_never_reports_a_status_is_not_a_success(db, lane, player, tmp_path):
    door = _fake_qemu_door(db, player, tmp_path, "nostatus")
    result = _play(lane, door, player)
    assert (result.reason, result.exit_code) == ("crashed", 1), result
    assert "did not report the door's exit status" in result.diagnostic


@posix_only
def test_a_hangup_powers_the_guest_down_before_anything_is_killed(db, lane, player, tmp_path):
    """qemu dies on SIGTERM without telling its guest; the game would never
    hear the hangup. The power button has to come first."""
    door = _fake_qemu_door(db, player, tmp_path, "hangup", stop_grace_seconds=20)
    started = time.monotonic()
    result = _play(lane, door, player, disconnect=True)
    assert result.reason == "caller_disconnected", result
    assert (tmp_path / "game" / "powered-down").exists()
    # The fake exits the moment it is powered down, so nothing waited out the grace.
    assert time.monotonic() - started < 15


@posix_only
def test_a_guest_which_never_boots_reports_that_instead_of_holding_the_node(db, lane, player, tmp_path):
    door = _fake_qemu_door(db, player, tmp_path, "noboot", options={"boot_timeout_seconds": 5})
    started = time.monotonic()
    result = _play(lane, door, player, limit=60)
    assert time.monotonic() - started < 20
    assert result.reason == "crashed", result
    assert "did not start its door within 5 seconds" in result.diagnostic


# -- the shipped template and recipe ----------------------------------------------


def test_the_guest_init_honours_every_step_of_the_contract():
    """The reference init is what the guide documents; keep the two together."""
    init = (Path(__file__).resolve().parent.parent / "examples" / "doors" / "vm" / "init").read_text()
    assert "\r" not in init, "a CR would break the shebang inside the guest"
    for step in ("mount -t 9p -o $options game /mnt/game", "mount -t 9p -o $options node /mnt/node",
                 "/dev/hvc0", "/mnt/node/geometry", ": >/mnt/node/booted", "sh /mnt/node/run.sh",
                 ">/mnt/node/exit.status", "poweroff -f", "power_signal=2", "kill -HUP"):
        assert step in init, step
    for name in (vm.RUN_SCRIPT, vm.GEOMETRY, vm.BOOTED, vm.EXIT_STATUS):
        assert f"/mnt/node/{name}" in init
