"""Tests for netbbs.moderation.blocklist."""

from __future__ import annotations

import nacl.signing
import pytest

from netbbs.auth.users import create_user
from netbbs.moderation import BlocklistError, block_user, is_blocked, list_blocklist, unblock_user
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=100)


@pytest.fixture
def alice(db):
    """Password-only user — no fingerprint."""
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    """Keypair-holding user — has a fingerprint."""
    signing_key = nacl.signing.SigningKey.generate()
    return create_user(db, "bob", verify_key=signing_key.verify_key, user_level=10)


# -- blocking password-only users (local_user_id path) ----------------------


def test_block_password_only_user_by_local_user_id(db, sysop, alice):
    entry = block_user(db, alice, blocked_by=sysop, reason="testing")
    assert entry.local_user_id == alice.id
    assert entry.fingerprint is None
    assert entry.reason == "testing"
    assert entry.blocked_by_user_id == sysop.id


def test_is_blocked_true_for_password_only_user(db, sysop, alice):
    block_user(db, alice, blocked_by=sysop)
    assert is_blocked(db, alice) is True


def test_is_blocked_false_for_unblocked_password_only_user(db, alice):
    assert is_blocked(db, alice) is False


# -- blocking keypair-holding users (fingerprint path) -----------------------


def test_block_keypair_user_by_fingerprint(db, sysop, bob):
    entry = block_user(db, bob, blocked_by=sysop)
    assert entry.fingerprint == bob.fingerprint
    assert entry.local_user_id is None


def test_is_blocked_true_for_keypair_user(db, sysop, bob):
    block_user(db, bob, blocked_by=sysop)
    assert is_blocked(db, bob) is True


def test_is_blocked_false_for_unblocked_keypair_user(db, bob):
    assert is_blocked(db, bob) is False


# -- double-blocking / unblocking --------------------------------------------


def test_blocking_already_blocked_user_fails(db, sysop, alice):
    block_user(db, alice, blocked_by=sysop)
    with pytest.raises(BlocklistError):
        block_user(db, alice, blocked_by=sysop)


def test_unblock_removes_entry(db, sysop, alice):
    block_user(db, alice, blocked_by=sysop)
    unblock_user(db, alice)
    assert is_blocked(db, alice) is False


def test_unblock_nonexistent_entry_does_not_raise(db, alice):
    unblock_user(db, alice)  # never blocked — must not raise


def test_reblock_after_unblock_succeeds(db, sysop, alice):
    block_user(db, alice, blocked_by=sysop)
    unblock_user(db, alice)
    entry = block_user(db, alice, blocked_by=sysop)  # must not raise
    assert entry.local_user_id == alice.id


# -- listing -----------------------------------------------------------------


def test_list_blocklist_returns_all_entries(db, sysop, alice, bob):
    block_user(db, alice, blocked_by=sysop, reason="reason A")
    block_user(db, bob, blocked_by=sysop, reason="reason B")
    entries = list_blocklist(db)
    assert len(entries) == 2


def test_list_blocklist_empty_when_nothing_blocked(db):
    assert list_blocklist(db) == []


# -- independence between users ----------------------------------------------


def test_blocking_one_user_does_not_affect_another(db, sysop, alice, bob):
    block_user(db, alice, blocked_by=sysop)
    assert is_blocked(db, alice) is True
    assert is_blocked(db, bob) is False


def test_is_blocked_checks_local_user_id_even_when_fingerprint_present(db, sysop):
    """
    Regression-style test for the edge case handled explicitly in
    is_blocked's docstring: a user blocked while password-only (by
    local_user_id) should still show as blocked even in a hypothetical
    check made after they'd have a fingerprint — verified here by
    checking the OR-based query directly rather than the (not yet
    implemented) "add a keypair later" flow, since that flow doesn't
    exist yet to test end-to-end.
    """
    signing_key = nacl.signing.SigningKey.generate()
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    block_user(db, carol, blocked_by=sysop)

    # Simulate "carol later gained a fingerprint" by constructing a User
    # object with the same id but a fingerprint set, without going
    # through a real (not-yet-existing) add-keypair flow.
    from netbbs.identity.keys import fingerprint_from_verify_key

    carol_with_fingerprint = carol.__class__(
        id=carol.id,
        username=carol.username,
        user_level=carol.user_level,
        fingerprint=fingerprint_from_verify_key(signing_key.verify_key),
        created_at=carol.created_at,
        last_login_at=carol.last_login_at,
    )
    assert is_blocked(db, carol_with_fingerprint) is True
