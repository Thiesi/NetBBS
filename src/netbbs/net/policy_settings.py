"""Operating policy the SysOp tunes from the console (issue #730).

Link limits, the login throttle and the shutdown delays used to live only
in `netbbs.toml` and on the command line. They are policy, not plumbing:
a SysOp adjusts them over time and should not need a shell to do it. Each
now resolves, per setting, in this order:

1. an explicit value in the config file or on the command line -- it wins,
   exactly as `[link] enabled` beats the SysOp's participation answer;
2. a value saved from the SysOp console, stored in `node_config` under
   `policy.<section>.<name>`;
3. the built-in default.

A config file that sets nothing here behaves exactly as before, and one
that sets something keeps it: the console shows that setting as "set in
config" and does not offer to change it.

Everything resolves once, at startup (`apply_stored_policy`). None of these
values is read live by the running code -- the carry caps, the rate limits
and the diagnostic retention are handed by value to the objects built at
startup -- so a change saved from the console applies at the next start,
and the screen says so. At startup the node also records what it resolved
(`record_startup_policy`), so the console -- including `python -m
netbbs.admin`, which never sees the config file -- can say which settings
the config file holds and what the running node is using.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, replace
from typing import Any

from netbbs.config import get_config, set_config_without_commit
from netbbs.digits import is_ascii_number
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

_KEY_PREFIX = "policy."
STARTUP_SNAPSHOT_KEY = "policy_startup_snapshot"

# No config-file bound exists above zero; this one only keeps a typed
# value from overflowing a timer or a counter somewhere downstream.
_MAXIMUM = 1_000_000_000

SECTIONS = ("link", "throttle", "shutdown")

# A token bucket spends one whole token per request or login attempt, so a
# burst capacity below 1 admits nothing at all -- every caller locked out.
_BURST_MINIMUM = 1.0
# A timeout or interval below a second is not a setting anyone means: a
# timeout that small fails every connection, and a sync interval that small
# (below the event loop's clock resolution) never sleeps at all.
_SECONDS_MINIMUM = 1.0
_SYNC_INTERVAL_MINIMUM = 10.0
# A refill rate is divided down to tokens per second; one so small that the
# division reaches zero never refills, and the bucket stays empty for good.
_REFILL_MINIMUM = 0.01
# Diagnostic retention is subtracted from today's date; far beyond this the
# subtraction leaves the calendar and every diagnostic write fails.
_MAX_RETENTION_DAYS = 36_500


@dataclass(frozen=True)
class PolicySetting:
    key: str                 # "link.max_peers"
    kind: type               # int, float, bool or list (of str)
    group: str               # the console screen that holds it
    label: str
    help: str
    # Carry caps may be 0 (issue #683: a curated node carries nothing
    # unasked); every other number must be greater than 0.
    allow_zero: bool = False
    # Tighter bounds where the code that uses the value needs them.
    minimum: float | None = None
    maximum: float = _MAXIMUM

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def attr(self) -> str:
        return self.key.split(".", 1)[1]


GROUP_CARRY = "Carry caps"
GROUP_PEERING = "Peering"
GROUP_LINK_LIMITS = "Link limits"
GROUP_RELAY = "Real-time relay"
GROUP_THROTTLE = "Throttle for logins"
GROUP_SHUTDOWN = "Shutdown"
GROUPS = (GROUP_CARRY, GROUP_PEERING, GROUP_LINK_LIMITS, GROUP_RELAY, GROUP_THROTTLE, GROUP_SHUTDOWN)

SETTINGS: tuple[PolicySetting, ...] = (
    PolicySetting("link.max_carried_boards", int, GROUP_CARRY, "Carried boards",
                  "How many linked message boards from other nodes this node carries on its own. "
                  "Past it, a new one waits under Link status -> Offered. 0 carries only what you accept.",
                  allow_zero=True),
    PolicySetting("link.max_carried_channels", int, GROUP_CARRY, "Carried channels",
                  "The same cap for linked chat channels.", allow_zero=True),
    PolicySetting("link.max_carried_file_areas", int, GROUP_CARRY, "Carried file areas",
                  "The same cap for linked file areas.", allow_zero=True),
    PolicySetting("link.max_peers", int, GROUP_PEERING, "Peers remembered",
                  "How many other nodes this node keeps as peers."),
    PolicySetting("link.seeds", list, GROUP_PEERING, "Manual seeds",
                  "Link addresses this node syncs with besides the reliable nodes, separated by "
                  "spaces or commas. Empty means none."),
    PolicySetting("link.sync_interval_seconds", float, GROUP_PEERING, "Sync interval (seconds)",
                  "How often this node syncs with its seeds and peers.", minimum=_SYNC_INTERVAL_MINIMUM),
    PolicySetting("link.relay_serving_enabled", bool, GROUP_PEERING, "Relay for others",
                  "Whether a full peer relays for nodes that cannot be dialled. An outgoing-only "
                  "node never relays, whatever this says."),
    PolicySetting("link.max_relay_clients", int, GROUP_PEERING, "Relay clients",
                  "How many nodes a full peer relays for at once."),
    PolicySetting("link.max_remote_files_per_area", int, GROUP_LINK_LIMITS, "Files per carried area",
                  "Catalogue entries kept for one carried file area."),
    PolicySetting("link.max_concurrent_file_transfers_per_peer", int, GROUP_LINK_LIMITS,
                  "File transfers per peer", "Concurrent file transfers served to one peer."),
    PolicySetting("link.request_rate_capacity", float, GROUP_LINK_LIMITS, "Request burst",
                  "Link requests one address may make in a burst.", minimum=_BURST_MINIMUM),
    PolicySetting("link.request_rate_refill_per_minute", float, GROUP_LINK_LIMITS, "Requests per minute",
                  "How fast that burst allowance refills.", minimum=_REFILL_MINIMUM),
    PolicySetting("link.request_rate_max_tracked_sources", int, GROUP_LINK_LIMITS, "Addresses tracked",
                  "How many requesting addresses the rate limit remembers at once."),
    PolicySetting("link.diagnostic_log_max_age_days", int, GROUP_LINK_LIMITS, "Diagnostics kept (days)",
                  "How long Link and MRC diagnostic entries are kept.", maximum=_MAX_RETENTION_DAYS),
    PolicySetting("link.diagnostic_log_max_rows", int, GROUP_LINK_LIMITS, "Diagnostics kept (entries)",
                  "At most this many diagnostic entries are kept, whichever limit is stricter."),
    PolicySetting("link.live_relay_max_concurrent_pairs", int, GROUP_RELAY, "Live bridges",
                  "Live chat conversations a full peer relays at once."),
    PolicySetting("link.live_relay_max_pending_rendezvous", int, GROUP_RELAY, "Pending rendezvous",
                  "Live relay requests waiting for their other side."),
    PolicySetting("link.live_relay_rendezvous_timeout_seconds", float, GROUP_RELAY,
                  "Rendezvous timeout (seconds)", "How long a relay request waits for its other side.", minimum=_SECONDS_MINIMUM),
    PolicySetting("link.live_relay_idle_timeout_seconds", float, GROUP_RELAY, "Idle timeout (seconds)",
                  "A relayed conversation with no traffic for this long is closed.", minimum=_SECONDS_MINIMUM),
    PolicySetting("link.live_relay_max_bytes_per_second", int, GROUP_RELAY, "Bytes per second",
                  "Bandwidth one relayed conversation may use."),
    PolicySetting("throttle.max_attempts_per_connection", int, GROUP_THROTTLE, "Attempts per connection",
                  "Failed logins allowed before a connection is closed."),
    PolicySetting("throttle.per_source_capacity", float, GROUP_THROTTLE, "Per address: burst",
                  "Login attempts one address may make in a burst.", minimum=_BURST_MINIMUM),
    PolicySetting("throttle.per_source_refill_per_minute", float, GROUP_THROTTLE, "Per address: per minute",
                  "How fast one address's allowance refills.", minimum=_REFILL_MINIMUM),
    PolicySetting("throttle.per_username_capacity", float, GROUP_THROTTLE, "Per account: burst",
                  "Login attempts against one account in a burst.", minimum=_BURST_MINIMUM),
    PolicySetting("throttle.per_username_refill_per_minute", float, GROUP_THROTTLE, "Per account: per minute",
                  "How fast one account's allowance refills.", minimum=_REFILL_MINIMUM),
    PolicySetting("throttle.global_capacity", float, GROUP_THROTTLE, "Whole node: burst",
                  "Login attempts across the whole node in a burst.", minimum=_BURST_MINIMUM),
    PolicySetting("throttle.global_refill_per_minute", float, GROUP_THROTTLE, "Whole node: per minute",
                  "How fast the node-wide allowance refills.", minimum=_REFILL_MINIMUM),
    PolicySetting("throttle.max_tracked_keys", int, GROUP_THROTTLE, "Addresses/accounts tracked",
                  "How many addresses and accounts the throttle remembers at once."),
    PolicySetting("throttle.max_concurrent_unauthenticated_sessions", int, GROUP_THROTTLE,
                  "Connections not logged in", "Connections allowed at once before anyone logs in."),
    PolicySetting("throttle.login_deadline_seconds", float, GROUP_THROTTLE, "Login deadline (seconds)",
                  "Time a connection has to finish logging in.", minimum=_SECONDS_MINIMUM),
    PolicySetting("throttle.unauthenticated_idle_timeout_seconds", float, GROUP_THROTTLE,
                  "Idle at login (seconds)", "A connection idle this long at the login prompt is closed.", minimum=_SECONDS_MINIMUM),
    PolicySetting("shutdown.graceful_delay_seconds", float, GROUP_SHUTDOWN, "Warning before shutdown (s)",
                  "How long callers are warned before a graceful shutdown (the service manager's stop) "
                  "disconnects them. Raise the service's stop timeout to match if you raise this.", minimum=_SECONDS_MINIMUM),
    PolicySetting("shutdown.background_task_drain_seconds", float, GROUP_SHUTDOWN, "Background drain (s)",
                  "How long shutdown waits for each background task and listener to stop.", minimum=_SECONDS_MINIMUM),
)

BY_KEY = {setting.key: setting for setting in SETTINGS}


class PolicyValueError(ValueError):
    """A value this setting cannot take."""


def validate_value(setting: PolicySetting, value: Any) -> Any:
    """`value` normalized for `setting`, or `PolicyValueError`. The bounds
    are `NodeConfig.validate`'s own, plus an upper limit."""
    if setting.kind is bool:
        if not isinstance(value, bool):
            raise PolicyValueError(f"{setting.label} must be yes or no.")
        return value
    if setting.kind is list:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise PolicyValueError(f"{setting.label} must be a list of addresses.")
        items = [item.strip() for item in value]
        if any(not item for item in items):
            raise PolicyValueError(f"{setting.label} must not contain an empty entry.")
        return items
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyValueError(f"{setting.label} must be a number.")
    too_big = f"{setting.label} must be at most {setting.maximum:,}."
    if setting.kind is int:
        if isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                raise PolicyValueError(f"{setting.label} must be a whole number.")
            value = int(value)
        # Compared as an integer, before anything turns it into a float:
        # a few hundred digits overflow a float instead of failing politely.
        if value > setting.maximum:
            raise PolicyValueError(too_big)
    else:
        try:
            value = float(value)
        except OverflowError as exc:
            # Either sign: say what the setting takes, not just the upper end.
            raise PolicyValueError(f"{setting.label} must be a finite number.") from exc
    # An int is always finite, and `math.isfinite` would convert it to a
    # float first -- which overflows for a few hundred digits either sign.
    if setting.kind is not int and not math.isfinite(value):
        raise PolicyValueError(f"{setting.label} must be a finite number.")
    if setting.minimum is not None:
        too_low, low = value < setting.minimum, f"at least {format_value(setting, setting.minimum)}"
    elif setting.allow_zero:
        too_low, low = value < 0, "0 or more"
    else:
        too_low, low = value <= 0, "greater than 0"
    if too_low or value > setting.maximum:
        raise PolicyValueError(f"{setting.label} must be {low} and at most {setting.maximum:,}.")
    return value


def parse_text(setting: PolicySetting, text: str) -> Any:
    """What a SysOp typed, as a value for `setting` (not yet validated)."""
    text = text.strip()
    if setting.kind is list:
        return [item for item in text.replace(",", " ").split() if item]
    if setting.kind is bool:
        lowered = text.lower()
        if lowered in ("y", "yes", "true", "on", "1"):
            return True
        if lowered in ("n", "no", "false", "off", "0"):
            return False
        raise PolicyValueError(f"{setting.label} must be yes or no.")
    try:
        return int(text) if setting.kind is int and is_ascii_number(text.lstrip("+-")) else float(text)
    except ValueError as exc:
        raise PolicyValueError(f"{setting.label} must be a number.") from exc


def format_value(setting: PolicySetting, value: Any) -> str:
    if setting.kind is bool:
        return "yes" if value else "no"
    if setting.kind is list:
        return ", ".join(value) if value else "(none)"
    if setting.kind is float and float(value).is_integer():
        return str(int(value))
    return str(value)


# -- stored values ------------------------------------------------------------


def load_stored_policy(db: Database) -> dict[str, Any]:
    """Every setting saved from the console, validated. A stored value that
    no longer validates is skipped with a warning -- a bad row must not keep
    the node from starting -- and the setting falls back to its default."""
    rows = db.connection.execute(
        "SELECT key, value FROM node_config WHERE key LIKE ?", (_KEY_PREFIX + "%",)
    ).fetchall()
    values: dict[str, Any] = {}
    for row in rows:
        setting = BY_KEY.get(row["key"][len(_KEY_PREFIX):])
        if setting is None:
            continue
        try:
            values[setting.key] = validate_value(setting, json.loads(row["value"]))
        except (ValueError, TypeError, OverflowError) as exc:
            _logger.warning("ignoring stored console setting %s: %s", setting.key, exc)
    return values


def save_policy_without_commit(db: Database, values: dict[str, Any]) -> None:
    """Store (or, for `None`, forget) console values. The caller owns the
    transaction, so the audit entry can commit with it."""
    for key, value in values.items():
        setting = BY_KEY[key]
        if value is None:
            db.connection.execute("DELETE FROM node_config WHERE key = ?", (_KEY_PREFIX + key,))
        else:
            set_config_without_commit(db, _KEY_PREFIX + key, json.dumps(validate_value(setting, value)))


# -- resolution ---------------------------------------------------------------


def config_value(config, setting: PolicySetting) -> Any:
    return getattr(getattr(config, setting.section), setting.attr)


def apply_stored_policy(config, stored: dict[str, Any]):
    """`config` with each stored console value applied, except where the
    config file or command line set that value explicitly."""
    by_section: dict[str, dict[str, Any]] = {}
    for key, value in stored.items():
        setting = BY_KEY.get(key)
        if setting is None or key in config.explicit_keys:
            continue
        by_section.setdefault(setting.section, {})[setting.attr] = list(value) if setting.kind is list else value
    for section, changes in by_section.items():
        config = replace(config, **{section: replace(getattr(config, section), **changes)})
    return config


def record_startup_policy(db: Database, config) -> None:
    """What this start resolved: every effective value, and which of them
    the config file or command line decided. Read back by the console."""
    snapshot = {
        "effective": {s.key: config_value(config, s) for s in SETTINGS},
        "overrides": sorted(key for key in config.explicit_keys if key in BY_KEY),
    }
    set_config_without_commit(db, STARTUP_SNAPSHOT_KEY, json.dumps(snapshot))
    db.connection.commit()


@dataclass(frozen=True)
class PolicyView:
    """One setting as the console shows it."""

    setting: PolicySetting
    default: Any
    stored: Any | None          # saved from the console, if any
    overridden: bool            # the config file or command line set it
    running: Any | None         # what the running (last started) node uses

    @property
    def saved_effective(self) -> Any:
        """What the next start will use, as far as the database decides."""
        if self.overridden and self.running is not None:
            return self.running
        return self.stored if self.stored is not None else self.default


def load_policy_views(db: Database, defaults) -> tuple[list[PolicyView], bool]:
    """Every setting's view, and whether a startup snapshot exists (a node
    that has not started since this version has none, and then nothing is
    known about the config file)."""
    stored = load_stored_policy(db)
    raw = get_config(db, STARTUP_SNAPSHOT_KEY)
    snapshot: dict = {}
    if raw:
        try:
            snapshot = json.loads(raw)
        except ValueError:
            snapshot = {}
    effective = snapshot.get("effective", {}) if isinstance(snapshot, dict) else {}
    overrides = set(snapshot.get("overrides", [])) if isinstance(snapshot, dict) else set()
    views = [
        PolicyView(
            setting=setting,
            default=config_value(defaults, setting),
            stored=stored.get(setting.key),
            overridden=setting.key in overrides,
            running=effective.get(setting.key),
        )
        for setting in SETTINGS
    ]
    return views, bool(snapshot)
