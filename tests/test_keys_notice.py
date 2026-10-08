"""The one-time "keys that work everywhere" screen (issue #1158, Decision 6):
shown once to an account that existed when the keys moved, never to one
made afterwards, never to a guest."""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user
from netbbs.net.keys_notice import PREFERENCE_KEY, show_keys_notice_once
from netbbs.storage.database import Database
from netbbs.storage.migrations import MIGRATIONS
from netbbs.user_preferences import get_user_preference, set_user_preference
from tests.test_admin_flow import FakeSession


def _show(db, user, session=None):
    session = session or FakeSession(["x"])
    asyncio.run(show_keys_notice_once(session, db, user, header_color=15, unicode_style=False))
    return "".join(session.written)


def test_an_account_marked_by_the_upgrade_sees_it_once(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_user_preference(db, user, PREFERENCE_KEY, "pending")
    assert "Keys that work everywhere" in _show(db, user)
    assert get_user_preference(db, user, PREFERENCE_KEY) == "seen"
    assert _show(db, user, FakeSession([])) == ""  # a read would fail: nothing is asked


def test_an_account_made_after_the_upgrade_never_sees_it(tmp_path):
    db = Database(tmp_path / "node.db")
    user = create_user(db, "newcomer", password="hunter2", user_level=10)
    assert _show(db, user, FakeSession([])) == ""


def test_the_migration_marks_every_account_that_exists(tmp_path, monkeypatch):
    """On a database one schema behind: the accounts it finds are pending,
    and nothing else in the preferences table changes."""
    from netbbs.storage import database as database_module
    from tests.legacy_schema import insert_user_on_old_schema

    path = tmp_path / "upgrade.db"
    with monkeypatch.context() as old_schema:
        old_schema.setattr(database_module, "MIGRATIONS", MIGRATIONS[:-1])
        with Database(path) as db:
            old = insert_user_on_old_schema(db, "old", user_level=10)
            set_user_preference(db, old, "unicode_style", "on")
    with Database(path) as db:
        rows = dict(db.connection.execute(
            "SELECT key, value FROM user_preferences WHERE user_id = ?", (old.id,)
        ).fetchall())
        assert rows == {"unicode_style": "on", PREFERENCE_KEY: "pending"}
        newcomer = create_user(db, "newcomer", password="hunter2", user_level=10)
        assert get_user_preference(db, newcomer, PREFERENCE_KEY) is None


def test_a_guest_never_sees_it(tmp_path):
    """A shared account's callers are strangers to each other: none of them
    is told about keys they never had."""
    db = Database(tmp_path / "node.db")
    user = create_user(db, "guest", password="hunter2", user_level=10)
    set_user_preference(db, user, PREFERENCE_KEY, "pending")
    session = FakeSession([])
    session.authenticated_without_credential = True
    assert _show(db, user, session) == ""
    assert get_user_preference(db, user, PREFERENCE_KEY) == "pending"
