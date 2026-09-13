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
    guest_designation,
    guest_is_eligible,
    guest_login_for,
    guest_user,
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
    assert guest_designation(db) is None
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
    assert guest_designation(db) is None
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
    """Neither a name nor an id alone is an identity here.

    A name lookup resolves whatever row holds the name now. And an id is
    *reusable*: `users.id` is `INTEGER PRIMARY KEY` without
    `AUTOINCREMENT`, so SQLite hands back the highest free rowid --
    delete the newest account and the next one created takes its number.

    The guest is created **last** here on purpose, so it holds the
    highest id and deleting it frees exactly that number. An earlier
    version of this test created it first, which meant the recreated
    account got a fresh id and the reuse never happened -- the test
    passed while the hole was wide open.
    """
    db, _, sysop = _db(tmp_path)
    guest = create_user(db, "guest2", password="hunter2", user_level=1)
    set_guest_user(db, guest)
    delete_user(db, guest, deleted_by=sysop)
    impostor = create_user(db, "guest2", password="different", user_level=200)
    assert impostor.id == guest.id, "this test is pointless unless the id is actually reused"
    assert guest_login_for(db, "guest2") is None
    db.close()


def test_eligibility_is_re_checkable_against_a_refreshed_row(tmp_path):
    """The login path re-fetches the account after its last await, so
    the row it finally returns has to be validated too -- a promotion
    landing in that window otherwise came back as a SysOp session."""
    db, guest, sysop = _db(tmp_path)
    set_guest_user(db, guest)
    assert guest_is_eligible(db, guest) is True

    from netbbs.auth.users import get_user_by_id, set_user_level

    set_user_level(db, guest, SYSOP_LEVEL, changed_by=sysop)
    assert guest_is_eligible(db, get_user_by_id(db, guest.id)) is False
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


# -- Neither number is unique on its own -------------------------------


def test_the_designation_goes_when_the_account_does(tmp_path):
    """Round five. The `(id, created_at)` pair is not quite unique:
    `users.id` is a reusable rowid, and `created_at` is not a tiebreaker
    either -- this project's suite has seen two accounts created close
    enough together to share a stored timestamp, which is why
    `list_users` sorts "registered" by id rather than by `created_at`
    alone. Deleting the account drops the designation in the same
    transaction, so there is nothing left to collide with.
    """
    db, _, sysop = _db(tmp_path)
    guest = create_user(db, "guest2", password="hunter2", user_level=1)
    set_guest_user(db, guest)
    delete_user(db, guest, deleted_by=sysop)
    assert guest_designation(db) is None
    db.close()


def test_a_stamp_sharing_replacement_is_still_refused(tmp_path):
    """The collision itself, forced rather than waited for: the
    replacement is given the deleted account's id *and* its timestamp.
    Without the deletion hook this is passwordless access to whatever
    the new account turned out to be.
    """
    db, _, sysop = _db(tmp_path)
    guest = create_user(db, "guest2", password="hunter2", user_level=1)
    set_guest_user(db, guest)
    delete_user(db, guest, deleted_by=sysop)

    impostor = create_user(db, "guest2", password="different", user_level=200)
    db.connection.execute(
        "UPDATE users SET id = ?, created_at = ? WHERE id = ?",
        (guest.id, guest.created_at, impostor.id),
    )
    db.connection.commit()

    assert guest_login_for(db, "guest2") is None
    db.close()


def test_touching_last_login_refuses_a_row_that_is_not_that_account(tmp_path):
    """`touch_last_login` re-reads by id after the login path's last
    await. An id freed in that window and handed to another account
    meant this wrote a login timestamp onto a stranger's row before the
    caller was refused (Codex review)."""
    from netbbs.auth.users import touch_last_login

    db, guest, sysop = _db(tmp_path)
    delete_user(db, guest, deleted_by=sysop)
    replacement = create_user(db, "someone-else", password="hunter2", user_level=1)
    db.connection.execute("UPDATE users SET id = ? WHERE id = ?", (guest.id, replacement.id))
    db.connection.commit()

    assert touch_last_login(db, guest) is None
    landed = db.connection.execute(
        "SELECT last_login_at FROM users WHERE id = ?", (guest.id,)
    ).fetchone()
    assert landed["last_login_at"] is None, "a refused guest attempt wrote to another account"
    db.close()


# -- The database decides what a name matches --------------------------


def test_a_name_the_database_calls_a_different_account_is_not_the_guest(tmp_path):
    """`strip().casefold()` is not SQLite's `COLLATE NOCASE`, and where
    the two disagree the difference was a way in (Codex review): a
    legacy long-s account and an ordinary `s` account can both exist,
    casefold calls their names equal, and typing one would have signed
    the caller in as the other.

    The long s is created directly, since account creation would reject
    it now -- which is exactly the "legacy row" this is about.
    """
    db, _, _ = _db(tmp_path)
    guest = create_user(db, "sam", password="hunter2", user_level=1)
    other = create_user(db, "zzz", password="hunter2", user_level=200)
    db.connection.execute(
        "UPDATE users SET username = ? WHERE id = ?", ("\u017fam", other.id)
    )
    db.connection.commit()
    set_guest_user(db, other)

    # Typed as "sam": casefold says that is the designated account, the
    # database says it is a different row. The database wins.
    assert guest_login_for(db, "sam") is None
    assert guest_login_for(db, "\u017fam") is not None
    assert guest_login_for(db, "sam") is None or guest_login_for(db, "sam").id != other.id
    assert guest.id != other.id
    db.close()


def test_the_name_is_still_matched_case_insensitively(tmp_path):
    """The database's own rule, which is case-insensitive -- so the
    forgiving behaviour a caller told to "sign in as guest" depends on
    survives this."""
    db, guest, _ = _db(tmp_path)
    set_guest_user(db, guest)
    assert guest_login_for(db, "GUEST") is not None
    assert guest_login_for(db, "  Guest  ") is not None
    db.close()
