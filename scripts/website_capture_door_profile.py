"""Capture the SysOp door-compatibility editor for the website gallery.

Loads a shipped template into a real door registration and renders the
production `edit_door_profile` draft editor, then leaves without saving.

    PYTHONPATH=src python scripts/website_capture_door_profile.py raw-door.txt

Nothing is installed, no static check runs and no game is launched -- only
the editor's first section is drawn.

Caption warning for whoever uses this shot: `Setup template` and
`Import JSON` are **action** fields. Their prompts open a picker or ask for a
path, and the shared `FieldSpec.render` falls back to `(none)` for any draft
key that is absent, so both rows read `(none)` even after a SysOp has picked
a template. Do not caption the screen as "template loaded" -- the DOS
identity is in `Adapter`, `I/O endpoint` and the emulator path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import tempfile
from pathlib import Path

# `tests.*` (the scripted FakeSession the door tests use) lives at the repo
# root, which is not on sys.path for a script in scripts/.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import netbbs.doors.profiles as _profiles                       # noqa: E402

# The DOS templates name a POSIX installation directory -- the only kind the
# door guide supports for DOS. On a Windows capture host `pathlib.Path` calls
# that relative and validation rejects it, so the is-absolute test alone is
# evaluated with POSIX semantics. Nothing rendered changes.
_profiles.Path = pathlib.PurePosixPath

from netbbs.auth.users import create_user                       # noqa: E402
from netbbs.doors.profiles import DoorProfile                   # noqa: E402
from netbbs.doors.registry import create_door                   # noqa: E402
from netbbs.net.door_profile_flow import edit_door_profile      # noqa: E402
from netbbs.storage.database import Database                    # noqa: E402
from netbbs.storage.execution import DatabaseLane               # noqa: E402
from tests.test_door_flow import FakeSession                    # noqa: E402

PRESETS = ROOT / "src" / "netbbs" / "doors" / "presets"


class CaptureSession(FakeSession):
    supports_truecolor = True

    def __init__(self, inputs):
        super().__init__(inputs)
        self.node_display_name = "Harbor Lights"


async def capture(template: str, title: str) -> str:
    tmp = Path(tempfile.mkdtemp(prefix="netbbs-shot-"))
    db = Database(tmp / "node.db")
    lane = DatabaseLane(db.path)
    try:
        sysop = create_user(db, "carrier", password="hunter2", user_level=255)
        value = json.loads((PRESETS / f"{template}.json").read_text(encoding="utf-8"))
        door = create_door(db, title, value["executable_path"], creator=sysop,
                           profile=DoorProfile.from_json(json.dumps(value["profile"])))
        session = CaptureSession(["b"])   # render the editor, then [B]ack
        await edit_door_profile(session, lane, sysop, door)
        return "".join(session.written)
    finally:
        lane.close()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path, help="raw ANSI capture to write")
    parser.add_argument("--template", default="dos-lord",
                        help="a template stem under src/netbbs/doors/presets")
    parser.add_argument("--title", default="Legend of the Red Dragon")
    args = parser.parse_args()

    # Bytes, not text: see the note in website_ansi_to_html.py.
    args.output.write_bytes(asyncio.run(capture(args.template, args.title)).encode("utf-8"))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
