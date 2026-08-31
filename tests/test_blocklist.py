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


# -- blocking keypair-holding users (also the local_user_id path) -----------


def test_block_keypair_user_by_local_user_id(db, sysop, bob):
    # Once an account can register more than one SSH key
    # (netbbs.auth.users.add_ssh_key), keying a block by whichever
    # fingerprint happened to be primary at block time would leave every
    # *other* key on the same account unblocked -- local_user_id has no
    # such gap, so block_user always uses it for a local account now,
    # never fingerprint, regardless of whether the account has a key
    # (see block_user's own docstring).
    entry = block_user(db, bob, blocked_by=sysop)
    assert entry.local_user_id == bob.id
    assert entry.fingerprint is None


def test_is_blocked_true_for_keypair_user(db, sysop, bob):
    block_user(db, bob, blocked_by=sysop)
    assert is_blocked(db, bob) is True


def test_is_blocked_false_for_unblocked_keypair_user(db, bob):
    assert is_blocked(db, bob) is False


def test_blocking_an_account_blocks_every_key_it_holds(db, sysop, bob):
    # The actual security gap local_user_id-only keying closes: block
    # bob (his one key at block time), then give him a second key --
    # both must stay blocked, since the block is account-level, never
    # tied to any one key. A fingerprint-keyed block (the old default
    # for a keypair-holding target) would have left this second key
    # free to log in.
    import nacl.signing

    from netbbs.auth.users import add_ssh_key

    block_user(db, bob, blocked_by=sysop)
    assert is_blocked(db, bob) is True

    second_key = nacl.signing.SigningKey.generate()
    bob_with_second_key = add_ssh_key(db, bob, second_key.verify_key, label="phone", changed_by=sysop)
    assert is_blocked(db, bob_with_second_key) is True


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


def test_is_blocked_survives_gaining_a_key_after_being_blocked_password_only(db, sysop):
    """
    Regression-style test for the edge case is_blocked's own docstring
    describes: a user blocked while password-only (by local_user_id)
    must still show as blocked after later gaining an SSH key. Used to
    be simulated with a synthetic User object, since no "add a keypair
    later" flow existed yet to test end-to-end -- netbbs.auth.users.
    add_ssh_key now makes this a real, exercisable flow.
    """
    signing_key = nacl.signing.SigningKey.generate()
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    block_user(db, carol, blocked_by=sysop)

    from netbbs.auth.users import add_ssh_key

    carol_with_key = add_ssh_key(db, carol, signing_key.verify_key, label="phone", changed_by=sysop)
    assert carol_with_key.fingerprint is not None
    assert is_blocked(db, carol_with_key) is True
