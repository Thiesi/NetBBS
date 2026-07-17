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

The canonicalization rule below (sorted keys, NFC-normalized strings, no
floats) is design doc round 110's formalization of what this function
already did informally since round 7 — extended, not replaced, so every
Phase 1/2 content-ID already computed under the old (sorted-key-only)
rule for genuinely ASCII/already-NFC content is unaffected. `netbbs.link.
events` (round 110/111) reuses `canonical_json_bytes` directly for
signing Link event envelopes, rather than maintaining a second
canonicalization implementation.
"""

from __future__ import annotations

import json
import unicodedata

import nacl.encoding
import nacl.hash

# 32 bytes (256 bits): the collision resistance appropriate for content
# IDs potentially referenced across a large Link, unlike the shorter
# 20-byte fingerprints in netbbs.identity — those are optimized for being
# human-typable, not for maximum collision resistance at network scale.
_CONTENT_ID_BYTES = 32


class ContentIdError(Exception):
    """Raised when `fields` passed to `compute_content_id`/
    `canonical_json_bytes` violates design doc round 110's
    canonicalization rule — currently just "no floats" (round 110 point
    4): floats are forbidden in anything hashed or signed, since float
    serialization isn't reliably deterministic across platforms/
    languages, and every current caller (board/post/channel/file-area
    IDs) already only ever passes strings."""


def _normalize_for_hashing(value):
    """
    Recursively normalize `value` per round 110's canonicalization rule:
    every string is Unicode-NFC-normalized (round 110 point 3), and a
    `float` anywhere raises `ContentIdError` (round 110 point 4) rather
    than being silently serialized. Dicts/lists are walked recursively
    so the rule applies uniformly regardless of nesting depth; every
    other type (str already handled, int, bool, None) passes through
    unchanged — `bool` is deliberately not mistaken for a numeric type
    here despite `isinstance(True, int)` being true in Python, since
    JSON already serializes it as `true`/`false`, never a number.
    """
    if isinstance(value, float):
        raise ContentIdError(f"floats are forbidden in content-addressed fields (round 110): {value!r}")
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {key: _normalize_for_hashing(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_for_hashing(item) for item in value]
    return value


def canonical_json_bytes(fields: dict) -> bytes:
    """
    The exact canonical UTF-8 bytes `compute_content_id` hashes —
    exposed separately (round 110/111) so a caller that needs to *sign*
    the same canonical representation (`netbbs.link.events`, Phase 3
    event envelopes), not just hash it, can do so without a second,
    potentially-divergent canonicalization implementation.

    Canonicalized as NFC-normalized, sorted-key, compact-separator,
    ASCII-escaped JSON (round 110) — sorted keys so the same logical
    content always produces the same bytes regardless of what order the
    fields happened to be constructed in; NFC normalization and the
    float ban close the two ambiguities round 27/90 had left open since
    this function only had local, non-cryptographic-signing stakes.
    """
    normalized = _normalize_for_hashing(fields)
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return canonical.encode("utf-8")


def compute_content_id(fields: dict) -> str:
    """
    Compute a deterministic, content-addressed ID from a dict of fields.

    Hex-encoded — unlike the base32 scheme identity fingerprints use —
    since these IDs are meant for programmatic reference (git-commit-
    hash style), not for a human to read aloud or type at a prompt.
    """
    digest = nacl.hash.blake2b(
        canonical_json_bytes(fields),
        digest_size=_CONTENT_ID_BYTES,
        encoder=nacl.encoding.RawEncoder,
    )
    return digest.hex()
