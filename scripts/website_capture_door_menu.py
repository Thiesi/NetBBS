"""Capture the caller-facing door picker for the website gallery.

Registers the three DOS templates, the two bundled native games and a remote
service against a real `Database`/`DatabaseLane`, then renders the production
`browse_doors` picker -- so the screen shows native, DOS-under-DOSBox-X and
remote doors side by side, which is the point of the shot.

    PYTHONPATH=src python scripts/website_capture_door_menu.py raw-doors.txt

Nothing is installed and no door is launched; only the picker is drawn.
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
import netbbs.doors.remote as _remote                           # noqa: E402

# The shipped templates name POSIX installation and credential paths -- the
# only kind the door guide supports. On a Windows capture host `pathlib.Path`
# calls those relative and validation rejects them, so the is-absolute test
# alone is evaluated with POSIX semantics. Nothing rendered changes.
_profiles.Path = pathlib.PurePosixPath
_remote.Path = pathlib.PurePosixPath

from netbbs.auth.users import create_user                       # noqa: E402
from netbbs.doors.profiles import DoorProfile                   # noqa: E402
from netbbs.doors.registry import create_door                   # noqa: E402
from netbbs.net.char_input import EditorKey, EditorKeyKind      # noqa: E402
from netbbs.net.door_flow import browse_doors                   # noqa: E402
from netbbs.storage.database import Database                    # noqa: E402
from netbbs.storage.execution import DatabaseLane               # noqa: E402
from tests.test_door_flow import FakeSession                    # noqa: E402

PRESETS = ROOT / "src" / "netbbs" / "doors" / "presets"


class CaptureSession(FakeSession):
    supports_truecolor = True
    snapshot: str | None = None

    def __init__(self):
        super().__init__([])
        self.node_display_name = "Harbor Lights"

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        # Snapshot the screen before the keystroke that leaves is echoed onto
        # the prompt. [B]ack is what actually leaves the picker -- Esc only
        # drops the cursor highlight, so an Esc-returning fake spins forever.
        if self.snapshot is None:
            self.snapshot = "".join(self.written)
        return EditorKey(EditorKeyKind.CHAR, char="b")


def preset(name: str) -> dict:
    return json.loads((PRESETS / f"{name}.json").read_text(encoding="utf-8"))


async def capture() -> str:
    tmp = Path(tempfile.mkdtemp(prefix="netbbs-shot-"))
    db = Database(tmp / "node.db")
    lane = DatabaseLane(db.path)
    try:
        sysop = create_user(db, "carrier", password="hunter2", user_level=255)
        caller = create_user(db, "alice", password="hunter2", user_level=10)

        def register(title, template, description, **overrides):
            value = preset(template)
            profile = dict(value["profile"], **overrides)
            create_door(db, title, value["executable_path"], creator=sysop,
                        description=description,
                        profile=DoorProfile.from_json(json.dumps(profile)))

        # Descriptions are kept short on purpose: the picker truncates them
        # to the terminal width, and a row ending in "..." reads badly.
        register("Legend of the Red Dragon", "dos-lord",
                 "DOS 4.07, via DOSBox-X and BNU.",
                 install_dir="/var/games/netbbs/lord")
        register("TradeWars 2002", "dos-tradewars-2002",
                 "DOS 3.09, persistent galaxy.",
                 install_dir="/var/games/netbbs/tw2002")
        register("Global War", "dos-global-war",
                 "DOS 2.7, turn-based conquest.",
                 install_dir="/var/games/netbbs/globalwar")
        register("Voidrunner", "native-stdio", "Native NetBBS door, sandboxed.")
        register("Retro Trivia", "native-stdio", "Native bundled quiz door.")
        register("Barren Realms Elite", "remote-doorparty", "Operator-run tunnel.")

        session = CaptureSession()
        await browse_doors(session, lane, caller)
        assert session.snapshot is not None, "picker never drew"
        return session.snapshot
    finally:
        lane.close()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path, help="raw ANSI capture to write")
    args = parser.parse_args()

    # Bytes, not text: see the note in website_ansi_to_html.py.
    args.output.write_bytes(asyncio.run(capture()).encode("utf-8"))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
