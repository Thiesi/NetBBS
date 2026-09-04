"""
SysOp-facing MRC screens in `netbbs.net.admin_flow` (issue #275):
Settings > Inter-BBS chat (MRC), the channel detail screen's `[M]RC
room` / `[P]ause` actions, and Node > Chat bridge (MRC) status --
driven through the real `admin_menu` with the scripted `FakeSession`
`tests.test_admin_flow` established, and a real `MrcBridge` against the
loopback fake hub where a live reaction (reconnect, announce) is the
thing under test.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.moderation.log import list_recent_actions
from netbbs.mrc.bridge import MrcBridge, MrcState
from netbbs.mrc.settings import MrcSettings, get_mrc_mapping, load_mrc_settings, save_mrc_settings, set_mrc_room
from netbbs.net import admin_flow
from netbbs.net.admin_flow import admin_menu
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.mrc_fake_hub import FakeMrcHub
from tests.test_admin_flow import FakeSession, _visible, _written_text


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
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def lobby(db, sysop):
    return create_channel(db, "lobby", creator=sysop)


def _controls(mrc_bridge=None) -> NodeControls:
    return NodeControls(
        session_registry=ActiveSessionRegistry(), maintenance=MaintenanceMode(),
        shutdown_event=asyncio.Event(), graceful_delay_seconds=60.0, mrc_bridge=mrc_bridge,
    )


def _bridge(lane, hub=None) -> MrcBridge:
    return MrcBridge(
        hub=hub or ChatHub(), lane=lane, version="5.7.0", rng=random.Random(1),
        min_backoff_seconds=0.05, max_backoff_seconds=0.2, stable_after_seconds=0.0,
    )


async def _wait_state(bridge: MrcBridge, state: MrcState, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while bridge.state is not state:
        assert asyncio.get_running_loop().time() < deadline, bridge.status()
        await asyncio.sleep(0.01)


# --- Settings > Inter-BBS chat (MRC) ------------------------------------------


def test_settings_menu_lists_inter_bbs_chat(db, lane, sysop):
    session = FakeSession(["s", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert "nter-BBS chat (MRC)" in _visible(_written_text(session))


def test_mrc_settings_screen_shows_defaults_and_applies_without_a_node(db, lane, sysop):
    # s: Settings, i: MRC screen, e/y: enable, h: host, n: site name, s: save, b/b/b: back out.
    session = FakeSession(["s", "i", "e", "y", "h", "hub.example.org", "n", "My Board", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _visible(_written_text(session))
    assert "Multi Relay Chat is a public, unauthenticated" in text
    assert "mrc.bottomlessabyss.net" in text
    assert "Applies the next time the node runs" in text
    saved = load_mrc_settings(db)
    assert saved.enabled and saved.host == "hub.example.org" and saved.site_name == "My Board"
    assert saved.tls is True and saved.port == 5001
    assert any(a.action == "set_mrc_settings" for a in list_recent_actions(db, limit=10))


def test_mrc_settings_tls_toggle_follows_the_well_known_port(db, lane, sysop):
    session = FakeSession(["s", "i", "t", "n", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    saved = load_mrc_settings(db)
    assert saved.tls is False and saved.port == 5000


def test_mrc_settings_rejects_a_bad_host_at_save(db, lane, sysop):
    session = FakeSession(["s", "i", "h", "two words", "s", "b", "y", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    assert "single host name" in _visible(_written_text(session))
    assert load_mrc_settings(db).host == "mrc.bottomlessabyss.net"


def test_mrc_settings_save_reconnects_the_running_bridge(db, lane, sysop, lobby):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        bridge = _bridge(lane)
        await bridge.start()
        try:
            assert bridge.state is MrcState.DISABLED
            set_mrc_room(db, lobby, "lobby")
            session = FakeSession([
                "s", "i", "e", "y", "h", "127.0.0.1", "p", str(fake.port), "t", "n", "n", "Test Board", "s",
                "b", "b", "b",
            ])
            await admin_menu(session, lane, sysop, node_controls=_controls(bridge))
            text = _visible(_written_text(session))
            assert "Saved and applied" in text
            await _wait_state(bridge, MrcState.CONNECTED)
            assert fake.handshakes and fake.handshakes[0].startswith("Test Board~NetBBS_")
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


# --- channel detail: [M]RC room, [P]ause ---------------------------------------


async def _open_channel_detail(session, lane, sysop, lobby, *, mrc_bridge=None):
    await admin_flow._channel_detail_screen(session, lane, sysop, lobby, mrc_bridge=mrc_bridge)


def test_channel_detail_maps_pauses_and_unmaps_a_room(db, lane, sysop, lobby):
    session = FakeSession(["m", "#General", "p", "p", "m", "-", "b"])
    asyncio.run(_open_channel_detail(session, lane, sysop, lobby))
    text = _visible(_written_text(session))
    assert "MRC room: none (not bridged)" in text
    assert "now bridged to MRC room #General" in text
    assert "MRC room: #General (bridged)" in text
    assert "MRC is switched off node-wide" in text
    assert "MRC bridge for 'lobby' paused." in text
    assert "MRC room: #General (paused)" in text
    assert "MRC bridge for 'lobby' resumed." in text
    assert "no longer bridged to MRC" in text
    assert get_mrc_mapping(db, lobby) is None
    actions = [a.action for a in list_recent_actions(db, limit=10)]
    assert {"set_mrc_room", "pause_mrc_bridge", "resume_mrc_bridge", "clear_mrc_room"} <= set(actions)


def test_channel_detail_rejects_a_room_already_taken(db, lane, sysop, lobby):
    other = create_channel(db, "other", creator=sysop)
    set_mrc_room(db, other, "lobby")
    session = FakeSession(["m", "LOBBY", "b"])
    asyncio.run(_open_channel_detail(session, lane, sysop, lobby))
    assert "already bridged to channel 'other'" in _visible(_written_text(session))
    assert get_mrc_mapping(db, lobby) is None


def test_channel_detail_mapping_announces_present_callers_live(db, lane, sysop, lobby):
    from netbbs.chat.hub import ParticipantId

    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        save_mrc_settings(db, MrcSettings(enabled=True, host="127.0.0.1", port=fake.port, tls=False, site_name="Board"))
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = _bridge(lane, hub)
        await bridge.start()
        try:
            await _wait_state(bridge, MrcState.CONNECTED)
            session = FakeSession(["m", "lobby", "b"])
            await _open_channel_detail(session, lane, sysop, lobby, mrc_bridge=bridge)
            newroom = await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            assert newroom.from_user == "alice"
            assert "MRC is switched off" not in _visible(_written_text(session))
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


# --- Node > Chat bridge (MRC) ---------------------------------------------------


def test_node_menu_status_without_a_running_bridge_says_so(db, lane, sysop):
    session = FakeSession(["n", "c", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_controls(None)))
    text = _visible(_written_text(session))
    assert "hat bridge (MRC)" in text
    assert "Not available here" in text


def test_node_status_screen_reports_live_state_rooms_and_reconnects(db, lane, sysop, lobby):
    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        save_mrc_settings(db, MrcSettings(enabled=True, host="127.0.0.1", port=fake.port, tls=False, site_name="Board"))
        set_mrc_room(db, lobby, "lobby")
        bridge = _bridge(lane)
        await bridge.start()
        try:
            await _wait_state(bridge, MrcState.CONNECTED)
            session = FakeSession(["n", "c", "r", "b", "b", "b"])
            await admin_menu(session, lane, sysop, node_controls=_controls(bridge))
            text = _visible(_written_text(session))
            assert "CONNECTED" in text
            assert "127.0.0.1:" in text and "(plain)" in text
            assert "lobby -> #lobby (bridged) -- 0 MRC users" in text
            assert "Reconnecting..." in text
            await fake.wait_for_connections(2)
            assert any(a.action == "reconnect_mrc" for a in list_recent_actions(db, limit=10))
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_node_status_screen_when_mrc_is_off(db, lane, sysop):
    async def scenario():
        bridge = _bridge(lane)
        await bridge.start()
        try:
            session = FakeSession(["n", "c", "b", "b", "b"])
            await admin_menu(session, lane, sysop, node_controls=_controls(bridge))
            text = _visible(_written_text(session))
            assert "MRC is off" in text
            assert "No channel is bridged" in text
        finally:
            await bridge.close()
    asyncio.run(scenario())


def test_deleting_a_mapped_channel_refreshes_the_running_bridge(db, lane, sysop, lobby):
    """Review of #275: the bridge kept a deleted channel's room mapping
    until the next inbound line for it failed."""
    from netbbs.chat.hub import ParticipantId

    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        save_mrc_settings(db, MrcSettings(enabled=True, host="127.0.0.1", port=fake.port, tls=False, site_name="Board"))
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        hub.join(lobby.name, ParticipantId("alice", 1))
        bridge = _bridge(lane, hub)
        await bridge.start()
        try:
            await _wait_state(bridge, MrcState.CONNECTED)
            await fake.wait_for(lambda p: p.body == "NEWROOM::lobby")
            session = FakeSession(["d", "lobby"])
            await _open_channel_detail(session, lane, sysop, lobby, mrc_bridge=bridge)
            assert "'lobby' deleted." in _visible(_written_text(session))
            assert bridge.mapping_for(lobby) is None
            await fake.wait_for(lambda p: p.body == "LOGOFF")
            assert bridge.state is MrcState.CONNECTED
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())


def test_operations_offers_diagnostics_on_a_running_node_without_link(db, lane, sysop):
    """Review of #275: MRC writes the same bounded diagnostic log as
    Link and can be switched on without it, so a running node always
    offers the log."""
    session = FakeSession(["o", "d", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_controls()))
    text = _visible(_written_text(session))
    assert "[D]iagnostics" in text
    assert "Nothing logged yet." in text  # the screen actually opened


def test_renaming_a_mapped_channel_refreshes_the_running_bridge(db, lane, sysop, lobby):
    """Review of #275: the bridge kept delivering inbound lines to the
    old ChatHub key after a SysOp renamed the channel."""
    from netbbs.chat.hub import ParticipantId

    async def scenario():
        fake = FakeMrcHub()
        await fake.start()
        save_mrc_settings(db, MrcSettings(enabled=True, host="127.0.0.1", port=fake.port, tls=False, site_name="Board"))
        set_mrc_room(db, lobby, "lobby")
        hub = ChatHub()
        bridge = _bridge(lane, hub)
        await bridge.start()
        try:
            await _wait_state(bridge, MrcState.CONNECTED)
            session = FakeSession(["e", "n", "lounge", "s", "b"])
            await _open_channel_detail(session, lane, sysop, lobby, mrc_bridge=bridge)
            assert "Updated 'lounge'" in _visible(_written_text(session))
            assert bridge.mapping_for(lobby).channel.name == "lounge"
            queue = hub.join("lounge", ParticipantId("alice", 1))
            await fake.send_line("bob~Other~lobby~~~lobby~still reaching you?~")
            delivered = await asyncio.wait_for(queue.get(), timeout=2)
            assert getattr(delivered, "body", None) == "still reaching you?"
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())
