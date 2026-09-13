"""Guest login as an authentication shortcut (issue #531).

The design decision this pins down: the guest is a *real account*, so
levels, per-object permissions, gates, moderation and auditing all keep
working for it exactly as for anybody else, and nothing in the codebase
branches on "is this caller a guest". Skipping the password prompt is
the entire feature.

These tests are mostly about what guest login does *not* skip. Several
of them exist because a review found it skipping things it should not:
the checks in `guest_login_for` are each somebody's way in if they are
missing.
"""

from __future__ import annotations

from netbbs.auth.users import SYSOP_LEVEL, create_user, delete_user, set_user_disabled, set_user_level
from netbbs.guest import (
    MAX_PRE_LOGIN_NOTICE_LENGTH,
    guest_login_for,
    guest_user,
    guest_user_id,
    pre_login_notice,
    set_guest_user,
    set_pre_login_notice,
)
from netbbs.storage.database import Database


def _db(tmp_path):
    db = Database(tmp_path / "node.db")
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    return db, guest, sysop


# -- Designating an account -------------------------------------------


def test_guest_login_is_off_until_an_account_is_designated(tmp_path):
    db, _, _ = _db(tmp_path)
    assert guest_user_id(db) is None
    assert guest_user(db) is None
    assert guest_login_for(db, "guest") is None
    db.close()


def test_a_designated_account_signs_in(tmp_path):
    db, guest, _ = _db(tmp_path)
    set_guest_user(db, guest)
    assert guest_login_for(db, "guest").id == guest.id
    db.close()


def test_the_name_is_matched_case_insensitively(tmp_path):
    """A caller told to "sign in as guest" should not be refused for
    typing "Guest", the same way the `new` sentinel is forgiving."""
    db, guest, _ = _db(tmp_path)
    set_guest_user(db, guest)
    assert guest_login_for(db, "Guest") is not None
    assert guest_login_for(db, "  GUEST  ") is not None
    db.close()


def test_another_name_is_not_guest_login(tmp_path):
    db, guest, _ = _db(tmp_path)
    create_user(db, "alice", password="hunter2", user_level=10)
    set_guest_user(db, guest)
    assert guest_login_for(db, "alice") is None
    db.close()


def test_clearing_it_turns_guest_login_off(tmp_path):
    db, guest, _ = _db(tmp_path)
    set_guest_user(db, guest)
    set_guest_user(db, None)
    assert guest_user_id(db) is None
    assert guest_login_for(db, "guest") is None
    db.close()


def test_the_account_survives_guest_login_being_turned_off(tmp_path):
    """Turning guest access off is a config change, not an account
    change -- the account keeps its password and can still sign in."""
    from netbbs.auth.users import authenticate_password

    db, guest, _ = _db(tmp_path)
    set_guest_user(db, guest)
    set_guest_user(db, None)
    assert authenticate_password(db, "guest", "hunter2") is not None
    db.close()


# -- What guest login refuses -----------------------------------------
#
# Every one of these was a way in before the review that found it.


def test_a_recreated_account_does_not_inherit_the_designation(tmp_path):
    """The designation is an account id, not a name.

    It was a name first, with a comment claiming that deleting and
    recreating under the same name would not point guest login
    elsewhere. Exactly backwards: a name lookup resolves whatever row
    holds the name now, so the replacement -- with whatever permissions
    it happened to have -- would have been handed passwordless access.
    """
    db, guest, sysop = _db(tmp_path)
    set_guest_user(db, guest)
    delete_user(db, guest, deleted_by=sysop)
    impostor = create_user(db, "guest", password="different", user_level=200)
    assert impostor.username == "guest"
    assert guest_login_for(db, "guest") is None
    db.close()


def test_a_deleted_guest_account_stops_being_special(tmp_path):
    db, guest, sysop = _db(tmp_path)
    set_guest_user(db, guest)
    delete_user(db, guest, deleted_by=sysop)
    assert guest_user(db) is None
    assert guest_login_for(db, "guest") is None
    db.close()


def test_a_disabled_guest_account_is_refused(tmp_path):
    """`get_user_by_username` filters no account status, so this gate
    has to be applied here -- the password path applies its own."""
    db, guest, sysop = _db(tmp_path)
    set_guest_user(db, guest)
    set_user_disabled(db, guest, disabled=True, changed_by=sysop)
    assert guest_login_for(db, "guest") is None
    db.close()


def test_an_account_awaiting_approval_is_refused(tmp_path):
    db, _, _ = _db(tmp_path)
    pending = create_user(db, "newcomer", password="hunter2", user_level=1, pending_approval=True)
    set_guest_user(db, pending)
    assert guest_login_for(db, "newcomer") is None
    db.close()


def test_promoting_the_guest_to_sysop_revokes_passwordless_login(tmp_path):
    """The prohibition has to hold when it is *used*, not only when the
    designation is saved. Checking it at save time alone left a
    passwordless privilege-escalation path: designate an ordinary
    account, then promote it through the user-detail level action."""
    db, guest, sysop = _db(tmp_path)
    set_guest_user(db, guest)
    assert guest_login_for(db, "guest") is not None
    set_user_level(db, guest, SYSOP_LEVEL, changed_by=sysop)
    assert guest_login_for(db, "guest") is None
    db.close()


# -- The pre-login notice ---------------------------------------------


def test_the_notice_is_empty_until_set(tmp_path):
    db, _, _ = _db(tmp_path)
    assert pre_login_notice(db) == ""
    db.close()


def test_the_notice_round_trips(tmp_path):
    db, _, _ = _db(tmp_path)
    set_pre_login_notice(db, "Here for NetBBS? Sign in as 'guest' to download.")
    assert pre_login_notice(db) == "Here for NetBBS? Sign in as 'guest' to download."
    db.close()


def test_an_over_long_notice_is_bounded(tmp_path):
    db, _, _ = _db(tmp_path)
    set_pre_login_notice(db, "x" * (MAX_PRE_LOGIN_NOTICE_LENGTH + 100))
    assert len(pre_login_notice(db)) == MAX_PRE_LOGIN_NOTICE_LENGTH
    db.close()


def test_the_notice_can_be_cleared(tmp_path):
    db, _, _ = _db(tmp_path)
    set_pre_login_notice(db, "something")
    set_pre_login_notice(db, "")
    assert pre_login_notice(db) == ""
    db.close()
