"""
Node runtime configuration (design doc round 28, issues #15/#1/#3).

Replaces the hardcoded settings `netbbs.__main__` used to carry
directly (fixed `0.0.0.0` binds, Telnet always started, SSH/web started
based only on which optional dependencies happened to be installed) with
a validated, explicit configuration model: an optional TOML file plus
CLI overrides, in that precedence order (CLI wins).

Two things this module intentionally does *not* try to do:

- **No TLS support built directly into the web transport.** A
  TLS-terminating reverse proxy (nginx, relayd, etc.) in front of a
  loopback-bound `aiohttp` instance is the documented, supported way to
  serve the web transport over HTTPS/WSS -- see README. Building
  certificate loading/rotation into `netbbs.net.web` itself would add
  real ongoing maintenance surface for a concern every mainstream
  reverse proxy already solves, and this project's other transports
  (SSH) already provide a secure, no-extra-infrastructure option.
- **No SSH-specific throttling config here.** SSH's own auth-attempt
  and login-deadline handling is asyncssh's job (see
  `netbbs.net.ssh.SSHServer`, which is handed `throttle_config.
  login_deadline_seconds` for asyncssh's own `login_timeout` option);
  only the per-source/per-username/global token-bucket checks are
  shared with Telnet/web, via `netbbs.net.throttle.LoginThrottle`.
"""

from __future__ import annotations

import argparse
import ipaddress
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

_LOOPBACK_HOSTNAMES = {"localhost"}


class ConfigError(Exception):
    """Raised for invalid or unreadable configuration. Always caught at
    the top level (`netbbs.__main__`) and reported as a clear message,
    never a raw traceback -- an operator who fat-fingers a port number
    should get told what's wrong, not `netbbs` crashing on line 40."""


def is_loopback_host(host: str) -> bool:
    """
    Best-effort check for whether `host` is a loopback bind address.

    Deliberately conservative in the "unsure" direction: an unparseable
    hostname (not a literal IP, not the literal string "localhost") is
    treated as NOT loopback. The one place this matters
    (`describe_insecure_bindings` below) uses this to decide whether to
    warn about an insecure listener being reachable off-box -- false
    positives (an extra warning for some exotic loopback-resolving
    hostname this doesn't recognize) are a minor annoyance; false
    negatives (silently not warning about a real external exposure)
    would defeat the point of issue #1's warning requirement.
    """
    if host in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class TransportConfig:
    enabled: bool
    host: str
    port: int


@dataclass(frozen=True)
class ThrottleConfig:
    """Defaults are deliberately chosen, reasonable starting points for
    the design doc's stated deployment scale (§14: low hundreds of
    users, not a public high-traffic target) -- not exhaustively tuned.
    All are operator-overridable via the `[throttle]` config-file
    table."""

    max_attempts_per_connection: int = 3
    per_source_capacity: float = 10.0
    per_source_refill_per_minute: float = 5.0
    per_username_capacity: float = 10.0
    per_username_refill_per_minute: float = 5.0
    global_capacity: float = 100.0
    global_refill_per_minute: float = 60.0
    max_tracked_keys: int = 10_000
    max_concurrent_unauthenticated_sessions: int = 100
    login_deadline_seconds: float = 120.0
    unauthenticated_idle_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class ShutdownConfig:
    """Design doc round 51: how long a *graceful* shutdown (SIGTERM)
    waits, after broadcasting the warning, before forcibly disconnecting
    everyone still connected — an immediate shutdown (SIGINT) skips this
    wait entirely. Operator-overridable via `[shutdown]`, matching
    `[throttle]`'s precedent."""

    graceful_delay_seconds: float = 60.0


@dataclass(frozen=True)
class NodeConfig:
    db_path: Path = Path("netbbs.db")
    # SSH defaults enabled -- issue #1's "make SSH the secure default
    # interactive transport". Telnet and the plain-HTTP web transport
    # default disabled and, when explicitly enabled without an operator-
    # chosen host, default to loopback-only rather than defaulting an
    # insecure listener straight onto every interface.
    #
    # Default ports 2323/2222/8080, not the standard 23/22/80: binding
    # any port below 1024 needs root/CAP_NET_BIND_SERVICE on POSIX
    # systems, more privilege than this process should need or want. A
    # real deployment wanting the standard ports would use a reverse
    # proxy / port-forward rule, a privilege-dropping wrapper, or an
    # inetd-style super-server -- an operator/deployment decision, not
    # this module's job to make for them, so the defaults stay on
    # unprivileged ports and every one is independently configurable
    # via `[telnet]`/`[ssh]`/`[web]` `port` regardless.
    telnet: TransportConfig = field(default_factory=lambda: TransportConfig(False, "127.0.0.1", 2323))
    ssh: TransportConfig = field(default_factory=lambda: TransportConfig(True, "0.0.0.0", 2222))
    web: TransportConfig = field(default_factory=lambda: TransportConfig(False, "127.0.0.1", 8080))
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    shutdown: ShutdownConfig = field(default_factory=ShutdownConfig)

    def validate(self) -> None:
        for name, transport in (("telnet", self.telnet), ("ssh", self.ssh), ("web", self.web)):
            if not (1 <= transport.port <= 65535):
                raise ConfigError(f"{name}.port must be between 1 and 65535, got {transport.port}")
            if not transport.host.strip():
                raise ConfigError(f"{name}.host must not be empty")

        t = self.throttle
        _require_positive = {
            "max_attempts_per_connection": t.max_attempts_per_connection,
            "per_source_capacity": t.per_source_capacity,
            "per_source_refill_per_minute": t.per_source_refill_per_minute,
            "per_username_capacity": t.per_username_capacity,
            "per_username_refill_per_minute": t.per_username_refill_per_minute,
            "global_capacity": t.global_capacity,
            "global_refill_per_minute": t.global_refill_per_minute,
            "max_tracked_keys": t.max_tracked_keys,
            "max_concurrent_unauthenticated_sessions": t.max_concurrent_unauthenticated_sessions,
            "login_deadline_seconds": t.login_deadline_seconds,
            "unauthenticated_idle_timeout_seconds": t.unauthenticated_idle_timeout_seconds,
        }
        for name, value in _require_positive.items():
            if value <= 0:
                raise ConfigError(f"throttle.{name} must be greater than 0, got {value}")

        if self.shutdown.graceful_delay_seconds <= 0:
            raise ConfigError(
                "shutdown.graceful_delay_seconds must be greater than 0, got "
                f"{self.shutdown.graceful_delay_seconds}"
            )

        if not self.telnet.enabled and not self.ssh.enabled and not self.web.enabled:
            raise ConfigError(
                "no transport is enabled -- a node with nothing listening can't serve "
                "anyone; enable at least one of telnet, ssh, or web"
            )

    def describe_insecure_bindings(self) -> list[str]:
        """
        Human-readable warnings for every enabled transport that both
        (a) accepts plaintext passwords and (b) is bound somewhere other
        than loopback -- issue #1's "emit prominent warnings when
        [Telnet or plain HTTP] is enabled on a non-loopback address".
        SSH is excluded regardless of bind address: it isn't plaintext.
        """
        warnings: list[str] = []
        if self.telnet.enabled and not is_loopback_host(self.telnet.host):
            warnings.append(
                f"Telnet is enabled on {self.telnet.host}:{self.telnet.port} -- this is a "
                "PLAINTEXT listener reachable beyond this machine. Passwords entered over "
                "it can be read or altered by anyone on the network path. Prefer SSH, or "
                "bind Telnet to 127.0.0.1 and restrict it to trusted/local use only."
            )
        if self.web.enabled and not is_loopback_host(self.web.host):
            warnings.append(
                f"The web transport is enabled on {self.web.host}:{self.web.port} without "
                "TLS -- this is a PLAINTEXT listener reachable beyond this machine. "
                "Passwords entered over it can be read or altered by anyone on the network "
                "path. Put a TLS-terminating reverse proxy in front of it (recommended: "
                "bind the web transport to 127.0.0.1 and have the proxy be the only thing "
                "reachable externally), or restrict it to trusted/local use only."
            )
        return warnings


_TRANSPORTS = ("telnet", "ssh", "web")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="netbbs", description="Run a NetBBS node.")
    parser.add_argument("--config", type=Path, default=None, help="path to a TOML config file")
    parser.add_argument("--db", type=Path, default=None, help="path to the node's SQLite database")
    for transport in _TRANSPORTS:
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            f"--enable-{transport}", dest=f"{transport}_enabled", action="store_true", default=None
        )
        group.add_argument(
            f"--disable-{transport}", dest=f"{transport}_enabled", action="store_false", default=None
        )
        parser.add_argument(f"--{transport}-host", dest=f"{transport}_host", default=None)
        parser.add_argument(f"--{transport}-port", dest=f"{transport}_port", type=int, default=None)
    return parser


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid TOML: {exc}") from exc


def _transport_from_toml(data: dict, name: str, current: TransportConfig) -> TransportConfig:
    table = data.get(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f"[{name}] in the config file must be a table")
    return TransportConfig(
        enabled=bool(table.get("enabled", current.enabled)),
        host=str(table.get("host", current.host)),
        port=int(table.get("port", current.port)),
    )


def _throttle_from_toml(data: dict, current: ThrottleConfig) -> ThrottleConfig:
    table = data.get("throttle", {})
    if not isinstance(table, dict):
        raise ConfigError("[throttle] in the config file must be a table")
    overrides = {key: table[key] for key in table if key in ThrottleConfig.__dataclass_fields__}
    unknown = set(table) - set(overrides)
    if unknown:
        raise ConfigError(f"[throttle] has unknown setting(s): {', '.join(sorted(unknown))}")
    return replace(current, **overrides)


def _shutdown_from_toml(data: dict, current: ShutdownConfig) -> ShutdownConfig:
    table = data.get("shutdown", {})
    if not isinstance(table, dict):
        raise ConfigError("[shutdown] in the config file must be a table")
    overrides = {key: table[key] for key in table if key in ShutdownConfig.__dataclass_fields__}
    unknown = set(table) - set(overrides)
    if unknown:
        raise ConfigError(f"[shutdown] has unknown setting(s): {', '.join(sorted(unknown))}")
    return replace(current, **overrides)


def _apply_toml(config: NodeConfig, data: dict) -> NodeConfig:
    known_tables = {"database", "telnet", "ssh", "web", "throttle", "shutdown"}
    unknown = set(data) - known_tables
    if unknown:
        raise ConfigError(f"config file has unknown section(s): {', '.join(sorted(unknown))}")

    db_table = data.get("database", {})
    if not isinstance(db_table, dict):
        raise ConfigError("[database] in the config file must be a table")
    db_path = Path(db_table["path"]) if "path" in db_table else config.db_path

    return NodeConfig(
        db_path=db_path,
        telnet=_transport_from_toml(data, "telnet", config.telnet),
        ssh=_transport_from_toml(data, "ssh", config.ssh),
        web=_transport_from_toml(data, "web", config.web),
        throttle=_throttle_from_toml(data, config.throttle),
        shutdown=_shutdown_from_toml(data, config.shutdown),
    )


def _apply_cli_overrides(config: NodeConfig, args: argparse.Namespace) -> NodeConfig:
    if args.db is not None:
        config = replace(config, db_path=args.db)
    for transport in _TRANSPORTS:
        current: TransportConfig = getattr(config, transport)
        enabled = getattr(args, f"{transport}_enabled")
        host = getattr(args, f"{transport}_host")
        port = getattr(args, f"{transport}_port")
        if enabled is None and host is None and port is None:
            continue
        config = replace(
            config,
            **{
                transport: TransportConfig(
                    enabled=current.enabled if enabled is None else enabled,
                    host=current.host if host is None else host,
                    port=current.port if port is None else port,
                )
            },
        )
    return config


def load_config(argv: list[str] | None = None) -> NodeConfig:
    """
    Build a validated `NodeConfig` from an optional TOML file (`--config
    PATH`) plus CLI overrides (CLI wins over file, file wins over
    built-in defaults). Raises `ConfigError` for anything invalid --
    callers should catch this and exit with a clear message rather than
    letting a raw traceback surface (see `netbbs.__main__`).
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    config = NodeConfig()
    if args.config is not None:
        config = _apply_toml(config, _load_toml(args.config))
    config = _apply_cli_overrides(config, args)
    config.validate()
    return config
