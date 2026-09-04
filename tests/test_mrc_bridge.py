"""
Socket-level tests for `netbbs.mrc.bridge.MrcBridge` (issue #275)
against `tests.mrc_fake_hub.FakeMrcHub` -- a real loopback TCP hub
speaking the real tilde protocol, a real SQLite database behind a
`DatabaseLane`, and a real `ChatHub` participant queue on the
receiving end, so every assertion covers the full path from hub socket
to what a locally-connected caller would actually see.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub, ParticipantId
from netbbs.chat.scrollback import ChannelMessage, get_scrollback, record_message
from netbbs.mrc.bridge import MrcBridge, MrcState
from netbbs.mrc.protocol import MrcPacket
from netbbs.mrc.settings import (
    MrcSettings,
    clear_mrc_room,
    load_mrc_settings,
    save_mrc_settings,
    set_mrc_paused,
    set_mrc_room,
)
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.mrc_fake_hub import FakeMrcHub


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
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def lobby(db, sysop):
    return create_channel(db, "lobby", creator=sysop)


def _enable(db, hub_port: int, *, site_name: str = "My Board", tls: bool = False) -> MrcSettings:
    return save_mrc_settings(db, MrcSettings(
        enabled=True, host="127.0.0.1", port=hub_port, tls=tls, site_name=site_name, info_sysop="Thiesi",
    ))


def _bridge(hub: ChatHub, lane: DatabaseLane, **overrides) -> MrcBridge:
    kwargs = dict(
        hub=hub, lane=lane, version="5.7.0", rng=random.Random(1),
        min_backoff_seconds=0.05, max_backoff_seconds=0.2, stable_after_seconds=0.0,
        connect_timeout_seconds=2.0, keepalive_interval_seconds=0.2,
    )
    kwargs.update(overrides)
    return MrcBridge(**kwargs)


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def _connected_bridge(db, lane, hub, fake, **overrides) -> MrcBridge:
    bridge = _bridge(hub, lane, **overrides)
    await bridge.start()
    await _wait_until(lambda: bridge.state is MrcState.CONNECTED)
    return bridge


def test_connects_handshakes_and_announces_site_info(db, lane, lobby):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body.startswith("CAPABILITIES:"))
            assert fake.handshakes == ["My Board~NetBBS_5.7.0/" + fake.handshakes[0].split("/", 1)[1]]
            assert fake.handshakes[0].endswith("/1.3.5")
            info = fake.packets(body_prefix="INFOSYS:")[0]
            assert (info.from_user, info.from_site, info.to_user) == ("CLIENT", "My_Board", "SERVER")
            assert info.body == "INFOSYS:Thiesi"
            alive = fake.packets(body_prefix="IMALIVE:")[0]
            assert alive.body == "IMALIVE:My Board"
            status = bridge.status()
            assert status.connected and status.attempts == 1 and status.last_error is None
            assert status.bridged_channels == 1 and status.site_name == "My Board"
        finally:
            await bridge.close()
            await fake.close()
        assert fake.packets(body_prefix="SHUTDOWN")
    asyncio.run(scenario())


def test_local_join_message_and_leave_reach_the_room(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        participant = ParticipantId("alice", 1)
        hub.join(lobby.name, participant)
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            # start() reconciles against the ChatHub roster, so a caller
            # already inside the channel is announced at connect time.
            newroom = await fake.wait_for(lambda p: p.body.startswith("NEWROOM:"))
            assert (newroom.from_user, newroom.from_site, newroom.body) == ("alice", "My_Board", "NEWROOM::lobby")
            await fake.wait_for(lambda p: p.body == "USERLIST")
            await bridge.local_join(lobby, "alice")  # idempotent
            assert len(fake.packets(body_prefix="NEWROOM:")) == 1

            recorded = record_message(db, lobby, kind="message", author_label="alice", author_fingerprint=None, body="hello ~ world")
            assert await bridge.local_message(lobby, recorded) == (True, False)
            chat = await fake.wait_for(lambda p: p.to_user == "" and p.body.startswith("hello"))
            assert (chat.from_user, chat.from_site, chat.from_room, chat.to_room, chat.body) == (
                "alice", "My_Board", "lobby", "lobby", "hello world",
            )
            # The hub echoed that line back; it must not be recorded twice.
            await asyncio.sleep(0.05)
            assert [m.body for m in get_scrollback(db, lobby) if m.kind == "message"] == ["hello ~ world"]

            action = record_message(db, lobby, kind="action", author_label="alice", author_fingerprint=None, body="waves")
            assert await bridge.local_message(lobby, action) == (True, False)
            await fake.wait_for(lambda p: p.body == "* alice waves")

            long_line = record_message(db, lobby, kind="message", author_label="alice", author_fingerprint=None, body=" ".join(["word"] * 200))
            # alice's per-user bucket has one token left of a burst of 3
            # -- a pasted wall of text is bounded, and the caller is told.
            relayed, truncated = await bridge.local_message(lobby, long_line)
            assert truncated is True

            # Second session of the same account: still one MRC user.
            hub.join(lobby.name, ParticipantId("alice", 2))
            hub.leave(lobby.name, participant)
            await bridge.local_leave(lobby, "alice")
            await asyncio.sleep(0.05)
            assert not fake.packets(body_prefix="LOGOFF")
            hub.leave(lobby.name, ParticipantId("alice", 2))
            await bridge.local_leave(lobby, "alice")
            logoff = await fake.wait_for(lambda p: p.body == "LOGOFF")
            assert (logoff.from_user, logoff.from_room, logoff.to_room) == ("alice", "lobby", "lobby")
            assert bridge.status().participants == 0
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_inbound_room_message_is_recorded_as_external_author(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body.startswith("NEWROOM:"))
            await fake.send_line("bob~Other~lobby~~~lobby~|07hi \x1b[31mfrom |12MRC~")
            delivered = await asyncio.wait_for(queue.get(), timeout=2)
            assert isinstance(delivered, ChannelMessage)
            assert delivered.kind == "message"
            assert delivered.author_label == "bob@Other (MRC)"
            assert delivered.author_fingerprint is None
            assert delivered.body == "hi from MRC"
            assert [m.body for m in get_scrollback(db, lobby) if m.kind == "message"] == ["hi from MRC"]

            # Unmapped room: ignored entirely.
            await fake.send_line("bob~Other~elsewhere~~~elsewhere~secret~")
            # Presence chatter and NOTME lines: ephemeral notice, not recorded.
            await fake.send_line("bob~Other~lobby~NOTME~~lobby~|07- |12bob |04has joined|08.~")
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert isinstance(notice, str) and "[MRC] - bob has joined." in notice
            await fake.send_line("SERVER~~~~~lobby~*** Joining lobby: carol@Third~")
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert "[MRC] *** Joining lobby: carol@Third" in notice
            await fake.send_line("SERVER~~~CLIENT~~~ROOMTOPIC:lobby:|15Be excellent~")
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert "[MRC] room topic: Be excellent" in notice
            assert queue.empty()
            assert len([m for m in get_scrollback(db, lobby) if m.kind == "message"]) == 1
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_paused_mapping_neither_sends_nor_records(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body.startswith("NEWROOM:"))
            set_mrc_paused(db, lobby, True)
            await bridge.refresh_channel_mappings()
            await fake.wait_for(lambda p: p.body == "LOGOFF")
            assert not bridge.is_bridged(lobby)
            assert bridge.mapping_for(lobby) is not None

            recorded = record_message(db, lobby, kind="message", author_label="alice", author_fingerprint=None, body="local only")
            assert await bridge.local_message(lobby, recorded) == (False, False)
            await fake.send_line("bob~Other~lobby~~~lobby~not for you~")
            await asyncio.sleep(0.1)
            assert queue.empty()
            assert [m.body for m in get_scrollback(db, lobby) if m.kind == "message"] == ["local only"]

            set_mrc_paused(db, lobby, False)
            await bridge.refresh_channel_mappings()
            await _wait_until(lambda: len(fake.packets(body_prefix="NEWROOM:")) == 2)
            assert bridge.is_bridged(lobby)
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_private_message_is_not_delivered_but_noticed_once(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body.startswith("NEWROOM:"))
            await fake.send_line("bob~Other~lobby~Alice~~lobby~psst secret~")
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert isinstance(notice, str)
            assert "bob@Other tried to message you privately" in notice
            assert "secret" not in notice
            await fake.send_line("bob~Other~lobby~alice~~lobby~again~")
            await fake.send_line("bob~Other~lobby~someoneelse~~lobby~not here~")
            await asyncio.sleep(0.1)
            assert queue.empty()
            assert [m for m in get_scrollback(db, lobby) if m.kind == "message"] == []
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_malformed_and_oversized_lines_are_dropped_without_disconnecting(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body.startswith("NEWROOM:"))
            await fake.send_line("garbage without tildes")
            await fake.send_line("x" * 5000)
            await fake.send_line("bob~Other~lobby~~~lobby~" + "y" * 3000 + "~")
            await fake.send_line("bob~Other~lobby~~~lobby~still alive~")
            delivered = await asyncio.wait_for(queue.get(), timeout=2)
            assert isinstance(delivered, ChannelMessage) and delivered.body == "still alive"
            assert bridge.state is MrcState.CONNECTED
            assert bridge.status().dropped_inbound >= 2
            assert fake.connections == 1
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_ping_gets_imalive_and_keepalive_sends_iamhere(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake, keepalive_interval_seconds=0.1)
        try:
            before = len(fake.packets(body_prefix="IMALIVE:"))
            await fake.ping()
            await _wait_until(lambda: len(fake.packets(body_prefix="IMALIVE:")) == before + 1)
            here = await fake.wait_for(lambda p: p.body == "IAMHERE")
            assert (here.from_user, here.from_room, here.to_room) == ("alice", "lobby", "lobby")
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_userlist_reply_feeds_remote_roster_minus_own_callers(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body == "USERLIST")
            fake.users[("other", "bob")] = "lobby"
            fake.users[("third", "carol")] = "lobby"
            fake.users[("third", "dave")] = "elsewhere"
            await fake.send_line("SERVER~~~alice~~lobby~USERLIST:alice@My_Board,Carol@third,bob@other~")
            await _wait_until(lambda: bridge.remote_roster(lobby) == ["bob@other", "Carol@third"])
            assert bridge.status().rooms == {"lobby": ("alice@My_Board", "Carol@third", "bob@other")}
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_reconnects_after_hub_drop_and_reannounces_callers(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body.startswith("NEWROOM:"))
            await fake.drop_clients()
            await _wait_until(lambda: bridge.state in (MrcState.BACKOFF, MrcState.CONNECTING))
            await fake.wait_for_connections(2)
            await _wait_until(lambda: bridge.state is MrcState.CONNECTED)
            await _wait_until(lambda: len(fake.packets(body_prefix="NEWROOM:")) == 2)
            assert len(fake.handshakes) == 2
            assert bridge.status().attempts == 2
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_unreachable_hub_backs_off_and_reports_the_error(db, lane, lobby):
    async def scenario():
        # Bind-then-close so the port is known to refuse connections.
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        _enable(db, port)
        set_mrc_room(db, lobby, "lobby")
        bridge = _bridge(ChatHub(), lane)
        await bridge.start()
        try:
            await _wait_until(lambda: bridge.status().attempts >= 2, timeout=3.0)
            status = bridge.status()
            assert status.state in (MrcState.BACKOFF, MrcState.CONNECTING)
            assert status.last_error
        finally:
            await bridge.close()
        assert bridge.state is MrcState.DISABLED
    asyncio.run(scenario())


def test_oldversion_rejection_is_fatal_until_settings_change(db, lane, lobby):
    async def scenario():
        fake = FakeMrcHub(reject_version="9.9.9")
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        bridge = _bridge(ChatHub(), lane)
        await bridge.start()
        try:
            await _wait_until(lambda: bridge.state is MrcState.ERROR)
            await asyncio.sleep(0.3)
            assert fake.connections == 1
            assert "newer MRC client version" in bridge.status().last_error
            # A SysOp "applies" settings again: one more attempt is made.
            await bridge.reload_settings()
            await fake.wait_for_connections(2)
            await _wait_until(lambda: bridge.state is MrcState.ERROR)
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_disabled_bridge_starts_nothing_and_reload_enables_it(db, lane, lobby, alice):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        bridge = _bridge(hub, lane)
        await bridge.start()
        try:
            assert bridge.state is MrcState.DISABLED
            assert not bridge.is_bridged(lobby)
            recorded = record_message(db, lobby, kind="message", author_label="alice", author_fingerprint=None, body="x")
            assert await bridge.local_message(lobby, recorded) == (False, False)
            await asyncio.sleep(0.1)
            assert fake.connections == 0

            _enable(db, fake.port)
            await bridge.reload_settings()
            await _wait_until(lambda: bridge.state is MrcState.CONNECTED)
            assert bridge.is_bridged(lobby)

            save_mrc_settings(db, MrcSettings(**{**load_mrc_settings(db).__dict__, "enabled": False}))
            await bridge.reload_settings()
            assert bridge.state is MrcState.DISABLED
            assert fake.packets(body_prefix="SHUTDOWN")
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_mapping_changes_announce_and_logoff_without_reconnecting(db, lane, lobby, alice, sysop):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await asyncio.sleep(0.05)
            assert not fake.packets(body_prefix="NEWROOM:")
            set_mrc_room(db, lobby, "lobby")
            await bridge.refresh_channel_mappings()
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            clear_mrc_room(db, lobby)
            await bridge.refresh_channel_mappings()
            await fake.wait_for(lambda p: p.body == "LOGOFF")
            assert fake.connections == 1
            assert bridge.mapping_for(lobby) is None
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_outbound_queue_is_bounded(db, lane, lobby):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        bridge = await _connected_bridge(db, lane, hub, fake, outbound_queue_size=2)
        try:
            for name in ("u1", "u2", "u3", "u4", "u5"):
                await bridge.local_join(lobby, name)
            await asyncio.sleep(0.1)
            assert bridge.status().dropped_outbound > 0
            assert bridge.state is MrcState.CONNECTED
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_tls_setting_requests_a_verified_context(db, lane, lobby):
    seen: list[dict] = []

    async def fake_open_connection(host, port, **kwargs):
        seen.append({"host": host, "port": port, **kwargs})
        raise ConnectionRefusedError("nope")

    async def scenario():
        _enable(db, 5001, tls=True)
        set_mrc_room(db, lobby, "lobby")
        bridge = _bridge(ChatHub(), lane, open_connection=fake_open_connection)
        await bridge.start()
        try:
            await _wait_until(lambda: len(seen) >= 1)
        finally:
            await bridge.close()
        call = seen[0]
        assert call["host"] == "127.0.0.1" and call["port"] == 5001
        assert call["server_hostname"] == "127.0.0.1"
        context = call["ssl"]
        assert context.verify_mode.name == "CERT_REQUIRED" and context.check_hostname is True
    asyncio.run(scenario())


def test_status_snapshot_shape_when_never_started(lane):
    bridge = _bridge(ChatHub(), lane)
    status = bridge.status()
    assert status.state is MrcState.DISABLED and not status.enabled
    assert status.host == "" and status.rooms == {}


def test_pre_hello_room_packets_pass_the_same_inbound_bound(db, lane, lobby, alice):
    """Review of #275: everything a hub sends before HELLO used to skip
    the inbound size cap and token bucket, so a hub could write
    unbounded scrollback during every handshake window."""
    from netbbs.mrc.bridge import INBOUND_BURST
    from netbbs.mrc.protocol import build_line

    flood = INBOUND_BURST + 20

    class FloodingHub(FakeMrcHub):
        async def _serve(self, reader, writer):
            self.connections += 1
            self._writers.append(writer)
            try:
                await reader.readline()
                for index in range(flood):
                    writer.write(f"bob~Other~lobby~~~lobby~flood {index}~\n".encode("ascii"))
                await writer.drain()
                writer.write(build_line(MrcPacket("SERVER", "", "", "CLIENT", "", "", "HELLO")).encode("ascii"))
                await writer.drain()
                while await reader.readline():
                    pass
            except (ConnectionError, asyncio.IncompleteReadError):
                pass

    async def scenario():
        fake = FloodingHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        # No participant: nobody would drain a delivery queue while the
        # pre-HELLO flood is being recorded, and the bound under test is
        # the one ahead of the database write, not delivery.
        hub = ChatHub()
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            recorded = [m for m in get_scrollback(db, lobby) if m.kind == "message"]
            # The burst plus at most a few refilled tokens while the
            # flood was being read -- never the whole flood.
            assert 0 < len(recorded) <= INBOUND_BURST + 10
            assert bridge.status().dropped_inbound >= flood - INBOUND_BURST - 10
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_mapping_an_occupied_channel_tells_the_occupants_first(db, lane, lobby, alice):
    """Review of #275: a caller already inside a channel when the SysOp
    maps it never saw the join-time disclosure -- they are told before
    anything they say leaves the node. A reconnect re-announces without
    repeating the notice."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            set_mrc_room(db, lobby, "lobby")
            await bridge.refresh_channel_mappings()
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert isinstance(notice, str)
            assert "now bridged to MRC room #lobby" in notice and "'alice'" in notice
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")

            await fake.drop_clients()
            await fake.wait_for_connections(2)
            await _wait_until(lambda: bridge.state is MrcState.CONNECTED)
            await fake.wait_for(lambda p: len(fake.packets(body_prefix="NEWROOM:")) >= 2)
            await asyncio.sleep(0.1)
            assert queue.empty()
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_remapping_an_occupied_channel_moves_its_callers(db, lane, lobby, alice):
    """Review of #275: pointing an occupied channel at a different room
    left the hub roster in the old room while messages went to the new."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        queue = hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            set_mrc_room(db, lobby, "gaming")
            await bridge.refresh_channel_mappings()
            moved = await fake.wait_for(lambda p: p.body == "NEWROOM:lobby:gaming")
            assert moved.from_user == "alice"
            assert fake.users[("my_board", "alice")] == "gaming"
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert "MRC room changed from #lobby to #gaming" in notice
            assert not fake.packets(body_prefix="LOGOFF")
            assert fake.connections == 1
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_multi_chunk_message_is_all_or_nothing_under_the_per_caller_bucket(db, lane, lobby, alice):
    """Review of #275: with fewer tokens than chunks, the prefix used to
    reach MRC while the caller was told nothing was relayed."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")

            def _msg(body):
                return ChannelMessage(
                    id=0, channel_id=lobby.id, kind="message", author_label="alice",
                    author_fingerprint=None, body=body, created_at="2026-09-04T12:00:00+00:00",
                )

            assert await bridge.local_message(lobby, _msg("one")) == (True, False)
            assert await bridge.local_message(lobby, _msg("two")) == (True, False)
            long_body = " ".join(["word"] * 60)  # three wire chunks, one token left
            assert await bridge.local_message(lobby, _msg(long_body)) == (False, False)
            await fake.wait_for(lambda p: p.body == "two")
            await asyncio.sleep(0.1)
            assert not [p for p in fake.received if p.body.startswith("word word")]
            assert bridge.status().dropped_outbound == 1
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_remote_roster_keeps_a_same_named_user_from_another_site(db, lane, lobby, alice):
    """Review of #275: an MRC identity is nick *and* site -- a remote
    `alice@Other` is not this node's `alice` and must stay listed."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body == "USERLIST")
            await fake.send_line("SERVER~~~alice~~lobby~USERLIST:alice@My_Board,alice@Other,Alice,bob@other~")
            await _wait_until(lambda: bridge.remote_roster(lobby) == ["alice@Other", "bob@other"])
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_inbound_line_for_a_deleted_channel_drops_the_mapping_not_the_connection(db, lane, lobby, alice, sysop):
    """Review of #275: a channel deleted underneath a cached mapping
    (the standalone admin CLI cannot tell the running node) made the
    next inbound line raise inside the reader and reconnect forever."""
    from netbbs.chat.channels import delete_channel

    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            delete_channel(db, lobby, deleted_by=sysop)
            await fake.send_line("bob~Other~lobby~~~lobby~still there?~")
            await _wait_until(lambda: bridge.mapping_for(lobby) is None)
            await asyncio.sleep(0.2)
            assert bridge.state is MrcState.CONNECTED
            assert fake.connections == 1
            assert bridge.status().dropped_inbound == 1
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_oldversion_on_a_live_session_stops_relaying_immediately(db, lane, lobby, alice):
    """Review of #275: a version rejection after HELLO on a socket the
    hub leaves open used to be noted and otherwise ignored."""
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        _enable(db, fake.port)
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = await _connected_bridge(db, lane, hub, fake)
        try:
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            await fake.send_line("SERVER~~~CLIENT~~~OLDVERSION:9.9.9~")
            await _wait_until(lambda: bridge.state is MrcState.ERROR)
            await asyncio.sleep(0.3)
            assert fake.connections == 1
            assert "newer MRC client version" in bridge.status().last_error
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())
