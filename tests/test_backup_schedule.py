"""Issue #727: scheduled backups, their retention, and the backup destination."""

from __future__ import annotations

import asyncio
import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from netbbs import backup_schedule as bs
from netbbs.backup_schedule import (
    BackupSchedule,
    BackupScheduleError,
    backup_root,
    due_slot,
    latest_slot,
    list_scheduled_backups,
    load_schedule,
    next_slot,
    parse_time,
    prune_scheduled_backups,
    record_scheduled_backup,
    run_backup_scheduler,
    run_scheduled_backup_pass,
    save_schedule,
    schedule_status,
    set_destination_setting,
    validate_destination,
)
from netbbs.config import get_config
from netbbs.operational_history import list_operational_run_history
from netbbs.storage.database import Database
from netbbs.timeutil import set_display_timezone

UTC = datetime.timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture(autouse=True)
def isolated_voidrunner_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("VOIDRUNNER_SAVE_DIR", str(tmp_path / "voidrunner-careers"))


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "node.db"
    Database(path).close()
    return path


@pytest.fixture
def identity_dir(tmp_path):
    path = tmp_path / "identity"
    path.mkdir()
    (path / "marker").write_text("identity")
    return path


def _at(year, month, day, hour, minute, tz=UTC) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=tz)


def _with_db(db_path, fn):
    db = Database(db_path)
    try:
        return fn(db)
    finally:
        db.close()


# -- slots -------------------------------------------------------------------


def test_daily_slots_are_local_wall_clock_times():
    schedule = BackupSchedule(frequency="daily", hour=3, minute=0)
    # 01:30 UTC on 2026-09-27 is 03:30 in Berlin (CEST, +2): today's 03:00 has passed.
    now = _at(2026, 9, 27, 1, 30)
    assert latest_slot(schedule, now, BERLIN) == _at(2026, 9, 27, 3, 0, BERLIN)
    assert next_slot(schedule, now, BERLIN) == _at(2026, 9, 28, 3, 0, BERLIN)
    # 00:30 UTC is 02:30 local: today's slot is still ahead.
    earlier = _at(2026, 9, 27, 0, 30)
    assert latest_slot(schedule, earlier, BERLIN) == _at(2026, 9, 26, 3, 0, BERLIN)


def test_weekly_slot_lands_on_the_chosen_weekday():
    schedule = BackupSchedule(frequency="weekly", hour=4, minute=15, weekday=0)  # Monday
    now = _at(2026, 9, 27, 12, 0)  # a Sunday
    assert latest_slot(schedule, now, UTC) == _at(2026, 9, 21, 4, 15)
    assert next_slot(schedule, now, UTC) == _at(2026, 9, 28, 4, 15)
    on_the_day_before = _at(2026, 9, 28, 4, 0)
    assert latest_slot(schedule, on_the_day_before, UTC) == _at(2026, 9, 21, 4, 15)


def test_daily_slot_across_a_dst_change_keeps_the_wall_clock_time():
    schedule = BackupSchedule(frequency="daily", hour=3, minute=0)
    # Berlin leaves summer time on 2026-10-25.
    after = next_slot(schedule, _at(2026, 10, 24, 12, 0), BERLIN)
    assert after.astimezone(BERLIN).hour == 3
    assert after.astimezone(UTC) == _at(2026, 10, 25, 2, 0)


def test_off_has_no_slots():
    assert latest_slot(BackupSchedule(), _at(2026, 9, 27, 0, 0), UTC) is None
    assert next_slot(BackupSchedule(), _at(2026, 9, 27, 0, 0), UTC) is None


@pytest.mark.parametrize("text", ["3", "25:00", "03:60", "aa:bb", ""])
def test_bad_times_are_refused(text):
    with pytest.raises(BackupScheduleError):
        parse_time(text)


def test_keep_and_frequency_are_validated(db_path):
    db = Database(db_path)
    try:
        with pytest.raises(BackupScheduleError):
            save_schedule(db, BackupSchedule(frequency="hourly"))
        with pytest.raises(BackupScheduleError):
            save_schedule(db, BackupSchedule(frequency="daily", keep=0))
        assert load_schedule(db) == BackupSchedule()
    finally:
        db.close()


def test_saving_never_fires_for_a_slot_already_past(db_path):
    """Switching a schedule on at 10:00 with a 03:00 slot must not back up at
    once: counting starts from the save."""
    db = Database(db_path)
    try:
        save_schedule(db, BackupSchedule(frequency="daily", hour=3), now=_at(2026, 9, 27, 10, 0))
        assert due_slot(db, _at(2026, 9, 27, 10, 1)) is None
        assert due_slot(db, _at(2026, 9, 28, 3, 0)) == _at(2026, 9, 28, 3, 0)
    finally:
        db.close()


def test_status_reports_an_overdue_slot(db_path):
    db = Database(db_path)
    try:
        save_schedule(db, BackupSchedule(frequency="daily", hour=3), now=_at(2026, 9, 20, 10, 0))
        status = schedule_status(db, now=_at(2026, 9, 27, 10, 0))
        assert status.overdue
        assert status.next_run == _at(2026, 9, 28, 3, 0)
    finally:
        db.close()


# -- one pass ----------------------------------------------------------------


def _enable(db_path, *, keep=7, saved_at=_at(2026, 9, 20, 10, 0), **kwargs):
    _with_db(db_path, lambda db: save_schedule(
        db, BackupSchedule(frequency="daily", hour=3, keep=keep, **kwargs), now=saved_at,
    ))


def test_a_node_down_across_several_slots_makes_exactly_one_catch_up_backup(db_path, identity_dir):
    _enable(db_path)
    now = _at(2026, 9, 27, 10, 0)  # six slots since the save

    first = run_scheduled_backup_pass(db_path, identity_dir, now=now)
    second = run_scheduled_backup_pass(db_path, identity_dir, now=now + datetime.timedelta(minutes=1))

    assert first == "succeeded (scheduled)"
    assert second is None
    backups = list((db_path.parent / "node_backups").iterdir())
    assert len(backups) == 1 and (backups[0] / "manifest.json").is_file()
    assert (backups[0] / "identity" / "marker").read_text() == "identity"
    recorded = _with_db(db_path, list_scheduled_backups)
    assert [path for _, path in recorded] == backups
    history = _with_db(db_path, lambda db: list_operational_run_history(db, "backup"))
    assert history[0].outcome == "succeeded (scheduled)"


def test_nothing_runs_while_the_schedule_is_off(db_path, identity_dir):
    assert run_scheduled_backup_pass(db_path, identity_dir, now=_at(2026, 9, 27, 10, 0)) is None
    assert not (db_path.parent / "node_backups").exists()


def test_a_failed_slot_is_recorded_and_not_retried_until_the_next(db_path, tmp_path):
    _enable(db_path)
    missing_identity = tmp_path / "gone"
    now = _at(2026, 9, 27, 10, 0)

    outcome = run_scheduled_backup_pass(db_path, missing_identity, now=now)

    assert outcome.startswith("scheduled run skipped:") and "identity" in outcome
    assert run_scheduled_backup_pass(db_path, missing_identity, now=now + datetime.timedelta(hours=1)) is None
    history = _with_db(db_path, lambda db: list_operational_run_history(db, "backup"))
    assert history[0].outcome == outcome
    # The next slot is the retry.
    assert run_scheduled_backup_pass(db_path, missing_identity, now=_at(2026, 9, 28, 3, 0)) is not None


def test_retention_deletes_only_the_schedules_own_oldest_backups(db_path, identity_dir):
    _enable(db_path, keep=2)
    root = db_path.parent / "node_backups"
    root.mkdir()
    manual = root / "backup-manual"
    manual.mkdir()
    (manual / "manifest.json").write_text("{}")
    stray = root / "notes"
    stray.mkdir()

    created = []
    for day in (21, 22, 23):
        assert run_scheduled_backup_pass(db_path, identity_dir, now=_at(2026, 9, day, 3, 5)) == "succeeded (scheduled)"
        created.append(_with_db(db_path, list_scheduled_backups)[0][1])

    assert not created[0].exists()
    assert created[1].exists() and created[2].exists()
    assert manual.exists() and stray.exists()
    assert [path for _, path in _with_db(db_path, list_scheduled_backups)] == [created[2], created[1]]


def test_retention_never_deletes_a_recorded_path_that_is_not_a_backup(db_path, tmp_path):
    not_a_backup = tmp_path / "precious"
    not_a_backup.mkdir()
    (not_a_backup / "data.txt").write_text("keep me")
    db = Database(db_path)
    try:
        record_scheduled_backup(db, not_a_backup)
        record_scheduled_backup(db, tmp_path / "newer")
        report = prune_scheduled_backups(db, keep=1)
    finally:
        db.close()
    assert not_a_backup.exists() and report.deleted == []
    assert "no longer holds a backup" in report.errors[0]


def test_a_retention_failure_is_reported_and_retried(db_path, tmp_path, monkeypatch):
    old = tmp_path / "old-backup"
    old.mkdir()
    (old / "manifest.json").write_text("{}")
    db = Database(db_path)
    try:
        record_scheduled_backup(db, old)
        record_scheduled_backup(db, tmp_path / "newer")

        def refuse(path):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(bs.shutil, "rmtree", refuse)
        report = prune_scheduled_backups(db, keep=1)
        assert old.exists() and "could not delete" in report.errors[0]
        assert len(list_scheduled_backups(db)) == 2  # still recorded: tried again next time

        monkeypatch.undo()
        report = prune_scheduled_backups(db, keep=1)
        assert report.deleted == [old] and not old.exists()
    finally:
        db.close()


def test_scheduled_backups_go_to_the_configured_destination(db_path, identity_dir, tmp_path):
    destination = tmp_path / "elsewhere"
    destination.mkdir()
    _with_db(db_path, lambda db: set_destination_setting(db, destination, db_path=db_path, identity_dir=identity_dir))
    _enable(db_path)

    assert run_scheduled_backup_pass(db_path, identity_dir, now=_at(2026, 9, 27, 10, 0)) == "succeeded (scheduled)"
    assert len(list(destination.iterdir())) == 1
    assert not (db_path.parent / "node_backups").exists()


def test_a_vanished_destination_is_not_recreated(db_path, identity_dir, tmp_path):
    destination = tmp_path / "usb-disk"
    destination.mkdir()
    _with_db(db_path, lambda db: set_destination_setting(db, destination, db_path=db_path))
    destination.rmdir()
    _enable(db_path)

    outcome = run_scheduled_backup_pass(db_path, identity_dir, now=_at(2026, 9, 27, 10, 0))

    assert outcome.startswith("scheduled run skipped:")
    assert not destination.exists()


def test_destination_validation(db_path, tmp_path, identity_dir):
    with pytest.raises(BackupScheduleError, match="absolute"):
        validate_destination(Path("relative/dir"), db_path=db_path)
    with pytest.raises(BackupScheduleError, match="not an existing directory"):
        validate_destination(tmp_path / "missing", db_path=db_path)
    storage = db_path.parent / "node_files" / "sub"
    storage.mkdir(parents=True)
    with pytest.raises(BackupScheduleError, match="file storage"):
        validate_destination(storage, db_path=db_path)
    with pytest.raises(BackupScheduleError, match="identity"):
        validate_destination(identity_dir, db_path=db_path, identity_dir=identity_dir)
    ok = tmp_path / "ok"
    ok.mkdir()
    assert validate_destination(ok, db_path=db_path) == ok


def test_clearing_the_destination_returns_to_the_default(db_path, tmp_path):
    somewhere = tmp_path / "somewhere"
    somewhere.mkdir()
    db = Database(db_path)
    try:
        set_destination_setting(db, somewhere, db_path=db_path)
        assert backup_root(db, db_path) == somewhere
        set_destination_setting(db, None, db_path=db_path)
        assert backup_root(db, db_path) == db_path.parent / "node_backups"
    finally:
        db.close()


# -- the node's task ---------------------------------------------------------


def test_scheduler_runs_the_catch_up_on_its_first_pass_then_polls(db_path, identity_dir):
    _enable(db_path)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    async def main():
        with pytest.raises(asyncio.CancelledError):
            await run_backup_scheduler(
                db_path, identity_dir, sleep=fake_sleep, now=lambda: _at(2026, 9, 27, 10, 0),
            )

    asyncio.run(main())

    assert sleeps == [bs.POLL_SECONDS, bs.POLL_SECONDS]
    assert len(list((db_path.parent / "node_backups").iterdir())) == 1


def test_scheduler_survives_a_failing_pass(db_path, identity_dir, monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("unexpected")

    monkeypatch.setattr(bs, "run_scheduled_backup_pass", boom)

    async def fake_sleep(seconds):
        if len(calls) >= 2:
            raise asyncio.CancelledError

    async def main():
        with pytest.raises(asyncio.CancelledError):
            await run_backup_scheduler(db_path, identity_dir, sleep=fake_sleep)

    asyncio.run(main())
    assert len(calls) == 2


def test_scheduler_cancelled_mid_backup_lets_the_backup_finish(db_path, identity_dir, monkeypatch):
    """Shutdown cancels the task; the worker thread cannot be stopped and
    must not be abandoned half-written."""
    import threading

    started = threading.Event()
    release = threading.Event()
    finished = []

    def slow_pass(*args, **kwargs):
        started.set()
        release.wait(5)
        finished.append(True)
        return "succeeded (scheduled)"

    monkeypatch.setattr(bs, "run_scheduled_backup_pass", slow_pass)

    async def main():
        task = asyncio.create_task(run_backup_scheduler(db_path, identity_dir))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        # Cancelled, but still owning the worker: shutdown waits for it
        # (Codex review) rather than removing the PID file under a backup.
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == [True]

    asyncio.run(main())


def test_first_enable_on_a_restored_node_starts_counting_now(db_path):
    """A schedule written without its last-slot marker (a hand edit, an old
    restore) must not fire for the past on the first poll."""
    db = Database(db_path)
    try:
        save_schedule(db, BackupSchedule(frequency="daily", hour=3))
        db.connection.execute("DELETE FROM node_config WHERE key = 'backup_schedule_last_slot'")
        db.connection.commit()
        assert due_slot(db, _at(2026, 9, 27, 10, 0)) is None
        assert get_config(db, "backup_schedule_last_slot") is not None
    finally:
        db.close()


def test_schedule_uses_the_node_display_timezone(db_path):
    db = Database(db_path)
    try:
        set_display_timezone(db, "Europe/Berlin")
        save_schedule(db, BackupSchedule(frequency="daily", hour=3), now=_at(2026, 9, 26, 12, 0))
        # 01:00 UTC is 03:00 in Berlin.
        assert due_slot(db, _at(2026, 9, 27, 0, 59)) is None
        assert due_slot(db, _at(2026, 9, 27, 1, 0)) == _at(2026, 9, 27, 3, 0, BERLIN)
    finally:
        db.close()


# -- the SysOp console -------------------------------------------------------

from netbbs.net.admin_flow import admin_menu  # noqa: E402
from tests.test_admin_flow import (  # noqa: E402,F401 -- fixtures
    FakeSession,
    _node_controls,
    _normalized_visible,
    _visible,
    _written_text,
    db,
    isolated_door_career_directory,
    lane,
    sysop,
)


def _tall(inputs):
    """The Backup screen pages at 24 rows; these tests read the whole panel."""
    session = FakeSession(inputs)
    session.terminal_height = 100
    return session


def test_backup_screen_shows_the_schedule_and_default_destination(db, lane, sysop):
    identity = db.path.parent / "identity"
    identity.mkdir()
    session = _tall(["k", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=identity)))

    text = _normalized_visible(_written_text(session))
    assert "SCHEDULE" in text.upper()
    assert "Schedule: off" in text
    # A long temporary path wraps inside the panel; compare without the wrap.
    assert "Destination:" in text and "node_backups" in "".join(text.split()).replace("│", "")
    assert "the default, beside the database" in text
    assert "[S]chedule & destination" in _visible(_written_text(session))


def test_schedule_editor_saves_a_daily_schedule_and_audits_it(db, lane, sysop):
    from netbbs.moderation.log import list_recent_actions

    identity = db.path.parent / "identity"
    identity.mkdir()
    # k: Backup; s: editor; f: off -> daily; s: save; b: Backup; b: landing.
    session = _tall(["k", "s", "f", "s", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=identity)))

    assert load_schedule(db) == BackupSchedule(frequency="daily", hour=3, minute=0)
    assert any(action.action == "set_backup_schedule" for action in list_recent_actions(db))
    text = _normalized_visible(_written_text(session))
    assert "Backup schedule: daily at 03:00." in text
    assert "Next run:" in text


def test_schedule_editor_refuses_a_bad_time_and_keeps_the_draft(db, lane, sysop):
    identity = db.path.parent / "identity"
    identity.mkdir()
    session = _tall(["k", "s", "f", "t", "25:00", "s", "b", "y", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=identity)))

    assert load_schedule(db) == BackupSchedule()
    assert "between 00:00 and 23:59" in _normalized_visible(_written_text(session))


def test_schedule_editor_refuses_a_destination_that_does_not_exist(db, lane, sysop, tmp_path):
    identity = db.path.parent / "identity"
    identity.mkdir()
    missing = tmp_path / "no-such-disk"
    session = _tall(["k", "s", "d", str(missing), "s", "b", "y", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=identity)))

    assert backup_root(db, db.path) == db.path.parent / "node_backups"
    assert "is not an existing directory" in _normalized_visible(_written_text(session))


def test_create_backup_now_uses_the_configured_destination(db, lane, sysop, tmp_path):
    identity = db.path.parent / "identity"
    identity.mkdir()
    destination = tmp_path / "second-disk"
    destination.mkdir()
    set_destination_setting(db, destination, db_path=db.path)
    session = _tall(["k", "c", "y", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=_node_controls(backup_identity_dir=identity)))

    assert len(list(destination.iterdir())) == 1
    assert not (db.path.parent / "node_backups").exists()
    assert list_scheduled_backups(db) == []  # a manual backup is never the schedule's to delete


def test_standalone_console_edits_the_schedule_and_says_who_runs_it(db, lane, sysop):
    session = _tall(["o", "k", "s", "f", "s", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=None))

    assert load_schedule(db).frequency == "daily"
    assert "The running node makes scheduled backups" in _normalized_visible(_written_text(session))


def test_dashboard_names_the_next_scheduled_backup(db, lane, sysop):
    save_schedule(db, BackupSchedule(frequency="daily", hour=3))
    session = _tall(["b"])
    asyncio.run(admin_menu(session, lane, sysop))

    assert "Next backup:" in _normalized_visible(_written_text(session))

    compact = FakeSession(["b"])  # 24 rows: the compact panel, one row for backups
    asyncio.run(admin_menu(compact, lane, sysop))
    assert "BACKUP never next" in _normalized_visible(_written_text(compact))


# -- review round 1 ------------------------------------------------------------


def test_a_slot_in_a_spring_forward_gap_comes_due_exactly_once(db_path, identity_dir):
    """02:30 does not exist in Berlin on 2026-03-29. The slot must still be
    one instant, so polls between 03:00 and 03:30 local do not each start a
    backup (Codex review)."""
    db = Database(db_path)
    try:
        set_display_timezone(db, "Europe/Berlin")
        save_schedule(db, BackupSchedule(frequency="daily", hour=2, minute=30), now=_at(2026, 3, 28, 12, 0))
    finally:
        db.close()
    runs = [
        run_scheduled_backup_pass(db_path, identity_dir, now=_at(2026, 3, 29, 1, minute))  # 03:mm CEST
        for minute in (0, 5, 29, 31, 45)
    ]
    assert [outcome for outcome in runs if outcome is not None] == ["succeeded (scheduled)"]


def test_changing_only_keep_leaves_an_overdue_catch_up_in_place(db_path):
    db = Database(db_path)
    try:
        save_schedule(db, BackupSchedule(frequency="daily", hour=3), now=_at(2026, 9, 20, 10, 0))
        save_schedule(db, BackupSchedule(frequency="daily", hour=3, keep=3), now=_at(2026, 9, 27, 10, 0))
        assert due_slot(db, _at(2026, 9, 27, 10, 1)) is not None
        # Moving the time does reset it.
        save_schedule(db, BackupSchedule(frequency="daily", hour=4, keep=3), now=_at(2026, 9, 27, 10, 2))
        assert due_slot(db, _at(2026, 9, 27, 10, 3)) is None
    finally:
        db.close()


def test_a_destination_on_another_device_than_when_chosen_is_refused(db_path, identity_dir, tmp_path, monkeypatch):
    """An unmounted disk leaves its mount point behind as a writable
    directory on the disk beneath (Codex review)."""
    destination = tmp_path / "mnt-backups"
    destination.mkdir()
    _with_db(db_path, lambda db: set_destination_setting(db, destination, db_path=db_path))
    _enable(db_path)
    real_stat = os.stat

    class _OtherDevice:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return getattr(self._st, name)

        @property
        def st_dev(self):
            return self._st.st_dev + 1

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        return _OtherDevice(result) if Path(path) == destination else result

    monkeypatch.setattr(bs.os, "stat", fake_stat)
    outcome = run_scheduled_backup_pass(db_path, identity_dir, now=_at(2026, 9, 27, 10, 0))

    assert outcome.startswith("scheduled run skipped:") and "unmounted" in outcome
    assert list(destination.iterdir()) == []


def test_one_history_row_per_scheduled_run_even_when_retention_fails(db_path, identity_dir, monkeypatch):
    _enable(db_path, keep=1)
    assert run_scheduled_backup_pass(db_path, identity_dir, now=_at(2026, 9, 21, 3, 5)) == "succeeded (scheduled)"

    def refuse(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(bs.shutil, "rmtree", refuse)
    outcome = run_scheduled_backup_pass(db_path, identity_dir, now=_at(2026, 9, 22, 3, 5))

    assert outcome.startswith("succeeded (scheduled); retention: could not delete")
    history = _with_db(db_path, lambda db: list_operational_run_history(db, "backup"))
    assert [run.outcome for run in history] == [outcome, "succeeded (scheduled)"]


def test_a_lone_scheduled_failure_shows_on_the_backup_screen(db, lane, sysop, tmp_path):
    save_schedule(db, BackupSchedule(frequency="daily", hour=3), now=_at(2026, 9, 20, 10, 0))
    run_scheduled_backup_pass(db.path, tmp_path / "no-identity", now=_at(2026, 9, 27, 10, 0))
    session = _tall(["o", "k", "b", "b", "b"])
    asyncio.run(admin_menu(session, lane, sysop, node_controls=None))

    text = _normalized_visible(_written_text(session))
    assert "RECENT BACKUPS" in text.upper() and "scheduled run skipped" in text
