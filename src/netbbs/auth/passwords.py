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

# INTERACTIVE tier: appropriate for something checked on every login
# rather than a long-lived identity file at rest (see identity/keys.py's
# SENSITIVE-tier choice for that case) — still real Argon2id cost, just
# calibrated for login-time latency rather than maximum brute-force
# resistance. Module-level so the test suite can monkeypatch these down
# to a cheaper tier (see tests/conftest.py) without touching call sites.
# Explicit rather than relying on nacl.pwhash.argon2id.str()'s own
# default, so the actual cost tier in use is visible in this file rather
# than implicit in a library default that could change between PyNaCl
# versions.
_PASSWORD_OPSLIMIT = nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE
_PASSWORD_MEMLIMIT = nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE


def hash_password(password: str) -> str:
    """
    Hash a password for storage.

    Returns a self-contained encoded string (includes the salt and
    Argon2id parameters actually used), safe to store directly in the
    `users.password_hash` column — no separate salt/params columns
    needed, and `verify_password` doesn't need to be told which
    parameters were used since the string carries them itself.
    """
    hashed = nacl.pwhash.argon2id.str(
        password.encode("utf-8"),
        opslimit=_PASSWORD_OPSLIMIT,
        memlimit=_PASSWORD_MEMLIMIT,
    )
    return hashed.decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    """Check a password attempt against a previously hashed value."""
    try:
        return nacl.pwhash.argon2id.verify(stored_hash.encode("ascii"), password.encode("utf-8"))
    except InvalidkeyError:
        return False
