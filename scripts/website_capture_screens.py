"""Capture the caller- and SysOp-facing NetBBS screens for the website gallery.

Nine of the twelve screens on www.netbbs.org had no way to be regenerated:
they came into the repo with the site itself (`e59fb7f6`), made before `web/`
was the source of truth, and nothing here could redraw them. So they aged
invisibly -- by v7.5.0 the files shot was showing a prose layout the product
replaced several releases ago, the chat shot had no timestamps and the old
prompt glyph, and every grey on both pages was the pre-v7.5.0 `MUTED_COLOR`.

Each screen below drives a **production render path** against a real
`Database`/`DatabaseLane` in a throwaway directory. Nothing is installed, no
door is launched, no socket is opened, and no network is touched.

    PYTHONPATH=src python scripts/website_capture_screens.py files out/raw-files.txt
    PYTHONPATH=src python scripts/website_ansi_to_html.py out/raw-files.txt shot.html

`--list` names every screen. See `website_capture_chat_mrc.py` for the same
shape applied to a screen that needs a fake MRC hub, and `web/README.md` for
the rules a capture has to obey once it is embedded.

Content is deliberately *worn*: a node with real posts, real uploads and a
real backlog, because a freshly seeded one draws every list at its least
informative. Same reasoning the door captures already follow.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

# `tests.*` (the scripted FakeSessions the flow tests use) lives at the repo
# root, which is not on sys.path for a script in scripts/.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netbbs.auth import users as users_module                   # noqa: E402
from netbbs.auth.users import SYSOP_LEVEL, create_user            # noqa: E402
from netbbs.boards.boards import create_board                     # noqa: E402
from netbbs.boards import posts as posts_module                   # noqa: E402
from netbbs.boards.posts import create_post                       # noqa: E402
from netbbs.files import entries as entries_module                # noqa: E402
from netbbs.files.areas import create_file_area                   # noqa: E402
from netbbs.files.entries import upload_file                      # noqa: E402
from netbbs.storage.database import Database                      # noqa: E402
from netbbs.storage.execution import DatabaseLane                 # noqa: E402

NODE_NAME = "Harbor Lights"

#: Fixed instants, so a capture does not change because the day did. Chosen
#: to be plausibly recent rather than round: a gallery of 00:00:00 timestamps
#: reads as a fixture, which is exactly what it is trying not to look like.
SEEDED_AT = [
    "2026-09-02T19:41:07.000000Z",
    "2026-09-04T08:16:52.000000Z",
    "2026-09-07T21:03:18.000000Z",
    "2026-09-09T17:28:44.000000Z",
    "2026-09-11T11:52:09.000000Z",
    "2026-09-12T20:14:33.000000Z",
]


class CaptureSession:
    """The screen a caller would see, collected instead of written to a socket.

    Deliberately not a subclass of any one test's `FakeSession`: the screens
    below live in four different flow modules whose test doubles differ, and
    a capture needs exactly one behaviour from all of them -- record what was
    written, answer the key that leaves.
    """

    supports_truecolor = True

    def __init__(self, keys=(), lines=(), width=80, height=24):
        # Keys are pressed *before* the screen is frozen, so a capture can
        # show a state a caller reaches rather than only the state a screen
        # opens in -- the file listing's cursor, for one, does not exist
        # until the first arrow press.
        self._keys = iter(keys)
        self._pending = len(keys)
        self._lines = iter(lines)
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = NODE_NAME
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"
        self.snapshot: str | None = None

    # -- the screen ----------------------------------------------------
    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def write_raw(self, data: bytes) -> None:  # pragma: no cover - unused
        raise NotImplementedError("a capture never writes raw bytes")

    def take(self) -> str:
        return self.snapshot if self.snapshot is not None else "".join(self.written)

    # -- input ---------------------------------------------------------
    def _leaving(self) -> bool:
        """Freeze the screen once the scripted keys are spent.

        Returns whether this read is the one that leaves. Without the
        snapshot the capture would end with the `b` the fake typed to get
        out, printed onto the prompt -- a detail that is true of the session
        and false of the screen.
        """
        if self._pending:
            self._pending -= 1
            # Everything drawn so far is a screen the caller has already
            # replaced. Keeping it would put two renders of the same screen
            # in one capture, stacked -- which is what a scrolling terminal
            # does and not what a gallery shot is.
            self.written.clear()
            return False
        if self.snapshot is None:
            self.snapshot = "".join(self.written)
        return True

    #: Set by a caller that wants the session to *wait* rather than answer --
    #: a live screen like chat, which has no "and then it returns" moment.
    inputs = None

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        if self.inputs is not None:
            return await self.inputs.get()
        if self._leaving():
            return "b"
        return next(self._lines, "b")

    async def read_key(self) -> str:
        if self._leaving():
            return "b"
        return next(self._keys, "b")

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        from netbbs.net.char_input import EditorKey, EditorKeyKind

        if self._leaving():
            return EditorKey(EditorKeyKind.CHAR, char="b")
        key = next(self._keys)
        return key if isinstance(key, EditorKey) else EditorKey(EditorKeyKind.CHAR, char=key)


def _node(tmp: Path) -> tuple[Database, DatabaseLane]:
    db = Database(tmp / "node.db")
    return db, DatabaseLane(db.path)


def _controls():
    """The live-node bundle screens need to draw their clock and status
    tags. Empty schedulers and a fresh registry: nothing here is draining
    or shutting down, which is what a gallery should show."""
    import asyncio as _asyncio

    from netbbs.net.session_registry import ActiveSessionRegistry
    from netbbs.net.shutdown import MaintenanceMode, NodeControls

    return NodeControls(
        session_registry=ActiveSessionRegistry(),
        maintenance=MaintenanceMode(),
        shutdown_event=_asyncio.Event(),
        graceful_delay_seconds=60.0,
    )


#: When the accounts were made. A node whose every member joined today
#: reads as a fixture; these are the dates a year-old node would show.
JOINED_AT = [
    "2025-11-04T18:22:41.000000Z",   # keeper, who started it
    "2026-01-17T20:09:12.000000Z",
    "2026-03-30T13:47:55.000000Z",
    "2026-08-21T09:15:03.000000Z",
]


def _people(db):
    """One SysOp and the regulars the gallery's copy already names."""
    stamps = iter(JOINED_AT)
    original = users_module.utc_now_iso
    users_module.utc_now_iso = lambda: next(stamps, JOINED_AT[-1])
    try:
        return {
            "keeper": create_user(db, "keeper", password="hunter2", user_level=SYSOP_LEVEL),
            "wren": create_user(db, "wren", password="hunter2", user_level=20),
            "otto": create_user(db, "otto", password="hunter2", user_level=20),
            "alice": create_user(db, "alice", password="hunter2", user_level=10),
        }
    finally:
        users_module.utc_now_iso = original


# -- files -------------------------------------------------------------


async def capture_files(tmp: Path) -> str:
    """The columnar file listing (`# / Filename / Size / Date / Uploader`).

    The shot this replaces showed a prose layout with `[74 B]` and
    `uploaded by` -- a screen NetBBS stopped drawing several releases ago.
    """
    from netbbs.net.file_flow import _show_area

    db, lane = _node(tmp)
    try:
        people = _people(db)
        area = create_file_area(db, "Utilities", creator=people["keeper"],
                                description="Node utilities and helper scripts")
        uploads = [
            ("backup-rotate.sh", people["otto"], b"#!/bin/sh\n# keep seven days of node backups\n" * 3,
             "Nightly backup rotation, seven days retained."),
            ("netbbs-7.5.0.tar.gz", people["keeper"], b"\x1f\x8b" + b"\x00" * 2_736_000,
             "The current release, source distribution."),
            ("ansi-preset-pack.zip", people["wren"], b"PK\x03\x04" + b"\x00" * 48_000,
             "Twelve masthead presets, 80 and 132 columns."),
            ("welcome.txt", people["keeper"], b"Welcome aboard. Read the rules, then say hello.\n",
             None),
        ]
        stamps = iter(SEEDED_AT)
        original = entries_module.utc_now_iso
        entries_module.utc_now_iso = lambda: next(stamps, SEEDED_AT[-1])
        try:
            for name, who, payload, description in uploads:
                upload_file(db, area, who, name, payload, description=description)
        finally:
            entries_module.utc_now_iso = original

        # One Down press, so the shot carries the cursor. It is a
        # reverse-video bar as of v7.5.0, and a listing drawn with no row
        # selected shows none of that -- which is how the highlight came to
        # be invisible for so long in the first place.
        from netbbs.net.char_input import EditorKey, EditorKeyKind

        session = CaptureSession(keys=(EditorKey(EditorKeyKind.DOWN),), width=88)
        await _show_area(session, lane, area, people["alice"])
        return session.take()
    finally:
        lane.close()
        db.close()


# -- message boards ----------------------------------------------------


async def capture_boards(tmp: Path) -> str:
    """A board's post list: the screen a caller reads a board from."""
    from netbbs.net.board_flow import _show_board

    db, lane = _node(tmp)
    try:
        people = _people(db)
        board = create_board(db, "Announcements", creator=people["keeper"],
                             description="Node news and release notes")
        posts = [
            (people["keeper"], "NetBBS 7.5.0 is up",
             "Guest login, a readable palette and a backup that no longer\n"
             "quietly skips Voidrunner careers. Notes are in the releases page."),
            (people["wren"], "Masthead preset pack",
             "Twelve presets in the file area, 80 and 132 columns both."),
            (people["otto"], "Re: NetBBS 7.5.0 is up",
             "The file listing finally reads as a table over ssh. Thank you."),
            (people["keeper"], "Link sync window moved to 04:00",
             "Quieter for everyone, and it stops colliding with the backup."),
        ]
        stamps = iter(SEEDED_AT)
        original = posts_module.utc_now_iso
        posts_module.utc_now_iso = lambda: next(stamps, SEEDED_AT[-1])
        try:
            for who, subject, body in posts:
                create_post(db, board, who, subject, body)
        finally:
            posts_module.utc_now_iso = original

        session = CaptureSession(width=88)
        await _show_board(session, db, board, people["alice"])
        return session.take()
    finally:
        lane.close()
        db.close()


# -- directory ---------------------------------------------------------


async def capture_directory(tmp: Path) -> str:
    """A caller's vcard, the finger-style detail view in the directory.

    Visibility is resolved by `get_vcard`, not by this script: the profile
    below is opted in, which is why it has anything to show.
    """
    from netbbs.directory import set_bio, set_bio_visible
    from netbbs.net.directory_flow import _show_vcard

    db, _lane = _node(tmp)
    _lane.close()
    try:
        people = _people(db)
        keeper = people["keeper"]
        set_bio(
            db, keeper,
            "Runs this node from a NetBSD box in Kiel. Wrote most of what you "
            "are looking at. Around most evenings; leave a message on "
            "Announcements and I will see it.",
        )
        set_bio_visible(db, keeper, True)

        session = CaptureSession(width=80)
        await _show_vcard(session, db, keeper, people["alice"])
        return session.take()
    finally:
        db.close()


# -- main menu ---------------------------------------------------------


async def capture_mainmenu(tmp: Path) -> str:
    """The main menu under a SysOp-chosen masthead.

    The window title on the site says `telnet harborlights.example.net` --
    that is the mock terminal's title bar, not the screen. The screen is
    this: a masthead, the three menu columns, and the clock the node
    controls prefix onto the prompt.
    """
    from netbbs.chat.mailbox import MessageMailbox
    from netbbs.net.banner_presets import MAIN_MENU_BANNER_PRESETS, load_main_menu_banner_preset
    from netbbs.net.main_menu import _draw_main_menu
    from netbbs.net.main_menu_banner import main_menu_banner_path, set_main_menu_banner_enabled

    db, _lane = _node(tmp)
    _lane.close()
    try:
        people = _people(db)
        preset = next(p for p in MAIN_MENU_BANNER_PRESETS if p.key == "outrun_sunset_strip")
        main_menu_banner_path(db).write_bytes(load_main_menu_banner_preset(preset))
        set_main_menu_banner_enabled(db, True)

        session = CaptureSession(width=80)
        await _draw_main_menu(session, db, MessageMailbox(), people["keeper"],
                              node_controls=_controls())
        return session.take()
    finally:
        db.close()


# -- chat --------------------------------------------------------------


async def capture_chat(tmp: Path) -> str:
    """A live channel with scrollback, presence and the pinned status line.

    The shot this replaces predates two changes that are the point of
    looking at it: timestamps are on by default now, and the prompt glyph
    is one a legacy Windows font actually has.
    """
    from netbbs.chat.channels import create_channel, set_topic
    from netbbs.chat.hub import ChatHub
    from netbbs.chat.mailbox import MessageMailbox
    from netbbs.chat.presence import PresenceRegistry
    from netbbs.chat import scrollback as scrollback_module
    from netbbs.chat.scrollback import record_message
    from netbbs.net import chat_flow
    from netbbs.net.char_input import InputHistory

    db, lane = _node(tmp)
    try:
        people = _people(db)
        channel = create_channel(db, "lobby", creator=people["keeper"],
                                 description="General chat")
        channel = set_topic(db, channel, "release day: 7.5.0", set_by=people["keeper"])
        said = [
            ("join", "wren", None),
            ("message", "wren", "evening -- did the 7.5.0 wheel land yet?"),
            ("join", "otto", None),
            ("message", "keeper", "it did. the file listing is a real table now"),
            ("message", "otto", "just pulled it. the cursor is finally visible over ssh"),
            ("message", "wren", "timestamps on by default is the one I wanted"),
            ("message", "keeper", "that one changes for everybody, so it is in the notes"),
            ("action", "otto", "reads the notes properly this time"),
            ("message", "wren", "anyone else on the link tonight? quiet over here"),
            ("message", "keeper", "two peers up. sync window moved to 04:00 last week"),
        ]
        # One evening, in order. The shared `SEEDED_AT` dates are days apart,
        # and chat shows only the *time* -- so they read as a conversation
        # whose timestamps run backwards.
        stamps = iter([
            f"2026-09-12T{t}.000000Z" for t in
            ("19:58:11", "19:58:40", "20:01:02", "20:01:37",
             "20:02:14", "20:03:05", "20:03:51", "20:04:22",
             "20:06:09", "20:06:48")
        ])
        original = scrollback_module.utc_now_iso
        scrollback_module.utc_now_iso = lambda: next(stamps, SEEDED_AT[-1])
        try:
            for kind, who, body in said:
                record_message(db, channel, kind=kind, author_label=who, body=body)
        finally:
            scrollback_module.utc_now_iso = original

        presence = PresenceRegistry()
        for name in ("keeper", "wren", "otto"):
            presence.enter(name)

        # Run as a task and cancel it, the way `website_capture_chat_mrc.py`
        # does: `_chat_loop` is a live screen with background receive and
        # clock tasks, so it does not "finish" the way a menu render does.
        # Its `read_line` waits on a queue nothing is ever put into, which
        # is what leaves the caller sitting in the channel rather than
        # typing their way back out of it.
        hub = ChatHub()
        session = CaptureSession(width=80, height=24)
        session.inputs = asyncio.Queue()
        task = asyncio.create_task(chat_flow._chat_loop(
            session, lane, hub, presence, MessageMailbox(), InputHistory(),
            channel, people["keeper"],
        ))
        try:
            for _ in range(200):                       # ~5s, checked 40x a second
                if hub.participant_count(channel.name) > 0 and session.written:
                    break
                await asyncio.sleep(0.025)
            else:
                raise RuntimeError("the caller never joined the channel")
            await asyncio.sleep(0.4)                   # let the scrollback replay land
            return "".join(session.written)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        lane.close()
        db.close()


# -- who's online ------------------------------------------------------


async def capture_who(tmp: Path) -> str:
    """The caller-facing Who screen: who else is connected, right now."""
    from netbbs.chat.hub import ChatHub
    from netbbs.chat.presence import PresenceRegistry
    from netbbs.net.directory_flow import _caller_who_screen

    db, lane = _node(tmp)
    try:
        people = _people(db)
        controls = _controls()
        presence = PresenceRegistry()
        for session_of in ("wren", "otto"):
            registered = CaptureSession(width=80)
            registered.username = session_of
            controls.session_registry.enter(registered)
            controls.session_registry.mark_authenticated(registered, session_of)
            presence.enter(session_of)
        presence.enter("keeper")
        presence.set_away("otto", "back in ten")

        session = CaptureSession(width=80)
        await _caller_who_screen(session, db, controls, people["keeper"],
                                 ChatHub(), presence, None, lane)
        return session.take()
    finally:
        lane.close()
        db.close()


# -- SysOp console -----------------------------------------------------


async def capture_console(tmp: Path) -> str:
    """The SysOp landing page: an operations overview, not a link list."""
    from netbbs.chat.channels import create_channel
    from netbbs.net.admin_flow import _draw_admin_menu

    db, lane = _node(tmp)
    try:
        people = _people(db)
        for name, description in (
            ("Announcements", "Node news and release notes"),
            ("Support", "Ask here when something breaks"),
            ("Off topic", "Everything else"),
        ):
            board = create_board(db, name, creator=people["keeper"], description=description)
            if name == "Announcements":
                create_post(db, board, people["keeper"], "NetBBS 7.5.0 is up",
                            "Notes are on the releases page.")
        create_file_area(db, "Utilities", creator=people["keeper"],
                         description="Node utilities and helper scripts")
        create_channel(db, "lobby", creator=people["keeper"], description="General chat")
        # Something for the attention queue to be attending to. A console
        # whose every counter reads zero shows the panel but not the point
        # of it -- and the caption beside this shot says the queue is
        # actually waiting on someone, which has to be true.
        create_user(db, "newcomer", password="hunter2", user_level=10,
                    pending_approval=True)

        session = CaptureSession(width=80, height=40)
        await _draw_admin_menu(session, lane, people["keeper"],
                               node_controls=_controls(), link_context=None)
        return session.take()
    finally:
        lane.close()
        db.close()


# -- Settings > Node colors --------------------------------------------


async def capture_colors(tmp: Path) -> str:
    """The one screen whose subject is colour, which makes it the one
    screen where a stale capture is most obviously stale."""
    from netbbs.net.admin_flow import _theme_colors_menu
    from netbbs.net.node_theme import set_accent_color_override

    db, lane = _node(tmp)
    try:
        people = _people(db)
        # A node that has actually been branded. Three rows reading
        # "default" demonstrate the screen exists; they do not show what it
        # is for, and the live preview above the fields is the whole point
        # of the draft editor.
        set_accent_color_override(db, (255, 140, 60))
        session = CaptureSession(width=80)
        await _theme_colors_menu(session, lane, people["keeper"])
        return session.take()
    finally:
        lane.close()
        db.close()


# -- welcome banner ----------------------------------------------------


async def capture_login(tmp: Path) -> str:
    """What a caller sees on connecting: the SysOp's welcome banner and
    the login prompt underneath it.

    Rendered through `write_preformatted_line` exactly as `login_flow`
    does, because a banner is operator-authored art and how it reaches the
    wire is part of whether it survives.
    """
    from netbbs.net.nodeconfig import ThrottleConfig
    from netbbs.net.throttle import LoginThrottle
    from netbbs.net.banner_presets import WELCOME_BANNER_PRESETS, load_welcome_banner_preset
    from netbbs.net.login_flow import _login
    from netbbs.net.session import write_preformatted_line
    from netbbs.net.welcome_banner import (
        banner_path,
        load_welcome_banner,
        set_welcome_banner_enabled,
    )

    db, _lane = _node(tmp)
    _lane.close()
    try:
        _people(db)
        # A preset whose art is unambiguously art. `cyberpunk_sunset_gold`
        # -- the one this shot used to carry -- bakes `NODE: Megacity-Prime`,
        # `PEERS: 18 Active Nodes` and `UPTIME: 100 Days+` into the `.ans`.
        # Nothing fills those in, so as a *screenshot* they claim NetBBS
        # renders live telemetry on the login screen, which it does not, and
        # they name a node no other shot in the gallery mentions. This one
        # carries no field/value rows at all, and no U+276F either -- three
        # shipped presets use that Dingbats glyph, which is the character
        # v7.5.0 removed from the chat prompt for rendering as a hollow box
        # in the fonts a Windows terminal reaches for.
        preset = next(p for p in WELCOME_BANNER_PRESETS if p.key == "cathedral_of_signals")
        banner_path(db).write_bytes(load_welcome_banner_preset(preset))
        set_welcome_banner_enabled(db, True)

        session = CaptureSession(width=80)
        await write_preformatted_line(
            session, load_welcome_banner(db, truecolor=session.supports_truecolor)
        )
        # `_login` itself draws the rest: the `Sign in` title, the
        # registration subtitle, and the prompt. Nothing here retypes any of
        # it -- an earlier version of this function hand-wrote
        # `"Username (or [N]ew): "`, which invented a hotkey NetBBS has no
        # such thing as (the sentinel is the whole word `new`) and shipped
        # it to the website as a screenshot. A capture that composes its own
        # text is not a capture.
        limits = ThrottleConfig()
        throttle = LoginThrottle(
            per_source_capacity=limits.per_source_capacity,
            per_source_refill_per_minute=limits.per_source_refill_per_minute,
            per_username_capacity=limits.per_username_capacity,
            per_username_refill_per_minute=limits.per_username_refill_per_minute,
            global_capacity=limits.global_capacity,
            global_refill_per_minute=limits.global_refill_per_minute,
            max_tracked_keys=limits.max_tracked_keys,
            max_concurrent_unauthenticated_sessions=limits.max_concurrent_unauthenticated_sessions,
        )
        await _login(session, db, throttle, max_attempts=1, idle_timeout=5.0)
        return session.take()
    finally:
        db.close()


SCREENS = {
    "boards": capture_boards,
    "chat": capture_chat,
    "colors": capture_colors,
    "console": capture_console,
    "directory": capture_directory,
    "files": capture_files,
    "login": capture_login,
    "mainmenu": capture_mainmenu,
    "who": capture_who,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("screen", nargs="?", help="which screen to capture")
    parser.add_argument("output", nargs="?", type=Path, help="raw ANSI capture to write")
    parser.add_argument("--list", action="store_true", help="name every screen and exit")
    args = parser.parse_args()

    if args.list or not args.screen:
        for name in sorted(SCREENS):
            print(name)
        return
    if args.screen not in SCREENS:
        raise SystemExit(f"unknown screen {args.screen!r}; --list names them all")
    if args.output is None:
        raise SystemExit("an output path is required")

    tmp = Path(tempfile.mkdtemp(prefix="netbbs-shot-"))
    screen = asyncio.run(SCREENS[args.screen](tmp))
    if not screen.strip():
        raise SystemExit(f"{args.screen} drew nothing")
    # Bytes, not text: see the note in website_ansi_to_html.py.
    args.output.write_bytes(screen.encode("utf-8"))
    print(f"wrote {args.output} ({len(screen)} chars)")


if __name__ == "__main__":
    main()
