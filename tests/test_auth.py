"""Tests for netbbs.auth — account creation, password login, keypair login."""

from __future__ import annotations

import nacl.signing
import pytest

from netbbs.auth.users import (
    AuthError,
    authenticate_keypair,
    authenticate_password,
    authorize_public_key,
    create_user,
    generate_challenge,
    get_user_by_username,
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
