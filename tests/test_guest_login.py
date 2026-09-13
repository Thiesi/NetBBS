"""Guest login as an authentication shortcut (issue #531).

The design decision this pins down: the guest is a *real account*, so
levels, per-object permissions, gates, moderation and auditing all keep
working for it exactly as for anybody else, and nothing in the codebase
branches on "is this caller a guest". Skipping the password prompt is
the entire feature.

These tests are mostly about what guest login does *not* skip.
"""

from __future__ import annotations

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.guest import (
    MAX_PRE_LOGIN_NOTICE_LENGTH,
    guest_user,
    guest_username,
    is_guest_login,
    pre_login_notice,
    set_guest_username,
    set_pre_login_notice,
)
from netbbs.storage.database import Database


def _db(tmp_path):
    db = Database(tmp_path / "node.db")
    return db, create_user(db, "guest", password="hunter2", user_level=1)


# -- Designating an account -------------------------------------------


def test_guest_login_is_off_until_an_account_is_designated(tmp_path):
    db, _ = _db(tmp_path)
    assert guest_username(db) is None
    assert guest_user(db) is None
    assert is_guest_login(db, "guest") is False
    db.close()


def test_a_designated_account_is_resolved(tmp_path):
    db, guest = _db(tmp_path)
    set_guest_username(db, "guest")
    assert guest_user(db).id == guest.id
    assert is_guest_login(db, "guest") is True
    db.close()


def test_the_name_is_matched_case_insensitively(tmp_path):
    """A caller told to "sign in as guest" should not be refused for
    typing "Guest", the same way the `new` sentinel is forgiving."""
    db, _ = _db(tmp_path)
    set_guest_username(db, "guest")
    assert is_guest_login(db, "Guest") is True
    assert is_guest_login(db, "  GUEST  ") is True
    db.close()


def test_another_name_is_not_guest_login(tmp_path):
    db, _ = _db(tmp_path)
    create_user(db, "alice", password="hunter2", user_level=10)
    set_guest_username(db, "guest")
    assert is_guest_login(db, "alice") is False
    db.close()


def test_clearing_it_turns_guest_login_off(tmp_path):
    db, _ = _db(tmp_path)
    set_guest_username(db, "guest")
    set_guest_username(db, None)
    assert guest_username(db) is None
    assert is_guest_login(db, "guest") is False
    db.close()


def test_the_account_survives_guest_login_being_turned_off(tmp_path):
    """Turning guest access off is a config change, not an account
    change -- the account keeps its password and can still sign in."""
    from netbbs.auth.users import authenticate_password

    db, _ = _db(tmp_path)
    set_guest_username(db, "guest")
    set_guest_username(db, None)
    assert authenticate_password(db, "guest", "hunter2") is not None
    db.close()


# -- Failing closed ---------------------------------------------------


def test_a_deleted_guest_account_resolves_to_nothing(tmp_path):
    """The designation outlives the account, so it has to fail closed:
    guest login stops working rather than the name matching something
    unintended."""
    from netbbs.auth.users import delete_user

    db, guest = _db(tmp_path)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_guest_username(db, "guest")
    delete_user(db, guest, deleted_by=sysop)
    assert guest_username(db) == "guest"  # the configuration is still there
    assert guest_user(db) is None  # ...but it resolves to nobody
    db.close()


# -- The pre-login notice ---------------------------------------------


def test_the_notice_is_empty_until_set(tmp_path):
    db, _ = _db(tmp_path)
    assert pre_login_notice(db) == ""
    db.close()


def test_the_notice_round_trips(tmp_path):
    db, _ = _db(tmp_path)
    set_pre_login_notice(db, "Here for NetBBS? Sign in as 'guest' to download.")
    assert pre_login_notice(db) == "Here for NetBBS? Sign in as 'guest' to download."
    db.close()


def test_an_over_long_notice_is_bounded(tmp_path):
    """It is a display string with no meaning to anything else, so it is
    truncated rather than refused -- a SysOp cannot see the length while
    typing."""
    db, _ = _db(tmp_path)
    set_pre_login_notice(db, "x" * (MAX_PRE_LOGIN_NOTICE_LENGTH + 100))
    assert len(pre_login_notice(db)) == MAX_PRE_LOGIN_NOTICE_LENGTH
    db.close()


def test_the_notice_can_be_cleared(tmp_path):
    db, _ = _db(tmp_path)
    set_pre_login_notice(db, "something")
    set_pre_login_notice(db, "")
    assert pre_login_notice(db) == ""
    db.close()
