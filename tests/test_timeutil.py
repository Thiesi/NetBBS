"""Tests for netbbs.timeutil — deterministic timestamp formatting."""

from __future__ import annotations

import re

from netbbs.timeutil import utc_now_iso

# Exactly 6 fractional digits, always — the whole point of this helper is
# that this width never varies, unlike bare datetime.isoformat().
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def test_format_always_matches_fixed_pattern():
    # Call several times; even if one happens to land on a whole second
    # (microsecond == 0), the format must not change shape.
    for _ in range(20):
        assert _TIMESTAMP_RE.match(utc_now_iso())


def test_timestamps_are_monotonic_non_decreasing():
    a = utc_now_iso()
    b = utc_now_iso()
    assert b >= a  # string comparison works here because the format is fixed-width
