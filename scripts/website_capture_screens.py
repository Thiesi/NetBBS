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
import atexit
import shutil
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
            ("netbbs-7.14.0.tar.gz", people["keeper"], b"\x1f\x8b" + b"\x00" * 3_954_000,
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
        session.location = ("Files",)  # where the walk came from
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
            (people["keeper"], "NetBBS 7.14.0 is up",
             "Guest login, a readable palette and a backup that no longer\n"
             "quietly skips Voidrunner careers. Notes are in the releases page."),
            (people["wren"], "Masthead preset pack",
             "Twelve presets in the file area, 80 and 132 columns both."),
            (people["otto"], "Re: NetBBS 7.14.0 is up",
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
        session.location = ("Message boards",)  # where the walk came from
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
        session.location = ("Directory",)  # where the walk came from
        await _show_vcard(session, db, keeper, people["alice"])
        return session.take()
    finally:
        db.close()


# -- main menu ---------------------------------------------------------


async def capture_mainmenu(tmp: Path) -> str:
    """The main menu as Harbor Lights' own art.

    The node's SysOp has drawn the whole menu (`website_harbor_lights_menu.py`)
    and switched the masthead's mode to *the menu itself*: NetBBS fills its
    slots with keeper's name, level, mail, the callers online and the node's
    clock, and puts keeper's items it doesn't draw into the `{menu}` row.

    The window title on the site says `telnet harborlights.example.net` --
    that is the mock terminal's title bar, not the screen.
    """
    from website_harbor_lights_menu import art

    from netbbs.chat.mailbox import MessageMailbox
    from netbbs.communities import create_community
    from netbbs.config import set_node_display_name
    from netbbs.net import main_menu as main_menu_module
    from netbbs.net.main_menu import _draw_main_menu
    from netbbs.net.main_menu_banner import (
        main_menu_banner_path, set_main_menu_art_mode, set_main_menu_banner_enabled,
    )

    db, _lane = _node(tmp)
    _lane.close()
    try:
        set_node_display_name(db, NODE_NAME)
        people = _people(db)
        main_menu_banner_path(db).write_bytes(art())
        set_main_menu_banner_enabled(db, True)
        set_main_menu_art_mode(db, "slots")
        # A node with content: an empty one tells its SysOp where to start.
        create_board(db, "Announcements", creator=people["keeper"])
        # A Community gives keeper [T]opics, an item the art doesn't draw:
        # NetBBS puts it into the art's {menu} row.
        create_community(db, "Makers", description="Hardware, solder and smoke",
                         creator=people["keeper"])
        controls = _controls()
        for name in ("keeper", "wren", "otto"):
            registered = CaptureSession(width=80)
            registered.username = name
            controls.session_registry.enter(registered)
            controls.session_registry.mark_authenticated(registered, name)

        # The node's clock -- the art's date and time, the prompt's -- reads
        # just before keeper walks into the chat capture's channel.
        original_clock = main_menu_module.utc_now_iso
        main_menu_module.utc_now_iso = lambda: "2026-10-12T20:11:37.000000Z"
        try:
            session = CaptureSession(width=80, height=24)
            await _draw_main_menu(session, db, MessageMailbox(), people["keeper"],
                                  node_controls=controls)
        finally:
            main_menu_module.utc_now_iso = original_clock
        return session.take()
    finally:
        db.close()


# -- chat --------------------------------------------------------------


async def capture_chat(tmp: Path) -> str:
    """A live channel with scrollback, presence, the pinned status line -- and
    Baud, the bundled chat bot, taking part.

    Baud runs for real: its own process under `BotRuntime`, answering what
    the caller types into the channel. It greets the caller on joining,
    answers `top` with a door's top ten as a titled block, answers
    `seen` privately ("(to you)", asked with `/msg`), and answers `stats`.
    The channel's clock is pinned to one evening, and each live line is
    stamped with its own minute, so a recapture shows the same times
    whenever it is run. A reminder is left out: Baud's confirmation now
    names the time it falls due, read from Baud's own clock, which the
    capture cannot pin.
    """
    import datetime
    import json
    import re

    from netbbs.bots.registry import save_bot
    from netbbs.bots.runtime import BotRuntime
    from netbbs.chat.channels import create_channel, set_topic
    from netbbs.chat.hub import ChatHub, ParticipantId
    from netbbs.chat.mailbox import MessageMailbox
    from netbbs.chat.presence import PresenceRegistry
    from netbbs.chat import scrollback as scrollback_module
    from netbbs.chat.scrollback import record_message
    from netbbs.config import set_node_display_name
    from netbbs import session_history as history_module
    from netbbs.session_history import record_session_end, record_session_start
    from netbbs.net import chat_flow
    from netbbs.net.char_input import InputHistory

    from netbbs.bots import runtime as runtime_module
    from netbbs.doors import create_door
    from netbbs.doors.events import set_events_enabled
    from netbbs.doors.outbound import OUTBOUND_DIRNAME, drain, enable_outbound

    evening = datetime.datetime(2026, 10, 12, 20, 12, tzinfo=datetime.timezone.utc)

    def ago(minutes: float) -> str:
        return (evening - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    db, lane = _node(tmp)
    try:
        set_node_display_name(db, NODE_NAME)
        people = _people(db)
        channel = create_channel(db, "lobby", creator=people["keeper"],
                                 description="General chat")
        channel = set_topic(db, channel, "release day: 7.31.0", set_by=people["keeper"])

        # A third-party door reports its top ten through the door outbound
        # hook, the way a door with game events on does (door API 5):
        # `!top lunar lander` answers it as a titled block. Not a bundled
        # game: those do not report game events yet, and the capture must
        # not show one doing so.
        workdir = tmp / "lunar_lander"
        workdir.mkdir()
        door = create_door(db, "Lunar Lander", sys.executable, creator=people["keeper"])
        enable_outbound(db, door, enabled_by=people["keeper"])
        set_events_enabled(db, door, True, changed_by=people["keeper"])
        (workdir / OUTBOUND_DIRNAME).mkdir()
        (workdir / OUTBOUND_DIRNAME / "req0000001.json").write_text(json.dumps({"standings": {
            "title": "Top pilots",
            "rows": [{"user_id": people[name].id, "value": value, "label": label}
                     for name, value, label in (("wren", "9,840 pts", "14 soft landings"),
                                                ("otto", "9,215 pts", "12 soft landings"),
                                                ("keeper", "7,060 pts", "9 soft landings"),
                                                ("alice", "4,380 pts", "5 soft landings"))]}}), encoding="utf-8")
        drain(db, door, workdir)

        # alice called earlier this evening and has gone: what `seen` reports.
        stamps = iter([ago(95), ago(52)])
        original = history_module.utc_now_iso
        history_module.utc_now_iso = lambda: next(stamps)
        try:
            record_session_end(db, record_session_start(db, people["alice"]))
        finally:
            history_module.utc_now_iso = original

        said = [
            ("join", "wren", None, 14),
            ("message", "wren", "evening -- did the 7.31.0 wheel land yet?", 13.5),
            ("join", "otto", None, 12),
            ("message", "otto", "just pulled it. Baud can roll dice and run polls now", 11),
            ("message", "wren", "and /sysop pages the SysOp from any chat channel", 10),
            ("action", "otto", "goes looking for alice's new ANSI upload", 8),
        ]
        stamps = iter([ago(minutes) for *_rest, minutes in said])
        original = scrollback_module.utc_now_iso
        scrollback_module.utc_now_iso = lambda: next(stamps)
        try:
            for kind, who, body, _minutes in said:
                record_message(db, channel, kind=kind, author_label=who, body=body)
        finally:
            scrollback_module.utc_now_iso = original

        save_bot(db, actor=people["keeper"], name="Baud", channels=["lobby"], defaults=["lobby"],
                 enabled=True, queries=["seen", "stats", "top"], event_channels=["lobby"], greetings=True)

        presence = PresenceRegistry()
        for name in ("keeper", "wren", "otto"):
            presence.enter(name)

        # From here on every live line, Baud's included, is stamped at
        # `minute[0]` past `evening`, set before each line is said.
        minute = [2.0]
        original_clock = scrollback_module.utc_now_iso
        scrollback_module.utc_now_iso = lambda: ago(-minute[0])
        # A private answer is no channel message: the bot host stamps it.
        original_runtime_clock = runtime_module.utc_now_iso
        runtime_module.utc_now_iso = lambda: ago(-minute[0])
        hub = ChatHub()
        # wren and otto are in the channel too, and answer live: real
        # traffic is also what scrolls the join's blank block off the screen.
        others = {name: ParticipantId(name, 1) for name in ("wren", "otto")}
        for pid in others.values():
            hub.join(channel.name, pid)
        runtime = BotRuntime(lane, hub, presence=presence, poll_seconds=0.05)
        runtime.start()
        # A node up since the afternoon, not one booted for the photograph.
        runtime.started_at -= 4 * 3600 + 17 * 60
        session = CaptureSession(width=80, height=24)
        session.inputs = asyncio.Queue()
        task = None
        try:
            async def until(predicate, what: str, seconds: float = 10.0) -> None:
                for _ in range(int(seconds * 40)):
                    if predicate():
                        return
                    await asyncio.sleep(0.025)
                raise RuntimeError(f"timed out waiting for {what}")

            def shown(needle: str) -> bool:
                # As the caller reads it: colour codes can split a phrase.
                return needle in re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", "".join(session.written))

            await until(lambda: runtime.running_ids, "Baud to start")
            # Run as a task and cancel it, the way `website_capture_chat_mrc.py`
            # does: `_chat_loop` is a live screen with background receive and
            # clock tasks, so it does not "finish" the way a menu render does.
            session.location = ("Chat",)  # where the walk came from
            task = asyncio.create_task(chat_flow._chat_loop(
                session, lane, hub, presence, MessageMailbox(), InputHistory(),
                channel, people["keeper"],
            ))
            # wren, otto and Baud are already in the channel; only the
            # caller's own join notice says the caller is.
            await until(lambda: shown("Joined #lobby"), "the caller to join the channel")
            await until(lambda: shown("Welcome, keeper!"), "Baud's greeting")
            async def say(at: float, who: str, body: str) -> None:
                minute[0] = at
                recorded = record_message(db, channel, kind="message", author_label=who, body=body)
                await hub.broadcast(channel.name, recorded, exclude={others[who]})
                await until(lambda: shown(body), f"{who}'s line")

            async def type_(at: float, line: str, answer: str) -> None:
                """keeper types `line`; wait until `answer` is on screen."""
                minute[0] = at
                await asyncio.sleep(0.8)               # a person types; no burst
                await session.inputs.put(line)
                await until(lambda: shown(answer), f"the answer to {line!r}")

            await type_(3, "evening all. 7.31.0 is out", "7.31.0 is out")
            await say(4, "wren", "saw it! Baud keeps the lunar lander top ten now")
            await type_(5, "!top lunar lander", "Top pilots")
            await say(6, "otto", "second again. one more landing tonight")
            await say(7, "wren", "hi keeper! ask Baud about alice, she was on earlier")
            # Asked privately: the answer is drawn "(to you)", for keeper alone.
            await type_(8, "/msg Baud.bot seen alice", "last recorded")
            await type_(9, "missed her by an hour. and Baud told only me", "told only me")
            await type_(11, "!stats", "human accounts online")
            await say(12, "wren", "and if the wheel ever breaks, /sysop pages you now")
            await type_(13, "good. right, off to beat otto's score", "beat otto's score")
            await say(14, "otto", "o/")
            await asyncio.sleep(0.4)                   # let the status line settle
            return "".join(session.written)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runtime.close()
            for pid in others.values():
                hub.leave(channel.name, pid)
            scrollback_module.utc_now_iso = original_clock
            runtime_module.utc_now_iso = original_runtime_clock
    finally:
        lane.close()
        db.close()


# -- who's online ------------------------------------------------------


async def capture_who(tmp: Path) -> str:
    """The caller-facing Who screen: who else is connected, right now.

    Each caller's "connected since" is the evening the chat capture is set
    in -- wren at 19:58, otto at 20:00, when they join #lobby there -- not
    the moment the script ran, which drew both at the same minute."""
    from netbbs.chat.hub import ChatHub
    from netbbs.chat.presence import PresenceRegistry
    from netbbs.net.directory_flow import _caller_who_screen

    db, lane = _node(tmp)
    try:
        people = _people(db)
        controls = _controls()
        presence = PresenceRegistry()
        connected = {"wren": "2026-10-12T19:58:07.000000Z", "otto": "2026-10-12T20:00:41.000000Z"}
        for session_of, since in connected.items():
            registered = CaptureSession(width=80)
            registered.username = session_of
            controls.session_registry.enter(registered)
            controls.session_registry.mark_authenticated(registered, session_of)
            # The registry stamps an entry when it is made, with no way to
            # pass a time in: set it on the entry, as a real login would have.
            controls.session_registry._sessions[registered].connected_at = since
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
                create_post(db, board, people["keeper"], "NetBBS 7.14.0 is up",
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
    """The one screen whose subject is color, which makes it the one
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
        session.location = ("SysOp", "Settings")  # where the walk came from
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
        # carries no field/value rows at all.
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


async def capture_users(tmp: Path) -> str:
    """The Users sub-console: where v5.4.0's console work actually shows.

    The console *landing* page deliberately shows bare counts -- active
    sessions and the moderation queue have no configured capacity, so a
    `max(10, count)` denominator read as "100% full" past ten and both
    gauges were dropped (v5.4.0 release notes). The ratios that do have a
    real denominator live one level in, framed, which is the half of that
    release the gallery was never showing.
    """
    from netbbs.auth.users import get_user_by_username, set_user_disabled
    from netbbs.net.admin_flow import _users_menu

    db, lane = _node(tmp)
    try:
        people = _people(db)
        # A node with a real spread of accounts: the active ratio is the
        # gauge, so it has to be a ratio worth drawing.
        for name in ("mox", "pell", "ondra", "juno", "sable", "rask"):
            create_user(db, name, password="hunter2", user_level=10)
        create_user(db, "newcomer", password="hunter2", user_level=10, pending_approval=True)
        set_user_disabled(db, get_user_by_username(db, "rask"), True, changed_by=people["keeper"])

        session = CaptureSession(width=80, height=30)
        session.location = ("SysOp",)  # where the walk came from
        await _users_menu(session, lane, people["keeper"], node_controls=_controls())
        return session.take()
    finally:
        lane.close()
        db.close()


SCREENS = {
    "boards": capture_boards,
    "chat": capture_chat,
    "colors": capture_colors,
    "console": capture_console,
    "directory": capture_directory,
    "users": capture_users,
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
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    screen = asyncio.run(SCREENS[args.screen](tmp))
    if not screen.strip():
        raise SystemExit(f"{args.screen} drew nothing")
    # Bytes, not text: see the note in website_ansi_to_html.py.
    args.output.write_bytes(screen.encode("utf-8"))
    print(f"wrote {args.output} ({len(screen)} chars)")


if __name__ == "__main__":
    main()
