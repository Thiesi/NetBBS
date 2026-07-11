"""
Timestamp formatting shared across the codebase.

`datetime.isoformat()` silently drops the fractional-seconds field
whenever microseconds happen to be exactly zero, producing
`...T12:00:00+00:00` on one call and `...T12:00:00.482913+00:00` on the
next. That's an easy thing to overlook until a timestamp string ends up
inside something hashed or signed (see design doc §7 — DAG events use a
timestamp as an ordering tiebreaker) and two semantically-similar moments
serialize to different-shaped strings. `utc_now_iso()` forces a fixed,
always-6-digit microsecond field instead, so the format never varies.

Every part of the codebase that stamps a "when did this happen" value
should go through this function rather than calling
`datetime.now(...).isoformat()` directly, so the format can't drift
between call sites the way it briefly almost did between the identity and
auth modules.
"""

from __future__ import annotations

import datetime


def utc_now_iso() -> str:
    """Current UTC time as a fixed-format, always-6-decimal ISO 8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
