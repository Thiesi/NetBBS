"""Tests for netbbs.timeutil — deterministic timestamp formatting."""

from __future__ import annotations

import re

from netbbs.config import set_config
from netbbs.storage.database import Database
from netbbs.timeutil import (
    DISPLAY_FORMAT_CONFIG_KEY,
    DISPLAY_TIMEZONE_CONFIG_KEY,
    format_for_display,
    is_valid_display_format,
    is_valid_timezone,
    resolve_display_preferences,
    set_display_format,
    set_display_timezone,
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


# -- resolve_display_preferences (issue #57) ---------------------------------


def test_resolve_display_preferences_defaults_with_no_config_set(tmp_path):
    db = Database(tmp_path / "node.db")
    fmt, tz = resolve_display_preferences(db)
    stamp = "2026-07-09T14:32:07.123456Z"
    assert format_for_display(stamp, override_format=fmt, override_timezone=tz) == format_for_display(stamp, db)
    db.close()


def test_resolve_display_preferences_reflects_node_config(tmp_path):
    db = Database(tmp_path / "node.db")
    set_config(db, DISPLAY_FORMAT_CONFIG_KEY, "%Y-%m-%d %H:%M")
    set_config(db, DISPLAY_TIMEZONE_CONFIG_KEY, "America/New_York")
    fmt, tz = resolve_display_preferences(db)
    assert fmt == "%Y-%m-%d %H:%M"
    assert tz == "America/New_York"
    db.close()


def test_resolve_display_preferences_falls_back_on_invalid_stored_values(tmp_path):
    db = Database(tmp_path / "node.db")
    set_config(db, DISPLAY_FORMAT_CONFIG_KEY, "%Q garbage")
    set_config(db, DISPLAY_TIMEZONE_CONFIG_KEY, "not/a/real/zone")
    fmt, tz = resolve_display_preferences(db)
    assert is_valid_display_format(fmt)
    assert is_valid_timezone(tz)
    db.close()


def test_resolve_display_preferences_matches_repeated_format_for_display_calls(tmp_path):
    """The whole point: fetch once, apply to many items, with the exact
    same result format_for_display(..., db) would have given per-call."""
    db = Database(tmp_path / "node.db")
    set_config(db, DISPLAY_FORMAT_CONFIG_KEY, "%d.%m.%Y")
    fmt, tz = resolve_display_preferences(db)
    stamps = ["2026-07-09T14:32:07.123456Z", "2026-01-01T00:00:00.000000Z"]
    for stamp in stamps:
        assert format_for_display(stamp, override_format=fmt, override_timezone=tz) == format_for_display(stamp, db)
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


# -- timezone conversion ----------------------------------------------------


def test_default_timezone_is_utc_when_nothing_configured():
    stamp = "2026-07-09T14:32:07.123456Z"
    # With no db and the European default format, UTC 14:32 should stay
    # 14:32 (no conversion) when no timezone is configured at all.
    assert format_for_display(stamp) == "09.07.2026 14:32"


def test_timezone_conversion_via_node_config(tmp_path):
    db = Database(tmp_path / "node.db")
    set_display_timezone(db, "Europe/Berlin")
    # In July, Berlin is UTC+2 (CEST) -- 14:32 UTC becomes 16:32 local.
    stamp = "2026-07-09T14:32:07.123456Z"
    assert format_for_display(stamp, db) == "09.07.2026 16:32"
    db.close()


def test_timezone_override_takes_priority_over_node_config(tmp_path):
    db = Database(tmp_path / "node.db")
    set_display_timezone(db, "Europe/Berlin")
    stamp = "2026-07-09T14:32:07.123456Z"
    # America/New_York is UTC-4 in July (EDT) -- 14:32 UTC becomes 10:32.
    result = format_for_display(stamp, db, override_timezone="America/New_York")
    assert result == "09.07.2026 10:32"
    db.close()


def test_is_valid_timezone_accepts_real_zones():
    assert is_valid_timezone("UTC") is True
    assert is_valid_timezone("Europe/Berlin") is True
    assert is_valid_timezone("America/New_York") is True


def test_is_valid_timezone_rejects_garbage():
    assert is_valid_timezone("not_a_real_zone") is False
    assert is_valid_timezone("") is False


def test_is_valid_timezone_rejects_path_traversal_attempt():
    # zoneinfo itself guards against this (raises ValueError), verified
    # directly -- this test exists to confirm is_valid_timezone doesn't
    # accidentally let that ValueError propagate instead of returning
    # False like it's supposed to for any invalid input.
    assert is_valid_timezone("../../../etc/passwd") is False


def test_set_display_timezone_rejects_invalid_zone(tmp_path):
    import pytest

    db = Database(tmp_path / "node.db")
    with pytest.raises(ValueError):
        set_display_timezone(db, "not_a_real_zone")
    db.close()


def test_malformed_timezone_config_falls_back_to_utc(tmp_path):
    db = Database(tmp_path / "node.db")
    # Bypass the validated setter to simulate a corrupted/hand-edited
    # config value, same defense-in-depth reasoning as the malformed
    # format test above.
    set_config(db, DISPLAY_TIMEZONE_CONFIG_KEY, "not_a_real_zone")
    stamp = "2026-07-09T14:32:07.123456Z"
    assert format_for_display(stamp, db) == "09.07.2026 14:32"
    db.close()
