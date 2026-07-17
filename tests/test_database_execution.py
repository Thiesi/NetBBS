"""Tests for netbbs.storage.execution — the two-lane database execution
model (design doc round 91, issue #57)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from netbbs.auth.users import create_user, get_user_by_username
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane, LaneBusyError


@pytest.fixture
def lane(tmp_path):
    lane = DatabaseLane(tmp_path / "node.db")
    yield lane
    lane.close()


# -- basic dispatch -----------------------------------------------------------


def test_run_injects_db_as_first_argument(lane):
    def make_and_fetch(db: Database, username: str) -> str:
        create_user(db, username, password="hunter2", user_level=10)
        return get_user_by_username(db, username).username

    async def scenario():
        return await lane.run(make_and_fetch, "alice")

    assert asyncio.run(scenario()) == "alice"


def test_run_supports_keyword_arguments(lane):
    def make_user(db: Database, username: str, *, user_level: int) -> int:
        return create_user(db, username, password="hunter2", user_level=user_level).user_level

    async def scenario():
        return await lane.run(make_user, "bob", user_level=42)

    assert asyncio.run(scenario()) == 42


def test_lane_reuses_the_same_connection_across_calls(lane):
    def create(db: Database, username: str) -> None:
        create_user(db, username, password="hunter2", user_level=10)

    def fetch(db: Database, username: str) -> str:
        return get_user_by_username(db, username).username

    async def scenario():
        await lane.run(create, "carol")
        return await lane.run(fetch, "carol")  # a second call must see the first's write

    assert asyncio.run(scenario()) == "carol"


# -- runs on a dedicated worker thread, off the event loop --------------------


def test_job_runs_on_a_different_thread_than_the_caller(lane):
    caller_thread = threading.current_thread().ident

    def get_thread_id(db: Database) -> int:
        return threading.current_thread().ident

    async def scenario():
        return await lane.run(get_thread_id)

    worker_thread = asyncio.run(scenario())
    assert worker_thread != caller_thread


def test_multiple_jobs_run_on_the_same_worker_thread(lane):
    def get_thread_id(db: Database) -> int:
        return threading.current_thread().ident

    async def scenario():
        first = await lane.run(get_thread_id)
        second = await lane.run(get_thread_id)
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second  # max_workers=1: always the same thread


def test_run_does_not_block_the_event_loop(lane):
    """A slow lane job must not prevent other coroutines from making
    progress concurrently -- the actual point of moving DB work off the
    event loop."""
    import time

    def slow(db: Database) -> str:
        time.sleep(0.2)
        return "done"

    async def scenario():
        progress = []

        async def ticker():
            for _ in range(8):
                await asyncio.sleep(0.02)
                progress.append("tick")

        results = await asyncio.gather(lane.run(slow), ticker())
        return results[0], progress

    result, progress = asyncio.run(scenario())
    assert result == "done"
    assert len(progress) >= 4  # the ticker made real progress while the lane job slept


# -- backpressure ---------------------------------------------------------------


def test_block_if_busy_false_raises_when_at_capacity(tmp_path):
    import time

    busy_lane = DatabaseLane(tmp_path / "node.db", max_queued=1)
    try:
        def slow(db: Database) -> None:
            time.sleep(0.3)

        async def scenario():
            first = asyncio.create_task(busy_lane.run(slow))
            await asyncio.sleep(0.05)  # let the first job actually start
            with pytest.raises(LaneBusyError):
                await busy_lane.run(slow, block_if_busy=False)
            await first

        asyncio.run(scenario())
    finally:
        busy_lane.close()


def test_block_if_busy_true_waits_instead_of_raising(tmp_path):
    saturated_lane = DatabaseLane(tmp_path / "node.db", max_queued=1)
    try:
        def quick(db: Database, value: int) -> int:
            return value

        async def scenario():
            first = asyncio.create_task(saturated_lane.run(quick, 1))
            second = await saturated_lane.run(quick, 2, block_if_busy=True)
            await first
            return second

        assert asyncio.run(scenario()) == 2
    finally:
        saturated_lane.close()


# -- cancellation safety --------------------------------------------------------


def test_cancelling_the_awaiting_coroutine_does_not_abort_the_write(lane):
    """Round 91: a disconnected session's in-flight DB call still runs
    to completion on the worker thread -- the caller just doesn't get
    to see the result."""
    import time

    def slow_write(db: Database, username: str) -> None:
        time.sleep(0.15)
        create_user(db, username, password="hunter2", user_level=10)

    async def scenario():
        task = asyncio.create_task(lane.run(slow_write, "dave"))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Give the worker thread's already-running job time to finish
        # even though the awaiting coroutine was cancelled.
        await asyncio.sleep(0.3)

    asyncio.run(scenario())

    def fetch(db: Database) -> str:
        return get_user_by_username(db, "dave").username

    async def check():
        return await lane.run(fetch)

    assert asyncio.run(check()) == "dave"  # the write completed despite cancellation


# -- lifecycle --------------------------------------------------------------------


def test_close_on_a_never_used_lane_does_not_raise(tmp_path):
    unused_lane = DatabaseLane(tmp_path / "node.db")
    unused_lane.close()  # must not raise even though no job ever ran


def test_lane_database_persists_to_the_configured_path(tmp_path):
    db_path = tmp_path / "subdir" / "node.db"
    lane = DatabaseLane(db_path)
    try:
        def create(db: Database) -> None:
            create_user(db, "erin", password="hunter2", user_level=10)

        asyncio.run(lane.run(create))
    finally:
        lane.close()

    assert db_path.exists()
    direct = Database(db_path)
    try:
        assert get_user_by_username(direct, "erin").username == "erin"
    finally:
        direct.close()
