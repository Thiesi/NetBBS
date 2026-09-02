"""
Reserved-word blocklist for the managed-DNS service (design doc §16,
issue #201, Decision 3).

Deliberately narrow: "covers only the obvious cases (the project's own
names, trademarks, slurs) -- not a general dispute-avoidance mechanism"
(design doc). What's seeded here is the unambiguous, structural half --
names that would be actively confusing or infrastructure-colliding if
registered by anyone but the project itself (`netbbs`, common
service-name conventions like `www`/`admin`/`mail`). Curating a real
trademark/slur wordlist is an ongoing content-moderation judgment call
for whoever operates this service, not something to hardcode here
without that review -- `RESERVED_NAMES` is a plain, easily-extended set
specifically so that curation can happen as a simple edit, without
touching any of the validation logic around it.

Contested-name disputes beyond this list are manual and complaint-
driven (Decision 4) -- this blocklist is a preventive floor, not the
whole enforcement mechanism.
"""

from __future__ import annotations

RESERVED_NAMES: frozenset[str] = frozenset(
    {
        # The project's own identity -- registering these as a
        # *different* board would be actively misleading.
        "netbbs",
        "www",
        "managed",
        # Conventional infrastructure/service names that would collide
        # with likely future subdomains of netbbs.org itself, or read as
        # an official project address regardless of who actually holds it.
        "admin",
        "api",
        "ftp",
        "help",
        "mail",
        "root",
        "support",
    }
)


def is_reserved(name: str) -> bool:
    """`name` must already be normalized (`services.managed_dns.names.
    normalize_name`) -- this does no case-folding or validation of its
    own, so a caller skipping that step could bypass the blocklist via
    case variation."""
    return name in RESERVED_NAMES
