"""
Tests for `netbbs.admin.__main__`'s actor-resolution and bootstrap
logic (design doc -- SysOp foundation round): `_resolve_actor`,
`_bootstrap_first_sysop`, `run_admin_session`. Driven with the same
scripted `FakeSession` `tests/test_admin_flow.py` already defines
(single ordered input queue serving both `read_key`/`read_line`).
`main()` itself (argument parsing, real terminal wiring) is
deliberately not exercised here -- see that module's docstring for why
credential-based auth is intentionally absent from this whole path.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.admin.__main__ import _bootstrap_first_sysop, _resolve_actor, run_admin_session
from netbbs.auth.users import SYSOP_LEVEL, create_user, list_users
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.storage.database import Database
from tests.test_admin_flow import FakeSession, _written_text


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


# -- bootstrap: zero SysOps exist -----------------------------------------


def test_bootstrap_creates_the_first_sysop(db):
    session = FakeSession(["sysop", "hunter2", "hunter2", ""])
    user = asyncio.run(_bootstrap_first_sysop(session, db))
    assert user.username == "sysop"
    assert user.user_level == SYSOP_LEVEL


def test_bootstrap_self_attributes_the_audit_entry(db):
    session = FakeSession(["sysop", "hunter2", "hunter2", ""])
    user = asyncio.run(_bootstrap_first_sysop(session, db))
    entries = list_actions_for_target_user(db, user.id)
    bootstrap_entries = [e for e in entries if e.action == "bootstrap_create_sysop"]
    assert len(bootstrap_entries) == 1
    assert bootstrap_entries[0].actor_user_id == user.id


def test_resolve_actor_bootstraps_when_no_sysop_exists(db):
    session = FakeSession(["sysop", "hunter2", "hunter2", ""])
    actor = asyncio.run(_resolve_actor(session, db, None))
    assert actor.username == "sysop"


def test_run_admin_session_bootstraps_then_opens_the_menu(db):
    session = FakeSession(["sysop", "hunter2", "hunter2", "", "b"])
    asyncio.run(run_admin_session(session, db, None))
    assert any(u.username == "sysop" and u.user_level == SYSOP_LEVEL for u in list_users(db))
    assert "Attributed to 'sysop'" in _written_text(session)


# -- resolution: exactly one active SysOp --------------------------------


def test_resolve_actor_auto_selects_the_sole_sysop(db):
    create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    session = FakeSession([])  # no input needed -- auto-selected
    actor = asyncio.run(_resolve_actor(session, db, None))
    assert actor.username == "sysop"


# -- resolution: --as ---------------------------------------------------


def test_resolve_actor_honors_as_flag(db):
    create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    create_user(db, "root", password="hunter2", user_level=SYSOP_LEVEL)
    session = FakeSession([])
    actor = asyncio.run(_resolve_actor(session, db, "root"))
    assert actor.username == "root"


def test_resolve_actor_rejects_an_invalid_as_flag(db):
    create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    session = FakeSession([])
    with pytest.raises(SystemExit):
        asyncio.run(_resolve_actor(session, db, "nosuchuser"))


def test_resolve_actor_as_flag_excludes_disabled_sysops(db):
    from netbbs.auth.users import set_user_disabled

    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    root = create_user(db, "root", password="hunter2", user_level=SYSOP_LEVEL)
    set_user_disabled(db, root, True, changed_by=sysop)
    session = FakeSession([])
    with pytest.raises(SystemExit):
        asyncio.run(_resolve_actor(session, db, "root"))


# -- resolution: multiple active SysOps, no --as -> picker -----------------


def test_resolve_actor_shows_a_picker_with_multiple_sysops(db):
    create_user(db, "root", password="hunter2", user_level=SYSOP_LEVEL)
    create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    # "root" sorts before "sysop" alphabetically -- item 01.
    session = FakeSession(["0", "1"])
    actor = asyncio.run(_resolve_actor(session, db, None))
    assert actor.username == "root"
