"""
Human-facing address formatting: `user@node-fingerprint`.

See design doc §5 — Matrix-federation-style addressing, but the "domain"
half is a node's pubkey fingerprint rather than a DNS name, specifically
so no address can be broken or hijacked by seizing/expiring a domain.

This module only formats and parses address *strings* — it doesn't
resolve them to anything or verify the fingerprint refers to a real,
currently-reachable node. That's a Link-lookup concern for a later phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# User-part rules: kept deliberately conservative for a first pass —
# lowercase alphanumerics, underscore, hyphen, period. Easy to relax
# later if it turns out to be too strict for how people actually pick
# usernames; much harder to tighten after addresses are already in use.
_USER_PART_RE = re.compile(r"^[a-z0-9_.\-]{1,32}$")

# Fingerprints are lowercase base32 (see identity/keys.py's
# _encode_fingerprint) — this pattern intentionally matches that
# encoding's alphabet (a-z, 2-7) rather than being a generic
# "any hex-ish string" check, so a malformed fingerprint is rejected
# here rather than surfacing as a confusing failure much later.
_FINGERPRINT_RE = re.compile(r"^[a-z2-7]{4,64}$")


class AddressError(ValueError):
    """Raised when an address string is malformed."""


@dataclass(frozen=True)
class Address:
    """A parsed `user@node-fingerprint` address."""

    user: str
    node_fingerprint: str

    def __str__(self) -> str:
        return format_address(self.user, self.node_fingerprint)


def format_address(user: str, node_fingerprint: str) -> str:
    """Build a `user@node-fingerprint` address string, validating both parts."""
    _validate_user_part(user)
    _validate_fingerprint(node_fingerprint)
    return f"{user}@{node_fingerprint}"


def parse_address(address: str) -> Address:
    """
    Parse a `user@node-fingerprint` address string into its parts.

    Splits on the *last* `@`, not the first. Node fingerprints are
    guaranteed never to contain `@` (base32 alphabet), so the rightmost
    `@` is always the true delimiter regardless of what characters the
    username part allows — today or after any future relaxation of
    `_USER_PART_RE`. Splitting from the left would silently mis-attribute
    part of a malformed or (if username rules ever loosen) a legitimate
    username to the fingerprint half.
    """
    try:
        user, node_fingerprint = address.rsplit("@", 1)
    except ValueError as exc:
        raise AddressError(
            f"address {address!r} is not in user@node-fingerprint form"
        ) from exc

    _validate_user_part(user)
    _validate_fingerprint(node_fingerprint)
    return Address(user=user, node_fingerprint=node_fingerprint)


def _validate_user_part(user: str) -> None:
    if not _USER_PART_RE.match(user):
        raise AddressError(
            f"invalid user part {user!r}: expected 1-32 chars from [a-z0-9_.-]"
        )


def _validate_fingerprint(fingerprint: str) -> None:
    if not _FINGERPRINT_RE.match(fingerprint):
        raise AddressError(f"invalid node fingerprint {fingerprint!r}")
