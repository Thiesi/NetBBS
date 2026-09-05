"""
Issue #300 on the SysOp screens: the open-room half of Settings >
Inter-BBS chat (MRC) including the blocklist editor, an open room's
channel detail (Adopt, Retire, no Link, no Unbridge), and the status
screen's open-room line -- driven through the real screens with the
scripted `FakeSession` of `tests/test_admin_flow.py`, reusing
`tests/test_admin_flow_mrc.py`'s helpers.
"""

from __future__ import annotations

import asyncio

from netbbs.chat.channels import get_channel_by_name
from netbbs.chat.scrollback import record_message
from netbbs.link.channels import is_channel_linked
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.moderation.log import list_recent_actions
from netbbs.mrc.settings import (
    OpenRoomSettings,
    get_mrc_mapping,
    load_open_room_settings,
    materialize_open_room,
    save_open_room_settings,
)
from netbbs.net import admin_flow
from netbbs.net.admin_flow import admin_menu
from tests.test_admin_flow import FakeSession, _visible, _written_text
from tests.test_admin_flow_mrc import (  # noqa: F401 -- fixtures and helpers
    _bridge,
    _controls,
    _wait_state,
    db,
    lane,
    lobby,
    sysop,
)


def test_settings_screen_edits_the_open_room_half_and_its_blocklist(db, lane, sysop):
    # s: Settings, i: MRC; o/y: open rooms on; c: cap 5; r: retention 3;
    # k: blocklist -> a: add "Secret Room", a: add "|04evil", r: remove ->
    # pick 0,1 (the first entry), b: back; s: save; b/b/b out.
    session = FakeSession([
        "s", "i", "o", "y", "c", "5", "r", "3",
        "k", "a", "Secret Room", "a", "|04evil", "r", "0", "1", "b",
        "s", "b", "b", "b",
    ])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _visible(_written_text(session))
    assert "Callers may open any room" in text
    assert "Blocked MRC rooms" in text and "#Secret_Room" in text
    saved = load_open_room_settings(db)
    assert saved.enabled is True and saved.cap == 5 and saved.retention_days == 3
    assert saved.blocklist == ("evil",)
    actions = [a for a in list_recent_actions(db, limit=5) if a.action == "set_mrc_settings"]
    assert actions and "open_rooms=True cap=5 retention=3d" in (actions[0].detail or "")


def test_settings_screen_rejects_a_bad_open_room_value_and_keeps_the_draft(db, lane, sysop):
    session = FakeSession(["s", "i", "c", "0", "s", "c", "4", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop))
    text = _visible(_written_text(session))
    assert "The open-room cap must be between 1 and 500." in text
    assert load_open_room_settings(db).cap == 4


def test_open_room_detail_offers_adopt_and_retire_but_never_link_or_unbridge(db, lane, sysop):
    settings = save_open_room_settings(db, OpenRoomSettings(enabled=True))
    channel = materialize_open_room(db, "lobby", open_settings=settings).channel
    record_message(db, channel, kind="message", author_label="bob@Other (MRC)", author_fingerprint=None,
                   body="kept", external_source="mrc", index_body="kept")
    identity = bootstrap_node_identity("roanoke")

    class _Link:
        node_identity = identity
        node = None

    # The detail screen with Link available: no [L]ink, no [U]nbridge,
    # no [M]RC room for an open room; "l"/"u"/"m" are rejected keys.
    session = FakeSession(["l", "u", "m", "b"])
    asyncio.run(admin_flow._channel_detail_screen(session, lane, sysop, channel, link_context=_Link(), mrc_bridge=None))
    text = _visible(_written_text(session))
    assert "open room -- opened by a caller" in text
    assert "[A]dopt" in text and "Re[t]ire" in text
    assert "[L]ink this chat channel" not in text and "[U]nbridge" not in text and "[M]RC room" not in text
    assert not is_channel_linked(db, channel)

    # Adopt: origin cleared, scrollback kept, sweeper ignores it from now on.
    session = FakeSession(["a", "b"])
    asyncio.run(admin_flow._channel_detail_screen(session, lane, sysop, channel, mrc_bridge=None))
    text = _visible(_written_text(session))
    assert "Adopted: 'mrc:lobby' stays bridged to MRC room #lobby" in text
    mapping = get_mrc_mapping(db, channel)
    assert mapping is not None and not mapping.is_open_room
    assert "MRC room: #lobby (bridged)" in text
    assert "adopt_mrc_room" in {a.action for a in list_recent_actions(db, limit=5)}


def test_the_unlisted_pause_key_is_rejected_for_an_open_room(db, lane, sysop):
    settings = save_open_room_settings(db, OpenRoomSettings(enabled=True))
    channel = materialize_open_room(db, "lobby", open_settings=settings).channel
    session = FakeSession(["p", "b"])
    asyncio.run(admin_flow._channel_detail_screen(session, lane, sysop, channel, mrc_bridge=None))
    text = _visible(_written_text(session))
    assert "paused" not in text
    mapping = get_mrc_mapping(db, channel)
    assert mapping is not None and not mapping.paused


def test_retire_asks_first_and_then_removes_the_room(db, lane, sysop):
    settings = save_open_room_settings(db, OpenRoomSettings(enabled=True))
    channel = materialize_open_room(db, "lobby", open_settings=settings).channel
    # t/n: retire, declined -> still there; t/y: retire, confirmed -> gone.
    session = FakeSession(["t", "n", "t", "y"])
    asyncio.run(admin_flow._channel_detail_screen(session, lane, sysop, channel, mrc_bridge=None))
    text = _visible(_written_text(session))
    assert "Retire mrc:lobby and delete its scrollback now?" in text
    assert "Retired 'mrc:lobby'." in text
    assert get_mrc_mapping(db, channel) is None
    try:
        get_channel_by_name(db, "mrc:lobby")
    except Exception:
        pass
    else:
        raise AssertionError("retired room still exists")
    assert "retire_mrc_room" in {a.action for a in list_recent_actions(db, limit=5)}


def test_status_screen_reports_retained_open_rooms_with_mrc_switched_off(db, lane, sysop, lobby):
    """Second review of #300: rooms opened earlier keep ageing out after
    MRC is switched off node-wide, and the SysOp must see that."""
    async def scenario():
        settings = save_open_room_settings(db, OpenRoomSettings(enabled=True, retention_days=3))
        materialize_open_room(db, "leftover", open_settings=settings)
        save_open_room_settings(db, OpenRoomSettings(enabled=False, retention_days=3))
        bridge = _bridge(lane)
        await bridge.start()  # MRC disabled: no connection, sweeper still runs
        try:
            session = FakeSession(["n", "c", "b", "b", "b"])
            await admin_menu(session, lane, sysop, node_controls=_controls(bridge))
            # The line wraps at the terminal width; compare with the
            # wrap-inserted breaks folded back into single spaces.
            text = " ".join(_visible(_written_text(session)).split())
            assert "MRC is off node-wide; 1 opened earlier still age out after 3 idle days; 0 retired since start" in text
            assert "mrc:leftover -> #leftover (open room)" in text
        finally:
            await bridge.close()
    asyncio.run(scenario())


def test_status_screen_reports_open_rooms(db, lane, sysop, lobby):
    async def scenario():
        from tests.mrc_fake_hub import FakeMrcHub
        from netbbs.mrc.settings import MrcSettings, save_mrc_settings

        fake = FakeMrcHub()
        await fake.start()
        save_mrc_settings(db, MrcSettings(enabled=True, host="127.0.0.1", port=fake.port, tls=False, site_name="My Board"))
        save_open_room_settings(db, OpenRoomSettings(enabled=True, cap=8, retention_days=2))
        bridge = _bridge(lane)
        await bridge.start()
        await _wait_state(bridge, admin_flow.MrcState.CONNECTED)
        try:
            await bridge.open_room("garden", "alice")
            session = FakeSession(["n", "c", "b", "b", "b"])
            await admin_menu(session, lane, sysop, node_controls=_controls(bridge))
            text = _visible(_written_text(session))
            assert "Open rooms: 1 of 8 open, retired after 2 idle days" in text
            assert "mrc:garden -> #garden (open room)" in text
        finally:
            await bridge.close()
            await fake.close()
    asyncio.run(scenario())
