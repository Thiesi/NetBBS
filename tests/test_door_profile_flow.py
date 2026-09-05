"""Real draft editor: Back does not save, invalid fields retain the draft."""
import asyncio
import json
import sys

import pytest

from netbbs.doors.registry import create_door, get_door_by_name
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


@pytest.mark.parametrize("value, message", [({"profile":{},"args":"wrong"},"array of strings"),
                                          ({"profile":{},"executable_path":42},"executable_path"),
                                          ([],"JSON object")])
def test_invalid_import_preserves_existing_draft(db, lane, player, tmp_path, value, message):
    path = tmp_path / "import.json"
    path.write_text(json.dumps(value))
    door = create_door(db,"Import",sys.executable,creator=player)
    session = FakeSession(["j",str(path),"b"])
    assert asyncio.run(edit_door_profile(session,lane,player,door)) is None
    assert get_door_by_name(db,door.name) == door
    assert message in "".join(session.written)
