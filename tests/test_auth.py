"""Tests for netbbs.auth — account creation, password login, keypair login."""

from __future__ import annotations

import nacl.signing
import pytest

from netbbs.auth.users import (
    AuthError,
    authenticate_keypair,
    authenticate_password,
    authorize_public_key,
    clear_verify_key,
    create_user,
    generate_challenge,
    get_user_by_username,
    set_verify_key,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


# -- account creation -----------------------------------------------------


def test_create_user_with_password_only(db):
    user = create_user(db, "thiesi", password="hunter2")
    assert user.username == "thiesi"
    assert user.fingerprint is None


def test_create_user_with_keypair_only(db):
    signing_key = nacl.signing.SigningKey.generate()
    user = create_user(db, "thiesi", verify_key=signing_key.verify_key)
    assert user.fingerprint is not None


def test_create_user_with_both(db):
    signing_key = nacl.signing.SigningKey.generate()
    user = create_user(db, "thiesi", password="hunter2", verify_key=signing_key.verify_key)
    assert user.fingerprint is not None


def test_create_user_with_neither_fails(db):
    with pytest.raises(AuthError):
        create_user(db, "thiesi")


def test_create_duplicate_username_fails(db):
    create_user(db, "thiesi", password="hunter2")
    with pytest.raises(AuthError):
        create_user(db, "thiesi", password="different")


def test_create_case_variant_duplicate_username_fails(db):
    create_user(db, "thiesi", password="hunter2")
    with pytest.raises(AuthError):
        create_user(db, "Thiesi", password="different")


# -- username grammar (GitHub issue #26) ------------------------------------


def test_create_user_rejects_a_colon_in_the_username(db):
    """The delimiter netbbs.chat.hub.ParticipantId's string-encoded
    predecessor used to be parsed against -- a username containing ':'
    could be mistaken for a different account's session ID."""
    with pytest.raises(AuthError):
        create_user(db, "alice:alt", password="hunter2")


@pytest.mark.parametrize(
    "bad_username",
    [
        "alice bob",  # whitespace
        "alice/bob",  # path-separator-like punctuation
        "alice\tbob",  # control character
        "",  # empty
        "   ",  # whitespace-only
        "alice\x1b[2Jbob",  # ANSI escape sequence
    ],
)
def test_create_user_rejects_usernames_outside_the_allowlist(db, bad_username):
    with pytest.raises(AuthError):
        create_user(db, bad_username, password="hunter2")


@pytest.mark.parametrize("good_username", ["alice", "alice_bob", "alice-bob", "alice.bob", "Alice123"])
def test_create_user_allows_usernames_within_the_allowlist(db, good_username):
    user = create_user(db, good_username, password="hunter2")
    assert user.username == good_username


def test_create_user_rejects_a_username_over_the_length_limit(db):
    with pytest.raises(AuthError):
        create_user(db, "a" * 33, password="hunter2")


def test_create_user_allows_a_username_at_exactly_the_length_limit(db):
    user = create_user(db, "a" * 32, password="hunter2")
    assert len(user.username) == 32


def test_get_user_by_username(db):
    create_user(db, "thiesi", password="hunter2")
    user = get_user_by_username(db, "thiesi")
    assert user.username == "thiesi"


def test_get_user_by_username_is_case_insensitive(db):
    create_user(db, "Thiesi", password="hunter2")
    user = get_user_by_username(db, "thiesi")
    assert user.username == "Thiesi"


def test_get_nonexistent_user_fails(db):
    with pytest.raises(AuthError):
        get_user_by_username(db, "nobody")


# -- password login ---------------------------------------------------------


def test_password_login_succeeds_with_correct_password(db):
    create_user(db, "thiesi", password="hunter2")
    user = authenticate_password(db, "thiesi", "hunter2")
    assert user.username == "thiesi"


def test_password_login_fails_with_wrong_password(db):
    create_user(db, "thiesi", password="hunter2")
    with pytest.raises(AuthError):
        authenticate_password(db, "thiesi", "wrong-password")


def test_password_login_succeeds_with_different_case(db):
    create_user(db, "Thiesi", password="hunter2")
    user = authenticate_password(db, "THIESI", "hunter2")
    assert user.username == "Thiesi"


def test_password_login_fails_for_nonexistent_user(db):
    with pytest.raises(AuthError):
        authenticate_password(db, "nobody", "whatever")


def test_password_login_fails_for_keypair_only_account(db):
    signing_key = nacl.signing.SigningKey.generate()
    create_user(db, "thiesi", verify_key=signing_key.verify_key)
    with pytest.raises(AuthError):
        authenticate_password(db, "thiesi", "any-password")


def test_password_login_updates_last_login_at(db):
    create_user(db, "thiesi", password="hunter2")
    before = get_user_by_username(db, "thiesi")
    assert before.last_login_at is None

    after = authenticate_password(db, "thiesi", "hunter2")
    assert after.last_login_at is not None


# -- keypair (challenge-response) login --------------------------------------


def test_keypair_login_succeeds_with_correct_signature(db):
    signing_key = nacl.signing.SigningKey.generate()
    create_user(db, "thiesi", verify_key=signing_key.verify_key)

    challenge = generate_challenge()
    signature = signing_key.sign(challenge).signature

    user = authenticate_keypair(db, "thiesi", challenge, signature)
    assert user.username == "thiesi"


def test_keypair_login_succeeds_with_different_case(db):
    signing_key = nacl.signing.SigningKey.generate()
    create_user(db, "Thiesi", verify_key=signing_key.verify_key)

    challenge = generate_challenge()
    signature = signing_key.sign(challenge).signature

    user = authenticate_keypair(db, "THIESI", challenge, signature)
    assert user.username == "Thiesi"


def test_keypair_login_fails_with_wrong_key(db):
    signing_key = nacl.signing.SigningKey.generate()
    wrong_key = nacl.signing.SigningKey.generate()
    create_user(db, "thiesi", verify_key=signing_key.verify_key)

    challenge = generate_challenge()
    signature = wrong_key.sign(challenge).signature

    with pytest.raises(AuthError):
        authenticate_keypair(db, "thiesi", challenge, signature)


def test_keypair_login_fails_if_signature_is_over_different_challenge(db):
    signing_key = nacl.signing.SigningKey.generate()
    create_user(db, "thiesi", verify_key=signing_key.verify_key)

    real_challenge = generate_challenge()
    other_challenge = generate_challenge()
    signature = signing_key.sign(other_challenge).signature

    with pytest.raises(AuthError):
        authenticate_keypair(db, "thiesi", real_challenge, signature)


def test_keypair_login_fails_for_password_only_account(db):
    create_user(db, "thiesi", password="hunter2")
    signing_key = nacl.signing.SigningKey.generate()
    challenge = generate_challenge()
    signature = signing_key.sign(challenge).signature

    with pytest.raises(AuthError):
        authenticate_keypair(db, "thiesi", challenge, signature)


def test_generate_challenge_is_random(db):
    a = generate_challenge()
    b = generate_challenge()
    assert a != b


# -- public key authorization (SSH pubkey auth — no challenge/signature) ----


def test_authorize_public_key_succeeds_with_registered_key(db):
    """Unlike authenticate_keypair, this doesn't verify a signature over
    a challenge -- see the function's docstring for why (SSH's own
    protocol already proved possession before this is ever called)."""
    signing_key = nacl.signing.SigningKey.generate()
    create_user(db, "thiesi", verify_key=signing_key.verify_key)

    user = authorize_public_key(db, "thiesi", signing_key.verify_key)
    assert user.username == "thiesi"


def test_authorize_public_key_succeeds_with_different_case(db):
    signing_key = nacl.signing.SigningKey.generate()
    create_user(db, "Thiesi", verify_key=signing_key.verify_key)

    user = authorize_public_key(db, "THIESI", signing_key.verify_key)
    assert user.username == "Thiesi"


def test_authorize_public_key_fails_with_wrong_key(db):
    signing_key = nacl.signing.SigningKey.generate()
    wrong_key = nacl.signing.SigningKey.generate()
    create_user(db, "thiesi", verify_key=signing_key.verify_key)

    with pytest.raises(AuthError):
        authorize_public_key(db, "thiesi", wrong_key.verify_key)


def test_authorize_public_key_fails_for_password_only_account(db):
    create_user(db, "thiesi", password="hunter2")
    signing_key = nacl.signing.SigningKey.generate()

    with pytest.raises(AuthError):
        authorize_public_key(db, "thiesi", signing_key.verify_key)


def test_authorize_public_key_fails_for_nonexistent_user(db):
    signing_key = nacl.signing.SigningKey.generate()
    with pytest.raises(AuthError):
        authorize_public_key(db, "nobody", signing_key.verify_key)


def test_authorize_public_key_updates_last_login_at(db):
    signing_key = nacl.signing.SigningKey.generate()
    create_user(db, "thiesi", verify_key=signing_key.verify_key)
    before = get_user_by_username(db, "thiesi")
    assert before.last_login_at is None

    after = authorize_public_key(db, "thiesi", signing_key.verify_key)
    assert after.last_login_at is not None


# -- attaching a key to an existing account (dogfood: SSH surface) ---------


def test_set_verify_key_lets_a_password_only_account_then_log_in_with_it(db):
    # Dogfood follow-up: a self-registered (password-only) account had no
    # way to ever gain key-based SSH login short of a SysOp deleting and
    # recreating it. set_verify_key closes that gap -- prove the key
    # actually becomes usable for login, not just that it's stored.
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    thiesi = create_user(db, "thiesi", password="hunter2")
    assert thiesi.fingerprint is None
    signing_key = nacl.signing.SigningKey.generate()

    updated = set_verify_key(db, thiesi, signing_key.verify_key, changed_by=sysop)
    assert updated.fingerprint is not None

    user = authorize_public_key(db, "thiesi", signing_key.verify_key)
    assert user.username == "thiesi"


def test_set_verify_key_replaces_an_existing_key(db):
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    old_key = nacl.signing.SigningKey.generate()
    thiesi = create_user(db, "thiesi", verify_key=old_key.verify_key)
    new_key = nacl.signing.SigningKey.generate()

    set_verify_key(db, thiesi, new_key.verify_key, changed_by=sysop)

    with pytest.raises(AuthError):
        authorize_public_key(db, "thiesi", old_key.verify_key)
    user = authorize_public_key(db, "thiesi", new_key.verify_key)
    assert user.username == "thiesi"


def test_set_verify_key_refuses_a_key_already_used_by_another_account(db):
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    shared_key = nacl.signing.SigningKey.generate()
    create_user(db, "bob", verify_key=shared_key.verify_key)
    thiesi = create_user(db, "thiesi", password="hunter2")

    with pytest.raises(AuthError):
        set_verify_key(db, thiesi, shared_key.verify_key, changed_by=sysop)

    # Refused cleanly -- thiesi's own row is untouched, not left half-updated.
    assert get_user_by_username(db, "thiesi").fingerprint is None


# -- removing a key from an existing account (dogfood: no removal path) ----


def test_clear_verify_key_removes_the_key_from_a_password_and_key_account(db):
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    signing_key = nacl.signing.SigningKey.generate()
    thiesi = create_user(db, "thiesi", password="hunter2", verify_key=signing_key.verify_key)
    assert thiesi.fingerprint is not None

    updated = clear_verify_key(db, thiesi, changed_by=sysop)
    assert updated.fingerprint is None

    with pytest.raises(AuthError):
        authorize_public_key(db, "thiesi", signing_key.verify_key)
    # The password login the account still has keeps working, unaffected.
    assert authenticate_password(db, "thiesi", "hunter2") is not None


def test_clear_verify_key_refuses_a_key_only_account_with_no_password(db):
    # GitHub issue #212: removing the only credential a key-only account
    # has would lock it out of the node entirely (the users table's own
    # CHECK constraint, password_hash IS NOT NULL OR public_key IS NOT
    # NULL, exists for exactly this reason) -- must be refused, not
    # allowed to hit that constraint as a raw sqlite3.IntegrityError.
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    signing_key = nacl.signing.SigningKey.generate()
    thiesi = create_user(db, "thiesi", verify_key=signing_key.verify_key)
    assert thiesi.fingerprint is not None

    with pytest.raises(AuthError):
        clear_verify_key(db, thiesi, changed_by=sysop)

    # Refused cleanly -- the key is still there and still usable.
    assert get_user_by_username(db, "thiesi").fingerprint is not None
    authorize_public_key(db, "thiesi", signing_key.verify_key)


def test_clear_verify_key_preserves_an_existing_block(db):
    # Code review follow-up (PR #213): block_user keys a block by
    # fingerprint whenever the target has one at block time -- if
    # clear_verify_key then removed that fingerprint without migrating
    # the blocklist entry, is_blocked would stop finding it (it only
    # checks local_user_id once the account has no fingerprint left),
    # and a restricted account would read as unblocked on its very next
    # password login. Confirms the block survives the key removal.
    from netbbs.moderation.blocklist import block_user, is_blocked

    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    signing_key = nacl.signing.SigningKey.generate()
    thiesi = create_user(db, "thiesi", password="hunter2", verify_key=signing_key.verify_key)
    block_user(db, thiesi, blocked_by=sysop, reason="testing")
    assert is_blocked(db, thiesi) is True

    updated = clear_verify_key(db, thiesi, changed_by=sysop)
    assert updated.fingerprint is None
    assert is_blocked(db, updated) is True
