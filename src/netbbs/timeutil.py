"""
Timestamp storage and display formatting, shared across the codebase.

Two distinct concerns, deliberately kept separate:

- **Storage** (`utc_now_iso`): `datetime.isoformat()` silently drops the
  fractional-seconds field whenever microseconds happen to be exactly
  zero, producing `...T12:00:00+00:00` on one call and
  `...T12:00:00.482913+00:00` on the next. That's an easy thing to
  overlook until a timestamp string ends up inside something hashed or
  signed (see design doc §7 — DAG events use a timestamp as an ordering
  tiebreaker) and two semantically-similar moments serialize to
  different-shaped strings. `utc_now_iso()` forces a fixed, always-6-digit
  microsecond field instead, so the format never varies. Every part of
  the codebase that stamps a "when did this happen" value for storage
  should go through this function, never `datetime.now(...).isoformat()`
  directly.

- **Display** (`format_for_display`): what a user actually sees is a
  completely different concern from what gets stored — nobody should ever
  see microsecond precision, and the exact shape (day/month/year order,
  24-hour vs. 12-hour, separators) should be configurable rather than
  hardcoded. See that function's docstring for the resolution order
  between a future per-user preference, the node-wide default, and a
  hardcoded fallback.
"""

from __future__ import annotations

import datetime

from netbbs.config import get_config, set_config
from netbbs.storage.database import Database


def utc_now_iso() -> str:
    """Current UTC time as a fixed-format, always-6-decimal ISO 8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Config key for the node-wide default display format, stored via
# netbbs.config.
DISPLAY_FORMAT_CONFIG_KEY = "display_timestamp_format"

# European-style default (day.month.year, 24-hour clock, no seconds) —
# deliberately NOT the microsecond-precision storage format above. Users
# should never see sub-second precision; that level of detail exists
# purely for internal ordering and content-ID hashing (§7), never for
# display. This is only a starting default, not a judgment that it's
# correct for everyone — it's fully overridable via node config, and
# eventually per-user preference (see the resolution order below).
_DEFAULT_DISPLAY_FORMAT = "%d.%m.%Y %H:%M"

# strftime directive letters considered safe to accept into a stored
# display format. Deliberately a conservative allowlist rather than
# trying to catch bad input reactively — verified directly that
# strftime's handling of an *unknown* directive is platform-dependent
# (delegates to the C library): on glibc, `strftime("%Q garbage")`
# doesn't raise, it just returns "%Q garbage" back out literally. A
# try/except around strftime() therefore cannot reliably detect a
# malformed format string, and NetBSD's libc could easily behave
# differently again — so validation has to happen before strftime is
# ever called, not by catching a failure that may not occur.
_VALID_STRFTIME_DIRECTIVES = set("YmdHMSyIpAaBbZz%")


def is_valid_display_format(fmt: str) -> bool:
    """Check that `fmt` only uses directives from the known-safe allowlist."""
    i = 0
    while i < len(fmt):
        if fmt[i] == "%":
            if i + 1 >= len(fmt) or fmt[i + 1] not in _VALID_STRFTIME_DIRECTIVES:
                return False
            i += 2
        else:
            i += 1
    return True


def set_display_format(db: Database, fmt: str) -> None:
    """
    Set the node-wide display timestamp format, validating it first.

    Rejects (raises `ValueError`) rather than silently accepting an
    invalid format — better for whoever's setting it to get immediate,
    actionable feedback than to have it silently ignored later at
    display time.
    """
    if not is_valid_display_format(fmt):
        raise ValueError(f"invalid display format: {fmt!r}")
    set_config(db, DISPLAY_FORMAT_CONFIG_KEY, fmt)


def format_for_display(
    iso_timestamp: str,
    db: Database | None = None,
    *,
    override_format: str | None = None,
) -> str:
    """
    Format a stored UTC timestamp (as produced by `utc_now_iso()`) for
    showing to a user. Never includes sub-second precision, regardless of
    what's configured — see the docstring on `_DEFAULT_DISPLAY_FORMAT`.

    Resolution order, highest priority first:
      1. `override_format` — reserved for a future per-user preference.
         Nothing calls this with a real per-user value yet (no user
         preferences system exists — see design doc §13/§15 phasing), but
         the parameter exists now specifically so wiring that in later
         needs no changes here, only a caller passing the user's stored
         preference through.
      2. The node-wide default stored in `node_config` (via
         `netbbs.config`), if `db` is given.
      3. `_DEFAULT_DISPLAY_FORMAT`, if neither of the above apply.

    Whatever format is resolved is re-validated here regardless of
    source (see `is_valid_display_format`) and falls back to the
    hardcoded default if it doesn't pass — defense in depth in case
    something bypassed `set_display_format`'s own validation (writing
    directly via `netbbs.config.set_config`, or a future per-user
    preference path that doesn't route through validation).
    """
    parsed = datetime.datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=datetime.timezone.utc
    )

    if override_format is not None:
        fmt = override_format
    elif db is not None:
        fmt = get_config(db, DISPLAY_FORMAT_CONFIG_KEY, default=_DEFAULT_DISPLAY_FORMAT)
    else:
        fmt = _DEFAULT_DISPLAY_FORMAT

    if not is_valid_display_format(fmt):
        fmt = _DEFAULT_DISPLAY_FORMAT

    return parsed.strftime(fmt)
