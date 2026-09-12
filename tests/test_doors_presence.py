"""Which door a caller is in, on Who's online (issue #470, presence half).

"3 callers in Blacksite" is the recruitment a multiplayer door gets from the
BBS it runs on; before this there was no door presence at all, not even
"in a door".
"""

from __future__ import annotations

import asyncio
import sys

from netbbs.chat.presence import PresenceRegistry
from netbbs.doors import create_door
from netbbs.doors.profiles import DoorProfile
from netbbs.net.door_flow import browse_doors
from tests.test_doors_runtime import FakeSession, _write_script, db, lane, player


def _summary(username="keeper"):
    from netbbs.net.session_registry import SessionSummary
    return SessionSummary(session=FakeSession(), session_id=1, username=username,
                          connected_at="2026-09-12T10:00:00.000000Z", peer_address="127.0.0.1")


def test_nobody_is_playing_anything_by_default():
    presence = PresenceRegistry()
    presence.enter("carrier")
    assert presence.door_of("carrier") is None


def test_entering_and_leaving_a_door_is_reported():
    presence = PresenceRegistry()
    presence.enter("carrier")

    presence.enter_door("carrier", "LORD")
    assert presence.door_of("carrier") == "LORD"

    presence.leave_door("carrier", "LORD")
    assert presence.door_of("carrier") is None


def test_two_sessions_in_different_doors_do_not_erase_each_other():
    """An account can be connected twice; the answer must not depend on
    which session happened to leave its door first."""
    presence = PresenceRegistry()
    presence.enter("carrier")
    presence.enter("carrier")
    presence.enter_door("carrier", "LORD")
    presence.enter_door("carrier", "TradeWars")

    presence.leave_door("carrier", "TradeWars")

    assert presence.door_of("carrier") == "LORD", "the other session is still playing"


def test_leaving_a_door_nobody_entered_is_harmless():
    presence = PresenceRegistry()
    presence.leave_door("ghost", "LORD")
    assert presence.door_of("ghost") is None


def test_a_final_disconnect_clears_a_stranded_door_entry():
    """A session killed mid-door must not leave the account playing forever."""
    presence = PresenceRegistry()
    presence.enter("carrier")
    presence.enter_door("carrier", "LORD")

    presence.leave("carrier")

    assert presence.door_of("carrier") is None


def test_who_is_online_names_the_door(db):
    from netbbs.net.directory_flow import _who_entry_description

    presence = PresenceRegistry()
    presence.enter("keeper")
    entry = _summary()

    assert "playing" not in _who_entry_description(db, entry, presence)

    presence.enter_door("keeper", "Blacksite")
    described = _who_entry_description(db, entry, presence)

    assert described.startswith("playing Blacksite"), described
    assert "connected since" in described, "the existing information is kept"


def test_the_sysop_who_screen_names_the_door_too():
    from netbbs.net.admin_flow import _session_description

    presence = PresenceRegistry()
    presence.enter("keeper")
    presence.enter_door("keeper", "Blacksite")
    entry = _summary()

    assert _session_description(entry, "%Y-%m-%d", "UTC", presence).startswith("playing Blacksite")
    assert "playing" not in _session_description(entry, "%Y-%m-%d", "UTC")


def test_a_real_launch_records_and_clears_presence(db, lane, player, tmp_path):
    """End to end through browse_doors, sampled while the door is running.

    The door announces itself, the session records what presence says at that
    moment, and the door then exits non-zero -- so this also proves the crash
    path clears the entry rather than stranding it.
    """
    script = _write_script(tmp_path, "announce.py", """
        import sys
        sys.stdout.write("READY\\n"); sys.stdout.flush()
        raise SystemExit(3)
    """)
    door = create_door(db, "Boom", sys.executable, args=(str(script),), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path)))
    presence = PresenceRegistry()
    presence.enter(player.username)
    during = []

    class SamplingSession(FakeSession):
        async def write_raw(self, data):
            if b"READY" in bytes(data):
                during.append(presence.door_of(player.username))
            await super().write_raw(data)

        async def read_any_key(self):
            return "\r"

    async def scenario():
        import netbbs.net.door_flow as flow
        picks = iter([door, None])
        original = flow.pick_item

        async def fake_pick(*args, **kwargs):
            return next(picks, None)

        flow.pick_item = fake_pick
        try:
            await browse_doors(SamplingSession(), lane, player, presence=presence)
        finally:
            flow.pick_item = original

    asyncio.run(scenario())

    assert during == ["Boom"], f"presence while the door ran: {during}"
    assert presence.door_of(player.username) is None, "a crashed door left the caller playing"
