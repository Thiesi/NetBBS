"""
Cryptographic identity for NetBBS nodes and users.

See design doc §5 (Identity) and §11 (Node-to-node transport security).

Both nodes and individual users get a long-term Ed25519 signing keypair.
The fingerprint derived from a signing key's public half *is* that node's
or user's identity on NetBBS Link — there is no separate hierarchical
addressing scheme (see design doc §5 for why FidoNet-style zone:net/node
addressing was explicitly rejected).

A note on key types, since it's easy to conflate them: this module only
deals with **signing** keys (Ed25519), used for DAG message signatures
(§7) and HTTP+JSON payload authentication (§11). The separate **key
exchange** keys (X25519) needed for Phase 5's Noise Protocol real-time
chat transport are a later concern and intentionally not part of this
module yet.
"""

from netbbs.identity.keys import (
    Identity,
    IdentityError,
    fingerprint_from_verify_key,
    load_identity,
    verify_signature,
)
from netbbs.identity.addressing import format_address, parse_address

__all__ = [
    "Identity",
    "IdentityError",
    "load_identity",
    "verify_signature",
    "fingerprint_from_verify_key",
    "format_address",
    "parse_address",
]
