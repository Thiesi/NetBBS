"""Render every SysOp console screen as a page, the way a terminal shows it.

    python scripts/sysop_gallery.py --out build/sysop-gallery --open
    python scripts/sysop_gallery.py --out build/before --src ../main-checkout/src

Why this exists: whether a status screen reads well cannot be told from a diff,
and the test suite can only assert that a screen *fits* and that a label is not
the colour of its value -- never that it is clear. The console's status screens
were walls of `Label: value` in one colour, some of them taller than the
terminal, and every test was green. So a change to a console screen comes with
pictures: this seeds a small node, types its way to each screen in `WALKS`, keeps
what the terminal would be showing at that moment -- everything since the last
clear, painted by the same emulator the netbbs.org screenshots use -- and flags
any screen with more rows than the terminal has.

`--src` points at another checkout's `src` directory, so the same walks can be
rendered against the code before a change and the two pages compared. A walk
that does not reach its screen there (a hotkey that does not exist yet) is
shown as whatever it did reach, labelled as such.

Redraw-in-place is switched on for the seeded SysOp: it is the default for a
new account, and it is the mode in which a too-tall screen loses its top and a
result printed just before a menu redraw is never seen.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import pathlib
import re
import sys
import tempfile
import webbrowser

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent

CLEAR = "\x1b[2J"
_SGR = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

#: name -> (keys typed from the SysOp landing page, the title the screen must show).
#: A picker takes a two-digit row number. `tests/test_sysop_console_presentation.py`
#: walks the same list, so a screen added here is a screen held to the terminal's size.
WALKS: dict[str, tuple[list[str], str]] = {
    "dashboard": ([], "SysOp operations console"),
    "link status": (["l"], "Link status"),
    "link status, page 2": (["l", "PAGE_DOWN"], "Link status"),
    "outbox": (["x"], "Outbox"),
    "backup": (["k"], "Backup"),
    "backup, page 2": (["k", "PAGE_DOWN"], "Backup"),
    "managed dns": (["d"], "Managed DNS"),
    "node": (["n"], "Node management"),
    "node > chat bridge": (["n", "c"], "Chat bridge (MRC)"),
    "users": (["u"], "Users"),
    "users > registration": (["u", "r"], "Registration"),
    "users > retired names": (["u", "t"], "Retired usernames"),
    "users > a user": (["u", "l", "0", "1"], "alice"),
    "users > a user > history": (["u", "l", "0", "4", "h"], "Admin actions"),
    "content > a board": (["c", "m", "l", "0", "1"], "General"),
    "content > a file area": (["c", "f", "l", "0", "1"], "Uploads"),
    "content > gc storage": (["c", "f", "g"], "GC storage"),
    "content > a door": (["c", "d", "l", "0", "1"], "Trivia"),
    "content > a channel": (["c", "n", "l", "0", "1"], "lobby"),
    "content > a community": (["c", "o", "l", "0", "1"], "Makers"),
    "operations": (["o"], "Operations"),
    "operations > prune drafts": (["o", "p"], "Prune drafts"),
    "operations > repair": (["o", "r"], "Repair carried posts"),
    "operations > diagnostics": (["o", "d"], "Diagnostic log"),
    "operations > an audit entry": (["o", "a", "0", "1"], "Audit entry"),
    "settings": (["s"], "Settings"),
    "settings > node name": (["s", "n"], "Node name"),
    "settings > join link": (["s", "j"], "Join NetBBS Link"),
    "settings > update": (["s", "u"], "Self-update"),
    "settings > welcome banner": (["s", "m", "n", "w"], "Welcome banner"),
    "settings > main menu masthead": (["s", "m", "m", "m"], "Main-menu masthead"),
    "trust > domains": (["s", "p", "d"], "Trust domains"),
    "trust > anchors": (["s", "p", "a"], "Trust anchors"),
    "trust > reporters": (["s", "p", "r"], "Trusted reporters"),
    "trust > identity authorities": (["s", "p", "i"], "Identity authorities"),
    "trust > published identity": (["s", "p", "p"], "Published identity"),
    "trust > vouches": (["s", "p", "v"], "Vouches"),
    "trust > exceptions": (["s", "p", "e"], "Sole-authority exceptions"),
    "trust > history": (["s", "p", "h"], "Trust configuration history"),
}


class ScriptExhausted(Exception):
    """The keys ran out: what is on the terminal now is the screen being photographed."""


def seed(db):
    """A node with one of everything a console screen reports on. Returns its SysOp."""
    from netbbs.auth.users import SYSOP_LEVEL, create_user
    from netbbs.boards.boards import create_board
    from netbbs.chat.channels import create_channel
    from netbbs.communities import create_community
    from netbbs.doors.registry import create_door
    from netbbs.files.areas import create_file_area
    from netbbs.net.redraw_preference import set_redraw_in_place_enabled

    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_redraw_in_place_enabled(db, sysop, True)
    for name in ("alice", "bob", "carol"):
        create_user(db, name, password="hunter2")
    create_board(
        db, "General", description="General discussion for everyone on the node " * 3,
        moderated=True, min_age=18, name_requirement="verified", creator=sysop,
    )
    create_file_area(db, "Uploads", description="Things callers sent in", creator=sysop)
    create_channel(db, "lobby", description="The front room", creator=sysop)
    create_community(db, "Makers", description="People who build things", creator=sysop)
    create_door(db, "Trivia", sys.executable, description="Quiz", creator=sysop)
    return sysop


def node_controls(root: pathlib.Path):
    from netbbs.net.maintenance import MaintenanceMode
    from netbbs.net.session_registry import ActiveSessionRegistry
    from netbbs.net.shutdown import NodeControls

    return NodeControls(
        session_registry=ActiveSessionRegistry(), maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(), graceful_delay_seconds=60.0, backup_identity_dir=root / "identity",
    )


def link_context():
    from netbbs.link.boards import LinkConfigSnapshot, LinkContext
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode

    identity = bootstrap_node_identity("roanoke")
    return LinkContext(
        node_identity=identity, link_node=LinkNode(identity=identity),
        link_config=LinkConfigSnapshot(
            outgoing_only=False, advertised_host="roanoke.example", advertised_port=7862,
            seeds=("http://seed.example:7862",), sync_interval_seconds=60.0, relay_serving_enabled=True,
            max_relay_clients=8, max_peers=64, max_carried_boards=32, max_carried_channels=32,
        ),
    )


def make_session(keys, *, width: int, height: int):
    """A `Session` that types `keys` and then stops the walk. Built here rather
    than at import so `--src` decides which checkout's `Session` it subclasses."""
    from netbbs.net.char_input import EditorKey, EditorKeyKind
    from netbbs.net.session import Session

    kinds = {kind.name: kind for kind in EditorKeyKind}

    class Walker(Session):
        def __init__(self):
            self._keys = list(keys)
            self.written: list[str] = []
            self.terminal_width, self.terminal_height = width, height
            self.node_display_name = "Roanoke"
            self.peer_address = None

        def _next(self) -> str:
            if not self._keys:
                raise ScriptExhausted()
            return self._keys.pop(0)

        async def write(self, text):
            self.written.append(text)

        async def read_line(self, echo=True, history=None, completer=None, **kwargs):
            return self._next()

        async def read_key(self, echo=True):
            return self._next()

        async def read_editor_key(self, *, distinguish_ctrl_h=False):
            raw = self._next()
            if raw in kinds and len(raw) > 1:
                return EditorKey(kinds[raw])
            return EditorKey(EditorKeyKind.CHAR, char=raw)

        async def close(self):
            pass

        async def read_byte(self):
            raise NotImplementedError

        async def write_raw(self, data):
            raise NotImplementedError

    return Walker()


def on_terminal(written: list[str]) -> str:
    """What the terminal holds: everything since the last clear."""
    text = "".join(written)
    return text[text.rfind(CLEAR):] if CLEAR in text else text


def capture(lane, sysop, root, keys, *, width: int, height: int) -> str:
    from netbbs.net.admin_flow import admin_menu

    session = make_session(keys, width=width, height=height)
    try:
        asyncio.run(admin_menu(session, lane, sysop, node_controls=node_controls(root), link_context=link_context()))
    except ScriptExhausted:
        pass
    return on_terminal(session.written)


PAGE = """<!doctype html><meta charset="utf-8"><title>NetBBS SysOp console -- {label}</title>
<style>
 body {{ background:#0b0e14; color:#c9d1d9; font:14px/1.35 system-ui,sans-serif; margin:24px; }}
 h1 {{ font-size:18px; }} h2 {{ font-size:14px; margin:28px 0 6px; color:#8b949e; font-weight:600; }}
 .bad {{ color:#ff7b72; }} .ok {{ color:#7ee787; }}
 pre.term-body {{ background:#000; padding:10px 12px; border:1px solid #30363d; border-radius:6px;
   display:inline-block; font:14px/1.2 "Cascadia Mono","DejaVu Sans Mono",Consolas,monospace; white-space:pre; }}
</style>
<h1>SysOp console at {width}x{height} -- {label}</h1>
<p>{count} screens, {overflowing} taller than the terminal.</p>
{panels}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "build" / "sysop-gallery")
    parser.add_argument("--src", type=pathlib.Path, default=ROOT / "src", help="the checkout's src/ to render")
    parser.add_argument("--width", type=int, default=80)
    parser.add_argument("--height", type=int, default=24)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    sys.path[:0] = [str(args.src.resolve()), str(SCRIPTS)]
    import website_ansi_to_html as term
    from netbbs.storage.database import Database
    from netbbs.storage.execution import DatabaseLane

    root = pathlib.Path(tempfile.mkdtemp(prefix="netbbs-sysop-gallery-"))
    database = Database(root / "node.db")
    sysop = seed(database)
    database.close()
    lane = DatabaseLane(root / "node.db")
    panels, overflowing = [], 0
    try:
        for name, (keys, title) in WALKS.items():
            raw = capture(lane, sysop, root, keys, width=args.width, height=args.height)
            rows = _SGR.sub("", raw).split("\r\n")
            reached = rows[0].endswith(title) if rows else False
            over = len(rows) > args.height
            overflowing += over
            # A screen taller than the terminal is painted on a canvas tall
            # enough to show all of it, so the rows that scrolled away are
            # visible in the picture rather than merely counted.
            body = term.render(raw, width=args.width, height=max(args.height, len(rows)))
            verdict = (
                f'<span class="bad">{len(rows)} rows -- {len(rows) - args.height} scroll off the top</span>'
                if over else f'<span class="ok">{len(rows)} rows</span>'
            )
            where = "" if reached else ' <span class="bad">(did not reach this screen)</span>'
            panels.append(
                f"<h2>{html.escape(name)} &middot; keys {html.escape(' '.join(keys) or '(none)')} "
                f"&middot; {verdict}{where}</h2>\n<pre class=\"term-body\">{body}</pre>"
            )
    finally:
        lane.close()

    args.out.mkdir(parents=True, exist_ok=True)
    page = args.out / "index.html"
    page.write_text(PAGE.format(
        label=html.escape(str(args.src)), width=args.width, height=args.height, count=len(panels),
        overflowing=overflowing, panels="\n".join(panels),
    ), encoding="utf-8")
    print(f"{len(panels)} screens, {overflowing} taller than {args.height} rows -> {page}")
    if args.open:
        webbrowser.open(page.as_uri())


if __name__ == "__main__":
    main()
