"""
User account creation, password login, and keypair (challenge-response)
login.
"""

from __future__ import annotations

import base64
import sqlite3
from dataclasses import dataclass

import nacl.signing
import nacl.utils

from netbbs.auth.passwords import hash_password, verify_password
from netbbs.identity.keys import fingerprint_from_verify_key, verify_signature
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

# Length of the random nonce a client must sign to prove keypair
# ownership during login. 32 bytes gives a large enough search space that
# nonce-guessing isn't a realistic attack, while staying short enough to
# send over a slow telnet link without noticeable delay.
_CHALLENGE_BYTES = 32


class AuthError(Exception):
    """
    Raised for account-creation or login failures.

    Deliberately generic for anything reaching an actual login attempt —
    doesn't distinguish "no such user" from "wrong password" or "wrong
    signature" — to avoid username enumeration via error-message timing
    or content. Code that legitimately needs a finer-grained reason (e.g.
    a SysOp admin tool) should query the storage layer directly instead of
    relying on this exception's message.
    """


@dataclass(frozen=True)
class User:
    id: int
    username: str
    user_level: int
    fingerprint: str | None
    created_at: str
    last_login_at: str | None


def create_user(
    db: Database,
    username: str,
    *,
    password: str | None = None,
    verify_key: nacl.signing.VerifyKey | None = None,
    user_level: int = 0,
) -> User:
    """
    Register a new local user account.

    At least one of `password` / `verify_key` must be given, matching the
    users table's CHECK constraint (design doc §5: password and keypair
    auth are both supported, either usable alone). Passing a `verify_key`
    rather than a raw fingerprint is deliberate — the table needs the
    actual public key to verify future login signatures against; the
    fingerprint stored alongside it is a derived display/lookup value,
    never independently trusted for verification.
    """
    if password is None and verify_key is None:
        raise AuthError("a new account needs a password, a keypair, or both")

    password_hash = hash_password(password) if password is not None else None

    if verify_key is not None:
        public_key_b64 = base64.b64encode(bytes(verify_key)).decode("ascii")
        fingerprint = fingerprint_from_verify_key(verify_key)
    else:
        public_key_b64 = None
        fingerprint = None

    created_at = utc_now_iso()

    try:
        db.connection.execute(
            """
            INSERT INTO users
                (username, password_hash, public_key, fingerprint, user_level, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (username, password_hash, public_key_b64, fingerprint, user_level, created_at),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise AuthError(
            f"could not create account {username!r} — username or fingerprint already in use"
        ) from exc

    return get_user_by_username(db, username)


def get_user_by_username(db: Database, username: str) -> User:
    row = db.connection.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row is None:
        raise AuthError("login failed")  # see AuthError docstring re: enumeration
    return _row_to_user(row)


def generate_challenge() -> bytes:
    """
    Generate a random nonce for keypair-based login challenge-response.

    A login signature must be over a fresh, unpredictable nonce rather
    than some fixed message — otherwise a signature captured once (e.g.
    over an unencrypted telnet session) could simply be replayed later to
    log in again without the attacker ever holding the private key.
    """
    return nacl.utils.random(_CHALLENGE_BYTES)


def authenticate_password(db: Database, username: str, password: str) -> User:
    """Log in with a username/password pair."""
    row = db.connection.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row is None or row["password_hash"] is None:
        raise AuthError("login failed")
    if not verify_password(password, row["password_hash"]):
        raise AuthError("login failed")
    return _touch_last_login(db, row)


def authenticate_keypair(db: Database, username: str, challenge: bytes, signature: bytes) -> User:
    """
    Log in by proving ownership of the account's registered keypair.

    Caller is responsible for having generated `challenge` via
    `generate_challenge()` and sent it to the client immediately before
    this call — this function only verifies the signature, it doesn't
    manage challenge freshness/expiry itself. (A connection-scoped
    challenge with a short timeout is a reasonable place to enforce that,
    once the connection-handling layer exists.)
    """
    row = db.connection.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row is None or row["public_key"] is None:
        raise AuthError("login failed")

    stored_key = nacl.signing.VerifyKey(base64.b64decode(row["public_key"]))
    if not verify_signature(stored_key, challenge, signature):
        raise AuthError("login failed")

    return _touch_last_login(db, row)


def _touch_last_login(db: Database, row: sqlite3.Row) -> User:
    now = utc_now_iso()
    db.connection.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"])
    )
    db.connection.commit()
    # Re-fetch rather than patch the in-memory row, so the returned User
    # reflects exactly what's now in the database, not an assumption
    # about which columns `row` already had loaded.
    updated = db.connection.execute(
        "SELECT * FROM users WHERE id = ?", (row["id"],)
    ).fetchone()
    return _row_to_user(updated)


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        user_level=row["user_level"],
        fingerprint=row["fingerprint"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )
