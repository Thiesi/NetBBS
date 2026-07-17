"""
Tests for netbbs.identity.encryption -- deriving an X25519 keypair from
an existing Ed25519 identity key and sealing/opening content with it
(design doc round 93's encryption-mechanism decision for Link messages).
"""

from __future__ import annotations

import pytest

from netbbs.identity.encryption import (
    EncryptionError,
    decrypt_with,
    derive_encryption_private_key,
    derive_encryption_public_key,
    encrypt_for,
)
from netbbs.identity.keys import Identity, IdentityKind


def test_encrypt_and_decrypt_roundtrip():
    alice = Identity.generate(IdentityKind.USER, "alice")
    ciphertext = encrypt_for(alice.verify_key, b"the eagle flies at midnight")
    assert decrypt_with(alice, ciphertext) == b"the eagle flies at midnight"


def test_ciphertext_is_not_the_plaintext():
    alice = Identity.generate(IdentityKind.USER, "alice")
    ciphertext = encrypt_for(alice.verify_key, b"secret body")
    assert b"secret body" not in ciphertext


def test_two_encryptions_of_the_same_plaintext_differ():
    """SealedBox embeds a fresh ephemeral sender key per call -- two
    calls with identical plaintext must not produce identical
    ciphertext, or a passive observer could correlate repeated
    messages."""
    alice = Identity.generate(IdentityKind.USER, "alice")
    first = encrypt_for(alice.verify_key, b"same body")
    second = encrypt_for(alice.verify_key, b"same body")
    assert first != second
    assert decrypt_with(alice, first) == b"same body"
    assert decrypt_with(alice, second) == b"same body"


def test_decrypt_with_wrong_identity_fails():
    alice = Identity.generate(IdentityKind.USER, "alice")
    mallory = Identity.generate(IdentityKind.USER, "mallory")
    ciphertext = encrypt_for(alice.verify_key, b"for alice's eyes only")

    with pytest.raises(EncryptionError):
        decrypt_with(mallory, ciphertext)


def test_decrypt_rejects_tampered_ciphertext():
    alice = Identity.generate(IdentityKind.USER, "alice")
    ciphertext = bytearray(encrypt_for(alice.verify_key, b"original body"))
    ciphertext[-1] ^= 0xFF  # flip the last byte

    with pytest.raises(EncryptionError):
        decrypt_with(alice, bytes(ciphertext))


def test_derived_public_and_private_keys_are_a_matching_pair():
    alice = Identity.generate(IdentityKind.USER, "alice")
    private_key = derive_encryption_private_key(alice)
    public_key = derive_encryption_public_key(alice.verify_key)
    assert bytes(private_key.public_key) == bytes(public_key)


def test_derivation_is_deterministic_for_the_same_identity():
    alice = Identity.generate(IdentityKind.USER, "alice")
    assert bytes(derive_encryption_public_key(alice.verify_key)) == bytes(
        derive_encryption_public_key(alice.verify_key)
    )
    assert bytes(derive_encryption_private_key(alice)) == bytes(derive_encryption_private_key(alice))


def test_different_identities_derive_different_encryption_keys():
    alice = Identity.generate(IdentityKind.USER, "alice")
    bob = Identity.generate(IdentityKind.USER, "bob")
    assert bytes(derive_encryption_public_key(alice.verify_key)) != bytes(
        derive_encryption_public_key(bob.verify_key)
    )


def test_node_identity_can_also_derive_an_encryption_key():
    """Round 93's tier-1 case: a password-only user's message is
    encrypted to their *home node's* key, not a personal one -- confirms
    a NODE-kind identity derives just as well as a USER-kind one."""
    node = Identity.generate(IdentityKind.NODE, "roanoke")
    ciphertext = encrypt_for(node.verify_key, b"node-encrypted body")
    assert decrypt_with(node, ciphertext) == b"node-encrypted body"
