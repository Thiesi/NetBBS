"""Explicit emulator capability checks, using NetBBS's own fixtures, never the game.

DOS: machine code is reproducibly built from tests/fixtures/door_serial.asm.
No assembler or third-party game is needed at runtime. An optional FOSSIL
driver is copied from the operator's installation, never downloaded.

VM: a shell fixture run by the operator's own guest image (issue #474).
"""
import asyncio
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from netbbs.doors.profiles import DoorProfile
from netbbs.doors.runtime import run_door
from netbbs.net.session import Session

UART_PROBE = bytes.fromhex(
    "bafb03b080eebaf803b003ee4230c0eebafb03b003eebafc03b003eebe5701ac84c07405e81f00ebf6"
    "bafd03eca80174f8baf803ece80e003c5175edb8004ccd21b8014ccd215050bafd03eca82074f858baf803ee58c3"
    "1b5b33326d444f5320524541445920db1b5b306d0d0a00")
FOSSIL_PROBE = bytes.fromhex(
    "ba0000b80004cd143d54197520be3c01ac84c07405e81a00ebf6ba0000b402cd14e80e003c5175f2"
    "b8004ccd21b8014ccd2150ba0000b401cd1458c31b5b33326d444f5320524541445920db1b5b306d0d0a00")


class _ProbeSession(Session):
    terminal_width = 80
    terminal_height = 25

    def __init__(self, marker=b"DOS READY"):
        self.ready = asyncio.Event()
        self.output = bytearray()
        self.input = iter("éQ".encode())
        self.marker = marker

    async def write_raw(self, data):
        self.output.extend(data)
        del self.output[:-8192]
        if self.marker in self.output:
            self.ready.set()

    async def read_byte(self):
        await self.ready.wait()
        value = next(self.input, None)
        if value is None:
            await asyncio.Future()
        return value

    async def write(self, text):
        await self.write_raw(text.encode())

    async def read_line(self, **kwargs):
        raise NotImplementedError

    async def read_key(self, **kwargs):
        raise NotImplementedError

    async def read_editor_key(self, **kwargs):
        raise NotImplementedError

    async def close(self):
        pass


async def probe_dosbox(lane, door, actor):
    """Return the supervised result; failures never enable or save a profile."""
    with tempfile.TemporaryDirectory(prefix="netbbs-dos-probe-") as directory:
        root = Path(directory)
        fossil = door.profile.options.get("fossil", "")
        if fossil:
            name = fossil.split()[0]
            source = next((p for p in Path(door.profile.install_dir).iterdir() if p.name.upper() == name.upper()), None)
            if source is None:
                raise FileNotFoundError(f"FOSSIL driver is missing: {name}")
            shutil.copyfile(source, root / name)
        (root / "PROBE.COM").write_bytes(FOSSIL_PROBE if fossil else UART_PROBE)
        profile = DoorProfile(adapter="dosbox", endpoint="socketpair", encoding="cp437", width=80, height=25,
                              install_dir=directory, memory_mb=door.profile.memory_mb,
                              options={"command": "PROBE.COM", "fossil": fossil})
        candidate = replace(door, name=f"{door.name} (capability probe)", profile=profile, args=())
        session = _ProbeSession()
        def check_output():
            if not all(x.encode() in session.output for x in ("DOS READY", "█", "éQ")):
                return "COM1 probe output/CP437 echo did not match."
            return ""

        # A probe runs the real game to see whether it starts. It must never
        # publish anything the door happens to have queued (issue #520).
        return await run_door(session, lane, candidate, actor, wall_time_limit_seconds=12,
                              output_check=check_output, rehearsal=True)


#: The guest half of the VM probe. Runs under the guest's own sh through the
#: same run.sh a game does, so it tests the operator's image, not a stand-in:
#: the console in raw CP437 (0xDB is a full block), both exports, and the
#: exit-status handshake the runtime reads afterwards.
VM_PROBE_SCRIPT = r"""printf '\033[32mVM READY \333\033[0m\r\n'
reply=$(head -c 2)
printf 'VM GOT:%s\r\n' "$reply"
echo probe > /mnt/game/netbbs-probe.out
exit 0
"""


async def probe_vm(lane, door, actor):
    """Boot the SysOp's own guest image with NetBBS's fixture instead of the game."""
    with tempfile.TemporaryDirectory(prefix="netbbs-vm-probe-") as directory:
        root = Path(directory)
        (root / "netbbs-probe.sh").write_text(VM_PROBE_SCRIPT, encoding="ascii")
        profile = replace(door.profile, install_dir=directory, encoding="cp437", width=80, height=25,
                          drop_files=(), drop_subdir="", environment={}, max_sessions=1,
                          multinode_certified=False, service={},
                          options={**door.profile.options, "command": "sh {install_dir}/netbbs-probe.sh",
                                   "success_exit_codes": [0]})
        candidate = replace(door, name=f"{door.name} (capability probe)", profile=profile, args=())
        session = _ProbeSession(marker=b"VM READY")

        def check_output():
            if not all(x.encode() in session.output for x in ("VM READY", "█", "VM GOT:éQ")):
                return "Guest console output or CP437 echo did not match."
            if not (root / "netbbs-probe.out").is_file():
                return "The guest could not write to the installation export."
            return ""

        timeout = profile.options.get("boot_timeout_seconds", 60) + 30
        return await run_door(session, lane, candidate, actor, wall_time_limit_seconds=timeout,
                              output_check=check_output, rehearsal=True)
