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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


# Config key for the node-wide default display timezone, stored via
# netbbs.config. A separate axis from DISPLAY_FORMAT_CONFIG_KEY: format
# controls the *shape* of the displayed string (day/month order, 12h vs
# 24h), timezone controls *which instant* gets shown — a custom format
# alone does not convert UTC to local time, they're independent settings
# that both need to be right for a genuinely "correct local time" display.
DISPLAY_TIMEZONE_CONFIG_KEY = "display_timezone"

# UTC, not any particular locale's zone — a safe, unopinionated default
# that doesn't assume where a given node's SysOp or users are. Node
# operators are expected to set this explicitly (e.g. "Europe/Berlin")
# via `set_display_timezone`, same as the format string.
_DEFAULT_DISPLAY_TIMEZONE = "UTC"


def is_valid_timezone(name: str) -> bool:
    """
    Check that `name` is a real, loadable IANA timezone identifier.

    Unlike `is_valid_display_format`'s strftime situation, this
    try/except *is* reliable across platforms: `zoneinfo.ZoneInfo`'s
    failure modes are well-defined Python-level logic (does a matching
    tzdata file exist, is the key even a well-formed relative path) —
    verified directly, including that it correctly rejects a
    path-traversal-shaped key (e.g. "../../../etc/passwd") with
    `ValueError` on its own — not undefined behavior delegated to a C
    library the way strftime's unknown-directive handling turned out to
    be. Still depends on the system actually having IANA tzdata
    available (NetBSD's base system should, as a standard Unix
    installation, but this hasn't been verified on Thiesi's actual
    machine specifically — the `tzdata` PyPI package is listed as an
    optional dependency as a fallback, see pyproject.toml).
    """
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def set_display_timezone(db: Database, tz_name: str) -> None:
    """Set the node-wide display timezone, validating it first (same
    immediate-feedback reasoning as `set_display_format`)."""
    if not is_valid_timezone(tz_name):
        raise ValueError(f"invalid timezone: {tz_name!r}")
    set_config(db, DISPLAY_TIMEZONE_CONFIG_KEY, tz_name)


def get_node_timezone(db: Database) -> ZoneInfo:
    """
    The node-wide configured display timezone as an actual `ZoneInfo`
    object -- the same resolve-and-validate-with-fallback logic
    `format_for_display` applies internally for its own no-override
    case, factored out here for a caller that needs to do timezone-
    aware date/time arithmetic (e.g. `netbbs.chat.daybreak`'s local-
    midnight scheduling) rather than just formatting one already-known
    instant into display text.
    """
    tz_name = get_config(db, DISPLAY_TIMEZONE_CONFIG_KEY, default=_DEFAULT_DISPLAY_TIMEZONE)
    if not is_valid_timezone(tz_name):
        tz_name = _DEFAULT_DISPLAY_TIMEZONE
    return ZoneInfo(tz_name)


def resolve_display_preferences(db: Database) -> tuple[str, str]:
    """
    The node-wide `(format, timezone)` pair, resolved and validated once
    — design doc/issue #57: once a DB read only happens via
    `netbbs.storage.execution.DatabaseLane.run`, a caller building
    several `format_for_display` calls in a batch (e.g. every row of a
    `netbbs.net.picker.pick_item` listing) needs to fetch the node's
    config *once* via the lane, then pass the results back into
    `format_for_display`'s `override_format`/`override_timezone`
    parameters for every item — never call `format_for_display` itself
    with `db` directly inside a picker's `name_of`/`description_of`
    callback, since those run synchronously, outside the lane. Also
    strictly fewer DB reads than the old per-call pattern, which
    re-resolved both settings from scratch on every single
    `format_for_display(..., db)` call.
    """
    fmt = get_config(db, DISPLAY_FORMAT_CONFIG_KEY, default=_DEFAULT_DISPLAY_FORMAT)
    if not is_valid_display_format(fmt):
        fmt = _DEFAULT_DISPLAY_FORMAT
    tz_name = get_config(db, DISPLAY_TIMEZONE_CONFIG_KEY, default=_DEFAULT_DISPLAY_TIMEZONE)
    if not is_valid_timezone(tz_name):
        tz_name = _DEFAULT_DISPLAY_TIMEZONE
    return fmt, tz_name


def format_for_display(
    iso_timestamp: str,
    db: Database | None = None,
    *,
    override_format: str | None = None,
    override_timezone: str | None = None,
) -> str:
    """
    Format a stored UTC timestamp (as produced by `utc_now_iso()`) for
    showing to a user. Never includes sub-second precision, regardless of
    what's configured — see the docstring on `_DEFAULT_DISPLAY_FORMAT`.

    Two independent settings are resolved here, each with its own
    priority order (highest first): an eventual per-user value
    (`override_format`/`override_timezone` — reserved for a future
    preferences system, see design doc §13/§15; nothing calls these with
    real per-user values yet, but the parameters exist now so wiring that
    in later needs no changes here) > the node-wide default stored in
    `node_config` (if `db` is given) > a hardcoded fallback.

    Format and timezone are genuinely separate axes: format controls the
    *shape* of the string, timezone controls *which instant* is shown.
    Getting the format right without also converting to the right
    timezone still leaves users looking at UTC clock time, just reshaped.

    Whatever format/timezone are resolved are re-validated here
    regardless of source (see `is_valid_display_format` /
    `is_valid_timezone`) and fall back to the hardcoded defaults if
    invalid — defense in depth in case something bypassed the validated
    setters (writing directly via `netbbs.config.set_config`, or a future
    per-user preference path that doesn't route through validation).
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

    if override_timezone is not None:
        tz_name = override_timezone
    elif db is not None:
        tz_name = get_config(db, DISPLAY_TIMEZONE_CONFIG_KEY, default=_DEFAULT_DISPLAY_TIMEZONE)
    else:
        tz_name = _DEFAULT_DISPLAY_TIMEZONE

    if not is_valid_timezone(tz_name):
        tz_name = _DEFAULT_DISPLAY_TIMEZONE

    localized = parsed.astimezone(ZoneInfo(tz_name))
    return localized.strftime(fmt)
