"""
Subdomain name validation for the managed-DNS service (design doc §16,
issue #201).

A registered name becomes the left-hand label of `<name>.netbbs.org`, so
it must be a valid single DNS label (RFC 1035/1123): letters, digits,
and hyphens only, 1-63 characters, never starting or ending with a
hyphen. DNS names are case-insensitive, so a name is normalized to
lowercase before it's ever compared, stored, or looked up -- otherwise
`MyBoard` and `myboard` would be treated as two different, independently
claimable names, which they are not in real DNS.
"""

from __future__ import annotations

import re

_LABEL_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_LABEL_LENGTH = 63


class InvalidNameError(ValueError):
    """Raised by `normalize_name` for a name that can never be a valid
    DNS label, regardless of availability/blocklist status."""


def normalize_name(raw: str) -> str:
    """Lowercase and validate `raw` as a single DNS label. Raises
    `InvalidNameError` with a caller-facing reason on anything that
    isn't -- never silently truncates or substitutes characters."""
    candidate = raw.strip().lower()
    if not candidate:
        raise InvalidNameError("name must not be empty")
    if len(candidate) > _MAX_LABEL_LENGTH:
        raise InvalidNameError(f"name must be at most {_MAX_LABEL_LENGTH} characters")
    if not _LABEL_PATTERN.match(candidate):
        raise InvalidNameError(
            "name must contain only letters, digits, and hyphens, and must not start or end with a hyphen"
        )
    return candidate
