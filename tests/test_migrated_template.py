"""The suite's migrated template (issue #1569) is the schema the migrations make.

`tests/conftest.py` gives every brand-new test database a copy of one
database migrated once. If that copy ever differed from running the
migrations, every test would be checking a schema no node has.
"""

from __future__ import annotations

import pytest

import netbbs.storage.database as database_module
from netbbs.storage.database import Database
from netbbs.storage.migrations import MIGRATIONS


def _schema(db):
    rows = db.connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")
    return [tuple(row) for row in rows], db.connection.execute("PRAGMA user_version").fetchone()[0]


def test_a_copied_database_has_exactly_the_migrated_schema(tmp_path, monkeypatch):
    copied = Database(tmp_path / "copied.db")
    # Not the shipped list object, so this one runs every migration itself.
    monkeypatch.setattr(database_module, "MIGRATIONS", list(MIGRATIONS))
    migrated = Database(tmp_path / "migrated.db")
    assert _schema(copied) == _schema(migrated)
    assert _schema(copied)[1] == len(MIGRATIONS)
    assert copied.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert copied.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_an_existing_database_is_reopened_not_replaced(tmp_path):
    first = Database(tmp_path / "node.db")
    first.connection.execute("INSERT INTO node_config (key, value) VALUES ('kept', 'yes')")
    first.connection.commit()
    first.connection.close()
    again = Database(tmp_path / "node.db")
    assert again.connection.execute("SELECT value FROM node_config WHERE key = 'kept'").fetchone()[0] == "yes"


def test_a_truncated_migration_list_still_builds_an_older_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:10])
    old = Database(tmp_path / "old.db")
    assert old.connection.execute("PRAGMA user_version").fetchone()[0] == 10
    with pytest.raises(Exception):
        old.connection.execute("SELECT 1 FROM door_events").fetchone()
