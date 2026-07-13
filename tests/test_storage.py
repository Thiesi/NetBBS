"""Tests for netbbs.storage — database setup, pragmas, migrations."""

from __future__ import annotations

import sqlite3

import pytest

from netbbs.storage.database import Database


def test_database_creates_file_and_parent_dirs(tmp_path):
    db_path = tmp_path / "nested" / "node.db"
    db = Database(db_path)
    assert db_path.exists()
    db.close()


def test_database_enables_wal_mode(tmp_path):
    db = Database(tmp_path / "node.db")
    mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    db.close()


def test_database_enables_foreign_keys(tmp_path):
    db = Database(tmp_path / "node.db")
    enabled = db.connection.execute("PRAGMA foreign_keys").fetchone()[0]
    assert enabled == 1
    db.close()


def test_database_configures_busy_timeout(tmp_path):
    """Design doc round 30, issue #10: retries on a locked database
    (e.g. an admin script opening the same file the node process has
    open) rather than failing immediately, which is SQLite's default
    with no busy_timeout configured."""
    db = Database(tmp_path / "node.db")
    timeout_ms = db.connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms == 5000
    db.close()


def test_migrations_bring_user_version_to_latest(tmp_path):
    from netbbs.storage.migrations import MIGRATIONS

    db = Database(tmp_path / "node.db")
    version = db.connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(MIGRATIONS)
    db.close()


def test_reopening_existing_database_does_not_rerun_migrations(tmp_path):
    db_path = tmp_path / "node.db"
    db1 = Database(db_path)
    db1.connection.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        ("thiesi", "some-hash", "2026-01-01T00:00:00+00:00"),
    )
    db1.connection.commit()
    db1.close()

    # Reopening should not fail (e.g. by trying to CREATE TABLE users again)
    # and the previously inserted row should still be there.
    db2 = Database(db_path)
    row = db2.connection.execute("SELECT username FROM users").fetchone()
    assert row["username"] == "thiesi"
    db2.close()


def test_context_manager_closes_connection(tmp_path):
    db_path = tmp_path / "node.db"
    with Database(db_path) as db:
        db.connection.execute("SELECT 1")
    # Connection should now be closed; using it should raise.
    try:
        db.connection.execute("SELECT 1")
        assert False, "expected connection to be closed"
    except sqlite3.ProgrammingError:
        pass


def test_users_table_requires_password_or_public_key(tmp_path):
    db = Database(tmp_path / "node.db")
    try:
        db.connection.execute(
            "INSERT INTO users (username, created_at) VALUES (?, ?)",
            ("nobody", "2026-01-01T00:00:00+00:00"),
        )
        db.connection.commit()
        assert False, "expected CHECK constraint violation"
    except sqlite3.IntegrityError:
        pass
    finally:
        db.close()


def test_database_rejects_newer_schema_version(tmp_path):
    db_path = tmp_path / "node.db"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()

    with pytest.raises(RuntimeError, match="newer than this NetBBS build supports"):
        Database(db_path)


def test_failed_migration_rolls_back_all_statements_and_version(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import Migration

    failing_migration = Migration(
        description="Deliberately failing migration for rollback coverage.",
        sql="""
        CREATE TABLE should_be_rolled_back (id INTEGER PRIMARY KEY);
        INSERT INTO table_that_does_not_exist VALUES (1);
        """,
    )
    monkeypatch.setattr(database_module, "MIGRATIONS", [failing_migration])

    db_path = tmp_path / "node.db"
    with pytest.raises(sqlite3.OperationalError):
        Database(db_path)

    connection = sqlite3.connect(db_path)
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("should_be_rolled_back",),
    ).fetchone()
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()

    assert table is None
    assert version == 0
