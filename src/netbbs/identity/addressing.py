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

# User-part rules: exactly the local username grammar
# (`netbbs.auth.users._USERNAME_PATTERN`, at most 32 characters), so every
# account is addressable as it is displayed (issue #807). Capitals are kept:
# the recipient node looks the name up case-insensitively, so `OldNib@Q` and
# `oldnib@Q` reach the same account, and a sender's name goes out spelled
# the way it is shown.
_USER_PART_MAX_LENGTH = 32
_USER_PART_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")

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


def is_valid_user_part(user: str) -> bool:
    """Whether `user` can stand before the `@` of a Link address."""
    return isinstance(user, str) and bool(_USER_PART_RE.fullmatch(user))


def user_part_problem(user: str) -> str:
    """Why `user` cannot be the user half of an address, and what to type
    instead -- in words for a caller."""
    if len(user) > _USER_PART_MAX_LENGTH:
        return (
            f"{user!r} is longer than a user name can be. Type the name as their "
            f"BBS shows it, at most {_USER_PART_MAX_LENGTH} characters."
        )
    return (
        f"{user!r} is not a user name. Type the name as their BBS shows it: "
        "letters, digits, '.', '_' and '-' only."
    )


def _validate_user_part(user: str) -> None:
    if not is_valid_user_part(user):
        raise AddressError(user_part_problem(user))


def _validate_fingerprint(fingerprint: str) -> None:
    if not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise AddressError(f"invalid node fingerprint {fingerprint!r}")
