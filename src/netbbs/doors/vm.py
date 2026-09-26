"""Foreign-platform native doors in a virtual machine (issue #474).

The hypervisor, the guest kernel and the guest image are the SysOp's; NetBBS
builds the whole qemu command line, so no network, no host devices and no
display are guaranteed rather than hoped for. One guest per caller, reaped with
the door's process group like any other door. The guest receives exactly one
terminal (a virtio console on the door's socketpair) and two directory exports:
the persistent installation and this caller's node directory.

The guest image contract (docs/NetBBS-door-guide.md, "Foreign-platform doors
in a VM") is what an operator's init must do with those: mount the exports,
touch `booted`, run `run.sh` on the console, write `exit.status`, power off.
A guest program's exit code never reaches the host on its own -- qemu exits 0
whether the game crashed or not -- so the status file is the only verdict,
exactly as `EXIT.ERR`/`RETURN.OK` are for DOS.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shlex
import signal
import string
import subprocess
import threading
from pathlib import Path

GUEST_GAME = "/mnt/game"
GUEST_NODE = "/mnt/node"
ACCELERATORS = ("tcg", "nvmm", "kvm")
#: Where each accelerator's kernel device lives; `tcg` needs none.
ACCELERATOR_DEVICES = {"nvmm": "/dev/nvmm", "kvm": "/dev/kvm"}
OPTION_KEYS = frozenset({"kernel", "initrd", "command", "accel", "guest_memory_mb",
                         "boot_timeout_seconds", "kernel_args", "success_exit_codes"})
SUBSTITUTIONS = ("node", "node_dir", "install_dir", "door32", "door_sys")
DEFAULT_GUEST_MEMORY_MB = 256
DEFAULT_BOOT_TIMEOUT_SECONDS = 60
#: qemu maps guest RAM twice and reserves a few hundred MiB more at startup, so
#: the address-space ceiling must be well above what the guest sees. Measured on
#: qemu 11.1 under TCG: a 256 MiB guest fails to start at 768 MiB and starts at
#: 1000; 128 starts at 768, 512 fails at 1280 and starts at 1536.
ADDRESS_SPACE_OVERHEAD_MB = 512
#: Files NetBBS and the guest's init exchange through the node export.
RUN_SCRIPT, GEOMETRY, BOOTED, EXIT_STATUS = "run.sh", "geometry", "booted", "exit.status"
STOP_GRACE = "stop_grace"
#: Where a VM door finds its outbound receipts: a snapshot in the node export.
GUEST_RESULTS_DIRNAME = "outbound-results"
#: The guest console. Quiet, because it lands in the door's Last diagnostic;
#: `panic=-1` with `-no-reboot` turns a guest panic into qemu exiting instead of
#: a wedged VM sitting out the caller's time limit.
_KERNEL_ARGS = "console=ttyS0 quiet loglevel=3 panic=-1 no_timer_check"
# One fixed command, no shell: words are split on spaces and each is quoted
# when run.sh is written, so none of these can mean anything to the guest's sh.
_COMMAND_FORBIDDEN = set(";&|<>$`\\'\"*?()[]~!#\t\r\n\x00")
_KERNEL_ARGS_PATTERN = re.compile(r"[A-Za-z0-9_.,=:/+ -]{0,512}")
_POWERDOWN_TIMEOUT = 3
_STATUS_BYTES = 16


def options(profile) -> dict:
    """The adapter options with their defaults filled in."""
    given = profile.options
    return {"accel": given.get("accel", "tcg"),
            "guest_memory_mb": given.get("guest_memory_mb", DEFAULT_GUEST_MEMORY_MB),
            "boot_timeout_seconds": given.get("boot_timeout_seconds", DEFAULT_BOOT_TIMEOUT_SECONDS),
            "kernel_args": given.get("kernel_args", ""),
            "success_exit_codes": given.get("success_exit_codes", [0]),
            "kernel": given.get("kernel", ""), "initrd": given.get("initrd", ""),
            "command": given.get("command", "")}


def validate_vm(profile) -> None:
    """Raise ValueError for any profile NetBBS could not launch as specified."""
    if profile.endpoint != "socketpair" or not profile.install_dir:
        raise ValueError("A VM door requires the socketpair endpoint and an installation directory")
    if unknown := set(profile.options) - OPTION_KEYS:
        raise ValueError(f"unknown VM options: {', '.join(sorted(unknown))}")
    opts = options(profile)
    for key in ("kernel", "initrd"):
        value = opts[key]
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\x00"):
            raise ValueError(f"VM {key} must name the guest {key} file")
        if not Path(value).is_absolute():
            raise ValueError(f"VM {key} path must be absolute")
    command = opts["command"]
    if not isinstance(command, str) or not command.strip() or len(command) > 512:
        raise ValueError("VM command is required: one guest program and its arguments")
    if any(c in _COMMAND_FORBIDDEN for c in command):
        raise ValueError("invalid VM command: use one fixed program and arguments, without quotes, "
                         "redirection or shell syntax")
    # A malformed brace is Formatter's own ValueError, which is what we raise.
    for _, name, spec, conversion in string.Formatter().parse(command):
        if name is not None and (name not in SUBSTITUTIONS or spec or conversion):
            raise ValueError("VM commands accept only {node}, {node_dir}, {install_dir}, {door32} "
                             "and {door_sys} substitutions")
    if opts["accel"] not in ACCELERATORS:
        raise ValueError("VM accel must be tcg, nvmm or kvm")
    guest = opts["guest_memory_mb"]
    if type(guest) is not int or not 128 <= guest <= 768:
        raise ValueError("guest_memory_mb must be between 128 and 768")
    if profile.memory_mb < 2 * guest + ADDRESS_SPACE_OVERHEAD_MB:
        raise ValueError(f"Memory ceiling must be at least {2 * guest + ADDRESS_SPACE_OVERHEAD_MB} MiB for a "
                         f"{guest} MiB guest: qemu's address space is far larger than the guest's RAM")
    timeout = opts["boot_timeout_seconds"]
    if type(timeout) is not int or not 5 <= timeout <= 300:
        raise ValueError("boot_timeout_seconds must be between 5 and 300")
    extra = opts["kernel_args"]
    if not isinstance(extra, str) or not _KERNEL_ARGS_PATTERN.fullmatch(extra):
        raise ValueError("kernel_args may hold only letters, digits, spaces and _ . , = : / + -")
    success = opts["success_exit_codes"]
    if not isinstance(success, list) or not success or any(type(x) is not int or not 0 <= x <= 255
                                                           for x in success):
        raise ValueError("success_exit_codes must be an array of exit codes (0-255)")


def preflight_vm(profile, executable: str) -> list[str]:
    """Read-only host checks: the guest files and the accelerator's device.

    Also learns, once per qemu binary, how it wants its control monitor
    declared; preflight runs off the event loop, the launch that follows
    does not, and must not spawn anything to find out.
    """
    monitor_objects(executable)
    known_tsc_khz(learn=True)
    problems = []
    opts = options(profile)
    for key in ("kernel", "initrd"):
        path = Path(opts[key])
        if not path.is_file() or not os.access(path, os.R_OK):
            problems.append(f"Guest {key} is missing or unreadable: {path}. Build the guest image outside NetBBS.")
    device = ACCELERATOR_DEVICES.get(opts["accel"])
    if device and not os.access(device, os.R_OK | os.W_OK):
        problems.append(f"{device} is missing or not accessible to the service account; "
                        f"load the {opts['accel']} module and grant access, or use accel tcg.")
    return problems


def guest_substitutions(profile, node: int) -> dict[str, str]:
    """Host placeholders resolved to where the guest sees them."""
    node_dir = GUEST_NODE + "/" + (profile.drop_subdir + "/" if profile.drop_subdir else "")
    lower = profile.filename_case == "lower"
    return {"node": str(node), "node_dir": node_dir, "install_dir": GUEST_GAME,
            "door32": node_dir + ("door32.sys" if lower else "DOOR32.SYS"),
            "door_sys": node_dir + ("door.sys" if lower else "DOOR.SYS")}


def run_script(profile, node: int) -> str:
    """The guest-side launcher: a fixed environment, the installation as cwd, one exec."""
    paths = guest_substitutions(profile, node)
    argv = [word.format_map(paths) for word in options(profile)["command"].split()]
    environment = {"TERM": "ansi", **{key: value for key, value in profile.environment.items()
                                      if key != "WAR_DIALER_DB_PATH"},
                   "NETBBS_DOOR_INFO": GUEST_NODE + "/door_info.json",
                   "NETBBS_DOOR_NODE": str(node), "NETBBS_DOOR_NODE_DIR": paths["node_dir"]}
    lines = ["# Written by NetBBS for one caller's session; runs inside the guest."]
    lines += [f"export {key}={shlex.quote(value)}" for key, value in environment.items()]
    lines += [f"cd {GUEST_GAME} || exit 125", "exec " + " ".join(shlex.quote(word) for word in argv)]
    return "\n".join(lines) + "\n"


def host_tsc_khz() -> int | None:
    """The host's TSC frequency, or None where it cannot be read.

    Under TCG on an x86 host the guest's TSC *is* the host's, but a guest
    kernel calibrating it against an emulated PIT can fail on a loaded or
    virtualized host -- and one that fails hangs before its console is up
    (measured: 3 boots in 10 on a VMware-hosted NetBSD). Telling it the
    frequency outright skips the calibration.
    """
    if platform.machine().lower() not in ("amd64", "x86_64"):
        return None
    try:
        if platform.system() == "NetBSD":
            hertz = subprocess.run(["/sbin/sysctl", "-n", "machdep.tsc_freq"], capture_output=True,
                                   text=True, timeout=2, check=True).stdout
            return int(hertz) // 1000 or None
        return int(Path("/sys/devices/system/cpu/cpu0/tsc_freq_khz").read_text()) or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


_UNLEARNED = object()
_TSC_KHZ = _UNLEARNED


def known_tsc_khz(*, learn: bool = False) -> int | None:
    """The host TSC frequency once preflight has read it, else None.

    Reading it runs `sysctl` on NetBSD, which must not happen on the event
    loop the launch runs on: preflight (off the loop) learns it, and the launch
    only ever reads what was learnt. A launch before any preflight simply
    boots without the hint.
    """
    global _TSC_KHZ
    if learn and _TSC_KHZ is _UNLEARNED:
        _TSC_KHZ = host_tsc_khz()
    return None if _TSC_KHZ is _UNLEARNED else _TSC_KHZ


_MONITOR_OBJECTS: dict[tuple[str, int], bool] = {}
_MONITOR_LOCK = threading.Lock()


def monitor_objects(executable: str) -> bool:
    """Whether this qemu declares a QMP monitor as `-object monitor-qmp`.

    qemu 11.1 deprecated `-mon` for it, and warns on every launch -- into the
    door's Last diagnostic. Older builds (pkgsrc still carries 7.2) have only
    `-mon`. Keyed by modification time, so an upgraded qemu is asked again.
    """
    try:
        key = (executable, os.stat(executable).st_mtime_ns)
    except OSError:
        return False
    # Preflights run in worker threads; a burst of callers after a restart or
    # a qemu upgrade must ask qemu once, not once each.
    with _MONITOR_LOCK:
        if key not in _MONITOR_OBJECTS:
            try:
                listing = subprocess.run([executable, "-object", "help"], capture_output=True, text=True,
                                         timeout=10, stdin=subprocess.DEVNULL).stdout
            except (OSError, subprocess.SubprocessError):
                listing = ""
            _MONITOR_OBJECTS[key] = "monitor-qmp" in listing.split()
        return _MONITOR_OBJECTS[key]


def _cached_monitor_objects(executable: str) -> bool:
    try:
        return _MONITOR_OBJECTS.get((executable, os.stat(executable).st_mtime_ns), False)
    except OSError:
        return False


def _fsdev_path(path) -> str:
    # qemu's option parser splits on commas; a literal one is doubled.
    return str(path).replace(",", ",,")


def prepare_vm(door, directory: Path, node: int, width: int, height: int, door_fd: int, qmp_fd: int):
    """Write the guest-side files into the node directory and return qemu's argv."""
    profile = door.profile.validate()
    opts = options(profile)
    install = Path(profile.install_dir).resolve()
    (directory / RUN_SCRIPT).write_text(run_script(profile, node), encoding="utf-8")
    (directory / GEOMETRY).write_text(f"{height} {width}\n", encoding="ascii")
    # How long the guest may give its door after a hangup. The host waits this
    # long for qemu to exit before killing it, so a guest-side deadline any
    # shorter would cut off a door the SysOp gave more time to save.
    (directory / STOP_GRACE).write_text(f"{profile.stop_grace_seconds}\n", encoding="ascii")
    accel = opts["accel"]
    kernel_args = _KERNEL_ARGS
    if accel == "tcg" and (khz := known_tsc_khz()):
        kernel_args += f" tsc_early_khz={khz}"
    if opts["kernel_args"].strip():
        kernel_args += " " + opts["kernel_args"].strip()
    return [
        door.executable_path, "-nodefaults", "-no-user-config",
        "-machine", "microvm", "-accel", "tcg,tb-size=64" if accel == "tcg" else accel,
        "-cpu", "max", "-smp", "1", "-m", str(opts["guest_memory_mb"]),
        # The guest console is write-only on purpose. `-serial stdio` reads
        # stdin -- /dev/null here -- and the EOF it sees immediately changes
        # the UART's modem status, raising IRQ 4 before the guest kernel has
        # programmed an interrupt controller. About one boot in nine then took
        # a stray vector early and panicked; qemu, under -no-reboot, exited 0.
        # 40 of 40 clean with a file backend on the same host.
        "-display", "none", "-nic", "none", "-no-reboot", "-serial", "file:/dev/stdout",
        "-kernel", opts["kernel"], "-initrd", opts["initrd"], "-append", kernel_args,
        "-fsdev", f"local,id=game,path={_fsdev_path(install)},security_model=none",
        "-device", "virtio-9p-device,fsdev=game,mount_tag=game",
        "-fsdev", f"local,id=node,path={_fsdev_path(directory)},security_model=none",
        "-device", "virtio-9p-device,fsdev=node,mount_tag=node",
        "-chardev", f"socket,id=door,fd={door_fd}",
        "-device", "virtio-serial-device", "-device", "virtconsole,chardev=door",
        "-chardev", f"socket,id=qmp,fd={qmp_fd}",
        *(("-object", "monitor-qmp,id=control,chardev=qmp") if _cached_monitor_objects(door.executable_path)
          else ("-mon", "chardev=qmp,mode=control")),
    ]


def publish_guest_info(directory: Path, info_path: Path, info: dict, results_kept: int) -> dict:
    """Rewrite `door_info.json` so every path in it is one the guest can open.

    The outbound hook's receipts live beside the node database, outside both
    exports, so a guest could submit a request and never learn its outcome.
    Receipts are only written when a run drains, after the door has exited,
    so a copy taken now holds exactly what the live directory would show the
    door for its whole session. Copied, not exported: the guest must not be
    able to rewrite the node's record of what it was told.
    """
    outbound = info.get("outbound")
    if not outbound:
        return info
    source = Path(outbound["results"])
    target = directory / GUEST_RESULTS_DIRNAME
    target.mkdir(exist_ok=True)
    try:
        receipts = sorted(entry for entry in source.iterdir()
                          if entry.is_file() and not entry.is_symlink())[-results_kept:]
    except OSError:
        receipts = []
    for receipt in receipts:
        try:
            (target / receipt.name).write_bytes(receipt.read_bytes())
        except OSError:
            continue
    info = dict(info, outbound=dict(outbound, results=f"{GUEST_NODE}/{GUEST_RESULTS_DIRNAME}"))
    info_path.write_text(json.dumps(info), encoding="utf-8")
    return info


async def wait_booted(directory: Path, relay: asyncio.Task) -> None:
    """Hold the caller's time limit until the guest reaches its door.

    Returns when the guest has booted or the relay has already ended (the
    caller left, or the boot watchdog killed the guest). The boot itself is
    bounded by `watch_boot`, not by this.
    """
    while not relay.done() and not (directory / BOOTED).exists():
        await asyncio.sleep(0.1)


def guest_exit_code(profile, directory: Path) -> tuple[int, str]:
    """The game's own verdict, read from the status file its guest wrote."""
    try:
        # The guest is the untrusted side: read a status, never a whole file.
        # Not followed if the guest made it a link: it names a host file then.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(directory / EXIT_STATUS, flags), "rb") as source:
            text = source.read(_STATUS_BYTES + 1)
        if len(text) > _STATUS_BYTES or not re.fullmatch(rb"\s*[0-9]{1,3}\s*", text):
            raise ValueError("not an exit status")
        status = int(text)
    except (OSError, ValueError):
        if not (directory / BOOTED).exists():
            # A kernel panic or reset powers qemu off cleanly under
            # -no-reboot, which would otherwise read as a door that ran.
            return 1, ("The VM stopped before its door started (a guest kernel panic or reset powers qemu "
                       "off cleanly); the guest console lines above show why.\n")
        return 1, ("The guest did not report the door's exit status: its init must write exit.status "
                   "before powering off (see the guest image contract in the door guide).\n")
    if status in options(profile)["success_exit_codes"]:
        return 0, ""
    return status or 1, f"The door exited with status {status} inside the guest.\n"


async def watch_boot(directory: Path, proc, timeout: int, tail: bytearray) -> None:
    """Kill a guest which never reaches its door, with a reason a SysOp can read.

    Separate from the caller's time limit, so a VM that cannot boot reports that
    rather than holding a node for an hour.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not (directory / BOOTED).exists():
        if proc.returncode is not None:
            return
        if loop.time() >= deadline:
            tail.extend(f"The VM did not start its door within {timeout} seconds.\n".encode())
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return
        await asyncio.sleep(0.1)


async def power_down(qmp_socket, proc, grace: float) -> None:
    """Ask the guest to shut down, as a power button would, and wait for it.

    The guest's init hangs its door up on the button, so the game gets the same
    chance to save that a native door gets from SIGTERM; qemu itself would exit
    on SIGTERM without telling the guest anything. Best effort and bounded: the
    caller's process-group kill still follows whatever happens here.
    """
    if proc.returncode is not None or qmp_socket is None:
        return
    writer = None
    try:
        async with asyncio.timeout(_POWERDOWN_TIMEOUT):
            reader, writer = await asyncio.open_connection(sock=qmp_socket)

            async def reply():
                while True:
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("QMP closed")
                    message = json.loads(line)
                    if "error" in message:
                        # Refused: waiting out the grace for a shutdown that
                        # will not happen would only delay the kill.
                        raise ConnectionError(f"QMP refused: {message['error']}")
                    if "return" in message:
                        return message

            await reader.readline()  # greeting
            for command in ("qmp_capabilities", "system_powerdown"):
                writer.write(json.dumps({"execute": command}).encode() + b"\n")
                await writer.drain()
                await reply()
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except (OSError, ValueError, ConnectionError, asyncio.TimeoutError):
        pass
    finally:
        if writer is not None:
            writer.close()
