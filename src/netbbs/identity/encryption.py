"""
Link-message content encryption.

Design doc round 93 (Link messages) confirmed the mechanism: derive the
X25519 keypair a subject needs from its existing Ed25519 identity key
rather than minting/storing/rotating a separate encryption keypair.
Uses PyNaCl's own supported conversion -- `nacl.signing.SigningKey.
to_curve25519_private_key()` / `VerifyKey.to_curve25519_public_key()`
-- not a low-level `nacl.bindings` escape hatch.

Both directions derive from public key material alone where only a
public key is known, so encrypting *to* someone needs no separate
protocol exchange to learn their encryption key: whichever signing
verify key the existing Link peer/transition-chain resolution already
produces is sufficient.

Static long-term keys, not per-session ephemeral ones -- `SealedBox`
gives sender-side anonymity (a fresh ephemeral key per call) but not
forward secrecy on the recipient's static key: if a recipient's signing
key is ever compromised, every message ever sealed to it becomes
retroactively readable. Disclosed, accepted limitation (see design doc),
not something this module tries to fix.
"""

from __future__ import annotations

import nacl.public
import nacl.signing
from nacl.exceptions import CryptoError

from netbbs.identity.keys import Identity


class EncryptionError(Exception):
    """Raised for a decryption failure. Deliberately as uninformative as
    `IdentityError` about *why* (wrong key vs. corrupted/tampered
    ciphertext) -- a caller only needs to know "this message could not
    be read," not a precise cryptographic reason."""


def derive_encryption_public_key(verify_key: nacl.signing.VerifyKey) -> nacl.public.PublicKey:
    """The X25519 public key derived from `verify_key`, for encrypting
    *to* whichever subject that key belongs to."""
    return verify_key.to_curve25519_public_key()


def derive_encryption_private_key(identity: Identity) -> nacl.public.PrivateKey:
    """The X25519 private key derived from `identity`'s own signing key,
    for decrypting something sealed to it."""
    return identity.signing_key.to_curve25519_private_key()


def encrypt_for(recipient_verify_key: nacl.signing.VerifyKey, plaintext: bytes) -> bytes:
    """
    Seal `plaintext` to whichever subject `recipient_verify_key`
    belongs to.

    `SealedBox` embeds a fresh ephemeral sender key inside the
    ciphertext on every call -- the recipient can decrypt without
    learning who sent it, and two calls with identical plaintext never
    produce identical ciphertext.
    """
    box = nacl.public.SealedBox(derive_encryption_public_key(recipient_verify_key))
    return box.encrypt(plaintext)


def decrypt_with(identity: Identity, ciphertext: bytes) -> bytes:
    """
    Open `ciphertext` sealed to `identity`.

    Raises `EncryptionError` if it wasn't actually sealed to this
    identity's derived key, or has been corrupted/tampered with.
    """
    box = nacl.public.SealedBox(derive_encryption_private_key(identity))
    try:
        return box.decrypt(ciphertext)
    except CryptoError as exc:
        raise EncryptionError("could not decrypt: wrong key or corrupted ciphertext") from exc
