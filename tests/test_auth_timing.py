"""Regression tests for password-login failure work equalization."""

from __future__ import annotations

import base64

import nacl.signing
import pytest

from netbbs.auth import users
from netbbs.auth.users import AuthError, authenticate_password
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


def _insert_user(db: Database, username: str, *, password_hash: str | None = None) -> None:
    public_key = None
    fingerprint = None
    if password_hash is None:
        verify_key = nacl.signing.SigningKey.generate().verify_key
        public_key = base64.b64encode(bytes(verify_key)).decode("ascii")
        fingerprint = "test-fingerprint"

    db.connection.execute(
        """
        INSERT INTO users
            (username, password_hash, public_key, fingerprint, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (username, password_hash, public_key, fingerprint, utc_now_iso()),
    )
    db.connection.commit()


def test_unknown_user_still_performs_one_password_verification(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    seen_hashes: list[str] = []

    def fake_verify(password: str, stored_hash: str) -> bool:
        seen_hashes.append(stored_hash)
        return False

    monkeypatch.setattr(users, "verify_password", fake_verify)

    with pytest.raises(AuthError, match="login failed"):
        authenticate_password(db, "missing", "wrong")

    assert seen_hashes == [users._DUMMY_PASSWORD_HASH]
    db.close()


def test_key_only_user_uses_same_dummy_verification_path(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    _insert_user(db, "key-only")
    seen_hashes: list[str] = []

    def fake_verify(password: str, stored_hash: str) -> bool:
        seen_hashes.append(stored_hash)
        return False

    monkeypatch.setattr(users, "verify_password", fake_verify)

    with pytest.raises(AuthError, match="login failed"):
        authenticate_password(db, "key-only", "wrong")

    assert seen_hashes == [users._DUMMY_PASSWORD_HASH]
    db.close()


def test_password_user_verifies_the_stored_hash_once(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    _insert_user(db, "password-user", password_hash="stored-real-hash")
    seen_hashes: list[str] = []

    def fake_verify(password: str, stored_hash: str) -> bool:
        seen_hashes.append(stored_hash)
        return False

    monkeypatch.setattr(users, "verify_password", fake_verify)

    with pytest.raises(AuthError, match="login failed"):
        authenticate_password(db, "password-user", "wrong")

    assert seen_hashes == ["stored-real-hash"]
    db.close()
