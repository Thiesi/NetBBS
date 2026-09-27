"""Issue #726 part 2: War Dialer Next season / Reset competition in the console.

The maintainer relaxed the CLI's stopped-node rule for this route: maintenance
on, a fresh verified live backup, a reason and the typed world filename."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.backup import BackupError, write_pid_file
from netbbs.doors.war_dialer_admin import change_competition, set_maintenance, world_status
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.moderation.log import list_recent_actions
from netbbs.net.admin_flow import admin_menu
from tests.test_admin_flow import (  # noqa: F401 -- fixtures
    FakeSession,
    _node_controls,
    _normalized_visible,
    _war_dialer_door,
    _war_dialer_world,
    _war_dialer_world_keys,
    _written_text,
    db,
    isolated_door_career_directory,
    lane,
    sysop,
)


@pytest.fixture
def identity_dir(tmp_path):
    directory = tmp_path / "identity"
    bootstrap_node_identity("test-node").save(directory)
    return directory


def _live(session, lane, sysop, identity_dir):
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=identity_dir)))


def _with_receipt(db, path):
    """Give the world one retained receipt, as a played world has."""
    from netbbs.auth.users import get_user_by_username
    from netbbs.doors.bundled import war_dialer as wd

    pilot = get_user_by_username(db, "WarPilot")
    conn = wd.connect(path)
    try:
        wd.record_event(conn, pilot.id, "Rival", "A retained receipt", wd.now_utc())
    finally:
        conn.close()
    return path


def _backups(db):
    root = db.path.parent / (db.path.stem + "_backups")
    return sorted(root.iterdir()) if root.exists() else []


def test_the_standalone_console_points_at_the_cli_instead(db, lane, sysop):
    _war_dialer_door(db, sysop)
    _war_dialer_world(db)

    session = FakeSession(_war_dialer_world_keys("n"))
    asyncio.run(admin_menu(session, lane, sysop))
    text = _normalized_visible(_written_text(session))

    assert "Next season and Reset competition run from a live node's console" in text
    assert "[N]ext season" not in text


def test_next_season_needs_maintenance_first(db, lane, sysop, identity_dir):
    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)

    session = FakeSession(_war_dialer_world_keys("n"))
    _live(session, lane, sysop, identity_dir)
    text = _normalized_visible(_written_text(session))

    assert "Next season needs maintenance on first" in text
    assert world_status(db.path, path)["stored_season"] == "1"
    assert _backups(db) == []


def test_next_season_runs_on_a_live_node_behind_a_backup(db, lane, sysop, identity_dir):
    """The node's PID file is present, as on a running node: the CLI refuses
    there, the console does not."""
    door = _war_dialer_door(db, sysop)
    path = _with_receipt(db, _war_dialer_world(db))
    set_maintenance(db.path, path, True)
    write_pid_file(db.path)
    with pytest.raises(BackupError, match="running"):
        change_competition(db.path, path, identity_dir=identity_dir, backup_to=db.path.parent / "cli-backup",
                           confirm=path.name, reason="cli", reset=False)

    session = FakeSession(_war_dialer_world_keys("n", "monthly rollover", path.name))
    _live(session, lane, sysop, identity_dir)
    text = _normalized_visible(_written_text(session))

    assert "Next season done: season 2 has started." in text
    status = world_status(db.path, path)
    assert status["stored_season"] == "2"
    assert status["maintenance"] == "on", "maintenance stays on until the SysOp reopens the world"
    operation = status["recent_operations"][-1]
    assert (operation["action"], operation["operator"], operation["reason"]) == (
        "advance season", "sysop", "monthly rollover")
    [backup] = _backups(db)
    assert (backup / "manifest.json").exists()
    assert operation["backup"] == str(backup.resolve())
    entry = list_recent_actions(db, limit=1)[0]
    assert (entry.action, entry.object_type, entry.object_id) == ("war_dialer_season", "door", door.id)
    assert status["events"] > 0, "a season keeps receipts"


def test_reset_competition_also_clears_receipts(db, lane, sysop, identity_dir):
    _war_dialer_door(db, sysop)
    path = _with_receipt(db, _war_dialer_world(db))
    set_maintenance(db.path, path, True)
    assert world_status(db.path, path)["events"] > 0

    session = FakeSession(_war_dialer_world_keys("c", "fresh start", path.name))
    _live(session, lane, sysop, identity_dir)

    status = world_status(db.path, path)
    assert status["stored_season"] == "2"
    assert status["events"] == 0
    assert status["recent_operations"][-1]["action"] == "reset competition"
    assert list_recent_actions(db, limit=1)[0].action == "war_dialer_reset"


@pytest.mark.parametrize("answers", [("",), ("a reason", "wrong.db")], ids=["blank-reason", "wrong-filename"])
def test_cancelling_changes_nothing_and_takes_no_backup(db, lane, sysop, identity_dir, answers):
    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)

    session = FakeSession(_war_dialer_world_keys("n", *answers))
    _live(session, lane, sysop, identity_dir)

    assert "Cancelled. Nothing was changed." in _normalized_visible(_written_text(session))
    assert world_status(db.path, path)["stored_season"] == "1"
    assert _backups(db) == []


def test_a_caller_still_inside_stops_the_change(db, lane, sysop, identity_dir):
    from netbbs.doors.bundled import war_dialer as wd

    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)

    session = FakeSession(_war_dialer_world_keys("n", "rollover", path.name))
    with wd.world_session(path):
        _live(session, lane, sysop, identity_dir)
    text = _normalized_visible(_written_text(session))

    assert "Next season failed:" in text
    assert world_status(db.path, path)["stored_season"] == "1"


def test_an_overlong_reason_is_refused(db, lane, sysop, identity_dir):
    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)

    session = FakeSession(_war_dialer_world_keys("n", "x" * 241))
    _live(session, lane, sysop, identity_dir)

    assert "longer than 240 characters" in _normalized_visible(_written_text(session))
    assert world_status(db.path, path)["stored_season"] == "1"
