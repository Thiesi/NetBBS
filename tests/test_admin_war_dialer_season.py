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

    text = _normalized_visible(_written_text(session))
    # A blank reason is a plain cancel; a filename that doesn't match says
    # what was typed (issue #1119). Neither changes anything.
    assert "Cancelled. Nothing was changed." in text or "Cancelled: 'wrong.db' is not" in text
    assert "Nothing was changed." in text
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


# -- review round 1 (PR #744) ----------------------------------------------


def test_the_node_audit_records_the_effective_seasons_of_an_idle_world(db, lane, sysop, identity_dir, monkeypatch):
    """A world idle past natural cutoffs settles them first; the node audit
    must record what the change did (3->4), not the stale stored season."""
    from datetime import timedelta

    from netbbs.doors.bundled import war_dialer as wd

    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)
    later = wd.now_utc() + timedelta(days=70)
    monkeypatch.setattr(wd, "now_utc", lambda: later)

    session = FakeSession(_war_dialer_world_keys("n", "catch up", path.name))
    _live(session, lane, sysop, identity_dir)

    assert world_status(db.path, path)["stored_season"] == "4"
    entry = list_recent_actions(db, limit=1)[0]
    assert "season=3->4" in entry.detail
    assert "season 4 has started" in _normalized_visible(_written_text(session))


def test_a_second_concurrent_change_is_refused(db, lane, sysop, identity_dir):
    from netbbs.net import admin_flow

    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)

    session = FakeSession(_war_dialer_world_keys("n", "rollover", path.name))
    with admin_flow._WAR_DIALER_COMPETITION_LOCK:
        _live(session, lane, sysop, identity_dir)

    assert "another SysOp is changing a War Dialer competition" in _normalized_visible(_written_text(session))
    assert world_status(db.path, path)["stored_season"] == "1"
    assert _backups(db) == []


def test_a_dropped_session_still_records_the_change_it_made(db, lane, sysop, identity_dir, monkeypatch):
    import threading

    from netbbs.net import admin_flow

    door = _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)
    real = admin_flow._war_dialer_change_competition

    # The change begins, then holds until the session has been dropped: no
    # window of time the test has to hit, loaded machine or not.
    started = threading.Event()
    dropped = threading.Event()

    def _slow(**kwargs):
        started.set()
        dropped.wait(30.0)
        return real(**kwargs)

    monkeypatch.setattr(admin_flow, "_war_dialer_change_competition", _slow)
    status = world_status(db.path, path)

    async def scenario():
        session = FakeSession(["rollover", path.name])
        task = asyncio.create_task(admin_flow._war_dialer_competition_flow(
            session, lane, sysop, door, path, status, reset=False, identity_dir=identity_dir, db_path=db.path))
        # The session drops while the change runs -- once it has begun, not
        # after a fixed time, which a loaded machine may not have reached.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30.0
        while not started.is_set() and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "the season change never began"
        task.cancel()
        dropped.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert world_status(db.path, path)["stored_season"] == "2"
    entry = list_recent_actions(db, limit=1)[0]
    assert (entry.action, entry.object_id) == ("war_dialer_season", door.id)


def test_a_backup_that_failed_verification_is_not_called_a_backup(db, lane, sysop, identity_dir, monkeypatch):
    import netbbs.backup as backup_module

    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)

    def _reject(source, *, allow_migrate):
        raise BackupError("checksum mismatch")

    monkeypatch.setattr(backup_module, "_validate_backup_source", _reject)
    session = FakeSession(_war_dialer_world_keys("n", "rollover", path.name))
    _live(session, lane, sysop, identity_dir)
    text = _normalized_visible(_written_text(session))

    assert "Next season failed: checksum mismatch" in text
    assert "is not a verified backup" in text
    assert "verified backup taken first remains" not in text
    assert world_status(db.path, path)["stored_season"] == "1"


def test_maintenance_cannot_be_switched_while_a_competition_change_runs(db, lane, sysop):
    """Codex review, PR #744: switching maintenance off mid-change would let
    a caller in between the backup and the commit."""
    from netbbs.net import admin_flow

    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)

    session = FakeSession(_war_dialer_world_keys("m"))
    with admin_flow._WAR_DIALER_COMPETITION_LOCK:
        asyncio.run(admin_menu(session, lane, sysop))

    assert "competition change is running" in _normalized_visible(_written_text(session))
    assert world_status(db.path, path)["maintenance"] == "on"


def test_a_session_dropped_while_the_audit_is_written_still_records_it(db, lane, sysop, identity_dir, monkeypatch):
    """Codex review, PR #744: cancellation after the change but during the
    node-audit write must not abort that write."""
    import threading
    import time

    from netbbs.net import admin_flow

    door = _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)
    real = admin_flow.record_action
    writing = threading.Event()

    def _slow_record(*args, **kwargs):
        writing.set()
        time.sleep(0.4)
        return real(*args, **kwargs)

    monkeypatch.setattr(admin_flow, "record_action", _slow_record)
    status = world_status(db.path, path)

    async def scenario():
        session = FakeSession(["rollover", path.name])
        task = asyncio.create_task(admin_flow._war_dialer_competition_flow(
            session, lane, sysop, door, path, status, reset=False, identity_dir=identity_dir, db_path=db.path))
        while not writing.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    entry = list_recent_actions(db, limit=1)[0]
    assert (entry.action, entry.object_id) == ("war_dialer_season", door.id)


def test_the_backup_goes_to_the_configured_destination(db, lane, sysop, identity_dir, tmp_path):
    """Issue #727's destination applies to the season backup too."""
    from netbbs.backup_schedule import set_destination_setting

    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)
    elsewhere = tmp_path / "second-disk"
    elsewhere.mkdir()
    set_destination_setting(db, elsewhere, db_path=db.path, identity_dir=identity_dir)

    session = FakeSession(_war_dialer_world_keys("n", "rollover", path.name))
    _live(session, lane, sysop, identity_dir)

    assert world_status(db.path, path)["stored_season"] == "2"
    [backup] = sorted(elsewhere.iterdir())
    assert (backup / "manifest.json").exists()
    assert _backups(db) == []


def test_a_destination_that_is_gone_stops_the_change(db, lane, sysop, identity_dir, tmp_path):
    from netbbs.backup_schedule import set_destination_setting

    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)
    elsewhere = tmp_path / "unmounted"
    elsewhere.mkdir()
    set_destination_setting(db, elsewhere, db_path=db.path, identity_dir=identity_dir)
    elsewhere.rmdir()

    session = FakeSession(_war_dialer_world_keys("n"))
    _live(session, lane, sysop, identity_dir)

    assert "needs a usable backup destination first" in _normalized_visible(_written_text(session))
    assert world_status(db.path, path)["stored_season"] == "1"
    assert not elsewhere.exists()


def test_the_worker_rechecks_the_destination_right_before_the_backup(db, lane, sysop, identity_dir, tmp_path, monkeypatch):
    """Codex review, PR #744: a disk unmounted between the screen's check and
    the worker must stop the change, not get its mount point recreated."""
    from netbbs.backup_schedule import set_destination_setting
    from netbbs.net import admin_flow

    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)
    elsewhere = tmp_path / "usb"
    elsewhere.mkdir()
    set_destination_setting(db, elsewhere, db_path=db.path, identity_dir=identity_dir)
    real = admin_flow._war_dialer_change_competition

    def _unmount_first(**kwargs):
        elsewhere.rmdir()
        return real(**kwargs)

    monkeypatch.setattr(admin_flow, "_war_dialer_change_competition", _unmount_first)
    session = FakeSession(_war_dialer_world_keys("n", "rollover", path.name))
    _live(session, lane, sysop, identity_dir)

    assert "Next season failed:" in _normalized_visible(_written_text(session))
    assert world_status(db.path, path)["stored_season"] == "1"
    assert not elsewhere.exists(), "the mount point was not recreated"


def test_a_failed_node_audit_still_reports_the_change_as_done(db, lane, sysop, identity_dir, monkeypatch):
    """Codex review, PR #744: the rollover committed, so the SysOp is told it
    is done, and separately that the node audit entry is missing."""
    import sqlite3

    from netbbs.net import admin_flow

    _war_dialer_door(db, sysop)
    path = _war_dialer_world(db)
    set_maintenance(db.path, path, True)

    def _disk_full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(admin_flow, "record_action", _disk_full)
    session = FakeSession(_war_dialer_world_keys("n", "rollover", path.name))
    _live(session, lane, sysop, identity_dir)
    text = _normalized_visible(_written_text(session))

    assert "Next season done: season 2 has started." in text
    assert "Audit log entry for it could not be written (database or disk is full)" in text
    assert world_status(db.path, path)["stored_season"] == "2"
