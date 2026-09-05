"""Opt-in, real-game smoke harness; installs nothing and uses an isolated test DB.

Run from a checkout with PYTHONPATH=src. Arguments name an operator-installed
profile and game directory. Inputs are JSON [seconds, text] or [prompt, text] pairs.
This can change the game's own data; use a disposable copy for certification.
"""
import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

from netbbs.auth.users import create_user
from netbbs.doors.registry import create_door
from netbbs.doors.profiles import DoorProfile
from netbbs.doors.runtime import run_door
from netbbs.net.session import Session
from netbbs.rendering import sanitize_text
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.reflow import print_wrapped
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class ScriptSession(Session):
    terminal_width = 80
    terminal_height = 25
    node_display_name = "NetBBS"

    def __init__(self, auto_page=False):
        self.queue = asyncio.Queue(maxsize=8192)
        self.output = bytearray()
        self.pending_output = bytearray()
        self.page_output = bytearray()
        self.auto_page = auto_page
        self.last_output = time.monotonic()

    async def read_byte(self):
        return await self.queue.get()

    async def write_raw(self, data):
        self.last_output = time.monotonic()
        self.output.extend(data)
        del self.output[:-32768]
        self.pending_output.extend(data)
        del self.pending_output[:-8192]
        if self.auto_page:
            self.page_output.extend(data)
            del self.page_output[:-8192]
            plain = strip_ansi(self.page_output.decode("utf-8", errors="replace"))
            if re.search(r"<more>|\[Press A Key\]|\[Pause\]", plain, re.IGNORECASE):
                self.page_output.clear()
                self.queue.put_nowait(32)

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


async def scenario(args):
    value = json.loads(Path(args.profile).read_text())
    value["profile"]["install_dir"] = str(Path(args.install).resolve())
    profile = DoorProfile.from_json(json.dumps(value["profile"]))
    with tempfile.TemporaryDirectory(prefix="netbbs-door-certify-") as directory:
        db = Database(Path(directory) / "test.db")
        lane = DatabaseLane(db.path)
        try:
            user = create_user(db, "DoorTester", password="local-test-only", user_level=255)
            door = create_door(db, value["name"], args.emulator or value["executable_path"],
                               args=tuple(value.get("args", [])), creator=user, profile=profile)
            session = ScriptSession(args.auto_page)
            task = asyncio.create_task(run_door(session, lane, door, user, wall_time_limit_seconds=args.seconds))
            try:
                inputs = Path(args.input_file).read_text() if args.input_file else args.input
                steps = json.loads(inputs)
                completed = 0
                for delay, text in steps:
                    if isinstance(delay, str):
                        wanted = delay
                        while not task.done() and wanted not in strip_ansi(session.pending_output.decode("utf-8", errors="replace")):
                            # Some LORD 4.07 randomly selected title artworks stop
                            # without a printed pager. Only dismiss that initial
                            # title, never a gameplay or new-character question.
                            if args.auto_page and wanted == "Your choice, warrior?" and time.monotonic() - session.last_output > 2:
                                session.queue.put_nowait(32)
                                session.last_output = time.monotonic()
                            await asyncio.sleep(0.05)
                        session.pending_output.clear()
                    else:
                        await asyncio.sleep(delay)
                    if task.done():
                        break
                    for byte in text.encode():
                        session.queue.put_nowait(byte)
                        await asyncio.sleep(0.03)
                    completed += 1
                result = await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            print_wrapped(sanitize_text(strip_ansi(session.output.decode("utf-8", errors="replace"))))
            print_wrapped(f"RESULT: {result.reason}, exit={result.exit_code}")
            if not args.no_diagnostics:
                print_wrapped(sanitize_text(result.diagnostic))
            if completed != len(steps):
                print_wrapped(f"SMOKE FAILED: reached {completed}/{len(steps)} scripted steps")
            return 0 if result.reason == "exited" and completed == len(steps) else 1
        finally:
            lane.close()
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile")
    parser.add_argument("install")
    parser.add_argument("--emulator")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--input", default='[[2," "],[2,"Q"],[2,"Y"]]')
    parser.add_argument("--input-file", help="JSON file of [delay_seconds or expected_output, text] pairs")
    parser.add_argument("--no-diagnostics", action="store_true", help="Hide emulator stderr when inspecting game output")
    parser.add_argument("--auto-page", action="store_true", help="Automatically dismiss LORD/TradeWars art-page pauses")
    sys.exit(asyncio.run(scenario(parser.parse_args())))
