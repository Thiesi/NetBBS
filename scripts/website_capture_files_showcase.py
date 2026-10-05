"""Capture the file areas' caller and SysOp screens for the file areas showcase page.

    PYTHONPATH=src python scripts/website_capture_files_showcase.py --list
    PYTHONPATH=src python scripts/website_capture_files_showcase.py areas web/shots/raw-files-areas.txt

The file areas companion of `website_capture_mrc_showcase.py`. Each screen is
drawn by the production code a caller or SysOp reaches -- the file area list,
an area's listing, the browser transfer screen, Find, and the SysOp console's
file area and storage screens -- against a real `Database` in a throwaway
directory. Nothing is installed, no socket is opened, no network is touched.

The node is worn on purpose: areas with a history of uploads, several of them
carrying the description their archive's own FILE_ID.DIZ supplied, because a
freshly seeded listing shows nothing a caller would recognise.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from netbbs.auth import users as users_module                   # noqa: E402
from netbbs.auth.users import SYSOP_LEVEL, create_user            # noqa: E402
from netbbs.config import set_expiry_grace_period_days              # noqa: E402
from netbbs.files import entries as entries_module                # noqa: E402
from netbbs.files.areas import create_file_area                   # noqa: E402
from netbbs.files.entries import upload_file                      # noqa: E402
from netbbs.net.redraw_preference import set_redraw_in_place_enabled  # noqa: E402
from netbbs.storage.database import Database                      # noqa: E402
from netbbs.storage.execution import DatabaseLane                 # noqa: E402

NODE_NAME = "Harbor Lights"
CLEAR = "\x1b[2J"

JOINED_AT = [
    "2025-11-04T18:22:41.000000Z",
    "2026-01-17T20:09:12.000000Z",
    "2026-03-30T13:47:55.000000Z",
    "2026-08-21T09:15:03.000000Z",
]

#: (area, description, files). A file is (name, uploader, size, description, uploaded at).
AREAS = [
    ("Utilities", "Node utilities and helper scripts", [
        ("backup-rotate.sh", "otto", 1_284, "Nightly backup rotation, seven days retained.", "2026-08-14T21:07:33"),
        ("ansi-preview.py", "wren", 6_912, "Preview an .ANS file at 80 or 132 columns before you upload it.",
         "2026-08-22T18:41:02"),
        ("netbbs-7.15.3.tar.gz", "keeper", 3_954_211, "The current NetBBS release, source distribution.",
         "2026-10-04T10:12:55"),
        ("zmodem-howto.txt", "keeper", 4_406,
         "Getting files to and from this node: SyncTERM, NetRunner\nand plain lrzsz, with screenshots.",
         "2026-09-02T19:41:07"),
        ("nodelist-tools.zip", "otto", 88_312,
         "NODELIST TOOLS v2.1\n  merge, diff and pretty-print\n  BBS lists. Freeware.", "2026-09-11T11:52:09"),
    ]),
    ("ANSI Art", "Mastheads, logos and art packs from callers", [
        ("harbor-masthead.ans", "wren", 11_840, "This node's own masthead, 80 columns.", "2026-07-30T20:15:44"),
        ("ansi-preset-pack.zip", "wren", 48_004,
         "ANSI PRESET PACK\n  twelve masthead presets\n  80 and 132 columns\n  CP437 and UTF-8 versions",
         "2026-09-07T21:03:18"),
        ("lighthouse.ans", "alice", 7_215, "A lighthouse at dusk.", "2026-09-12T20:14:33"),
        ("fall-artpack-2026.zip", "otto", 1_206_877,
         "FALL 2026 ARTPACK\n  31 pieces from 9 artists\n  includes the voting results", "2026-09-28T22:51:10"),
    ]),
    ("Door Games", "Saves, maps and add-ons for the doors here", [
        ("lord-igm-pack.zip", "otto", 304_118, "Six in-game modules for LORD 4.07.", "2026-06-19T19:02:47"),
        ("gw-season3-saves.zip", "alice", 72_904, "Global War season 3, final turn.", "2026-06-23T21:40:12"),
        ("tw2002-old-universe.zip", "otto", 140_533, "The previous TradeWars universe.", "2026-06-30T18:05:51"),
        ("voidrunner-tips.txt", "wren", 5_120, "Trade routes that paid in ruleset 3.", "2026-08-30T20:11:38"),
        ("tw2002-maps.zip", "alice", 51_660, "Sector maps from the current universe.", "2026-09-20T17:33:05"),
    ]),
    ("Text Files", "Guides, histories and the occasional rant", [
        ("bbs-history-1983.txt", "keeper", 22_430, "How the first boards in town came to be.", "2026-03-02T16:20:11"),
        ("modem-init-strings.txt", "otto", 3_998, "Init strings for 40 modems, collected.", "2026-05-11T09:44:30"),
        ("netiquette.txt", "keeper", 2_210, None, "2026-01-18T12:00:00"),
    ]),
    # Moderated: uploads wait for the SysOp before callers see them.
    ("Incoming", "New uploads, checked by the SysOp before they go public", [
        ("qwk-reader-1.4.zip", "alice", 212_448,
         "QWK READER 1.4\n  offline mail for any BBS\n  DOS and Windows builds", "2026-10-03T22:18:40"),
        ("sunset-harbor.ans", "otto", 9_334, "Harbor at sunset, 80x50.", "2026-10-04T19:56:02"),
    ]),
]


#: The listing screens, drawn on a roomier terminal than 80x24: an area whose
#: files carry multi-line FILE_ID.DIZ descriptions is taller than 24 rows.
TALL = (88, 36)


def on_terminal(written: list[str]) -> str:
    """What the terminal holds: everything since the last clear."""
    text = "".join(written)
    return text[text.rfind(CLEAR):] if CLEAR in text else text


class Node:
    """A worn node: four regulars and four file areas with a history."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="netbbs-files-shot-"))
        self.db = Database(self.tmp / "node.db")
        stamps = iter(JOINED_AT)
        original = users_module.utc_now_iso
        users_module.utc_now_iso = lambda: next(stamps, JOINED_AT[-1])
        try:
            self.people = {
                "keeper": create_user(self.db, "keeper", password="hunter2", user_level=SYSOP_LEVEL),
                "wren": create_user(self.db, "wren", password="hunter2", user_level=20),
                "otto": create_user(self.db, "otto", password="hunter2", user_level=20),
                "alice": create_user(self.db, "alice", password="hunter2", user_level=10),
            }
        finally:
            users_module.utc_now_iso = original
        for user in self.people.values():
            set_redraw_in_place_enabled(self.db, user, True)
        self.areas = {}
        original = entries_module.utc_now_iso
        try:
            for name, description, files in AREAS:
                area = create_file_area(self.db, name, description=description, creator=self.people["keeper"],
                                        max_file_age_days=90 if name == "Door Games" else None,
                                        moderated=name == "Incoming")
                self.areas[name] = area
                for filename, who, size, text, at in files:
                    entries_module.utc_now_iso = lambda at=at: f"{at}.000000Z"
                    payload = (filename.encode() + b"\n") * (size // (len(filename) + 1) + 1)
                    upload_file(self.db, area, self.people[who], filename, payload[:size], description=text)
        finally:
            entries_module.utc_now_iso = original
        # A month's grace before expired files are purged, so the SysOp's
        # E[x]pired files screen has something to recover.
        set_expiry_grace_period_days(self.db, 30)
        self.lane = DatabaseLane(self.db.path)

    def close(self) -> None:
        self.lane.close()
        self.db.close()


def walker(keys, *, width: int = 80, height: int = 24):
    """sysop_gallery's scripted session: types `keys`, then stops the walk."""
    from sysop_gallery import make_session

    session = make_session(list(keys), width=width, height=height)
    session.node_display_name = NODE_NAME
    session.node_name_gradient = None
    session.supports_truecolor = True
    return session


async def walk(session, coroutine) -> str:
    from sysop_gallery import ScriptExhausted

    try:
        await coroutine
    except ScriptExhausted:
        pass
    return on_terminal(session.written)


async def shot_areas(node: Node) -> str:
    """Files: the file area list."""
    from netbbs.net.file_flow import browse_file_areas

    session = walker([])
    return await walk(session, browse_file_areas(session, node.lane, node.people["alice"]))


async def shot_listing(node: Node) -> str:
    """One file area, the cursor on a file with a FILE_ID.DIZ description."""
    from netbbs.net.file_flow import _show_area

    session = walker(["DOWN", "DOWN", "DOWN"], width=TALL[0], height=TALL[1])
    return await walk(session, _show_area(session, node.lane, node.areas["Utilities"], node.people["alice"]))


async def shot_transfer(node: Node) -> str:
    """[W]eb transfer: a single-use browser link for the file under the cursor."""
    from netbbs.net.file_flow import _show_area
    from netbbs.net.file_transfer import TransferGrants

    session = walker(["DOWN", "DOWN", "w", "d"], width=TALL[0], height=TALL[1])
    transfers = TransferGrants(base_url="https://harborlights.example.net")
    return await walk(session, _show_area(session, node.lane, node.areas["ANSI Art"], node.people["alice"],
                                          transfers=transfers))


async def shot_find(node: Node) -> str:
    """[/] Find: one query across message boards, file areas and chat channels."""
    from netbbs.chat.hub import ChatHub
    from netbbs.chat.mailbox import MessageMailbox
    from netbbs.chat.presence import PresenceRegistry
    from netbbs.net.char_input import InputHistory
    from netbbs.net.scan_and_find import _find_screen

    session = walker(["ansi"])
    return await walk(session, _find_screen(session, node.db, node.lane, ChatHub(), PresenceRegistry(),
                                            MessageMailbox(), InputHistory(), node.people["alice"]))


async def _console(node: Node, keys: list[str]) -> str:
    from sysop_gallery import link_context, node_controls

    from netbbs.net.admin_flow import admin_menu

    session = walker(keys)
    return await walk(session, admin_menu(session, node.lane, node.people["keeper"],
                                          node_controls=node_controls(node.tmp), link_context=link_context()))


async def shot_sysop_area(node: Node) -> str:
    """SysOp console > Content > File areas > one area."""
    return await _console(node, ["c", "f", "l", "0", "1"])


async def shot_sysop_expired(node: Node) -> str:
    """The same screen's E[x]pired files: what a file area's age limit took."""
    return await _console(node, ["c", "f", "l", "0", "3", "x", "0", "1"])


async def shot_sysop_pending(node: Node) -> str:
    """A moderated file area's pending uploads, waiting for the SysOp."""
    return await _console(node, ["c", "f", "l", "0", "5", "p"])


SHOTS = {
    "areas": shot_areas,
    "listing": shot_listing,
    "transfer": shot_transfer,
    "find": shot_find,
    "sysop-area": shot_sysop_area,
    "sysop-pending": shot_sysop_pending,
    "sysop-expired": shot_sysop_expired,
}


async def run(name: str) -> str:
    node = Node()
    try:
        return await SHOTS[name](node)
    finally:
        node.close()


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
    snapshot = asyncio.run(run(args.shot))
    # Bytes, not text: see the note in website_ansi_to_html.py.
    args.output.write_bytes(snapshot.encode("utf-8"))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
