"""Tests for netbbs.storage — database setup, pragmas, migrations."""

from __future__ import annotations

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
    import sqlite3

    try:
        db.connection.execute("SELECT 1")
        assert False, "expected connection to be closed"
    except sqlite3.ProgrammingError:
        pass


def test_users_table_requires_password_or_public_key(tmp_path):
    import sqlite3

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
