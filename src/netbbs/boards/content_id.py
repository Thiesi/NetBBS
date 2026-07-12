"""
Content-addressed IDs, shared by boards, posts, and (in later phases)
every other NetBBS Link event type.

Design doc §7: every Link message/event gets a unique ID derived from a
hash of its content, plus parent references for anything that's a reply.
Computing IDs this way starting in Phase 1 — even though no actual Link
networking exists yet — means local-only boards/posts never need an
ID-scheme migration when a board later becomes Linked in a later phase;
only the signing/relay machinery gets added on top then, not the
identity scheme itself.
"""

from __future__ import annotations

import json

import nacl.encoding
import nacl.hash

# 32 bytes (256 bits): the collision resistance appropriate for content
# IDs potentially referenced across a large Link, unlike the shorter
# 20-byte fingerprints in netbbs.identity — those are optimized for being
# human-typable, not for maximum collision resistance at network scale.
_CONTENT_ID_BYTES = 32


def compute_content_id(fields: dict) -> str:
    """
    Compute a deterministic, content-addressed ID from a dict of fields.

    Canonicalized as sorted-key JSON before hashing, so the same logical
    content always produces the same ID regardless of what order the
    fields happened to be constructed in. Hex-encoded — unlike the base32
    scheme identity fingerprints use — since these IDs are meant for
    programmatic reference (git-commit-hash style), not for a human to
    read aloud or type at a prompt.
    """
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = nacl.hash.blake2b(
        canonical.encode("utf-8"),
        digest_size=_CONTENT_ID_BYTES,
        encoder=nacl.encoding.RawEncoder,
    )
    return digest.hex()
