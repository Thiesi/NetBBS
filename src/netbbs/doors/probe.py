"""Explicit emulator capability check, using our own tiny DOS serial fixture.

Machine code is reproducibly built from tests/fixtures/door_serial.asm.
No assembler or third-party game is needed at runtime. An optional FOSSIL
driver is copied from the operator's installation, never downloaded.
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

    def __init__(self):
        self.ready = asyncio.Event()
        self.output = bytearray()
        self.input = iter("éQ".encode())

    async def write_raw(self, data):
        self.output.extend(data)
        del self.output[:-8192]
        if b"DOS READY" in self.output:
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
        result = await run_door(session, lane, candidate, actor, wall_time_limit_seconds=12)
        if result.reason == "exited" and not all(x.encode() in session.output for x in ("DOS READY", "█", "éQ")):
            result = replace(result, reason="relay_failed", diagnostic="COM1 probe output/CP437 echo did not match.")
        return result
