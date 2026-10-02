"""Issue #1059: a node's own background work held the SQLite write lock for
seconds after startup, and the scheduled tasks that met it gave up for the
whole uptime.

Two halves, both pinned here:

- the periodic trust recompute (issue #802) rewrote every subject's row every
  pass inside one transaction; it now writes only what changed, in batches;
- the node-lifetime tasks (update check, reliable-nodes refresh, daybreak
  announcer, Link sync) survive a failed pass instead of ending.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
import threading
import time

import pytest

import netbbs.link.reliable_nodes as reliable_nodes_module
import netbbs.link.sync as sync_module
import netbbs.net.daybreak as daybreak_module
import netbbs.selfupdate as selfupdate_module
from netbbs.link.trust import (
    TrustState,
    TrustSubject,
    get_effective_trust_state,
    recompute_all_trust_states,
    register_subject,
    set_trust_override,
    TrustDimension,
)
from netbbs.storage.database import Database

STAMP = "2026-09-01T00:00:00.000000Z"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _populate(db, count: int) -> list[TrustSubject]:
    subjects = []
    for index in range(count):
        subject = TrustSubject.node(f"{index:032x}")
        register_subject(db, subject, first_accepted_at=STAMP, now_iso=STAMP)
        subjects.append(subject)
    return subjects


def _longest_write_lock_during(db: Database, action) -> float:
    """Run `action` while a second connection keeps trying to take the write
    lock with no busy wait, as the reporter measured it; return the longest
    stretch the lock was held by someone else."""
    stop = threading.Event()
    longest = [0.0]

    def probe():
        held_since = None
        while not stop.is_set():
            con = sqlite3.connect(db.path, timeout=0, isolation_level=None)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("ROLLBACK")
                if held_since is not None:
                    longest[0] = max(longest[0], time.monotonic() - held_since)
                    held_since = None
            except sqlite3.OperationalError:
                if held_since is None:
                    held_since = time.monotonic()
            finally:
                con.close()
            time.sleep(0.002)
        if held_since is not None:
            longest[0] = max(longest[0], time.monotonic() - held_since)

    thread = threading.Thread(target=probe)
    thread.start()
    try:
        action()
    finally:
        stop.set()
        thread.join()
    return longest[0]


# -- the recompute ------------------------------------------------------------


def test_a_quiet_recompute_writes_nothing_and_takes_no_write_lock(db):
    """The startup recompute has just run; the first sync pass's recompute
    finds nothing changed. It must not rewrite 300 rows to say so -- that is
    what held the lock for seconds on a node that had met many callers."""
    _populate(db, 300)
    recompute_all_trust_states(db, now_iso="2026-09-02T00:00:00.000000Z")
    before = db.connection.total_changes
    longest = _longest_write_lock_during(
        db, lambda: recompute_all_trust_states(db, now_iso="2026-09-02T00:01:00.000000Z")
    )
    assert db.connection.total_changes == before
    assert longest == 0.0


def test_a_recompute_with_many_changes_commits_in_batches(db):
    """When many subjects do change, no single transaction covers them all:
    each batch commits, so the lock is released between batches."""
    subjects = _populate(db, 9)
    for subject in subjects:
        set_trust_override(
            db, subject, TrustDimension.CONTENT_CONDUCT, TrustState.QUARANTINED,
            reason="test", now_iso=STAMP, expires_at="2026-09-03T00:00:00.000000Z",
        )
    commits: list[str] = []
    db.connection.set_trace_callback(lambda sql: commits.append(sql) if sql.strip().upper() == "COMMIT" else None)
    try:
        # The overrides expired: every subject enters its recovery hold, a
        # changed row each, written in three transactions of at most four.
        recompute_all_trust_states(db, now_iso="2026-09-04T00:00:00.000000Z", batch_size=4)
    finally:
        db.connection.set_trace_callback(None)
    assert len(commits) == 3
    # A day later every hold releases, and every transition is reported.
    transitions = recompute_all_trust_states(db, now_iso="2026-09-05T01:00:00.000000Z", batch_size=4)
    assert len([t for t in transitions if t.dimension == "content_conduct"]) == 9
    for subject in subjects:
        assert get_effective_trust_state(
            db, subject, TrustDimension.CONTENT_CONDUCT
        ).state == TrustState.PROBATIONARY


# -- the tasks ----------------------------------------------------------------


def _parked_after(passes: int):
    """A fake sleep that lets `passes` passes run, then parks; records delays."""
    calls: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        if len(calls) >= passes:
            await parked.wait()
        await asyncio.sleep(0)

    return fake_sleep, calls


async def _run_until(task_factory, condition, timeout: float = 10.0):
    task = asyncio.create_task(task_factory())
    try:
        deadline = time.monotonic() + timeout
        while not condition():
            if task.done():
                task.result()  # surfaces an exception that ended the task
                raise AssertionError("the task ended")
            if time.monotonic() > deadline:
                raise AssertionError("condition not reached")
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_the_update_check_survives_a_locked_database_and_retries_soon(db, monkeypatch):
    """The reported failure: `save_release_cache` met the lock and the task
    ended, so automatic release checks stopped for the uptime."""
    from tests.test_selfupdate import _fake_releases_json

    fetch = lambda url, etag, token=None: (_fake_releases_json("v0.0.1"), None)
    real_save = selfupdate_module.save_release_cache
    attempts = {"n": 0}

    def flaky_save(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_save(*args, **kwargs)

    monkeypatch.setattr(selfupdate_module, "save_release_cache", flaky_save)
    fake_sleep, calls = _parked_after(2)

    asyncio.run(_run_until(
        lambda: selfupdate_module.run_scheduled_update_check(
            db, fetch=fetch, sleep=fake_sleep, interval_seconds=86400.0, min_recheck_interval_seconds=900.0,
        ),
        lambda: selfupdate_module.get_last_check_summary(db)[1] is not None,
    ))
    assert attempts["n"] == 2
    assert calls[0] == 900.0, "a failed pass retries after the recheck window, not a day later"


def test_the_reliable_nodes_refresh_survives_a_failed_save(db, monkeypatch):
    fetch = lambda url: json.dumps({"version": 1, "nodes": []}).encode()
    monkeypatch.setattr(reliable_nodes_module, "fetch_reliable_nodes", _async_value([]))
    saves = {"n": 0}

    def flaky_save(*args, **kwargs):
        saves["n"] += 1
        if saves["n"] == 1:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reliable_nodes_module, "set_cached_reliable_nodes", flaky_save)
    fake_sleep, calls = _parked_after(2)

    asyncio.run(_run_until(
        lambda: reliable_nodes_module.run_scheduled_reliable_nodes_refresh(
            db, fetch=fetch, sleep=fake_sleep, interval_seconds=86400.0,
        ),
        lambda: saves["n"] >= 2,
    ))
    assert calls[0] == reliable_nodes_module._RETRY_AFTER_FAILED_SAVE_SECONDS


def _async_value(value):
    async def fetch_reliable_nodes(*args, **kwargs):
        return value
    return fetch_reliable_nodes


def test_the_daybreak_announcer_survives_a_failed_announcement(db, monkeypatch):
    from netbbs.chat.hub import ChatHub

    announcements = {"n": 0}

    async def flaky_announce(*args, **kwargs):
        announcements["n"] += 1
        if announcements["n"] == 1:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(daybreak_module, "announce_new_day", flaky_announce)
    fake_sleep, _calls = _parked_after(3)
    now = lambda: datetime.datetime(2029, 4, 3, 23, 59, 0, tzinfo=datetime.timezone.utc)

    asyncio.run(_run_until(
        lambda: daybreak_module.run_daybreak_announcer(db, ChatHub(), now=now, sleep=fake_sleep),
        lambda: announcements["n"] >= 2,
    ))


def test_a_link_sync_pass_that_raises_does_not_end_the_loop(tmp_path, monkeypatch):
    """One pass meeting a locked database used to end outbound Link activity
    for the uptime."""
    import aiohttp

    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode
    from netbbs.storage.execution import DatabaseLane

    database = Database(tmp_path / "sync.db")
    lane = DatabaseLane(database.path)
    node = LinkNode(identity=bootstrap_node_identity("resilient"))
    passes = {"n": 0}
    stop = asyncio.Event()

    async def flaky_step(*args, **kwargs):
        passes["n"] += 1
        if passes["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        stop.set()

    monkeypatch.setattr(sync_module, "_reevaluate_trust_over_time", flaky_step)

    class Hello:
        async def refresh(self, lane):
            pass

        def __call__(self):
            return node.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00")

    async def scenario():
        async with aiohttp.ClientSession() as session:
            await asyncio.wait_for(
                sync_module.run_link_sync(node, session, [], Hello(), lane, interval_seconds=0.0, stop_event=stop),
                timeout=30,
            )

    try:
        asyncio.run(scenario())
    finally:
        lane.close()
        database.close()
    assert passes["n"] == 2


def test_the_relay_bundle_prune_leaves_no_transaction_open(tmp_path):
    """Issue #1059, the root cause, pinned without the lane's guard in the
    way (review of #1064): the prune committed only when a row went, so a
    pass with nothing expired left its DELETE's implicit transaction -- and
    the write lock -- open on the connection."""
    from netbbs.link.relay_mailbox import prune_expired_relay_attestation_bundles

    database = Database(tmp_path / "prune.db")
    try:
        assert prune_expired_relay_attestation_bundles(database) == 0
        assert not database.connection.in_transaction
    finally:
        database.close()


def test_no_write_lock_is_held_while_the_first_pass_waits_on_a_relay_dial(tmp_path, monkeypatch):
    """Issue #1059, end to end. The prune above is pinned directly; in
    production the lane's guard would also commit a leaked transaction, so
    this test shows the reporter's scenario stays free of the lock rather
    than guarding the prune itself. An outgoing-only node's first sync pass
    pruned the relay attestation-bundle slots and committed only if a row
    went, so the DELETE's implicit transaction -- and the database's write
    lock -- stayed open on the background lane until the next job's commit.
    That came after the first relay-candidate dial timed out, ~11 s later,
    and every other connection's write failed at its 5 s busy timeout. Here
    the dial never answers until released; while it hangs, another
    connection must be able to take the write lock at once."""
    import aiohttp

    import netbbs.storage.execution as execution_module
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode
    from netbbs.storage.execution import DatabaseLane

    # The production behaviour is what is measured: no strict-mode raise.
    monkeypatch.setattr(execution_module, "STRICT_TRANSACTIONS", False)
    database = Database(tmp_path / "outgoing.db")
    lane = DatabaseLane(database.path)
    node = LinkNode(identity=bootstrap_node_identity("outgoing-only"))
    dialling = asyncio.Event()
    release = asyncio.Event()
    stop = asyncio.Event()
    probe: dict[str, object] = {}

    async def hanging_relay_selection(*args, **kwargs):
        dialling.set()
        await release.wait()
        stop.set()

    monkeypatch.setattr(sync_module, "_maintain_relay_selection", hanging_relay_selection)

    class Hello:
        async def refresh(self, lane):
            pass

        def __call__(self):
            return node.build_hello(addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00")

    def try_write_lock():
        con = sqlite3.connect(database.path, timeout=0, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("ROLLBACK")
            return None
        except sqlite3.OperationalError as exc:
            return str(exc)
        finally:
            con.close()

    async def scenario():
        async with aiohttp.ClientSession() as session:
            task = asyncio.create_task(sync_module.run_link_sync(
                node, session, [], Hello(), lane, interval_seconds=60.0, stop_event=stop,
                enforce_trust_policy=True,
            ))
            await asyncio.wait_for(dialling.wait(), timeout=30)
            probe["error"] = await asyncio.to_thread(try_write_lock)
            release.set()
            await asyncio.wait_for(task, timeout=30)

    try:
        asyncio.run(scenario())
    finally:
        lane.close()
        database.close()
    assert probe["error"] is None, f"the write lock was held during the dial: {probe['error']}"


def test_a_lane_job_that_leaves_a_transaction_open_is_caught():
    """The class guard: in strict mode (the whole suite) a lane job that
    returns with a transaction open fails; in production it is committed
    and logged, so a missed commit can never hold the write lock."""
    import tempfile
    from pathlib import Path

    import netbbs.storage.execution as execution_module
    from netbbs.storage.execution import DatabaseLane, LeakedTransactionError

    path = Path(tempfile.mkdtemp()) / "lane.db"
    Database(path).close()

    def leaky(db):
        db.connection.execute("INSERT INTO node_config (key, value) VALUES ('leak', '1')")

    def committed(db):
        return db.connection.execute("SELECT value FROM node_config WHERE key = 'leak'").fetchone()

    lane = DatabaseLane(path)
    try:
        with pytest.raises(LeakedTransactionError, match="leaky"):
            asyncio.run(lane.run(leaky))
        # Strict mode rolled it back: nothing was written.
        assert asyncio.run(lane.run(committed)) is None
        execution_module.STRICT_TRANSACTIONS = False
        try:
            asyncio.run(lane.run(leaky))
        finally:
            execution_module.STRICT_TRANSACTIONS = True
        # Production committed it: the write the function made stands, and
        # the lock is free.
        assert asyncio.run(lane.run(committed))["value"] == "1"
        con = sqlite3.connect(path, timeout=0, isolation_level=None)
        con.execute("BEGIN IMMEDIATE")
        con.execute("ROLLBACK")
        con.close()
    finally:
        lane.close()
