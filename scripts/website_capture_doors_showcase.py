"""Capture the SysOp's door screens for the doors showcase page.

    PYTHONPATH=src python scripts/website_capture_doors_showcase.py --list
    PYTHONPATH=src python scripts/website_capture_doors_showcase.py profile-vm web/shots/raw-doors-profile-vm.txt

The companion of `website_capture_door_profile.py` (one template's profile
editor) and `website_capture_door_menu.py` (the caller's picker): the
compatibility profile of a VM door, and the SysOp
console's door list, each drawn by the production code a SysOp reaches.
Nothing is installed, launched or dialled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import netbbs.doors.profiles as _profiles                       # noqa: E402
import netbbs.doors.remote as _remote                           # noqa: E402
import netbbs.doors.vm as _vm                                   # noqa: E402

# The shipped templates name POSIX paths -- the only kind the door guide
# supports. On a Windows capture host `pathlib.Path` calls those relative and
# validation rejects them, so the is-absolute test alone is evaluated with
# POSIX semantics (as `website_capture_door_menu.py` does). The VM and remote
# adapters validate their own paths, so they get the same treatment.
_profiles.Path = pathlib.PurePosixPath
_remote.Path = pathlib.PurePosixPath
_vm.Path = pathlib.PurePosixPath

from netbbs.auth.users import create_user                       # noqa: E402
from netbbs.doors.profiles import DoorProfile                   # noqa: E402
from netbbs.doors.registry import create_door                   # noqa: E402
from netbbs.net.door_profile_flow import edit_door_profile      # noqa: E402
from netbbs.storage.database import Database                    # noqa: E402
from netbbs.storage.execution import DatabaseLane               # noqa: E402
from tests.test_door_flow import FakeSession                    # noqa: E402

PRESETS = ROOT / "src" / "netbbs" / "doors" / "presets"

#: The doors the console list shows: (title, template, description).
DOORS = [
    ("Voidrunner", None, "Bundled: trade, fight and explore a living galaxy"),
    ("War Dialer", None, "Bundled: build a phreaker crew, raid your rivals"),
    ("Legend of the Red Dragon", "dos-lord", "LORD 4.07 under DOSBox-X"),
    ("TradeWars 2002", "dos-tradewars-2002", "The classic space trading game"),
    ("Global War", "dos-global-war", "Turn-based world conquest"),
    ("Amiga Empire", "vm-linux", "A Linux door in a per-caller VM"),
    ("DoorParty", "remote-doorparty", "Dozens of doors on a remote server"),
]


class CaptureSession(FakeSession):
    supports_truecolor = True

    def __init__(self, inputs):
        super().__init__(inputs)
        self.node_display_name = "Harbor Lights"

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        # A named key ("PAGE_DOWN") is that key, so a walk can page through
        # the editor's sections; anything else is typed as before.
        from netbbs.net.char_input import EditorKey, EditorKeyKind
        if self._inputs and self._inputs[0] in EditorKeyKind.__members__ and len(self._inputs[0]) > 1:
            return EditorKey(EditorKeyKind[self._inputs.pop(0)])
        return await super().read_editor_key(distinguish_ctrl_h=distinguish_ctrl_h)


def template(name: str) -> dict:
    return json.loads((PRESETS / f"{name}.json").read_text(encoding="utf-8"))


def register(db, sysop, title: str, name: str | None, description: str = ""):
    if name is None:
        return create_door(db, title, sys.executable, description=description, creator=sysop)
    value = template(name)
    return create_door(db, title, value["executable_path"], description=description, creator=sysop,
                       profile=DoorProfile.from_json(json.dumps(value["profile"])))


async def profile(name: str, title: str, pages: int = 0) -> str:
    tmp = Path(tempfile.mkdtemp(prefix="netbbs-shot-"))
    db = Database(tmp / "node.db")
    lane = DatabaseLane(db.path)
    try:
        sysop = create_user(db, "carrier", password="hunter2", user_level=255)
        door = register(db, sysop, title, name)
        # Page to the section wanted, render it, then [B]ack.
        session = CaptureSession(["PAGE_DOWN"] * pages + ["b"])
        await edit_door_profile(session, lane, sysop, door)
        # Paging redraws below the previous section rather than clearing, so
        # keep only the last drawing: from the row its title is on.
        text = "".join(session.written)
        title = text.rfind("Door compatibility")
        return text[text.rfind("\n", 0, title) + 1:]
    finally:
        lane.close()
        db.close()


async def console_doors() -> str:
    """SysOp console > Content > Doors > List."""
    from sysop_gallery import link_context, make_session, node_controls, on_terminal

    from netbbs.net.admin_flow import admin_menu
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled

    tmp = Path(tempfile.mkdtemp(prefix="netbbs-shot-"))
    db = Database(tmp / "node.db")
    try:
        sysop = create_user(db, "carrier", password="hunter2", user_level=255)
        set_redraw_in_place_enabled(db, sysop, True)
        for title, name, description in DOORS:
            register(db, sysop, title, name, description)
    finally:
        db.close()
    lane = DatabaseLane(tmp / "node.db")
    session = make_session(["c", "d", "l"], width=80, height=24)
    session.node_display_name = "Harbor Lights"
    try:
        await admin_menu(session, lane, sysop, node_controls=node_controls(tmp), link_context=link_context())
    except Exception as exc:  # the walker stops by raising once its keys run out
        if type(exc).__name__ != "ScriptExhausted":
            raise
    finally:
        lane.close()
    return on_terminal(session.written)


SHOTS = {
    # The Advanced section: a VM door's guest and qemu settings.
    "profile-vm": lambda: profile("vm-linux", "Amiga Empire", pages=4),
    "console-doors": console_doors,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("shot", nargs="?", choices=sorted(SHOTS))
    parser.add_argument("output", nargs="?", type=Path, help="raw ANSI capture to write")
    parser.add_argument("--list", action="store_true", help="name the screens this script captures")
    args = parser.parse_args()
    if args.list:
        print("\n".join(SHOTS))
        return
    if args.shot is None or args.output is None:
        parser.error("name a shot and an output file, or pass --list")
    snapshot = asyncio.run(SHOTS[args.shot]())
    # Bytes, not text: see the note in website_ansi_to_html.py.
    args.output.write_bytes(snapshot.encode("utf-8"))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
