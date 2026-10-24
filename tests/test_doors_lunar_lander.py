"""Lunar Lander as a bundled door (issue #1542).

The door keeps its pilot records under `DOOR_LL_DATA_DIR`, defaulting to
`data/` beside the script -- for the bundled copy, inside the installed
package. A bundled launch therefore points it at the node's own
`<db>.doors/lunar_lander/`, and a guest call at a copy of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import sys

import pytest

from netbbs.auth.users import create_user
from netbbs.doors import create_door
from netbbs.doors.bundled import BUNDLED_DOORS, launched_bundled_door, resolve_bundled_door_path
from netbbs.doors.bundled import lunar_lander as ll
from netbbs.doors.profiles import DoorProfile
from netbbs.doors.runtime import lunar_lander_records_dir, node_lunar_lander_dir, run_door
from netbbs.guest import set_guest_user
from netbbs.guest_call import guest_call
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_doors_runtime import _BUNDLED_DOORS_DIR, FakeSession

_SCRIPT = _BUNDLED_DOORS_DIR / "lunar_lander.py"


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
def player(db):
    return create_user(db, "pilot", password="hunter2pw", user_level=10)


def _pilots(path):
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return sorted(row[0] for row in conn.execute("SELECT handle FROM pilots"))


def test_lunar_lander_is_catalogued_and_follows_resize():
    entry = next(door for door in BUNDLED_DOORS if door.key == "lunar_lander")
    path = resolve_bundled_door_path(entry)
    assert path is not None and entry.follows_resize
    assert launched_bundled_door(sys.executable, (path.as_posix(),)) is entry
    assert launched_bundled_door(sys.executable, ("-m", "netbbs.doors.bundled.lunar_lander")) is entry


def test_the_records_directory_is_the_nodes_own_unless_a_profile_names_one(db, player, tmp_path):
    door = create_door(db, "Lunar Lander", sys.executable, args=(str(_SCRIPT),), creator=player)
    assert lunar_lander_records_dir(db, door) == db.path.resolve().parent / "node.db.doors" / "lunar_lander"
    chosen = tmp_path / "chosen"
    profiled = create_door(db, "Profiled", sys.executable, args=(str(_SCRIPT),), creator=player,
                           profile=DoorProfile(environment={"DOOR_LL_DATA_DIR": str(chosen)}))
    assert lunar_lander_records_dir(db, profiled) == chosen.resolve()
    # Somebody else's door, or somebody's copy of this one, is not ours to place.
    fork = tmp_path / "lunar_lander.py"
    fork.write_bytes(_SCRIPT.read_bytes())
    other = create_door(db, "Fork", sys.executable, args=(str(fork),), creator=player)
    assert lunar_lander_records_dir(db, other) is None


def _fly_and_quit(session, lane, door, player, *, as_guest=False):
    """Launch the real door, wait for its mission briefing, and leave."""
    session.terminal_width, session.terminal_height = 80, 24
    if as_guest:
        session.authenticated_without_credential = True

    async def scenario():
        with guest_call() as call:
            task = asyncio.create_task(run_door(session, lane, door, player, wall_time_limit_seconds=60))
            for _ in range(3000):
                if b"FLIGHT DIRECTOR" in session.written or task.done():
                    break
                await asyncio.sleep(0.01)
            session.type_in("q")
            result = await task
            sandbox = call.directory / "doors" / "lunar_lander" / "pilots.db"
            return result, (_pilots(sandbox) if sandbox.exists() else None)

    return asyncio.run(scenario())


def test_a_bundled_launch_keeps_records_beside_the_node_database(db, lane, player):
    door = create_door(db, "Lunar Lander", sys.executable, args=(str(_SCRIPT),), creator=player)
    result, _ = _fly_and_quit(FakeSession(), lane, door, player)
    assert result.reason == "exited" and result.exit_code == 0, result.diagnostic
    assert _pilots(node_lunar_lander_dir(db.path) / "pilots.db") == []  # created, nobody landed
    # Never inside the installed package, which an upgrade replaces.
    assert not (_SCRIPT.parent / "data").exists()


def test_a_guest_flies_against_a_copy_of_the_records(db, lane, tmp_path):
    guest = create_user(db, "guest", password="hunter2pw", user_level=10)
    set_guest_user(db, guest)
    records = node_lunar_lander_dir(db.path) / "pilots.db"
    real = ll.Records(records)
    try:
        real.identify(ll.Host(handle="Armstrong", node="elsewhere", user=7))
    finally:
        real.close()
    door = create_door(db, "Lunar Lander", sys.executable, args=(str(_SCRIPT),), creator=guest)
    result, sandbox_pilots = _fly_and_quit(FakeSession(), lane, door, guest, as_guest=True)
    assert result.reason == "exited" and result.exit_code == 0, result.diagnostic
    # The call played against its own copy of the node's records.
    assert sandbox_pilots == ["Armstrong"]
    assert _pilots(records) == ["Armstrong"]


def test_a_guest_on_a_node_without_records_creates_none_of_the_nodes(db, lane):
    guest = create_user(db, "guest", password="hunter2pw", user_level=10)
    set_guest_user(db, guest)
    door = create_door(db, "Lunar Lander", sys.executable, args=(str(_SCRIPT),), creator=guest)
    result, sandbox_pilots = _fly_and_quit(FakeSession(), lane, door, guest, as_guest=True)
    assert result.reason == "exited" and result.exit_code == 0, result.diagnostic
    assert sandbox_pilots == []  # the door made its records in the call's directory
    assert not node_lunar_lander_dir(db.path).exists()
