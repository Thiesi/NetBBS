"""Capture Blacksite's street and Lattice screens from one played session.

Blacksite is a third-party door in its own repository (Thiesi/Blacksite) and
is not vendored here. Clone it, check out the pinned commit, and pass the
checkout with --source; a different HEAD is refused, so a recapture cannot
silently come from another build.

The helper starts the game's real service in this process on a private
loopback port, under --root, and runs the real door client
(`python -m blacksite.door`) as a subprocess over stdio pipes, the way a
NetBBS native stdio door runs it. Nothing here draws a frame. Keys are typed
one at a time, and movement waits for the server to report each step, so
the walk is the game's own: no teleports, grants or fixtures.

One new Ghost wakes in the Wake Hall, beats the training drone, jacks in
with its native rig and meets Sandman ICE in Sablier's Vatline core. It
jacks out, walks out onto Clinic Row beside the Cold Chain relay, and goes
on up Sablier Boulevard to Meridian Plaza. Before each capture the
helper presses Ctrl-L, the game's own redraw key, and keeps the bytes from
that full repaint to the moment of capture, so each raw file is a complete
frame exactly as the door wrote it.

    git clone https://github.com/Thiesi/Blacksite /path/to/Blacksite
    git -C /path/to/Blacksite checkout d80dacdd58065f221d199b69df573fcf12f7d7cf
    python scripts/website_capture_blacksite.py --source /path/to/Blacksite \\
        --out-dir web/shots

It writes raw-blacksite-lattice.txt, raw-blacksite-city.txt and
raw-blacksite-plaza.txt. Combat and ICE use the game's own randomness, so a
recapture shows the same places with different numbers.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from website_ansi_to_html import Screen  # noqa: E402

COMMIT = "d80dacdd58065f221d199b69df573fcf12f7d7cf"
WIDTH, HEIGHT = 80, 24
HANDLE = "wren"
UP, DOWN, RIGHT, LEFT = b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D"
REDRAW = b"\x0c"


class Session:
    """The real door client on pipes, plus a read-only view of the service."""

    def __init__(self, service, child) -> None:
        self.service, self.child = service, child
        self.output = bytearray()

    async def collect(self) -> None:
        while chunk := await self.child.stdout.read(65536):
            self.output.extend(chunk)

    def text(self, start: int = 0) -> str:
        screen = Screen(WIDTH, HEIGHT)
        screen.feed(bytes(self.output[start:]).decode("utf-8", errors="replace"))
        return "\n".join("".join(char for char, _ in row) for row in screen.cells)

    async def key(self, data: bytes, pause: float = .15) -> None:
        self.child.stdin.write(data)
        await self.child.stdin.drain()
        await asyncio.sleep(pause)

    async def type(self, text: str) -> None:
        for char in text:
            await self.key(char.encode("utf-8"))

    async def expect(self, needle: str, timeout: float = 20) -> None:
        end = time.monotonic() + timeout
        while needle not in self.text():
            if time.monotonic() > end:
                raise SystemExit(f"timed out waiting for {needle!r}:\n{self.text()}")
            await asyncio.sleep(.1)

    async def repeat_until(self, data: bytes, needle: str, every: float = 1.5, tries: int = 40) -> None:
        for _ in range(tries):
            if needle in self.text():
                return
            await self.key(data, every)
        raise SystemExit(f"never saw {needle!r}:\n{self.text()}")

    def me(self):
        return next(actor for actor in self.service.core.world.actors.values()
                    if actor.kind == "player" and actor.handle == HANDLE)

    async def step(self, data: bytes, axis: str, wanted: int) -> None:
        me = self.me()
        for _ in range(3):  # a step can meet the walking cooldown; retry it
            await self.key(data, 0)
            end = time.monotonic() + 1.5
            while getattr(me, axis) != wanted and time.monotonic() < end:
                await asyncio.sleep(.02)
            if getattr(me, axis) == wanted:
                await asyncio.sleep(.22)
                return
        raise SystemExit(f"blocked walking {axis} to {wanted} from {(me.zone, me.x, me.y)}")

    async def walk(self, x: int, y: int, first: str = "y") -> None:
        """Walk tile by tile, one key per server-confirmed move."""
        for axis in (first, "x" if first == "y" else "y"):
            target = y if axis == "y" else x
            while getattr(self.me(), axis) != target:
                here = getattr(self.me(), axis)
                delta = 1 if here < target else -1
                key = (DOWN if delta > 0 else UP) if axis == "y" else (RIGHT if delta > 0 else LEFT)
                await self.step(key, axis, here + delta)

    async def walk_out(self, data: bytes, zone: str) -> None:
        await self.key(data, 0)
        end = time.monotonic() + 5
        while self.me().zone != zone:
            if time.monotonic() > end:
                raise SystemExit(f"did not reach {zone}: {(self.me().zone, self.me().x, self.me().y)}")
            await asyncio.sleep(.05)
        await asyncio.sleep(1)

    async def capture(self, path: Path, settle: float = .5) -> None:
        start = len(self.output)
        await self.key(REDRAW, settle)
        path.write_bytes(bytes(self.output[start:]))
        print(f"wrote {path}")
        print(self.text(start))


async def play(source: Path, root: Path, out_dir: Path) -> None:
    sys.path.insert(0, str(source / "src"))
    from blacksite.server.app import Server

    service = Server(root / "world", tcp=True)
    await service.start()
    drop = root / "door_info.json"
    drop.write_text(json.dumps({"handle": HANDLE, "user_id": "website-capture", "terminal_width": WIDTH,
        "terminal_height": HEIGHT, "color_depth": "truecolor", "node_name": "website"}), encoding="utf-8")
    env = {**os.environ, "NETBBS_DOOR_INFO": str(drop), "DOOR_BLACKSITE_INSTALL_DIR": str(service.install_dir),
           "PYTHONPATH": str(source / "src")}
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "blacksite.door", env=env, cwd=str(root),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    game = Session(service, child)
    reader = asyncio.create_task(game.collect())
    try:
        # The Wake: a Ghost, the briefing skipped, a recruiter card taken.
        await game.expect("Choose your archetype")
        await game.key(DOWN)
        await game.expect("You carry a native rig")   # Ghost is under the cursor
        await game.key(b"\r", .5)
        await game.expect("Choose your name")
        await game.type("Wren\r")
        await game.expect("Briefing 1/3")
        await game.key(b"s")
        await game.expect("Recruiter cards")
        await game.key(UP, .5)
        await game.key(UP, .5)
        await game.key(b"\r", 1)
        await game.expect("Wake Hall")

        # The corridor drone teaches the combat rhythm.
        await game.walk(22, 14)
        await game.key(b"\t", .5)
        await game.repeat_until(b"f", "falls silent")

        # A Ghost's native rig jacks in anywhere: take on Sablier's Vatline core.
        await game.walk(30, 12)
        await game.key(b"j", 2)
        await game.expect("VATSIDE / LATTICE")
        for _ in range(2):
            await game.key(RIGHT, 1.3)
        await game.expect("Vatline entrance")
        await game.key(b"e", 1)
        await game.key(DOWN, .4)          # Enter with NPC risk
        await game.key(b"\r", 2)
        await game.expect("VATLINE / CORE")
        await game.key(RIGHT, 1.2)
        await game.key(RIGHT, 1.2)
        await game.key(DOWN, 1.5)
        await game.expect("Sandman")
        await game.key(b"\t", .8)
        await game.expect("Sandman 40/40 [attacking]")
        await asyncio.sleep(1.5)
        await game.capture(out_dir / "raw-blacksite-lattice.txt")

        # Escape, then walk out onto Clinic Row beside the Cold Chain relay.
        await game.key(b"j", 0)
        await game.expect("You jack out", timeout=30)
        await asyncio.sleep(1)
        await game.walk(38, 16, first="x")
        await game.walk_out(b"e", "vatside-clinics")
        await game.walk(31, 16)
        await asyncio.sleep(3)
        await game.capture(out_dir / "raw-blacksite-city.txt")

        # Up Sablier Boulevard to Meridian Plaza, the safe heart of the Core.
        await game.walk(31, 2)
        await game.walk(76, 2, first="x")
        await game.walk_out(RIGHT, "core-plaza")
        await game.walk(52, 12, first="y")
        await asyncio.sleep(3)
        await game.capture(out_dir / "raw-blacksite-plaza.txt")

        await game.key(b"\x18", .3)
        await game.key(b"y", 1)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await service.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True,
                        help=f"a Thiesi/Blacksite checkout at {COMMIT[:12]}")
    parser.add_argument("--out-dir", type=Path, default=Path("web/shots"))
    parser.add_argument("--root", type=Path, help="private world directory (default: a new temp dir)")
    args = parser.parse_args()
    source = args.source.resolve()
    head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    if head != COMMIT:
        raise SystemExit(f"{source} is at {head}; check out {COMMIT} first")
    root = (args.root or Path(tempfile.mkdtemp(prefix="blacksite-capture-"))).resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} is not empty; use a new private root")
    root.mkdir(parents=True, exist_ok=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(play(source, root, args.out_dir.resolve()))
    print(f"Blacksite {COMMIT[:12]}, {WIDTH}x{HEIGHT}, world under {root}")


if __name__ == "__main__":
    main()
