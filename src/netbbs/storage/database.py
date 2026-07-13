"""
Per-node SQLite database wrapper.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from netbbs.storage.migrations import MIGRATIONS


class Database:
    """
    Thin wrapper around a single node's SQLite connection.

    One `Database` instance = one node's local SQLite file. WAL mode is
    enabled for the connection's whole lifetime so concurrent asyncio
    readers (someone browsing a board) and writers (someone else posting)
    don't block each other more than necessary — relevant once the design
    doc §14 scale numbers (dozens–low hundreds of concurrent users) are
    actually being exercised.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self._configure_pragmas()
        self._apply_migrations()

    def _configure_pragmas(self) -> None:
        # Readers don't block writers and vice versa — see design doc
        # §14's note that write contention is the predicted first real
        # bottleneck at scale; WAL is the cheapest available mitigation.
        self.connection.execute("PRAGMA journal_mode = WAL")
        # SQLite ignores declared foreign keys by default unless this is
        # set on every connection that needs them enforced.
        self.connection.execute("PRAGMA foreign_keys = ON")
        # NORMAL is the standard, well-understood trade-off for WAL mode:
        # WAL's own crash-safety guarantees make FULL's extra fsyncs
        # unnecessary overhead here.
        self.connection.execute("PRAGMA synchronous = NORMAL")

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

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
