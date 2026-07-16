"""
Tests for GitHub issue #49: the last-SysOp safety check
(`netbbs.auth.users._refuse_if_last_sysop`) must be atomic with the
mutation it guards *across independent SQLite connections* -- this
node's own process plus `python -m netbbs.admin`, or two admin CLI
invocations against the same database file, not just two coroutines
sharing one connection (already safe, since they share one synchronous
connection with no `await` between the check and the mutation).

Each test opens two real, independent `Database` connections to the
same file (not two coroutines against one connection, which the
existing single-process tests in `test_user_management.py` already
cover) and drives them from two real OS threads, since `sqlite3`
connections block synchronously -- there is no `await` point to
interleave around the way `tests/test_password_work.py`'s own
event-loop-based concurrency tests do.

`record_action_without_commit` is monkeypatched to pause -- once, on
whichever thread's transaction reaches it first -- so the test doesn't
rely on incidental OS thread-scheduling to create the interleaving
window; it deterministically proves the *other* thread's own `BEGIN
IMMEDIATE` genuinely blocks until the first transaction resolves,
rather than merely happening to run after it.
"""

from __future__ import annotations

import threading

from netbbs.auth.users import (
    SYSOP_LEVEL,
    UserManagementError,
    count_sysops,
    create_user,
    delete_user,
    get_user_by_username,
    set_user_disabled,
    set_user_level,
)
from netbbs.moderation import log as log_module
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.storage.database import Database


def _pause_first_call(reached: threading.Event, release: threading.Event):
    """
    Wraps `record_action_without_commit` so that whichever thread's
    transaction reaches it *first* (i.e. whichever thread actually won
    the `BEGIN IMMEDIATE` race and is partway through committing its
    count-check-then-mutate transaction) signals `reached` and blocks on
    `release` before actually inserting the audit-log row and letting
    its caller commit. The losing thread's own `BEGIN IMMEDIATE` should
    still be blocked at the SQLite level at this point and therefore
    never reach this wrapper concurrently -- there is deliberately no
    lock here, since two genuinely concurrent calls reaching this point
    at once would itself be evidence the fix isn't working.
    """
    real = log_module.record_action_without_commit
    called = threading.Event()

    def wrapper(db, **kwargs):
        if not called.is_set():
            called.set()
            reached.set()
            release.wait(timeout=5)
        return real(db, **kwargs)

    return wrapper


def _race_two_removals(tmp_path, monkeypatch, mutate):
    """
    Sets up two usable SysOps (alice, bob) and two independent
    connections to the same database file, then has one thread start
    removing bob's SysOp status while a second thread, released only
    once the first is confirmed genuinely blocked mid-transaction,
    attempts to remove alice's -- exactly the interleaving GitHub issue
    #49 describes. `mutate(db, target, actor)` performs whichever
    specific removal (demote/disable/delete) this call is testing.

    Installs the `record_action_without_commit` pause itself (rather
    than each caller doing so) so the same `reached`/`release` events
    drive both the patched function and this helper's own wait/release
    points below -- two independently-constructed event pairs would
    never signal each other.

    Returns `(db, results)` where `db` is a fresh connection opened in
    the *calling* thread only after both worker threads have finished
    (Python's `sqlite3` connections are thread-affine -- each of the
    two "process" connections below is created and used entirely
    within its own worker thread, never touched from here), and
    `results` is a dict with keys `"a"`/`"b"` mapping to either the
    thread's return value or the exception it raised, so callers can
    assert exactly one side succeeded.
    """
    db_path = tmp_path / "node.db"
    setup_db = Database(db_path)
    create_user(setup_db, "alice", password="hunter2", user_level=SYSOP_LEVEL)
    create_user(setup_db, "bob", password="hunter2", user_level=SYSOP_LEVEL)
    setup_db.close()

    reached = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(log_module, "record_action_without_commit", _pause_first_call(reached, release))

    results: dict[str, object] = {}

    def run_a() -> None:
        db_a = Database(db_path)
        try:
            alice_a = get_user_by_username(db_a, "alice")
            bob_a = get_user_by_username(db_a, "bob")
            try:
                results["a"] = mutate(db_a, bob_a, alice_a)
            except Exception as exc:  # noqa: BLE001 -- captured for the test's own assertions
                results["a"] = exc
        finally:
            db_a.close()

    def run_b() -> None:
        # Only start once thread A is confirmed mid-transaction --
        # otherwise B might legitimately just run to completion first,
        # which wouldn't exercise the race window at all.
        assert reached.wait(timeout=5), "thread A never reached its pause point"
        db_b = Database(db_path)
        try:
            alice_b = get_user_by_username(db_b, "alice")
            bob_b = get_user_by_username(db_b, "bob")
            try:
                results["b"] = mutate(db_b, alice_b, bob_b)
            except Exception as exc:  # noqa: BLE001
                results["b"] = exc
        finally:
            db_b.close()

    thread_a = threading.Thread(target=run_a)
    thread_b = threading.Thread(target=run_b)

    thread_a.start()
    assert reached.wait(timeout=5), "thread A never reached its pause point"

    thread_b.start()
    # Thread B's own BEGIN IMMEDIATE must genuinely block here -- give
    # it a real window to (incorrectly) finish if the fix regressed.
    thread_b.join(timeout=0.3)
    assert thread_b.is_alive(), (
        "thread B finished before thread A released its transaction -- "
        "BEGIN IMMEDIATE did not actually serialize the two connections"
    )

    release.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()

    db = Database(db_path)
    return db, results


# -- set_user_disabled ------------------------------------------------------


def test_concurrent_disable_cannot_leave_zero_usable_sysops(tmp_path, monkeypatch):
    def mutate(db, target, actor):
        return set_user_disabled(db, target, True, changed_by=actor)

    db, results = _race_two_removals(tmp_path, monkeypatch, mutate)

    outcomes = list(results.values())
    successes = [r for r in outcomes if not isinstance(r, Exception)]
    failures = [r for r in outcomes if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], UserManagementError)

    assert count_sysops(db) == 1

    disable_entries = [
        entry
        for username in ("alice", "bob")
        for entry in list_actions_for_target_user(db, get_user_by_username(db, username).id)
        if entry.action == "disable"
    ]
    assert len(disable_entries) == 1

    db.close()


# -- set_user_level (demote) -------------------------------------------------


def test_concurrent_demote_cannot_leave_zero_usable_sysops(tmp_path, monkeypatch):
    def mutate(db, target, actor):
        return set_user_level(db, target, 10, changed_by=actor)

    db, results = _race_two_removals(tmp_path, monkeypatch, mutate)

    outcomes = list(results.values())
    successes = [r for r in outcomes if not isinstance(r, Exception)]
    failures = [r for r in outcomes if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], UserManagementError)

    assert count_sysops(db) == 1

    demote_entries = [
        entry
        for username in ("alice", "bob")
        for entry in list_actions_for_target_user(db, get_user_by_username(db, username).id)
        if entry.action == "demote"
    ]
    assert len(demote_entries) == 1

    db.close()


# -- delete_user --------------------------------------------------------


def test_concurrent_delete_cannot_leave_zero_usable_sysops(tmp_path, monkeypatch):
    def mutate(db, target, actor):
        delete_user(db, target, deleted_by=actor)
        return True

    db, results = _race_two_removals(tmp_path, monkeypatch, mutate)

    outcomes = list(results.values())
    successes = [r for r in outcomes if not isinstance(r, Exception)]
    failures = [r for r in outcomes if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], UserManagementError)

    assert count_sysops(db) == 1

    delete_entries = [
        entry for entry in
        db.connection.execute(
            "SELECT action FROM moderation_log WHERE action = 'delete_user'"
        ).fetchall()
    ]
    assert len(delete_entries) == 1

    db.close()
