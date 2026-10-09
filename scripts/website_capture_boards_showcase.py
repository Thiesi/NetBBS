"""Capture the message board screens for the message boards showcase page.

    PYTHONPATH=src python scripts/website_capture_boards_showcase.py --list
    PYTHONPATH=src python scripts/website_capture_boards_showcase.py board web/shots/raw-boards-board.txt

The companion of `website_capture_screens.py`, whose `boards` capture shows
one post list: this seeds a node with categories, threads, replies, a
moderated board with a held post and a board carried over NetBBS Link from
another node, then walks a caller and a SysOp through the production screens
-- the board list, a post list, a reply, the editor, New scan, Find, the
moderation queue, a post's revision history and the console's board
settings. A temp database, no sockets, no network.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from netbbs.activity import ensure_board_baseline, follow, record_post_opened  # noqa: E402
from netbbs.auth import users as users_module                                 # noqa: E402
from netbbs.auth.users import SYSOP_LEVEL, create_user                        # noqa: E402
from netbbs.boards import posts as posts_module                               # noqa: E402
from netbbs.boards.boards import create_board, get_board_by_name             # noqa: E402
from netbbs.boards.categories import create_category                          # noqa: E402
from netbbs.boards.posts import create_post, edit_post                        # noqa: E402
from netbbs.chat.hub import ChatHub                                           # noqa: E402
from netbbs.chat.mailbox import MessageMailbox                                # noqa: E402
from netbbs.chat.presence import PresenceRegistry                             # noqa: E402
from netbbs.link.boards import materialize_carried_board, materialize_carried_post  # noqa: E402
from netbbs.link.events import build_board_genesis, build_board_post          # noqa: E402
from netbbs.link.node_identity import bootstrap_node_identity                 # noqa: E402
from netbbs.link.protocol import LinkNode                                     # noqa: E402
from netbbs.link.store import save_peer                                       # noqa: E402
from netbbs.mail import send_mail                                             # noqa: E402
from netbbs.net.char_input import EditorKey, EditorKeyKind, InputHistory      # noqa: E402
from netbbs.net.redraw_preference import set_redraw_in_place_enabled          # noqa: E402
from netbbs.net.session import Session                                        # noqa: E402
from netbbs.quoting import quote_body                          # noqa: E402
from netbbs.storage.database import Database                                  # noqa: E402
from netbbs.storage.execution import DatabaseLane                             # noqa: E402

NODE_NAME = "Harbor Lights"
CLEAR = "\x1b[2J"


def _stamps(start_day: int):
    """Plausible, strictly increasing instants from early September 2026."""
    for n in itertools.count():
        day = start_day + n // 3
        yield f"2026-09-{min(day, 28):02d}T{(9 + 5 * (n % 3)) % 24:02d}:{(7 * n + 13) % 60:02d}:{(11 * n) % 60:02d}.000000Z"


class Walker(Session):
    """Types from a queue, and waits (rather than leaving) once it is empty,
    so a screen can be photographed while it is up."""

    supports_truecolor = True

    def __init__(self, keys=(), *, width=80, height=24):
        self.inputs: asyncio.Queue = asyncio.Queue()
        for key in keys:
            self.inputs.put_nowait(key)
        self.written: list[str] = []
        self.terminal_width, self.terminal_height = width, height
        self.node_display_name = NODE_NAME
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"

    async def write(self, text):
        self.written.append(text)

    async def read_line(self, echo=True, history=None, completer=None, **kwargs):
        line = await self.inputs.get()
        # A terminal shows what was typed, and Enter moves to the next row.
        self.written.append((line if echo else "") + "\r\n")
        return line

    async def read_key(self, echo=True):
        return await self.inputs.get()

    async def read_editor_key(self, *, distinguish_ctrl_h=False):
        key = await self.inputs.get()
        if isinstance(key, EditorKey):
            return key
        kinds = {kind.name: kind for kind in EditorKeyKind}
        if len(key) > 1 and key in kinds:
            return EditorKey(kinds[key])
        return EditorKey(EditorKeyKind.CHAR, char=key)

    async def close(self):
        pass

    async def read_byte(self):
        raise NotImplementedError

    async def write_raw(self, data):
        raise NotImplementedError


def on_terminal(session: Walker) -> str:
    text = "".join(session.written)
    return text[text.rfind(CLEAR):] if CLEAR in text else text


def visible(session: Walker) -> str:
    import re
    return " ".join(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[78]", "", "".join(session.written)).split())


async def snapshot(session: Walker, coroutine, expect: str, *, settle: float = 0.3) -> str:
    """Run a screen until `expect` is drawn and the input queue is drained,
    keep what the terminal shows, then stop it."""
    task = asyncio.create_task(coroutine)
    deadline = asyncio.get_running_loop().time() + 15
    try:
        while not (session.inputs.empty() and expect in visible(session)):
            if task.done():
                task.result()
                raise RuntimeError(f"the screen returned before {expect!r} was drawn")
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"waiting for {expect!r}; the screen ends: {visible(session)[-500:]!r}")
            await asyncio.sleep(0.02)
        await asyncio.sleep(settle)
        return on_terminal(session)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


# -- the node ------------------------------------------------------------

THREADS = {
    # board -> [(author, subject, body, [(replier, body), ...]), ...], oldest first
    "Retro Computing": [
        ("otto", "Recapping a C64 breadbin",
         "Finally opened up the breadbin I found at the flea market. Every\n"
         "electrolytic on the board is the original. Anyone have a cap list\n"
         "for the 250407 board, and is it worth doing the whole lot at once?",
         [("wren", "> Do them all at once. The 250407 list is in the file area under\n"
                   "Utilities -- recap-250407.txt. Mind the polarity on C35."),
          ("keeper", "And photograph the board before you desolder anything.")]),
        ("wren", "Which 3.5\" drives still read 720K?",
         "I have a stack of DOS disks from 1991 and a modern USB drive\n"
         "that refuses every one of them. What are people using?", []),
        ("marla", "Show us your desk",
         "Post a picture of where you call in from. Mine is a 486 on a\n"
         "kitchen table with a 14\" VGA and a very loud fan.",
         [("otto", "Amiga 1200 in a tower case, and a cat that sits on the keyboard."),
          ("alice", "> A laptop on the sofa, if I'm honest. But the terminal is green.")]),
        ("otto", "Zip disk click of death -- myth?",
         "My Zip 100 started clicking last night. Is the click of death\n"
         "real, or does a drive come back after a rest?", []),
        ("keeper", "Swap meet on the 26th",
         "Table space is free. Bring what you no longer use and leave\n"
         "with what someone else no longer uses.", []),
    ],
    "Door Games": [
        ("marla", "Voidrunner: who is running the Kessler lane?",
         "Someone keeps undercutting my ore price at Kessler. Own up.", [("otto", "Not me. Probably.")]),
        ("wren", "War Dialer season ends Sunday", "Last call for the top of the board.", []),
    ],
    "Announcements": [
        ("keeper", "Message boards now carry the NetBBS Users board",
         "We now carry NetBBS Users over NetBBS Link. Posts there come\n"
         "from callers on other nodes and are marked with their home node.", []),
        ("keeper", "Link sync window moved to 04:00",
         "Quieter for everyone, and it stops colliding with the backup.", []),
    ],
}

REMOTE_POSTS = [
    ("signal", "Running NetBSD on a Pi 4?",
     "Our node lives on a Raspberry Pi 4 under NetBSD 11. Happy to\n"
     "compare notes with anyone doing the same."),
    ("fen", "Masthead art swap",
     "Trading 80-column mastheads. Mine are in our file area;\n"
     "drop a reply if you want to swap."),
    ("signal", "Re: Masthead art swap", "Count me in. Sending two over mail."),
]


class Node:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db = Database(tmp / "node.db")
        self.lane = DatabaseLane(self.db.path)
        db = self.db

        stamps = iter(["2025-11-04T18:22:41.000000Z", "2026-01-17T20:09:12.000000Z",
                       "2026-03-30T13:47:55.000000Z", "2026-05-02T10:03:19.000000Z",
                       "2026-08-21T09:15:03.000000Z"])
        real_now = users_module.utc_now_iso
        users_module.utc_now_iso = lambda: next(stamps, "2026-08-21T09:15:03.000000Z")
        try:
            self.people = {
                "keeper": create_user(db, "keeper", password="hunter2", user_level=SYSOP_LEVEL),
                "wren": create_user(db, "wren", password="hunter2", user_level=20),
                "otto": create_user(db, "otto", password="hunter2", user_level=20),
                "marla": create_user(db, "marla", password="hunter2", user_level=20),
                "alice": create_user(db, "alice", password="hunter2", user_level=10),
            }
        finally:
            users_module.utc_now_iso = real_now
        for user in self.people.values():
            set_redraw_in_place_enabled(db, user, True)
        keeper = self.people["keeper"]

        node = create_category(db, "This node", description="News and housekeeping", created_by=keeper)
        hobbies = create_category(db, "Hobbies", description="What we do when we're not online", created_by=keeper)
        self.boards = {
            "Announcements": create_board(db, "Announcements", description="Node news and release notes",
                                          category_id=node.id, min_write_level=255, creator=keeper),
            "General": create_board(db, "General", description="Anything that fits nowhere else",
                                    creator=keeper),
            "Retro Computing": create_board(db, "Retro Computing", description="Old machines, new tricks",
                                            category_id=hobbies.id, creator=keeper),
            "Door Games": create_board(db, "Door Games", description="Strategy, rivalries and high scores",
                                       category_id=hobbies.id, creator=keeper),
            "Classifieds": create_board(db, "Classifieds", description="Buy, sell, swap -- every post is reviewed",
                                        moderated=True, max_post_age_days=60, creator=keeper),
            "After Hours": create_board(db, "After Hours", description="Grown-up talk, verified names only",
                                        min_age=18, name_requirement="verified", creator=keeper),
        }

        # Every post gets a dated timestamp instead of "now".
        clock = _stamps(2)
        real_post_now = posts_module.utc_now_iso
        posts_module.utc_now_iso = lambda: next(clock)
        try:
            self.posts = {}
            for board_name in ("Announcements", "Door Games", "Retro Computing"):
                board = self.boards[board_name]
                for author, subject, body, replies in THREADS[board_name]:
                    root = create_post(db, board, self.people[author], subject, body)
                    self.posts[subject] = root
                    for replier, reply in replies:
                        # "> " marks a reply written the way [R]eply starts
                        # one: the post it answers, quoted.
                        if reply.startswith("> "):
                            reply = quote_body(body, author=author) + reply[2:]
                        create_post(db, board, self.people[replier], f"Re: {subject}", reply,
                                    parent_post_id=root.post_id)
                    if subject == "Recapping a C64 breadbin":
                        # alice's first visit was after this thread: it, and
                        # what she opens below, are read; the rest is new.
                        ensure_board_baseline(db, self.people["alice"], board)
            # A post edited after it went up, so it has a revision history.
            first = self.posts["Which 3.5\" drives still read 720K?"]
            edit_post(db, first, self.boards["Retro Computing"], subject=first.subject,
                      body=first.body + "\n\nEdit: it is a Teac FD-235HF, if that helps.",
                      edited_by=self.people["wren"])
            night = create_post(db, self.boards["General"], self.people["alice"], "Hello from the night shift",
                                "Anyone else up at 3am? The node is quiet and the tea is hot.")
            create_post(db, self.boards["General"], self.people["wren"], "Re: Hello from the night shift",
                        quote_body(night.body, author="alice") + "Every night. Come to #lobby.",
                        parent_post_id=night.post_id)
            # Held for review on the moderated board.
            create_post(db, self.boards["Classifieds"], self.people["otto"], "FS: Hayes Smartmodem 2400",
                        "Boxed, with manual. Swap for a working Zip drive?")
            create_post(db, self.boards["Classifieds"], self.people["marla"], "WTB: Amiga 500 power supply",
                        "Mine finally gave out. Will pay postage.")
        finally:
            posts_module.utc_now_iso = real_post_now

        # A board carried from another node over NetBBS Link.
        remote = bootstrap_node_identity("bluewater")
        genesis = build_board_genesis(signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
                                      board_id="netbbs-users", name="NetBBS Users",
                                      created_at="2026-06-01T12:00:00Z")
        materialize_carried_board(db, genesis)
        # The origin introduced itself, so its posts read by node name.
        save_peer(db, LinkNode(identity=remote).handle_hello(LinkNode(identity=remote).build_hello(
            addresses=None, outgoing_only=True, created_at="2026-06-01T12:00:00+00:00",
            friendly_name="Bluewater", canonical_dns_name="bluewater.example.net")))
        self.boards["NetBBS Users"] = get_board_by_name(db, "NetBBS Users")
        for minute, (user, subject, body) in enumerate(REMOTE_POSTS):
            event = build_board_post(signing_identity=remote.signing_key, home_node_fingerprint=remote.fingerprint,
                                     local_user_id=user, board_id="netbbs-users", subject=subject, body=body,
                                     created_at=f"2026-09-1{minute + 1}T20:{10 + minute:02d}:00Z")
            materialize_carried_post(db, event, sender_fingerprint=remote.fingerprint)

        # alice has been reading: the first visit sets a floor, then she
        # opened the older threads; the newest ones are new to her.
        alice = self.people["alice"]
        retro = self.boards["Retro Computing"]
        follow(db, alice, "board", retro.id)
        send_mail(db, self.people["otto"], alice, "Swap meet table",
                  "Want to split a table on the 26th? I have too many Zip disks.")
        record_post_opened(db, alice, retro, self.posts["Which 3.5\" drives still read 720K?"])

    def close(self):
        self.lane.close()
        self.db.close()


# -- screens -------------------------------------------------------------

async def shot_list(node: Node) -> str:
    """The top level of Message boards: categories and boards together."""
    from netbbs.net.board_flow import _browse_boards

    session = Walker()
    return await snapshot(session, _browse_boards(session, node.db, node.people["alice"]), "Hobbies")


async def shot_board(node: Node) -> str:
    """Hobbies > Retro Computing: one page of posts, the unread ones marked."""
    from netbbs.net.board_flow import _show_board

    session = Walker()
    return await snapshot(session, _show_board(session, node.db, node.boards["Retro Computing"],
                                               node.people["alice"], breadcrumb=("Message boards", "Hobbies")),
                          "Show us your desk")


async def shot_post(node: Node) -> str:
    """Reading a thread: the post, its replies, and the actions on it."""
    from netbbs.net.board_flow import _show_board

    session = Walker(["0", "2"])
    return await snapshot(session, _show_board(session, node.db, node.boards["Retro Computing"],
                                               node.people["alice"], breadcrumb=("Message boards", "Hobbies")),
                          "ail author")


async def shot_newscan(node: Node) -> str:
    """New scan: what changed since the caller last looked, followed first."""
    from netbbs.net.scan_and_find import _new_scan_screen

    session = Walker()
    return await snapshot(session, _new_scan_screen(
        session, node.db, node.lane, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(),
        node.people["alice"]), "Retro")


async def shot_find(node: Node) -> str:
    """Find: one query across message boards, file areas and chat channels."""
    from netbbs.net.scan_and_find import _find_screen

    session = Walker(["drive"])
    return await snapshot(session, _find_screen(
        session, node.db, node.lane, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(),
        node.people["alice"]), "720K")


async def shot_linked(node: Node) -> str:
    """A board carried over NetBBS Link: posts written on another node."""
    from netbbs.net.board_flow import _show_board

    session = Walker()
    return await snapshot(session, _show_board(session, node.db, node.boards["NetBBS Users"],
                                               node.people["alice"]), "Masthead")


async def shot_queue(node: Node) -> str:
    """A moderator on Classifieds: posts held for review."""
    from netbbs.net.board_flow import _show_board

    session = Walker(["q"])
    return await snapshot(session, _show_board(session, node.db, node.boards["Classifieds"],
                                               node.people["keeper"]), "Hayes")


async def shot_history(node: Node) -> str:
    """A moderator reading an edited post's revisions."""
    from netbbs.net.board_flow import _show_board

    session = Walker(["0", "4", "h"])
    return await snapshot(session, _show_board(session, node.db, node.boards["Retro Computing"],
                                               node.people["keeper"], breadcrumb=("Message boards", "Hobbies")),
                          "Teac")


async def shot_settings(node: Node) -> str:
    """SysOp console > Content > Message boards > After Hours."""
    from sysop_gallery import link_context, node_controls

    from netbbs.net.admin_flow import admin_menu

    session = Walker(["c", "m", "l", *ROW_AFTER_HOURS, ">"])
    return await snapshot(session, admin_menu(session, node.lane, node.people["keeper"],
                                              node_controls=node_controls(node.tmp), link_context=link_context()),
                          "Name requirement")


#: After Hours' row in the console's board list (the SysOp's order).
ROW_AFTER_HOURS = "06"


async def shot_editor(node: Node) -> str:
    """Writing a reply: the full-screen editor, the quote already in place."""
    from netbbs.net.board_flow import _show_board
    from netbbs.net.editor_preference import set_fullscreen_editor_enabled

    set_fullscreen_editor_enabled(node.db, node.people["alice"], True)

    typed = "Mine is the same Teac. Check the HD jumper: 720K needs it open."
    session = Walker(["0", "4", "r", "", *typed])
    return await snapshot(session, _show_board(session, node.db, node.boards["Retro Computing"],
                                               node.people["alice"], breadcrumb=("Message boards", "Hobbies")),
                          "wrote")


SHOTS = {
    "list": shot_list,
    "board": shot_board,
    "post": shot_post,
    "editor": shot_editor,
    "newscan": shot_newscan,
    "find": shot_find,
    "linked": shot_linked,
    "queue": shot_queue,
    "history": shot_history,
    "settings": shot_settings,
}


async def run(name: str) -> str:
    node = Node(Path(tempfile.mkdtemp(prefix="netbbs-boards-shot-")))
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
    screen = asyncio.run(run(args.shot))
    # Bytes, not text: see the note in website_ansi_to_html.py.
    args.output.write_bytes(screen.encode("utf-8"))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
