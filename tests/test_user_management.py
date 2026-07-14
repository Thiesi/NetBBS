"""
Tests for netbbs.auth.users' SysOp-foundation additions (design doc --
SysOp foundation round): SYSOP_LEVEL, count_sysops, set_user_level,
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
    authenticate_keypair,
    authenticate_password,
    authenticate_password_async,
    authorize_public_key,
    count_sysops,
    create_user,
    delete_user,
    generate_challenge,
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
