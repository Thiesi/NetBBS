"""
Tests for netbbs.auth.users' SysOp-foundation additions (design doc
§4.3): SYSOP_LEVEL, count_sysops, set_user_level,
set_user_disabled, delete_user, and disabled-account rejection at every
auth entry point. Account creation/password/keypair-login behavior
itself is already covered in tests/test_auth.py; this file only
exercises what's new.
"""

from __future__ import annotations

import asyncio

import nacl.signing
import pytest

from netbbs.auth.users import (
    SYSOP_LEVEL,
    AuthError,
    UserManagementError,
    approve_pending_user,
    authenticate_keypair,
    authenticate_password,
    authenticate_password_async,
    authorize_public_key,
    count_sysops,
    create_user,
    delete_user,
    generate_challenge,
    get_user_by_username,
    has_password,
    list_users,
    password_matches,
    set_password,
    set_user_disabled,
    set_user_level,
)
from netbbs.moderation.log import list_actions_for_target_user
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


# -- list_users' order_by (design doc -- Thiesi's own dogfood-testing report,
# -- SysOps wanted more than the one fixed alphabetical order) --------------


def test_list_users_defaults_to_alphabetical(db, sysop):
    create_user(db, "zebra", password="hunter2", user_level=0)
    create_user(db, "alpha", password="hunter2", user_level=0)
    assert [u.username for u in list_users(db)] == ["alpha", "sysop", "zebra"]


def test_list_users_alphabetical_is_case_insensitive(db, sysop):
    create_user(db, "Bob", password="hunter2", user_level=0)
    create_user(db, "alice", password="hunter2", user_level=0)
    assert [u.username for u in list_users(db, order_by="alphabetical")] == ["alice", "Bob", "sysop"]


def test_list_users_alphabetical_desc_reverses_alphabetical(db, sysop):
    create_user(db, "zebra", password="hunter2", user_level=0)
    create_user(db, "alpha", password="hunter2", user_level=0)
    assert [u.username for u in list_users(db, order_by="alphabetical_desc")] == [
        "zebra", "sysop", "alpha"
    ]


def test_list_users_registered_orders_oldest_account_first(db, sysop):
    # sysop (created in the fixture) already exists before either of
    # these, so it's the oldest regardless of name.
    create_user(db, "zebra", password="hunter2", user_level=0)
    create_user(db, "alpha", password="hunter2", user_level=0)
    assert [u.username for u in list_users(db, order_by="registered")][0] == "sysop"


def test_list_users_registered_desc_orders_newest_account_first(db, sysop):
    create_user(db, "zebra", password="hunter2", user_level=0)
    create_user(db, "alpha", password="hunter2", user_level=0)
    assert [u.username for u in list_users(db, order_by="registered_desc")][-1] == "sysop"


def test_list_users_level_asc_orders_lowest_level_first_with_alphabetical_tiebreak(db, sysop):
    create_user(db, "alice", password="hunter2", user_level=50)
    create_user(db, "bob", password="hunter2", user_level=10)
    create_user(db, "carol", password="hunter2", user_level=10)
    # bob/carol tie at level 10 -- broken alphabetically; sysop (255) last.
    assert [u.username for u in list_users(db, order_by="level_asc")] == [
        "bob", "carol", "alice", "sysop"
    ]


def test_list_users_level_desc_orders_highest_level_first_with_alphabetical_tiebreak(db, sysop):
    create_user(db, "alice", password="hunter2", user_level=50)
    create_user(db, "bob", password="hunter2", user_level=10)
    create_user(db, "carol", password="hunter2", user_level=10)
    assert [u.username for u in list_users(db, order_by="level_desc")] == [
        "sysop", "alice", "bob", "carol"
    ]


def test_list_users_rejects_an_unknown_order_by(db, sysop):
    with pytest.raises(ValueError):
        list_users(db, order_by="nonsense")


# -- count_sysops -----------------------------------------------------------


def test_count_sysops_counts_only_active_sysop_level_accounts(db, sysop):
    create_user(db, "alice", password="hunter2", user_level=10)
    assert count_sysops(db) == 1


def test_count_sysops_excludes_disabled_sysops(db, sysop):
    other = create_user(db, "other", password="hunter2", user_level=SYSOP_LEVEL)
    set_user_disabled(db, other, True, changed_by=sysop)
    assert count_sysops(db) == 1


def test_count_sysops_zero_when_none_exist(db):
    create_user(db, "alice", password="hunter2", user_level=10)
    assert count_sysops(db) == 0


def test_count_sysops_excludes_pending_approval_sysop_level_accounts(db):
    """GitHub issue #44: a pending level-255 row (however it got that
    way) must not be counted as a usable SysOp."""
    create_user(db, "pending", password="hunter2", user_level=SYSOP_LEVEL, pending_approval=True)
    assert count_sysops(db) == 0


def test_approving_a_pending_sysop_level_account_counts_it_immediately(db, sysop):
    """GitHub issue #44: once approved, the previously-pending SysOp-
    level account becomes usable right away, no restart needed."""
    pending = create_user(
        db, "pending", password="hunter2", user_level=SYSOP_LEVEL, pending_approval=True
    )
    assert count_sysops(db) == 1
    approve_pending_user(db, pending, approved_by=sysop)
    assert count_sysops(db) == 2


# -- set_user_level -----------------------------------------------------


def test_set_user_level_promotes(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    updated = set_user_level(db, alice, SYSOP_LEVEL, changed_by=sysop)
    assert updated.user_level == SYSOP_LEVEL


def test_set_user_level_records_audit_entry(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    set_user_level(db, alice, 20, changed_by=sysop)
    entries = list_actions_for_target_user(db, alice.id)
    assert any(e.action == "promote" and e.actor_user_id == sysop.id for e in entries)


def test_set_user_level_demote_refused_for_sole_active_sysop(db, sysop):
    with pytest.raises(UserManagementError):
        set_user_level(db, sysop, 10, changed_by=sysop)
    # Refused, not partially applied.
    assert count_sysops(db) == 1


def test_set_user_level_demote_allowed_with_a_second_active_sysop(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    alice = set_user_level(db, alice, SYSOP_LEVEL, changed_by=sysop)
    updated = set_user_level(db, sysop, 10, changed_by=alice)
    assert updated.user_level == 10
    assert count_sysops(db) == 1


def test_set_user_level_is_a_noop_when_unchanged(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    updated = set_user_level(db, alice, 10, changed_by=sysop)
    assert updated.user_level == 10
    assert list_actions_for_target_user(db, alice.id) == []


def test_set_user_level_refuses_to_promote_a_pending_account_to_sysop(db, sysop):
    """GitHub issue #44: promoting a still-pending registration straight
    to SysOp level must be refused outright, not merely uncounted --
    otherwise the safety check below (does this leave a usable SysOp?)
    is never even reached for the *real* SysOp being removed."""
    pending = create_user(
        db, "pending", password="hunter2", user_level=0, pending_approval=True
    )
    with pytest.raises(UserManagementError):
        set_user_level(db, pending, SYSOP_LEVEL, changed_by=sysop)
    assert count_sysops(db) == 1


def test_pending_sysop_level_row_does_not_defeat_last_sysop_protection(db, sysop):
    """GitHub issue #44 repro: even if a pending account already holds
    a SysOp-level row (bypassing the promotion-time check above, e.g.
    via direct database maintenance), it must not let the one real,
    usable SysOp be disabled/demoted/deleted."""
    pending = create_user(
        db, "pending", password="hunter2", user_level=SYSOP_LEVEL, pending_approval=True
    )
    with pytest.raises(UserManagementError):
        set_user_level(db, sysop, 10, changed_by=sysop)
    with pytest.raises(UserManagementError):
        set_user_disabled(db, sysop, True, changed_by=sysop)
    with pytest.raises(UserManagementError):
        delete_user(db, sysop, deleted_by=sysop)
    # The pending row itself is not protected -- it was never usable.
    updated = set_user_disabled(db, pending, True, changed_by=sysop)
    assert updated.disabled_at is not None


# -- set_user_disabled ----------------------------------------------------


def test_set_user_disabled_disables_and_reenables(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    disabled = set_user_disabled(db, alice, True, changed_by=sysop)
    assert disabled.disabled_at is not None
    enabled = set_user_disabled(db, disabled, False, changed_by=sysop)
    assert enabled.disabled_at is None


def test_set_user_disabled_records_audit_entry(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    set_user_disabled(db, alice, True, changed_by=sysop)
    entries = list_actions_for_target_user(db, alice.id)
    assert any(e.action == "disable" for e in entries)


def test_set_user_disabled_refused_for_sole_active_sysop(db, sysop):
    with pytest.raises(UserManagementError):
        set_user_disabled(db, sysop, True, changed_by=sysop)


def test_set_user_disabled_allowed_with_a_second_active_sysop(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=SYSOP_LEVEL)
    updated = set_user_disabled(db, sysop, True, changed_by=alice)
    assert updated.disabled_at is not None


# -- delete_user --------------------------------------------------------


def test_delete_user_removes_the_account(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    delete_user(db, alice, deleted_by=sysop)
    with pytest.raises(AuthError):
        authenticate_password(db, "alice", "hunter2")


def test_delete_user_refused_for_sole_active_sysop(db, sysop):
    with pytest.raises(UserManagementError):
        delete_user(db, sysop, deleted_by=sysop)
    # Refused, not partially applied.
    assert authenticate_password(db, "sysop", "hunter2").username == "sysop"


def test_delete_user_allowed_with_a_second_active_sysop(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=SYSOP_LEVEL)
    delete_user(db, sysop, deleted_by=alice)
    assert count_sysops(db) == 1


def test_delete_user_leaves_an_audit_trail_with_null_target(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    delete_user(db, alice, deleted_by=sysop)
    row = db.connection.execute(
        "SELECT action, target_user_id, detail FROM moderation_log WHERE action = 'delete_user'"
    ).fetchone()
    assert row["target_user_id"] is None
    assert "alice" in row["detail"]


def test_self_delete_does_not_break_the_audit_log(db, sysop):
    """The deleting SysOp deletes their own account -- record_action's
    actor_user_id FK must not blow up once that row is gone (see
    delete_user's log-before-delete ordering)."""
    alice = create_user(db, "alice", password="hunter2", user_level=SYSOP_LEVEL)
    delete_user(db, alice, deleted_by=alice)
    row = db.connection.execute(
        "SELECT actor_user_id, target_user_id FROM moderation_log WHERE action = 'delete_user'"
    ).fetchone()
    assert row["actor_user_id"] is None
    assert row["target_user_id"] is None


# -- disabled-account rejection at every auth entry point --------------------


def test_disabled_account_rejected_at_password_login(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    set_user_disabled(db, alice, True, changed_by=sysop)
    with pytest.raises(AuthError):
        authenticate_password(db, "alice", "hunter2")


def test_disabled_account_rejected_at_async_password_login(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    set_user_disabled(db, alice, True, changed_by=sysop)

    async def scenario() -> None:
        with pytest.raises(AuthError):
            await authenticate_password_async(db, "alice", "hunter2")

    asyncio.run(scenario())


def test_disabled_account_rejected_at_keypair_login(db, sysop):
    signing_key = nacl.signing.SigningKey.generate()
    alice = create_user(db, "alice", verify_key=signing_key.verify_key, user_level=10)
    set_user_disabled(db, alice, True, changed_by=sysop)
    challenge = generate_challenge()
    signature = signing_key.sign(challenge).signature
    with pytest.raises(AuthError):
        authenticate_keypair(db, "alice", challenge, signature)


def test_disabled_account_rejected_at_pubkey_authorization(db, sysop):
    signing_key = nacl.signing.SigningKey.generate()
    alice = create_user(db, "alice", verify_key=signing_key.verify_key, user_level=10)
    set_user_disabled(db, alice, True, changed_by=sysop)
    with pytest.raises(AuthError):
        authorize_public_key(db, "alice", signing_key.verify_key)


def test_reenabled_account_can_log_in_again(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    alice = set_user_disabled(db, alice, True, changed_by=sysop)
    set_user_disabled(db, alice, False, changed_by=sysop)
    user = authenticate_password(db, "alice", "hunter2")
    assert user.username == "alice"


# -- password lifecycle (issue #611) --------------------------------------
#
# Until #611 `users.password_hash` was written once, at creation, and had
# no UPDATE anywhere. These prove the domain half; the screens that call
# it are driven end to end in tests/test_password_screen.py.


def test_set_password_replaces_the_password_for_the_next_login(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    set_password(db, alice, "n3w-pass", changed_by=alice)
    assert authenticate_password(db, "alice", "n3w-pass").username == "alice"
    with pytest.raises(AuthError):
        authenticate_password(db, "alice", "hunter2")


def test_set_password_records_who_changed_it_and_nothing_else(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    set_password(db, alice, "n3w-pass", changed_by=sysop)
    entry = list_actions_for_target_user(db, alice.id)[-1]
    assert entry.action == "set_password"
    assert entry.actor_user_id == sysop.id
    assert entry.detail is None


def test_set_password_refuses_a_blank_password(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    with pytest.raises(AuthError, match="cannot be blank"):
        set_password(db, alice, "", changed_by=alice)
    assert authenticate_password(db, "alice", "hunter2").username == "alice"
    assert list_actions_for_target_user(db, alice.id) == []


def test_clearing_the_password_is_refused_without_a_key(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    with pytest.raises(AuthError, match="no SSH/public key"):
        set_password(db, alice, None, changed_by=sysop)
    assert has_password(db, alice)
    assert authenticate_password(db, "alice", "hunter2").username == "alice"


def test_clearing_the_password_makes_the_account_key_only(db, sysop):
    signing_key = nacl.signing.SigningKey.generate()
    alice = create_user(db, "alice", password="hunter2", verify_key=signing_key.verify_key, user_level=10)
    set_password(db, alice, None, changed_by=alice)
    assert not has_password(db, alice)
    with pytest.raises(AuthError):
        authenticate_password(db, "alice", "hunter2")
    challenge = generate_challenge()
    assert authenticate_keypair(db, "alice", challenge, signing_key.sign(challenge).signature).username == "alice"
    assert list_actions_for_target_user(db, alice.id)[-1].action == "clear_password"


def test_a_key_only_account_can_be_given_a_password(db, sysop):
    signing_key = nacl.signing.SigningKey.generate()
    alice = create_user(db, "alice", verify_key=signing_key.verify_key, user_level=10)
    assert not has_password(db, alice)
    set_password(db, alice, "first", changed_by=sysop)
    assert has_password(db, alice)
    assert authenticate_password(db, "alice", "first").username == "alice"


def test_password_matches_answers_without_logging_in(db, sysop):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    assert password_matches(db, alice, "hunter2") is True
    assert password_matches(db, alice, "wrong") is False
    # Not a login: nothing recorded, disabled state irrelevant.
    assert get_user_by_username(db, "alice").last_login_at is None
    alice = set_user_disabled(db, alice, True, changed_by=sysop)
    assert password_matches(db, alice, "hunter2") is True


def test_password_matches_is_false_for_a_key_only_account(db, sysop):
    signing_key = nacl.signing.SigningKey.generate()
    alice = create_user(db, "alice", verify_key=signing_key.verify_key, user_level=10)
    assert password_matches(db, alice, "") is False
    assert password_matches(db, alice, "anything") is False


def test_set_password_survives_a_stale_target_reference(db, sysop):
    # `User` is frozen and callers hold snapshots; the function acts on
    # the current row, same as every other setter in this module.
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    set_user_level(db, alice, 20, changed_by=sysop)
    updated = set_password(db, alice, "n3w-pass", changed_by=sysop)
    assert updated.user_level == 20
    assert authenticate_password(db, "alice", "n3w-pass").user_level == 20
