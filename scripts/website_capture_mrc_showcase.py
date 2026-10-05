"""Capture the MRC bridge's caller and SysOp screens for the MRC showcase page.

    PYTHONPATH=src python scripts/website_capture_mrc_showcase.py --list
    PYTHONPATH=src python scripts/website_capture_mrc_showcase.py rooms web/shots/raw-mrc-rooms.txt

The companion of `website_capture_chat_mrc.py`, which captures the bridged
chat itself: a real `MrcBridge` speaks the real MRC wire protocol to
`tests.mrc_fake_hub.FakeMrcHub` over loopback, and each screen is drawn by
the production code a caller or SysOp reaches -- the chat section's room
picker, a bridged channel's join and `/who`, and the SysOp console's bridge
status and settings. Like that script, it never touches the public MRC
network: the bridge is handed `loopback_only`.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from netbbs.auth.users import SYSOP_LEVEL, create_user                 # noqa: E402
from netbbs.chat.channels import create_channel                        # noqa: E402
from netbbs.chat.hub import ChatHub, ParticipantId                     # noqa: E402
from netbbs.chat.mailbox import MessageMailbox                         # noqa: E402
from netbbs.chat.presence import PresenceRegistry                      # noqa: E402
from netbbs.mrc.bridge import MrcBridge, MrcState                      # noqa: E402
from netbbs.mrc.protocol import DEFAULT_HOST                           # noqa: E402
from netbbs.mrc.settings import (                                      # noqa: E402
    DIRECTORY_TIMESTAMP_FORMAT, DirectoryEntry, MrcSettings, OpenRoomSettings,
    hub_identity, save_directory_snapshot, save_mrc_settings, save_open_room_settings, set_mrc_room,
)
from netbbs.net import chat_flow                                       # noqa: E402
from netbbs.net.char_input import InputHistory                         # noqa: E402
from netbbs.net.redraw_preference import set_redraw_in_place_enabled   # noqa: E402
from netbbs.storage.database import Database                           # noqa: E402
from netbbs.storage.execution import DatabaseLane                      # noqa: E402
from tests.mrc_fake_hub import FakeMrcHub                              # noqa: E402
from website_capture_chat_mrc import (                                 # noqa: E402
    NODE_NAME, CaptureSession, loopback_only, mrc, on_screen, pin_clock_to_the_evening, until,
)

CLEAR = "\x1b[2J"

#: Who the hub reports in each room, as (site, nick). The lobby is the room
#: the SysOp bridged; the rest are the network's other rooms.
SEATED = {
    "lobby": [("Vertigo", "jasper"), ("TheOuterRim", "marla"), ("Blackwater", "kite"),
              ("Amberlight", "nyx"), ("SilverLining", "otto")],
    "doors": [("Blackwater", "sable"), ("Pixelpit", "rook")],
    "retro": [("TheOuterRim", "wren"), ("Vertigo", "dex"), ("Pixelpit", "quill")],
}

#: The hub's last room listing, as `LIST` reported it.
DIRECTORY = [
    ("lobby", 14, "Welcome to MRC -- be excellent to each other"),
    ("doors", 4, "LORD, TradeWars, Global War: find an opponent"),
    ("retro", 6, "Vintage hardware, modems and the 8-bit years"),
    ("ansi", 3, "ANSI and ASCII art, show your latest"),
    ("sysops", 9, "Running a board? Ask here"),
    ("trivia", 2, "Daily quiz at 20:00 UTC"),
]


def listing() -> list[str]:
    """`DIRECTORY` the way the hub answers `LIST`: a header, one row per
    room, a footer."""
    rows = [f"*.:  #{room:<22} {users:>3}  {topic}" for room, users, topic in DIRECTORY]
    return ["*. __Rooms___________________Usr__Topic_______________________", *rows,
            "*.:__                             # = Normal  # = Locked"]


def on_terminal(session) -> str:
    """What the terminal holds: everything since the last clear."""
    text = "".join(session.written)
    return text[text.rfind(CLEAR):] if CLEAR in text else text


class Node:
    """A node with MRC on, one bridged channel and the bridge connected."""

    async def __aenter__(self) -> Node:
        # `async with` never calls `__aexit__` when `__aenter__` raises, so a
        # setup that fails part way (the bridge never connecting, say) closes
        # what it opened here, before the error leaves.
        self.db = self.lane = self.fake = self.bridge = None
        try:
            await self._setup()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def _setup(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="netbbs-mrc-shot-"))
        self.db = Database(self.tmp / "node.db")
        self.lane = DatabaseLane(self.db.path)
        self.fake = FakeMrcHub()
        self.sysop = create_user(self.db, "carrier", password="hunter2", user_level=SYSOP_LEVEL)
        self.alice = create_user(self.db, "alice", password="hunter2", user_level=10)
        create_user(self.db, "bob", password="hunter2", user_level=10)
        for user in (self.sysop, self.alice):
            set_redraw_in_place_enabled(self.db, user, True)
        self.lobby = create_channel(self.db, "lobby", description="The front room, bridged to MRC",
                                    creator=self.sysop)
        create_channel(self.db, "harbor-talk", description="Local only: news and notes from the node",
                       creator=self.sysop)
        create_channel(self.db, "games", description="Door games, high scores and trash talk",
                       creator=self.sysop)

        await self.fake.start()
        self.fake.banner = None
        answer = self.fake.reply_lines
        self.fake.reply_lines = lambda command, params: listing() if command == "LIST" else answer(command, params)
        for room, people in SEATED.items():
            for site, nick in people:
                self.fake.users[(site.lower(), nick.lower())] = room
        settings = MrcSettings(
            enabled=True, host=DEFAULT_HOST, port=5001, tls=True, site_name=NODE_NAME,
            info_sysop="Carrier", info_description="A small harbor town of a board",
            info_telnet="harborlights.example.net:2323", info_web="https://harborlights.example.net",
        )
        save_mrc_settings(self.db, settings)
        save_open_room_settings(self.db, OpenRoomSettings(enabled=True, min_level=10))
        set_mrc_room(self.db, self.lobby, "lobby")
        # Newest first is the picker's order: stamp the listing so it reads top-down.
        now = datetime.datetime.now(datetime.timezone.utc)
        save_directory_snapshot(self.db, hub_identity(settings), [
            DirectoryEntry(room, users, topic,
                           (now - datetime.timedelta(seconds=index)).strftime(DIRECTORY_TIMESTAMP_FORMAT))
            for index, (room, users, topic) in enumerate(DIRECTORY)
        ])

        self.hub = ChatHub()
        self.bridge = MrcBridge(hub=self.hub, lane=self.lane, version="7.15.3",
                                open_connection=loopback_only(self.fake.port), rng=random.Random(1),
                                min_backoff_seconds=0.05, max_backoff_seconds=0.2,
                                stable_after_seconds=0.0, per_user_interval_seconds=0.0)
        await self.bridge.start()
        await until(lambda: self.bridge.state is MrcState.CONNECTED, 5,
                    f"the bridge to connect ({self.bridge.status()})")

    async def __aexit__(self, *exc) -> None:
        if self.bridge is not None:
            await self.bridge.close()
        if self.fake is not None:
            await self.fake.close()
        if self.lane is not None:
            self.lane.close()
        if self.db is not None:
            self.db.close()

    async def seat_local(self, username: str, session_id: int) -> None:
        """A local caller already in the bridged lobby, announced to the hub."""
        self.hub.join(self.lobby.name, ParticipantId(username, session_id))
        await self.bridge.local_join(self.lobby, username)


async def snapshot_task(session, coroutine, expect: str, *, settle: float = 0.5) -> str:
    """Run a screen until `expect` is drawn, keep what the terminal shows, then stop it."""
    task = asyncio.create_task(coroutine)
    try:
        try:
            await until(lambda: expect in on_screen(session) or task.done(), 10, f"{expect!r} on screen")
        except TimeoutError as error:
            raise TimeoutError(f"{error}; the screen ends: {on_screen(session)[-400:]!r}") from None
        if task.done():
            task.result()
            # The screen returned on its own: `until` stopped on `task.done()`,
            # not on `expect`, so what is on the terminal may be another screen.
            if expect not in on_screen(session):
                raise RuntimeError(f"the screen returned before {expect!r} was drawn")
        await asyncio.sleep(settle)
        return on_terminal(session)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class WaitingSession(CaptureSession):
    """Like the chat capture's session, but also answers single keys from
    the queue, so pickers and console menus can be walked and then left
    waiting on the screen being photographed."""

    async def read_key(self, echo=True):
        return await self.inputs.get()

    async def read_editor_key(self, *, distinguish_ctrl_h=False):
        from netbbs.net.char_input import EditorKey, EditorKeyKind
        key = await self.inputs.get()
        return EditorKey(EditorKeyKind.CHAR, char=key)


async def shot_channels(node: Node) -> str:
    """The caller's chat section: local channels with the MRC section on top."""
    await node.seat_local("bob", 900)
    session = WaitingSession()
    return await snapshot_task(session, chat_flow.browse_channels(
        session, node.lane, node.hub, PresenceRegistry(), MessageMailbox(), InputHistory(), node.alice,
        mrc_bridge=node.bridge), "harbor-talk")


async def shot_rooms(node: Node) -> str:
    """Chat > Multi Relay Chat: rooms open here, and the network's rooms."""
    mapping = await node.bridge.open_room("retro", "bob")
    node.hub.join(mapping.channel.name, ParticipantId("bob", 900))
    await node.bridge.local_join(mapping.channel, "bob")
    await asyncio.sleep(1.0)
    session = WaitingSession()
    return await snapshot_task(session, chat_flow._pick_mrc_room(
        session, node.lane, node.hub, node.alice, node.bridge), "Open a room by name")


#: Lines in the lobby before `/who`; "> " marks what the caller types.
WHO_TRAFFIC = [
    mrc("jasper", "Vertigo", "evening, harbor lights"),
    mrc("nyx", "Amberlight", "waves from the other side of the network", action=True),
    "> hi all -- first night on MRC from here",
    mrc("marla", "TheOuterRim", "welcome aboard! what are you running?"),
    "> NetBBS. the bridge is built in, no extra daemon",
    mrc("otto", "SilverLining", "nice. is that the python one with the message network?"),
    "> that one. our local lobby is this room, so callers just walk in",
    mrc("kite", "Blackwater", "trivia in #trivia at 20:00 UTC tomorrow, bring snacks"),
]


async def shot_who(node: Node) -> str:
    """Joining the bridged lobby, a little traffic, then `/who`."""
    await node.seat_local("bob", 900)
    session = CaptureSession()
    task = asyncio.create_task(chat_flow._chat_loop(
        session, node.lane, node.hub, PresenceRegistry(), MessageMailbox(), InputHistory(),
        node.lobby, node.alice, mrc_bridge=node.bridge))
    try:
        await until(lambda: "is visible to everyone" in on_screen(session), 10, "the join notice")
        await asyncio.sleep(1.0)
        for line in WHO_TRAFFIC:
            if line.startswith("> "):
                session.inputs.put_nowait(line[2:])
                await asyncio.sleep(1.5)
                continue
            await node.fake.send_line(line)
            await asyncio.sleep(0.3)
        await until(lambda: "bring snacks" in on_screen(session), 10, "the MRC lines")
        await asyncio.sleep(1.5)
        session.inputs.put_nowait("/who")
        # Only the roster says "(on MRC)"; the chat lines above name the same
        # people, and the typed "/who" is never echoed to look for.
        await until(lambda: "(on MRC)" in on_screen(session), 10, "the /who roster")
        await asyncio.sleep(0.5)
        return "".join(session.written)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _console(node: Node, keys: list[str], expect: str) -> str:
    from sysop_gallery import link_context, node_controls

    from netbbs.net.admin_flow import admin_menu

    controls = dataclasses.replace(node_controls(node.tmp), mrc_bridge=node.bridge)
    session = WaitingSession()
    session.terminal_width, session.terminal_height = 80, 24
    for key in keys:
        session.inputs.put_nowait(key)
    return await snapshot_task(session, admin_menu(
        session, node.lane, node.sysop, node_controls=controls, link_context=link_context()), expect)


async def shot_status(node: Node) -> str:
    """SysOp console > Node > Chat bridge (MRC): the live link."""
    await node.seat_local("alice", 901)
    await node.seat_local("bob", 900)
    # The bridge asks STATS on its five-minute roster refresh; ask now, the
    # same request, rather than wait one out.
    node.bridge._request_stats(node.bridge.nick_for("alice"), "lobby")
    await until(lambda: node.bridge.status().network_bbses is not None, 10, "the hub's STATS reply")
    return await _console(node, ["n", "c"], "Site name")


async def shot_bridged(node: Node) -> str:
    """The same screen's second page: one row per bridged channel."""
    await node.seat_local("alice", 901)
    await node.seat_local("bob", 900)
    mapping = await node.bridge.open_room("retro", "carrier")
    node.hub.join(mapping.channel.name, ParticipantId("carrier", 902))
    await node.bridge.local_join(mapping.channel, "carrier")
    await asyncio.sleep(1.0)
    return await _console(node, ["n", "c", "n"], "Page 2 of 2")


async def shot_settings(node: Node) -> str:
    """SysOp console > Settings > Inter-BBS chat (MRC)."""
    return await _console(node, ["s", "i"], "Hub host")


SHOTS = {
    "channels": shot_channels,
    "rooms": shot_rooms,
    "who": shot_who,
    "status": shot_status,
    "bridged": shot_bridged,
    "settings": shot_settings,
}


async def run(name: str) -> str:
    async with Node() as node:
        return await SHOTS[name](node)


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
    pin_clock_to_the_evening()
    snapshot = asyncio.run(run(args.shot))
    # Bytes, not text: see the note in website_ansi_to_html.py.
    args.output.write_bytes(snapshot.encode("utf-8"))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
