"""
Caller-facing MRC bridge wiring in `netbbs.net.chat_flow` (issue #275):
the real `_chat_loop` driven by a scripted `FakeSession`, a real
`MrcBridge` connected to `tests.mrc_fake_hub.FakeMrcHub` over loopback
-- the join notice, message relay, `/who`/`/names`/`/mrc`, the leave
LOGOFF, and an inbound MRC line rendered to the caller as an external
author. `mrc_bridge=None` (no running node) must leave chat exactly as
it was.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.chat.scrollback import get_scrollback
from netbbs.mrc.bridge import MrcBridge, MrcState
from netbbs.mrc.settings import MrcSettings, save_mrc_settings, set_mrc_paused, set_mrc_room
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.rendering.ansi import strip_ansi
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.mrc_fake_hub import FakeMrcHub
from tests.test_chat_flow_moderation import FakeSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def hub():
    return ChatHub()


@pytest.fixture
def presence():
    return PresenceRegistry()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, sysop):
    return create_channel(db, "lobby", creator=sysop)


class _Rig:
    """One fake hub plus one connected bridge, torn down together."""

    def __init__(self, fake: FakeMrcHub, bridge: MrcBridge):
        self.fake = fake
        self.bridge = bridge

    async def close(self) -> None:
        await self.bridge.close()
        await self.fake.close()


async def _rig(db, lane, hub, channel, *, room: str = "lobby") -> _Rig:
    fake = FakeMrcHub()
    await fake.start()
    save_mrc_settings(db, MrcSettings(enabled=True, host="127.0.0.1", port=fake.port, tls=False, site_name="My Board"))
    set_mrc_room(db, channel, room)
    bridge = MrcBridge(
        hub=hub, lane=lane, version="5.7.0", rng=random.Random(1),
        min_backoff_seconds=0.05, max_backoff_seconds=0.2, stable_after_seconds=0.0,
    )
    await bridge.start()
    deadline = asyncio.get_running_loop().time() + 2
    while bridge.state is not MrcState.CONNECTED:
        assert asyncio.get_running_loop().time() < deadline, bridge.status()
        await asyncio.sleep(0.01)
    return _Rig(fake, bridge)


class _QueueSession(FakeSession):
    """`FakeSession` whose input arrives through a queue, so a test can
    push a line *after* the chat loop has already joined the channel --
    `FakeSession.read_line` itself blocks forever once its scripted
    list is exhausted and never looks again."""

    def __init__(self):
        super().__init__([])
        self.inputs: asyncio.Queue[str] = asyncio.Queue()

    async def read_line(self, echo=True, history=None, completer=None, *, live_buffer=None, lock=None, list_candidates=None):
        return await self.inputs.get()


async def _run(lane, hub, presence, channel, user, lines, *, mrc_bridge=None, while_joined=None):
    """Drive `_chat_loop`. With `while_joined`, the scripted `lines` are
    fed only once the caller is actually in the channel (a `ChatHub`
    participant), after that coroutine ran -- so the fake hub can push
    inbound traffic the caller is there to receive."""
    mailbox = MessageMailbox()
    history = InputHistory()
    if while_joined is None:
        session = FakeSession(lines)
        loop_coro = chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, user, mrc_bridge=mrc_bridge)
        return session, await asyncio.wait_for(loop_coro, timeout=4)

    session = _QueueSession()
    task = asyncio.create_task(
        chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, user, mrc_bridge=mrc_bridge)
    )
    deadline = asyncio.get_running_loop().time() + 2
    while hub.participant_count(channel.name) == 0:
        assert asyncio.get_running_loop().time() < deadline, "caller never joined"
        await asyncio.sleep(0.01)
    await while_joined()
    for line in lines:
        session.inputs.put_nowait(line)
    return session, await asyncio.wait_for(task, timeout=4)


def _text(session: FakeSession) -> str:
    return strip_ansi("\n".join(session.written))


def test_join_announces_caller_and_explains_the_bridge(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            session, _ = await _run(lane, hub, presence, channel, alice, ["/quit"], mrc_bridge=rig.bridge)
            text = _text(session)
            assert "bridged to MRC room #lobby on 127.0.0.1" in text
            assert "your handle 'alice' is visible" in text
            newroom = await rig.fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            assert newroom.from_user == "alice"
            await rig.fake.wait_for(lambda p: p.body == "LOGOFF" and p.from_user == "alice")
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_message_and_action_are_relayed_under_the_callers_handle(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            session, _ = await _run(
                lane, hub, presence, channel, alice, ["hello mrc", "/me waves", "/quit"], mrc_bridge=rig.bridge,
            )
            chat = await rig.fake.wait_for(lambda p: p.body == "hello mrc")
            assert (chat.from_user, chat.from_site, chat.to_room) == ("alice", "My_Board", "lobby")
            await rig.fake.wait_for(lambda p: p.body == "* alice waves")
            assert "not relayed" not in _text(session)
            assert [m.body for m in get_scrollback(db, channel) if m.kind == "message"] == ["hello mrc"]
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_inbound_mrc_line_is_rendered_as_an_external_author(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            async def push():
                await rig.fake.send_line("bob~Other~lobby~~~lobby~|12greetings \x1b[31mfrom afar~")
                await asyncio.sleep(0.15)

            session, _ = await _run(
                lane, hub, presence, channel, alice, ["/quit"], mrc_bridge=rig.bridge, while_joined=push,
            )
            text = _text(session)
            assert "bob@Other (MRC)" in text
            assert "greetings from afar" in text
            # The remote line's own SGR was stripped at the wire boundary.
            assert "\x1b[31m" not in "\n".join(session.written)
            recorded = [m for m in get_scrollback(db, channel) if m.kind == "message"]
            assert [(m.author_label, m.author_fingerprint, m.body) for m in recorded] == [
                ("bob@Other (MRC)", None, "greetings from afar"),
            ]
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_who_names_and_mrc_show_the_hub_roster_and_link_state(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            rig.fake.users[("other", "bob")] = "lobby"
            rig.fake.users[("third", "carol")] = "lobby"
            session, _ = await _run(
                lane, hub, presence, channel, alice, ["/who", "/names", "/mrc", "/quit"], mrc_bridge=rig.bridge,
            )
            # The roster arrives asynchronously from the hub's USERLIST
            # reply; a second pass once it has landed is the real check.
            await rig.fake.wait_for(lambda p: p.body == "USERLIST")
            deadline = asyncio.get_running_loop().time() + 2
            while not rig.bridge.remote_roster(channel):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            session, _ = await _run(
                lane, hub, presence, channel, alice, ["/who", "/names", "/mrc", "/quit"], mrc_bridge=rig.bridge,
            )
            text = _text(session)
            assert "bob@other (on MRC)" in text
            assert "carol@third (on MRC)" in text
            assert "bob@other (MRC), carol@third (MRC)" in text
            assert "MRC room #lobby via 127.0.0.1:" in text
            assert "CONNECTED" in text
            assert "2 MRC users here: bob@other, carol@third" in text
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_paused_bridge_is_honest_in_mrc_and_status_line(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            set_mrc_paused(db, channel, True)
            await rig.bridge.refresh_channel_mappings()
            session, _ = await _run(lane, hub, presence, channel, alice, ["hi", "/mrc", "/quit"], mrc_bridge=rig.bridge)
            text = _text(session)
            assert "bridged to MRC room" not in text  # no opt-in banner while paused
            assert "Bridge paused by the SysOp" in text
            await asyncio.sleep(0.1)
            assert not rig.fake.packets(body_prefix="hi")
            groups = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
            assert "[MRC]" not in "".join(span.text for span in groups[0])
            set_mrc_paused(db, channel, False)
            groups = chat_flow._render_chat_status_line(db, hub, presence, channel, alice)
            assert "".join(span.text for span in groups[0]).endswith("[PUBLIC][MRC]")
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_offline_hub_keeps_chat_local_and_says_so(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            await rig.fake.close()
            deadline = asyncio.get_running_loop().time() + 2
            while rig.bridge.state is MrcState.CONNECTED:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            session, _ = await _run(lane, hub, presence, channel, alice, ["still here", "/quit"], mrc_bridge=rig.bridge)
            text = _text(session)
            assert "The MRC link is currently offline" in text
            assert "not relayed to MRC: the MRC link is offline" in text
            assert [m.body for m in get_scrollback(db, channel) if m.kind == "message"] == ["still here"]
        finally:
            await rig.bridge.close()
    asyncio.run(scenario())


def test_no_bridge_leaves_chat_untouched(db, lane, hub, presence, channel, alice):
    session, _ = asyncio.run(_run(lane, hub, presence, channel, alice, ["hello", "/mrc", "/quit"]))
    text = _text(session)
    assert "MRC room" not in text
    assert "This channel isn't bridged to MRC." in text
    assert [m.body for m in get_scrollback(db, channel) if m.kind == "message"] == ["hello"]


def test_mrc_command_is_only_suggested_in_a_mapped_channel(db, channel, alice):
    assert not chat_flow._channel_is_mrc_bridged(db, channel, alice)
    set_mrc_room(db, channel, "lobby")
    assert chat_flow._channel_is_mrc_bridged(db, channel, alice)
    assert "mrc" in chat_flow._COMMAND_INFO and "mrc" in chat_flow._COMMANDS
