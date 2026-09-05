"""Capture a real MRC-bridged chat channel for the website gallery.

Drives the production `chat_flow._chat_loop` with a real `MrcBridge` speaking
the real tilde-delimited MRC wire protocol to `tests.mrc_fake_hub.FakeMrcHub`
over loopback. Every line on the resulting screen crossed the actual bridge:
inbound lines arrive as MRC packets and are rendered by NetBBS's own code.

    PYTHONPATH=src python scripts/website_capture_chat_mrc.py raw-mrc.txt
    PYTHONPATH=src python scripts/website_ansi_to_html.py raw-mrc.txt shot.html

**This never touches the public MRC network.** See `loopback_only` below for
why that takes deliberate effort rather than being the default.

Framing note: joining a channel emits a fixed block of blank rows after the
bridge notice, and the pinned status line always sits on rows 22-24. Playing
the whole exchange scrolls the preamble and that blank block off the top, so
the capture is a screen full of conversation rather than one with a gap in
the middle. `--exchanges` tunes that; fewer leaves the join notice visible.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import tempfile
from pathlib import Path

# `tests.*` (the fake hub, and the scripted FakeSession the chat tests use)
# lives at the repo root, which is not on sys.path for a script in scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netbbs.auth.users import create_user                       # noqa: E402
from netbbs.chat.channels import create_channel                 # noqa: E402
from netbbs.chat.hub import ChatHub                             # noqa: E402
from netbbs.chat.mailbox import MessageMailbox                  # noqa: E402
from netbbs.chat.presence import PresenceRegistry               # noqa: E402
from netbbs.mrc.bridge import MrcBridge, MrcState               # noqa: E402
from netbbs.mrc.protocol import DEFAULT_HOST                    # noqa: E402
from netbbs.mrc.settings import (                               # noqa: E402
    MrcSettings, save_mrc_settings, set_mrc_room,
)
from netbbs.net import chat_flow                                # noqa: E402
from netbbs.net.char_input import InputHistory                  # noqa: E402
from netbbs.storage.database import Database                    # noqa: E402
from netbbs.storage.execution import DatabaseLane               # noqa: E402
from tests.mrc_fake_hub import FakeMrcHub                       # noqa: E402
from tests.test_chat_flow_moderation import FakeSession         # noqa: E402

NODE_NAME = "Harbor Lights"


def loopback_only(port: int):
    """The connector handed to `MrcBridge`, refusing anything but the hub name.

    The node is configured exactly as a real one is (`DEFAULT_HOST`), and this
    resolves that name to the local stand-in hub the way a hosts-file entry
    would, so the wire traffic and every rendered line stay the production
    bridge's.

    Pass this explicitly. `MrcBridge` binds `asyncio.open_connection` as a
    **default argument at import time**, so monkeypatching the asyncio module
    afterwards does not reach it and the bridge dials the real public hub --
    which announces a fake caller to every board on the network.
    """
    async def open_connection(host=None, hub_port=None, **kwargs):
        if host != DEFAULT_HOST:
            raise AssertionError(f"refusing to dial {host!r}: capture is loopback-only")
        kwargs.pop("ssl", None)
        kwargs.pop("server_hostname", None)
        return await asyncio.open_connection("127.0.0.1", port, **kwargs)

    return open_connection


class CaptureSession(FakeSession):
    """Truecolor, named, and fed its typed lines through a queue so the fake
    hub can push traffic while the caller is already in the channel."""

    supports_truecolor = True
    node_display_name = NODE_NAME

    def __init__(self):
        super().__init__([])
        self.inputs: asyncio.Queue[str] = asyncio.Queue()

    async def read_line(self, echo=True, history=None, completer=None, *,
                        live_buffer=None, lock=None, list_candidates=None):
        return await self.inputs.get()


def mrc(nick: str, site: str, body: str, action: bool = False) -> str:
    """One MRC room line on the wire:
    `nick~site~room~to_user~to_site~to_room~body~`, the body carrying the
    sender's own coloured handle the way every MRC client writes it."""
    coloured = (f"|15* |13{nick} {body}" if action
                else f"|03<|11{nick}|03>|16|07 {body}")
    return f"{nick}~{site}~lobby~~~lobby~{coloured}~"


# Said before the caller joins, so it is real scrollback the join replays.
EARLIER = [
    mrc("jasper", "Vertigo", "evening all -- who else is on tonight?"),
    mrc("marla", "TheOuterRim", "finally got the new modem working. 33.6, at last"),
    mrc("jasper", "Vertigo", "raises a mug", action=True),
]

# ... and while the caller is in the room. A `None` inbound is a line the
# caller types, so the screen shows traffic crossing the bridge both ways.
LIVE = [
    (mrc("kite", "Blackwater", "anyone here running LORD? I need someone to fight"), None),
    (None, "kite: we have it -- LORD 4.07 under DOSBox-X. bring a sword"),
    (mrc("marla", "TheOuterRim", "kite: half the boards on here have it by now"), None),
    (mrc("jasper", "Vertigo", "what are you all running these days?"), None),
    (None, "tradewars and global war are on the door menu too"),
    (mrc("kite", "Blackwater", "tw2002! I have not played that since the 90s"), None),
    (mrc("marla", "TheOuterRim", "we run trade wars here as well. same universe age?"), None),
    (None, "fresh one, started it over the weekend"),
    (mrc("jasper", "Vertigo", "how are you hosting the dos ones?"), None),
    (None, "dosbox-x with a fossil driver, over a serial link"),
    (mrc("jasper", "Vertigo", "whistles", action=True), None),
    (mrc("kite", "Blackwater", "that is the part I could never get working"), None),
    (None, "the setup guide has the emulator patches. took an evening"),
    (mrc("marla", "TheOuterRim", "bookmarking that for the weekend, thanks"), None),
    (mrc("kite", "Blackwater", "right. making a character now. what is the address?"), None),
    (None, "telnet harborlights.example.net 2323 -- doors are on the main menu"),
    (mrc("kite", "Blackwater", "see you in the woods"), None),
    (mrc("marla", "TheOuterRim", "what is your board called, for the list?"), None),
    (None, "harbor lights. we sit in #lobby most evenings"),
    (mrc("jasper", "Vertigo", "noted. good to have another board on here"), None),
    (mrc("marla", "TheOuterRim", "raises a mug back", action=True), None),
]


async def capture(exchanges: int) -> str:
    tmp = Path(tempfile.mkdtemp(prefix="netbbs-shot-"))
    db = Database(tmp / "node.db")
    lane = DatabaseLane(db.path)
    fake = FakeMrcHub()
    bridge = None
    try:
        sysop = create_user(db, "carrier", password="hunter2", user_level=255)
        alice = create_user(db, "alice", password="hunter2", user_level=10)
        channel = create_channel(db, "lobby", creator=sysop)

        await fake.start()
        save_mrc_settings(db, MrcSettings(enabled=True, host=DEFAULT_HOST, port=5000,
                                          tls=False, site_name=NODE_NAME))
        set_mrc_room(db, channel, "lobby")
        hub = ChatHub()
        bridge = MrcBridge(hub=hub, lane=lane, version="6.0.0",
                           open_connection=loopback_only(fake.port),
                           rng=random.Random(1), min_backoff_seconds=0.05,
                           max_backoff_seconds=0.2, stable_after_seconds=0.0)
        await bridge.start()
        deadline = asyncio.get_running_loop().time() + 5
        while bridge.state is not MrcState.CONNECTED:
            assert asyncio.get_running_loop().time() < deadline, bridge.status()
            await asyncio.sleep(0.01)

        for line in EARLIER:
            await fake.send_line(line)
            await asyncio.sleep(0.25)

        session = CaptureSession()
        task = asyncio.create_task(chat_flow._chat_loop(
            session, lane, hub, PresenceRegistry(), MessageMailbox(),
            InputHistory(), channel, alice, mrc_bridge=bridge))

        deadline = asyncio.get_running_loop().time() + 5
        while hub.participant_count(channel.name) == 0:
            assert asyncio.get_running_loop().time() < deadline, "caller never joined"
            await asyncio.sleep(0.01)

        # Let the join notice and the scrollback replay finish before any live
        # line arrives, or it lands in scrollback in time to be replayed as
        # history too and then appears twice on the screen.
        await asyncio.sleep(1.0)

        for inbound, typed in LIVE[:exchanges]:
            if inbound is not None:
                await fake.send_line(inbound)
            else:
                session.inputs.put_nowait(typed)
            await asyncio.sleep(0.4)
        await asyncio.sleep(0.6)

        # The screen as the caller sees it while still in the channel: /quit
        # clears it on the way out, so snapshot before leaving.
        snapshot = "".join(session.written)
        session.inputs.put_nowait("/quit")
        await asyncio.wait_for(task, timeout=6)
        return snapshot
    finally:
        if bridge is not None:
            await bridge.close()
        await fake.close()
        lane.close()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path, help="raw ANSI capture to write")
    parser.add_argument("--exchanges", type=int, default=len(LIVE),
                        help=f"how many of the {len(LIVE)} scripted lines to play")
    args = parser.parse_args()

    snapshot = asyncio.run(capture(args.exchanges))
    # Bytes, not text: see the note in website_ansi_to_html.py.
    args.output.write_bytes(snapshot.encode("utf-8"))
    print(f"wrote {args.output} ({args.exchanges} exchanges)")


if __name__ == "__main__":
    main()
