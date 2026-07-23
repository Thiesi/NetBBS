"""
Cryptographic identity for NetBBS nodes and users.

See design doc §5 (Identity) and §11 (Node-to-node transport security).

Both nodes and individual users get a long-term Ed25519 signing keypair.
The fingerprint derived from a signing key's public half *is* that node's
or user's identity on NetBBS Link — there is no separate hierarchical
addressing scheme (see design doc §5 for why FidoNet-style zone:net/node
addressing was explicitly rejected).

A note on key types, since it's easy to conflate them: the long-term
identity keypair itself is always **signing** (Ed25519), used for DAG
message signatures (§7) and HTTP+JSON payload authentication (§11).
Where **encryption** (X25519) is needed -- Link messages --
`netbbs.identity.encryption` derives it from that same Ed25519 key
rather than provisioning a separate one; see that module's own
docstring. Phase 5's Noise Protocol real-time chat transport is a
distinct, still-later concern, not served by this derivation.
"""

from netbbs.identity.keys import (
    Identity,
    IdentityError,
    fingerprint_from_verify_key,
    load_identity,
    verify_signature,
)
from netbbs.identity.addressing import format_address, parse_address
from netbbs.identity.encryption import (
    EncryptionError,
    decrypt_with,
    derive_encryption_private_key,
    derive_encryption_public_key,
    encrypt_for,
)

__all__ = [
    "Identity",
    "IdentityError",
    "load_identity",
    "verify_signature",
    "fingerprint_from_verify_key",
    "format_address",
    "parse_address",
    "EncryptionError",
    "encrypt_for",
    "decrypt_with",
    "derive_encryption_public_key",
    "derive_encryption_private_key",
]
