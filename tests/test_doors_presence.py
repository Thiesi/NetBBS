"""Which door a caller is in, on Who's online (issue #470, presence half).

"3 callers in Blacksite" is the recruitment a multiplayer door gets from the
BBS it runs on; before this there was no door presence at all, not even
"in a door".

Keyed by session, not by account: both Who screens render a row per session
and the SysOp one disconnects the row that is selected.
"""

from __future__ import annotations

import asyncio
import gc
import sys

from netbbs.chat.presence import PresenceRegistry
from netbbs.doors import create_door
from netbbs.doors.profiles import DoorProfile
from netbbs.net.door_flow import browse_doors
from tests.test_doors_runtime import FakeSession, _write_script, db, lane, player


def _summary(session, username="keeper", session_id=1):
    from netbbs.net.session_registry import SessionSummary
    return SessionSummary(session=session, session_id=session_id, username=username,
                          connected_at="2026-09-12T10:00:00.000000Z", peer_address="127.0.0.1")


def test_no_session_is_playing_anything_by_default():
    assert PresenceRegistry().door_of(FakeSession()) is None


def test_entering_and_leaving_a_door_is_reported():
    presence, session = PresenceRegistry(), FakeSession()

    presence.enter_door(session, 1, "LORD")
    assert presence.door_of(session) == (1, "LORD")

    presence.leave_door(session)
    assert presence.door_of(session) is None


def test_one_account_with_two_sessions_reports_each_separately():
    """The reason this is keyed by session: an idle connection must not claim
    the door its sibling is playing, or the SysOp cannot tell which row to
    disconnect and the apparent player count is inflated."""
    presence = PresenceRegistry()
    playing, idle = FakeSession(), FakeSession()
    presence.enter("carrier")
    presence.enter("carrier")

    presence.enter_door(playing, 1, "LORD")

    assert presence.door_of(playing) == (1, "LORD")
    assert presence.door_of(idle) is None, "the idle session claimed its sibling's door"


def test_two_sessions_in_different_doors_do_not_erase_each_other():
    presence = PresenceRegistry()
    first, second = FakeSession(), FakeSession()
    presence.enter_door(first, 1, "LORD")
    presence.enter_door(second, 2, "TradeWars")

    presence.leave_door(second)

    assert presence.door_of(first) == (1, "LORD")
    assert presence.door_of(second) is None


def test_leaving_a_door_nobody_entered_is_harmless():
    presence = PresenceRegistry()
    presence.leave_door(FakeSession())


def test_a_vanished_session_cannot_stay_listed_as_playing():
    """Weak-keyed, so a session which died without a clean exit drops out --
    and a later object cannot inherit its door through a reused identity."""
    presence = PresenceRegistry()
    session = FakeSession()
    presence.enter_door(session, 1, "LORD")

    del session
    gc.collect()

    assert len(presence._doors) == 0


def test_who_is_online_names_the_door(db, player, tmp_path):
    from netbbs.net.directory_flow import _who_entry_description

    door = create_door(db, "Blacksite", sys.executable, args=(), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path)))
    presence, session = PresenceRegistry(), FakeSession()
    entry = _summary(session)

    assert "playing" not in _who_entry_description(db, entry, presence, player)

    presence.enter_door(session, door.id, door.name)
    described = _who_entry_description(db, entry, presence, player)

    assert described.startswith("playing Blacksite"), described
    assert "connected since" in described, "the existing information is kept"


def test_who_is_online_hides_a_door_the_viewer_may_not_play(db, player, tmp_path):
    """The picker already hides a restricted door; naming it here would
    advertise a SysOp-only game to someone who cannot open it."""
    from netbbs.auth.users import create_user
    from netbbs.net.directory_flow import _who_entry_description

    restricted = create_door(db, "SysOp Only", sys.executable, args=(), creator=player,
                             min_play_level=255, profile=DoorProfile(install_dir=str(tmp_path)))
    presence, session = PresenceRegistry(), FakeSession()
    presence.enter_door(session, restricted.id, restricted.name)
    entry = _summary(session)

    ordinary = create_user(db, "ordinary", password="hunter2", user_level=10)
    sysop = create_user(db, "chief", password="hunter2", user_level=255)

    assert "SysOp Only" not in _who_entry_description(db, entry, presence, ordinary)
    assert "playing" not in _who_entry_description(db, entry, presence, ordinary)
    assert "playing SysOp Only" in _who_entry_description(db, entry, presence, sysop)


def test_who_is_online_does_not_name_a_deleted_door(db, player, tmp_path):
    from netbbs.doors import delete_door
    from netbbs.net.directory_flow import _who_entry_description

    door = create_door(db, "Gone", sys.executable, args=(), creator=player,
                       profile=DoorProfile(install_dir=str(tmp_path)))
    presence, session = PresenceRegistry(), FakeSession()
    presence.enter_door(session, door.id, door.name)
    delete_door(db, door, deleted_by=player)

    assert "playing" not in _who_entry_description(db, _summary(session), presence, player)


def test_the_sysop_who_screen_names_the_door_too():
    from netbbs.net.admin_flow import _session_description

    presence, session = PresenceRegistry(), FakeSession()
    presence.enter_door(session, 7, "Blacksite")
    entry = _summary(session)

    assert _session_description(entry, "%Y-%m-%d", "UTC", presence).startswith("playing Blacksite")
    assert "playing" not in _session_description(entry, "%Y-%m-%d", "UTC")


def test_the_sysop_who_screen_distinguishes_two_sessions_of_one_account():
    """What the finding was actually about: telling the rows apart."""
    from netbbs.net.admin_flow import _session_description

    presence = PresenceRegistry()
    playing, idle = FakeSession(), FakeSession()
    presence.enter_door(playing, 7, "Blacksite")

    assert "playing" in _session_description(_summary(playing, session_id=1), "%Y-%m-%d", "UTC", presence)
    assert "playing" not in _session_description(_summary(idle, session_id=2), "%Y-%m-%d", "UTC", presence)


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
    during = []

    class SamplingSession(FakeSession):
        async def write_raw(self, data):
            if b"READY" in bytes(data):
                during.append(presence.door_of(self))
            await super().write_raw(data)

        async def read_any_key(self):
            return "\r"

    session = SamplingSession()

    async def scenario():
        import netbbs.net.door_flow as flow
        picks = iter([door, None])
        original = flow.pick_item

        async def fake_pick(*args, **kwargs):
            return next(picks, None)

        flow.pick_item = fake_pick
        try:
            await browse_doors(session, lane, player, presence=presence)
        finally:
            flow.pick_item = original

    asyncio.run(scenario())

    assert [name for _, name in during] == ["Boom"], f"presence while the door ran: {during}"
    assert presence.door_of(session) is None, "a crashed door left the session playing"
