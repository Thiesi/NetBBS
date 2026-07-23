"""
Per-node SQLite database wrapper.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from netbbs.storage.migrations import MIGRATIONS


class DatabaseIntegrityError(Exception):
    """Raised by `Database.check_integrity` when `PRAGMA integrity_check`
    finds a genuinely corrupted file -- distinct from `Database.
    __init__`'s own plain `RuntimeError` for a too-new schema version,
    which is a build mismatch, not corruption."""


class Database:
    """
    Thin wrapper around a single node's SQLite connection.

    One `Database` instance = one node's local SQLite file, and — this
    is the part worth being precise about, corrected in design doc
    round 30/issue #10 after an earlier version of this docstring
    overclaimed what WAL mode buys here — **exactly one `sqlite3.
    Connection`, shared for the whole node's lifetime and used
    synchronously.** WAL mode's real-world benefit (readers and writers
    on *separate* connections not blocking each other) does not apply
    within a single connection: every `self.connection.execute(...)`
    call here is a direct, synchronous DB-API call with no `await`
    around it, so it runs to completion — and blocks the entire asyncio
    event loop, not just the calling coroutine — before any other
    session's coroutine gets to run. Concurrent *sessions* using this
    node are not concurrent *database access*; they're serialized by
    Python's own cooperative scheduling, the same as any other
    synchronous call made from a coroutine. This is fine at today's
    declared scale (design doc §14: dozens–low hundreds of users, small
    fast queries) but is the real bottleneck implied by "before
    targeting low hundreds of users, consider a bounded connection
    pool, database actor, or off-loop execution for expensive queries"
    (issue #10's own recommended direction) — a genuinely separate,
    larger design question than this round attempts, deliberately not
    taken on here (see round 30's sign-off note for the full reasoning
    on why pagination, not a connection-model rewrite, is this round's
    actual scope).

    WAL mode is still worth keeping despite the above: it *does* help
    the one place multiple independent connections against the same
    file realistically exist today — an admin/dev script (e.g.
    `scripts/create_test_board.py`) opened against a database file
    while the node process is also running against it, a second OS
    process rather than a second coroutine in this one. `busy_timeout`
    (below) is what actually keeps that scenario from surfacing as a
    raw "database is locked" error.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self._configure_pragmas()
        self._apply_migrations()

    def _configure_pragmas(self) -> None:
        # See this class's docstring for what WAL does and doesn't buy
        # with a single shared connection -- kept for the separate-
        # process case (an admin script run against a live node's
        # database file), not for concurrency between sessions within
        # this one process/connection.
        self.connection.execute("PRAGMA journal_mode = WAL")
        # SQLite ignores declared foreign keys by default unless this is
        # set on every connection that needs them enforced.
        self.connection.execute("PRAGMA foreign_keys = ON")
        # NORMAL is the standard, well-understood trade-off for WAL mode:
        # WAL's own crash-safety guarantees make FULL's extra fsyncs
        # unnecessary overhead here.
        self.connection.execute("PRAGMA synchronous = NORMAL")
        # Retry for up to 5s before raising "database is locked", rather
        # than SQLite's default of failing immediately -- the concrete
        # fix for the separate-process contention case described above
        # (design doc round 30, issue #10's "configure busy_timeout").
        # 5000ms is a conservative, commonly-used default; not
        # separately benchmarked against this project's actual access
        # patterns, since the scenario it protects is occasional
        # (an admin script, not routine node operation), not a hot path.
        self.connection.execute("PRAGMA busy_timeout = 5000")

    def _apply_migrations(self) -> None:
        current_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        latest_version = len(MIGRATIONS)
        if current_version > latest_version:
            raise RuntimeError(
                f"database schema version {current_version} is newer than this "
                f"NetBBS build supports ({latest_version})"
            )

        pending = MIGRATIONS[current_version:]
        for offset, migration in enumerate(pending):
            new_version = current_version + offset + 1
            # executescript() commits any transaction which was already open,
            # so the transaction must be part of the script itself. Updating
            # user_version before COMMIT keeps both schema changes and the
            # recorded version in the same atomic unit.
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{migration.sql}\n"
                f"PRAGMA user_version = {new_version};\n"
                "COMMIT;"
            )
            try:
                self.connection.executescript(script)
            except sqlite3.Error:
                # A failing statement leaves the explicit transaction open.
                # Roll it back so no earlier statement in this migration is
                # retained and the connection remains usable by callers/tests.
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise

    def check_integrity(self) -> None:
        """
        Runs SQLite's own `PRAGMA integrity_check` against this
        connection (design doc §13.11, issue #60) -- deliberately not
        called from `__init__`, and not something most callers should
        ever need to call at all: a full-database scan on *every*
        `Database()` construction would tax every admin script and this
        project's entire test suite (thousands of constructions) for a
        check only the one long-lived node process actually needs, once,
        at its own startup (`netbbs.__main__.run`'s one real call site).

        Raises `DatabaseIntegrityError`, naming every problem found (a
        healthy database returns exactly one row, the literal string
        `"ok"` -- anything else, including more than one row, describes
        a real problem), rather than letting corruption surface later as
        a confusing raw `sqlite3` error the first time some unlucky
        query happens to touch the damaged page.
        """
        rows = self.connection.execute("PRAGMA integrity_check").fetchall()
        problems = [row[0] for row in rows if row[0] != "ok"]
        if problems:
            raise DatabaseIntegrityError(
                f"database at {self.path} failed integrity check: " + "; ".join(problems)
            )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
