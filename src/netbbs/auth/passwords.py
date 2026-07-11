"""
Password hashing for local BBS accounts.

Uses libsodium's Argon2id password-hashing helpers (via PyNaCl) — the same
underlying primitive already used for identity file encryption in
`netbbs.identity.keys`, so the project has one hashing story rather than
pulling in a second dependency (e.g. bcrypt/passlib) for fundamentally the
same job.
"""

from __future__ import annotations

import nacl.pwhash
from nacl.exceptions import InvalidkeyError


def hash_password(password: str) -> str:
    """
    Hash a password for storage.

    Returns a self-contained encoded string (includes the salt and
    Argon2id parameters), safe to store directly in the `users.password_hash`
    column — no separate salt column needed.
    """
    hashed = nacl.pwhash.argon2id.str(password.encode("utf-8"))
    return hashed.decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    """Check a password attempt against a previously hashed value."""
    try:
        return nacl.pwhash.argon2id.verify(stored_hash.encode("ascii"), password.encode("utf-8"))
    except InvalidkeyError:
        return False
