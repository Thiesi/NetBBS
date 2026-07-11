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
        pending = MIGRATIONS[current_version:]
        for offset, migration in enumerate(pending):
            new_version = current_version + offset + 1
            self.connection.executescript(migration.sql)
            # PRAGMA doesn't support bound parameters; safe here because
            # new_version is an int we computed ourselves, never
            # user-supplied input.
            self.connection.execute(f"PRAGMA user_version = {new_version}")
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
