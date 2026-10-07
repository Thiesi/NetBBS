"""Issue #1131: an account id is never handed to another account.

`users.id` is an INTEGER PRIMARY KEY without AUTOINCREMENT, so SQLite on its
own gives the highest free rowid -- delete the newest account and the next one
takes its id, and a door keyed on `user_id` hands it the deleted player's saves.
"""

from __future__ import annotations

from netbbs.auth.users import create_user, delete_user
from netbbs.storage.database import Database


def test_deleting_the_newest_account_does_not_free_its_id(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    newest = create_user(db, "alice", password="hunter2")
    delete_user(db, newest, deleted_by=sysop)

    replacement = create_user(db, "bob", password="hunter2")

    assert replacement.id == newest.id + 1


def test_the_high_water_mark_survives_a_reopen(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    for name in ("alice", "bob", "carol"):
        delete_user(db, create_user(db, name, password="hunter2"), deleted_by=sysop)
    db.connection.close()

    reopened = Database(tmp_path / "node.db")

    assert create_user(reopened, "dave", password="hunter2").id == 5


def test_the_migration_seeds_the_mark_from_the_highest_existing_id(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "Issue #1131" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    old = Database(tmp_path / "node.db")
    for user_id, name in ((1, "sysop"), (7, "alice")):
        old.connection.execute(
            "INSERT INTO users (id, username, password_hash, user_level, created_at) "
            "VALUES (?, ?, 'x', 0, '2026-01-01T00:00:00+00:00')",
            (user_id, name),
        )
    old.connection.commit()
    old.connection.close()
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)

    db = Database(tmp_path / "node.db")
    db.connection.execute("DELETE FROM users WHERE id = 7")
    db.connection.commit()

    assert create_user(db, "bob", password="hunter2").id == 8
