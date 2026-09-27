"""Scheduled node backups, their retention, and the backup destination (issue #727).

Before this, a recurring backup was a cron job the SysOp wrote around
`python -m netbbs.backup create`, plus their own pruning of old
directories. The node now does both when a SysOp switches it on:

- **Schedule.** Off, daily, or weekly on one weekday, at a local wall-clock
  time in the node's display timezone. One *slot* is one scheduled moment.
  A slot is handled at most once, whether its backup succeeded, failed or
  was skipped; a failed slot is not retried, the next slot is the retry.
- **Missed slots.** A node that was down across one or more slots runs
  exactly one catch-up backup when it next starts (or when the SysOp next
  turns the schedule on again), not one per missed slot. Saving a schedule
  marks "now" as handled, so switching it on never fires for a slot that
  was already in the past.
- **Retention.** Only backups the scheduler itself created are ever
  deleted: each is recorded in `scheduled_backups` when it succeeds, and
  pruning walks that table, newest kept. A directory is deleted only if it
  is still a real directory (not a link) holding a backup manifest; a
  manual backup, or anything a SysOp put beside the backups, is never
  touched. A deletion that fails is reported and the record kept, so the
  next pass tries again.
- **Destination.** An optional directory that both "Create backup now" and
  the scheduler write into; unset means `<db-stem>_backups` beside the
  database, as before.

Domain functions here are synchronous and `db`-first. `run_scheduled_backup_pass`
does the blocking work and is what the node's background task runs in a
worker thread, with its own `Database` handle.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from netbbs.config import get_config, set_config
from netbbs.operational_history import record_operational_run
from netbbs.storage.database import Database
from netbbs.timeutil import get_node_timezone, utc_now_iso

FREQUENCIES = ("off", "daily", "weekly")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DEFAULT_TIME = (3, 0)
DEFAULT_KEEP = 7
MAX_KEEP = 365

_FREQUENCY_KEY = "backup_schedule_frequency"
_TIME_KEY = "backup_schedule_time"
_WEEKDAY_KEY = "backup_schedule_weekday"
_KEEP_KEY = "backup_schedule_keep"
_LAST_SLOT_KEY = "backup_schedule_last_slot"
_DESTINATION_KEY = "backup_destination_dir"
#: `st_dev` of the destination when the SysOp set it. An unmounted disk leaves
#: its mount point behind as an ordinary writable directory on the disk
#: beneath; a different device is how that shows.
_DESTINATION_DEVICE_KEY = "backup_destination_device"

_logger = logging.getLogger(__name__)

_MANIFEST_FILENAME = "manifest.json"
#: Operational-history outcome strings are shown in a table cell.
_MAX_REASON_CHARS = 200


class BackupScheduleError(ValueError):
    """A schedule or destination the SysOp entered cannot be used."""


@dataclass(frozen=True)
class BackupSchedule:
    frequency: str = "off"
    hour: int = DEFAULT_TIME[0]
    minute: int = DEFAULT_TIME[1]
    #: 0 = Monday, as `datetime.date.weekday()`. Only used when weekly.
    weekday: int = 6
    keep: int = DEFAULT_KEEP

    @property
    def enabled(self) -> bool:
        return self.frequency != "off"

    @property
    def time_text(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def describe(self) -> str:
        if self.frequency == "daily":
            return f"daily at {self.time_text}"
        if self.frequency == "weekly":
            return f"weekly on {WEEKDAY_NAMES[self.weekday]} at {self.time_text}"
        return "off"


def parse_time(text: str) -> tuple[int, int]:
    """`HH:MM`, 24-hour."""
    try:
        hour_text, minute_text = text.strip().split(":")
        hour, minute = int(hour_text), int(minute_text)
    except ValueError:
        raise BackupScheduleError(f"Time must look like 03:00 (24-hour), not {text!r}.") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise BackupScheduleError(f"Time must be between 00:00 and 23:59, not {text!r}.")
    return hour, minute


def validate_schedule(schedule: BackupSchedule) -> None:
    if schedule.frequency not in FREQUENCIES:
        raise BackupScheduleError(f"Frequency must be one of {', '.join(FREQUENCIES)}.")
    if not (0 <= schedule.hour <= 23 and 0 <= schedule.minute <= 59):
        raise BackupScheduleError("Time must be between 00:00 and 23:59.")
    if not 0 <= schedule.weekday <= 6:
        raise BackupScheduleError("Weekday must be Monday to Sunday.")
    if not 1 <= schedule.keep <= MAX_KEEP:
        raise BackupScheduleError(f"Keep must be between 1 and {MAX_KEEP} scheduled backups.")


def load_schedule(db: Database) -> BackupSchedule:
    """The stored schedule; anything unreadable falls back to its default
    rather than stopping the Backup screen from opening."""
    frequency = get_config(db, _FREQUENCY_KEY) or "off"
    if frequency not in FREQUENCIES:
        frequency = "off"
    try:
        hour, minute = parse_time(get_config(db, _TIME_KEY) or "")
    except BackupScheduleError:
        hour, minute = DEFAULT_TIME
    try:
        weekday = int(get_config(db, _WEEKDAY_KEY) or 6)
    except ValueError:
        weekday = 6
    try:
        keep = int(get_config(db, _KEEP_KEY) or DEFAULT_KEEP)
    except ValueError:
        keep = DEFAULT_KEEP
    return BackupSchedule(
        frequency=frequency, hour=hour, minute=minute,
        weekday=weekday if 0 <= weekday <= 6 else 6,
        keep=keep if 1 <= keep <= MAX_KEEP else DEFAULT_KEEP,
    )


def _timing(schedule: BackupSchedule) -> tuple:
    return (schedule.frequency, schedule.hour, schedule.minute,
            schedule.weekday if schedule.frequency == "weekly" else None)


def save_schedule(db: Database, schedule: BackupSchedule, *, now: datetime.datetime | None = None) -> None:
    """Persist `schedule`. When *when* it runs changed, `now` counts as
    handled, so a schedule switched on (or moved) never fires for a slot
    already in the past; a change to Keep alone leaves an overdue slot
    overdue, so its catch-up still runs (Codex review)."""
    validate_schedule(schedule)
    timing_changed = _timing(schedule) != _timing(load_schedule(db))
    values = [
        (_FREQUENCY_KEY, schedule.frequency),
        (_TIME_KEY, schedule.time_text),
        (_WEEKDAY_KEY, str(schedule.weekday)),
        (_KEEP_KEY, str(schedule.keep)),
    ]
    if timing_changed or get_config(db, _LAST_SLOT_KEY) is None:
        values.append((_LAST_SLOT_KEY, _iso(now or _utc_now())))
    with db.connection:
        for key, value in values:
            db.connection.execute(
                "INSERT INTO node_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


# -- destination -----------------------------------------------------------


def default_backup_root(db_path: Path) -> Path:
    return db_path.parent / f"{db_path.stem}_backups"


def get_destination_setting(db: Database) -> Path | None:
    value = get_config(db, _DESTINATION_KEY)
    return Path(value) if value else None


def backup_root(db: Database, db_path: Path) -> Path:
    """Where backups go: the configured directory, else beside the database."""
    return get_destination_setting(db) or default_backup_root(db_path)


def validate_destination(
    path: Path, *, db_path: Path, identity_dir: Path | None = None, expected_device: int | None = None,
) -> Path:
    """An existing, writable directory that no backup would copy into itself,
    on the device it was on when the SysOp chose it (`expected_device`)."""
    if not path.is_absolute():
        raise BackupScheduleError("Give the destination as an absolute path.")
    if not path.is_dir():
        raise BackupScheduleError(f"{path} is not an existing directory.")
    if not os.access(path, os.W_OK | os.X_OK):
        raise BackupScheduleError(f"The node's account cannot write to {path}.")
    resolved = path.resolve()
    # A backup copies these trees; a destination inside one would copy every
    # earlier backup into each new one.
    for tree, what in (
        (db_path.parent / f"{db_path.stem}_files", "the node's file storage"),
        (identity_dir, "the node's identity directory"),
    ):
        if tree is not None and (resolved == tree.resolve() or resolved.is_relative_to(tree.resolve())):
            raise BackupScheduleError(f"The destination cannot be inside {what}.")
    if expected_device is not None and os.stat(path).st_dev != expected_device:
        raise BackupScheduleError(
            f"{path} is no longer on the disk it was on when it was chosen; is that disk "
            "unmounted? Mount it, or choose the destination again."
        )
    return path


def get_destination_device(db: Database) -> int | None:
    value = get_config(db, _DESTINATION_DEVICE_KEY)
    try:
        return int(value) if value else None
    except ValueError:
        return None


def check_destination(db: Database, db_path: Path, identity_dir: Path | None) -> Path:
    """Where the next backup goes, refusing a configured destination that
    has gone or moved to another disk. The default is always usable."""
    configured = get_destination_setting(db)
    if configured is None:
        return default_backup_root(db_path)
    return validate_destination(
        configured, db_path=db_path, identity_dir=identity_dir, expected_device=get_destination_device(db),
    )


def set_destination_setting(db: Database, path: Path | None, *, db_path: Path,
                            identity_dir: Path | None = None) -> None:
    """`None` returns to the default beside the database."""
    if path is None:
        set_config(db, _DESTINATION_KEY, "")
        set_config(db, _DESTINATION_DEVICE_KEY, "")
        return
    validate_destination(path, db_path=db_path, identity_dir=identity_dir)
    set_config(db, _DESTINATION_KEY, str(path))
    set_config(db, _DESTINATION_DEVICE_KEY, str(os.stat(path).st_dev))


# -- slots -----------------------------------------------------------------


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).isoformat()


def _parse(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=datetime.timezone.utc)


def _slot_on(date: datetime.date, schedule: BackupSchedule, tz: datetime.tzinfo) -> datetime.datetime:
    """The slot on `date`, as a UTC instant.

    UTC, not the local zone: two datetimes sharing one `tzinfo` compare by
    wall-clock time, and a wall time inside a spring-forward gap (02:30 in
    Berlin on the last Sunday in March) names an instant later than a real
    03:00 that compares *after* it. Converting settles every slot on one
    instant -- a skipped time runs as the pre-transition reading (03:30
    local), a repeated one at its first occurrence -- so a slot can come
    due exactly once (Codex review)."""
    local = datetime.datetime.combine(date, datetime.time(schedule.hour, schedule.minute), tzinfo=tz)
    return local.astimezone(datetime.timezone.utc)


def latest_slot(schedule: BackupSchedule, now: datetime.datetime, tz: datetime.tzinfo) -> datetime.datetime | None:
    """The most recent scheduled moment at or before `now`."""
    if not schedule.enabled:
        return None
    date = now.astimezone(tz).date()
    if schedule.frequency == "weekly":
        date -= datetime.timedelta(days=(date.weekday() - schedule.weekday) % 7)
    slot = _slot_on(date, schedule, tz)
    if slot > now:
        slot = _slot_on(date - datetime.timedelta(days=7 if schedule.frequency == "weekly" else 1), schedule, tz)
    return slot


def next_slot(schedule: BackupSchedule, now: datetime.datetime, tz: datetime.tzinfo) -> datetime.datetime | None:
    """The first scheduled moment after `now`."""
    latest = latest_slot(schedule, now, tz)
    if latest is None:
        return None
    step = datetime.timedelta(days=7 if schedule.frequency == "weekly" else 1)
    return _slot_on(latest.astimezone(tz).date() + step, schedule, tz)


def due_slot(db: Database, now: datetime.datetime) -> datetime.datetime | None:
    """The slot to run now, if one has come round and is not yet handled."""
    schedule = load_schedule(db)
    slot = latest_slot(schedule, now, get_node_timezone(db))
    if slot is None:
        return None
    handled = _parse(get_config(db, _LAST_SLOT_KEY))
    if handled is None:
        # Never saved through the screen (a restored or hand-edited node):
        # start counting from now rather than firing for the past.
        set_config(db, _LAST_SLOT_KEY, _iso(now))
        return None
    return slot if slot > handled else None


@dataclass(frozen=True)
class ScheduleStatus:
    schedule: BackupSchedule
    next_run: datetime.datetime | None
    #: A slot came round while the node was not running; it runs on start.
    overdue: bool


def schedule_status(db: Database, now: datetime.datetime | None = None) -> ScheduleStatus:
    now = now or _utc_now()
    schedule = load_schedule(db)
    tz = get_node_timezone(db)
    latest = latest_slot(schedule, now, tz)
    handled = _parse(get_config(db, _LAST_SLOT_KEY))
    overdue = latest is not None and handled is not None and latest > handled
    return ScheduleStatus(schedule=schedule, next_run=next_slot(schedule, now, tz), overdue=overdue)


# -- retention -------------------------------------------------------------


def record_scheduled_backup(db: Database, path: Path) -> None:
    with db.connection:
        db.connection.execute(
            "INSERT OR REPLACE INTO scheduled_backups (path, created_at) VALUES (?, ?)",
            (str(path), utc_now_iso()),
        )


def list_scheduled_backups(db: Database) -> list[tuple[int, Path]]:
    """Newest first."""
    rows = db.connection.execute("SELECT id, path FROM scheduled_backups ORDER BY id DESC").fetchall()
    return [(row["id"], Path(row["path"])) for row in rows]


@dataclass
class PruneReport:
    deleted: list[Path]
    errors: list[str]


def prune_scheduled_backups(db: Database, keep: int) -> PruneReport:
    """Delete scheduled backups beyond the newest `keep`. Only recorded
    directories that still hold a backup manifest are deleted."""
    report = PruneReport(deleted=[], errors=[])
    for record_id, path in list_scheduled_backups(db)[keep:]:
        forget = True
        if path.is_symlink():
            report.errors.append(f"{path} is a link now; not deleted")
        elif path.is_dir():
            if not (path / _MANIFEST_FILENAME).is_file():
                report.errors.append(f"{path} no longer holds a backup; not deleted")
            else:
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    report.errors.append(f"could not delete {path}: {exc.strerror or exc}")
                    forget = False
                else:
                    report.deleted.append(path)
        # A path that is gone already (moved off-node by the SysOp) just
        # stops counting.
        if forget:
            with db.connection:
                db.connection.execute("DELETE FROM scheduled_backups WHERE id = ?", (record_id,))
    return report


# -- one pass --------------------------------------------------------------


def _short(reason: object) -> str:
    text = " ".join(str(reason).split())
    return text if len(text) <= _MAX_REASON_CHARS else text[: _MAX_REASON_CHARS - 3] + "..."


def run_scheduled_backup_pass(
    db_path: Path, identity_dir: Path, *, now: datetime.datetime | None = None,
) -> str | None:
    """Run the due slot's backup, if any, and prune. Blocking; returns the
    outcome recorded in the node's backup history, or `None` if nothing was
    due."""
    from netbbs.backup import BackupError, create_backup, default_backup_destination

    now = now or _utc_now()
    db = Database(db_path)
    try:
        slot = due_slot(db, now)
        if slot is None:
            return None
        # Handled before the work, not after: a pass that crashes the worker
        # must not turn into one attempt per poll for the rest of the slot.
        set_config(db, _LAST_SLOT_KEY, _iso(now))
        schedule = load_schedule(db)
        root = backup_root(db, db_path)
        try:
            if not identity_dir.is_dir():
                raise BackupError(f"configured identity directory is unavailable: {identity_dir}")
            root = check_destination(db, db_path, identity_dir)
            destination = default_backup_destination(db_path, root=root)
            created = create_backup(
                db_path=db_path, identity_dir=identity_dir, destination=destination, trigger="scheduled",
            )
        except (BackupError, BackupScheduleError, OSError, sqlite3.Error) as exc:
            outcome = f"scheduled run skipped: {_short(exc)}"
            record_operational_run(db, "backup", outcome, detail=str(root))
            return outcome
        record_scheduled_backup(db, created)
        report = prune_scheduled_backups(db, schedule.keep)
        # One history row per run, success and retention together (Codex review).
        outcome = "succeeded (scheduled)"
        if report.errors:
            outcome += f"; retention: {_short('; '.join(report.errors))}"
        record_operational_run(db, "backup", outcome, detail=str(created))
        return outcome
    finally:
        db.close()


# -- the node's task -------------------------------------------------------

#: How often the node looks for a due slot. A minute late is on time for a
#: backup, and a short poll also notices a schedule the SysOp just changed.
POLL_SECONDS = 60.0


def _retrieve_abandoned(task: asyncio.Future) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _logger.error("scheduled backup failed while the node was stopping", exc_info=exc)
    elif task.result() is not None:
        _logger.info("scheduled backup finished during shutdown: %s", task.result())


async def run_backup_scheduler(
    db_path: Path,
    identity_dir: Path,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    poll_seconds: float = POLL_SECONDS,
    now: Callable[[], datetime.datetime] = _utc_now,
) -> None:
    """Runs for the node's lifetime: each poll runs whatever slot is due.

    The pass itself is blocking work in a worker thread, which cancellation
    cannot stop. On cancel (shutdown) this task keeps owning the worker and
    waits for it before the cancellation propagates, the same way the live
    Backup screen's `_create_live_backup_owned` does: a backup still copying
    after the node had removed its PID file could have a restore replace
    the state underneath it (Codex review). Shutdown therefore waits for a
    running backup to finish. The first pass runs at once, which is how a
    slot missed while the node was down gets its one catch-up run.
    """
    while True:
        worker = asyncio.ensure_future(
            asyncio.to_thread(run_scheduled_backup_pass, db_path, identity_dir, now=now())
        )
        try:
            outcome = await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            _retrieve_abandoned(worker)
            raise
        except Exception:
            _logger.exception("scheduled backup pass failed")
        else:
            if outcome is not None:
                _logger.info("scheduled backup: %s", outcome)
        await sleep(poll_seconds)
