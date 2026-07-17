"""
Two-lane database execution model (design doc round 91, resolves issue
#57 — Phase 3 background-work prerequisite).

Round 30 diagnosed the problem: `netbbs.storage.database.Database` is
exactly one synchronous `sqlite3.Connection`, called directly from
coroutines with no `await` around it — every query blocks the entire
asyncio event loop, not just the calling coroutine, until it returns.
Round 91 chose the fix: two dedicated single-worker-thread lanes (a
**foreground** lane for interactive Telnet/SSH/web session work, and a
**background** lane reserved for Phase 3's continuous Link activity —
peer inventory exchange, event verification/ingestion, retry/outbox
processing, none of which exists yet), each owning its own SQLite
connection (WAL) against the same database file.

**Existing business-logic functions are completely unchanged** — every
one of them keeps taking `db: Database` as its first parameter and
keeps its already-correct transaction ownership exactly where it is.
Only the *call sites* move: a direct synchronous call becomes `await
lane.run(func, *args, **kwargs)`, with the lane injecting its own `db`
as the first positional argument. A caller that used to hold an
explicit `db: Database` purely to pass down to an eventual leaf call
now holds a `DatabaseLane` instead — the lane, not a `Database`, is what
threads through async handler code from here on, since no single shared
`Database` object exists anymore for it to hold.
"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, TypeVar

from netbbs.storage.database import Database

T = TypeVar("T")

# Round 91: "Backpressure: a bounded semaphore per lane, not the
# executor's default unbounded queue... Exact numeric limits... are
# #60's job [the operational model]." A conservative placeholder
# pending that round's actual implementation — large enough that
# ordinary interactive load at this project's declared scale (§14:
# dozens-to-low-hundreds of users, small fast queries) never comes
# close, small enough to bound worst-case queued-work memory
# regardless of how many sessions pile up submissions faster than the
# single worker thread can drain them.
DEFAULT_MAX_QUEUED = 200


class LaneBusyError(Exception):
    """Raised when a lane's queue is already at `max_queued` and a new
    submission would exceed it — the "reject rather than grow without
    limit" half of round 91's backpressure requirement. `asyncio.
    Semaphore` alone only ever blocks, never rejects; this class exists
    so a caller that would rather fail fast than queue indefinitely
    behind an already-saturated lane has that option (see `run`'s
    `block_if_busy` parameter)."""


class DatabaseLane:
    """
    One single-worker-thread execution lane with its own dedicated
    `Database` connection, opened lazily on first use.

    Opened *from inside the lane's own worker thread*, not whatever
    thread constructs this object — `Database`/`sqlite3.connect` binds a
    connection to its creating thread (see `Database`'s own docstring
    for why that single-connection, single-thread model is exactly the
    event-loop-blocking problem this class exists to fix, not to route
    around by relaxing `check_same_thread`). `max_workers=1` on the
    underlying executor is what makes this safe: every job this lane
    ever runs, for its entire lifetime, executes on that one thread, so
    a connection opened by the first job stays valid for every job
    after it.
    """

    def __init__(self, path: Path, *, max_queued: int = DEFAULT_MAX_QUEUED) -> None:
        self._path = path
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._semaphore = asyncio.Semaphore(max_queued)
        self._max_queued = max_queued
        self._db: Database | None = None

    @property
    def path(self) -> Path:
        """This lane's database file path — exposed directly so a
        caller that needs the path itself (e.g. deriving a sibling
        directory, as `netbbs.net.mail_flow._mail_draft_path` does) can
        read it without an executor round-trip: it's a plain in-memory
        attribute, not a query, and was never something that needed to
        move off the event loop in the first place."""
        return self._path

    def _ensure_db(self) -> Database:
        # Only ever called from inside a job already running on
        # self._executor's one worker thread -- see this class's own
        # docstring for why that's what makes the connection this opens
        # safe to keep reusing for this lane's whole lifetime.
        if self._db is None:
            self._db = Database(self._path)
        return self._db

    async def run(self, func: Callable[..., T], *args, block_if_busy: bool = True, **kwargs) -> T:
        """
        Run `func(db, *args, **kwargs)` on this lane's dedicated worker
        thread, off the asyncio event loop — `db` (this lane's own
        `Database`, opened on first use) is injected as `func`'s first
        positional argument, matching the `db: Database`-first
        convention every existing business-logic function already
        follows, so a migrated call site just drops the explicit `db`
        it used to pass directly.

        `block_if_busy` (round 91's bounded-semaphore backpressure):
        `True` (default) waits for a free slot, same as an ordinary
        bounded queue; `False` raises `LaneBusyError` immediately
        instead of waiting, for a caller that would rather fail fast
        than queue behind `max_queued` other pending jobs.
        """
        if not block_if_busy and self._semaphore.locked():
            raise LaneBusyError(
                f"lane at {self._path} already has {self._max_queued} job(s) queued or running"
            )
        async with self._semaphore:
            loop = asyncio.get_running_loop()
            job = functools.partial(self._run_job, func, *args, **kwargs)
            return await loop.run_in_executor(self._executor, job)

    def _run_job(self, func: Callable[..., T], *args, **kwargs) -> T:
        # Executes on the worker thread (round 91: "cancellation is
        # safe by a property of the mechanism" -- if the awaiting
        # coroutine is cancelled, this function keeps running to
        # completion regardless, since Python cannot abort a thread
        # mid-flight; the caller just never sees the result).
        db = self._ensure_db()
        return func(db, *args, **kwargs)

    def close(self) -> None:
        """
        Close this lane's `Database` connection and shut down its
        worker thread.

        The close itself is submitted as one more job on the same
        thread that opened the connection, not called directly from
        whatever thread calls `close()` — the same thread-binding
        reasoning as `_ensure_db`. A lane that was never used (no job
        ever ran, `self._db` still `None`) has nothing to close.
        """
        if self._db is not None:
            future = self._executor.submit(self._db.close)
            future.result()
        self._executor.shutdown(wait=True)
