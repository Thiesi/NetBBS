"""Real draft editor: Back does not save, invalid fields retain the draft."""
import asyncio
import json
import sys

import pytest

from netbbs.doors.registry import create_door, get_door_by_name
from netbbs.doors.profiles import DoorProfile
from netbbs.net.door_profile_flow import edit_door_profile
from tests.test_door_flow import FakeSession, db, lane, player


def test_compatibility_back_does_not_add_profile(db, lane, player):
    door = create_door(db, "Untouched", sys.executable, creator=player)
    session = FakeSession(["b"])
    assert asyncio.run(edit_door_profile(session, lane, player, door)) is None
    assert get_door_by_name(db, door.name) == door
    assert "External installs are manual" in "".join(session.written)


def test_compatibility_invalid_save_keeps_editor_open(db, lane, player):
    door = create_door(db, "Invalid draft", sys.executable, creator=player)
    session = FakeSession(["w", "not a number", "s", "b", "y"])
    assert asyncio.run(edit_door_profile(session, lane, player, door)) is None
    assert get_door_by_name(db, door.name) == door
    assert "whole number" in "".join(session.written)
    assert not session._inputs


def test_compatibility_explicit_save_persists_profile(db, lane, player):
    door = create_door(db, "Saved draft", sys.executable, creator=player)
    session = FakeSession(["s"])
    saved = asyncio.run(edit_door_profile(session, lane, player, door))
    assert saved.profile.adapter == "native"
    assert get_door_by_name(db, door.name).profile == saved.profile


@pytest.mark.parametrize("save", [False, True])
def test_restore_original_api_only_on_save(save, db, lane, player):
    door = create_door(db, "Restore", sys.executable, args=("original.py",),
                       description="Keep me", min_play_level=10, pinned=True,
                       creator=player, profile=DoorProfile(encoding="cp437"))
    session = FakeSession(["1", "s"] if save else ["1", "b", "y"])
    result = asyncio.run(edit_door_profile(session, lane, player, door))
    stored = get_door_by_name(db, door.name)
    if save:
        from dataclasses import replace
        assert result == stored == replace(door, profile=None)
        assert db.connection.execute("SELECT profile_json FROM doors WHERE id=?", (door.id,)).fetchone()[0] is None
    else:
        assert result is None
        assert stored == door
    assert not session._inputs


def test_ordinary_door_update_preserves_profile(db, player):
    from netbbs.doors.registry import update_door
    door = create_door(db, "Keep profile", sys.executable, creator=player, profile=DoorProfile(encoding="cp437"))
    updated = update_door(db, door, name="Renamed", description=None, executable_path=door.executable_path,
                          args=(), min_play_level=0, pinned=False, community_id=None, changed_by=player)
    assert updated.profile == door.profile


@pytest.mark.parametrize("value, message", [({"profile":{},"args":"wrong"},"array of strings"),
                                          ({"profile":{},"executable_path":42},"executable_path"),
                                          ({"profile":{"endpoint":"socketpair"}},"DOOR32.SYS"),
                                          ([],"JSON object")])
def test_invalid_import_preserves_existing_draft(db, lane, player, tmp_path, value, message):
    path = tmp_path / "import.json"
    path.write_text(json.dumps(value))
    door = create_door(db,"Import",sys.executable,creator=player)
    session = FakeSession(["j",str(path),"b"])
    assert asyncio.run(edit_door_profile(session,lane,player,door)) is None
    assert get_door_by_name(db,door.name) == door
    assert message in "".join(session.written)
