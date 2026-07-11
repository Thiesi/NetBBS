"""
Shared pytest configuration.

Automatically downgrades the Argon2id cost parameters used for identity
file encryption (`netbbs.identity.keys`) and password hashing
(`netbbs.auth.passwords`) to libsodium's cheapest ("MIN") tier for the
whole test session.

Production code defaults to much more expensive tiers — SENSITIVE for
identity files, INTERACTIVE for password hashing — appropriate for real
use, but multiplied across dozens of tests it adds real wall-clock time
for no benefit, since none of our tests are testing Argon2id's own
cost/security properties. Applying this via an autouse fixture means
individual test files never need to know this is happening — no test
call site needs to change.
"""

from __future__ import annotations

import nacl.pwhash
import pytest

import netbbs.auth.passwords as passwords_module
import netbbs.identity.keys as keys_module


@pytest.fixture(autouse=True)
def _fast_argon2id(monkeypatch):
    monkeypatch.setattr(keys_module, "_SAVE_OPSLIMIT", nacl.pwhash.argon2id.OPSLIMIT_MIN)
    monkeypatch.setattr(keys_module, "_SAVE_MEMLIMIT", nacl.pwhash.argon2id.MEMLIMIT_MIN)
    monkeypatch.setattr(passwords_module, "_PASSWORD_OPSLIMIT", nacl.pwhash.argon2id.OPSLIMIT_MIN)
    monkeypatch.setattr(passwords_module, "_PASSWORD_MEMLIMIT", nacl.pwhash.argon2id.MEMLIMIT_MIN)
    yield
