"""Tests for netbbs.timeutil — deterministic timestamp formatting."""

from __future__ import annotations

import re

from netbbs.config import set_config
from netbbs.storage.database import Database
from netbbs.timeutil import (
    DISPLAY_FORMAT_CONFIG_KEY,
    format_for_display,
    is_valid_display_format,
    set_display_format,
    utc_now_iso,
)

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


# -- format_for_display ----------------------------------------------------


def test_display_never_includes_microseconds():
    stamp = utc_now_iso()
    displayed = format_for_display(stamp)
    # Check for the actual shape of microsecond output (a period followed
    # by 6 digits), not a blanket absence of periods — the European
    # default format itself legitimately uses periods as date separators
    # (e.g. "09.07.2026 14:32"), so "no periods at all" isn't the right
    # test for "no microseconds."
    assert not re.search(r"\.\d{6}", displayed)


def test_display_uses_hardcoded_default_with_no_db():
    stamp = "2026-07-09T14:32:07.123456Z"
    assert format_for_display(stamp) == "09.07.2026 14:32"


def test_display_uses_node_config_when_set(tmp_path):
    db = Database(tmp_path / "node.db")
    set_config(db, DISPLAY_FORMAT_CONFIG_KEY, "%Y-%m-%d %H:%M")
    stamp = "2026-07-09T14:32:07.123456Z"
    assert format_for_display(stamp, db) == "2026-07-09 14:32"
    db.close()


def test_display_falls_back_to_default_when_no_node_config_set(tmp_path):
    db = Database(tmp_path / "node.db")
    stamp = "2026-07-09T14:32:07.123456Z"
    assert format_for_display(stamp, db) == "09.07.2026 14:32"
    db.close()


def test_override_format_takes_priority_over_node_config(tmp_path):
    db = Database(tmp_path / "node.db")
    set_config(db, DISPLAY_FORMAT_CONFIG_KEY, "%Y-%m-%d %H:%M")
    stamp = "2026-07-09T14:32:07.123456Z"
    result = format_for_display(stamp, db, override_format="%m/%d/%Y %I:%M %p")
    assert result == "07/09/2026 02:32 PM"
    db.close()


def test_malformed_config_format_falls_back_to_default(tmp_path):
    db = Database(tmp_path / "node.db")
    # %Q isn't a valid strftime directive. Verified directly (see
    # timeutil.py's comment on is_valid_display_format) that glibc's
    # strftime does NOT raise for this -- it returns "%Q garbage" back
    # out literally -- so this test specifically exercises the allowlist
    # validation catching what a naive try/except around strftime()
    # would silently miss, not exception handling.
    set_config(db, DISPLAY_FORMAT_CONFIG_KEY, "%Q garbage")
    stamp = "2026-07-09T14:32:07.123456Z"
    assert format_for_display(stamp, db) == "09.07.2026 14:32"
    db.close()


# -- is_valid_display_format -------------------------------------------


def test_valid_format_with_common_directives_accepted():
    assert is_valid_display_format("%d.%m.%Y %H:%M") is True
    assert is_valid_display_format("%m/%d/%Y %I:%M %p") is True


def test_format_with_no_directives_is_valid():
    assert is_valid_display_format("just plain text") is True


def test_unknown_directive_rejected():
    # Confirmed directly: this specific input does NOT raise from
    # strftime on glibc, which is exactly why upfront validation (rather
    # than catching a strftime failure) is necessary.
    assert is_valid_display_format("%Q garbage") is False


def test_trailing_percent_rejected():
    assert is_valid_display_format("100%") is False
    assert is_valid_display_format("%") is False


def test_literal_percent_directive_accepted():
    assert is_valid_display_format("100%%") is True


# -- set_display_format ---------------------------------------------------


def test_set_display_format_accepts_valid_format(tmp_path):
    db = Database(tmp_path / "node.db")
    set_display_format(db, "%Y-%m-%d")
    stamp = "2026-07-09T14:32:07.123456Z"
    assert format_for_display(stamp, db) == "2026-07-09"
    db.close()


def test_set_display_format_rejects_invalid_format(tmp_path):
    import pytest

    db = Database(tmp_path / "node.db")
    with pytest.raises(ValueError):
        set_display_format(db, "%Q garbage")
    db.close()
