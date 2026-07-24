"""
Node runtime configuration (design doc, issues #15/#1/#3).

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

from netbbs import __version__
from netbbs.storage.migrations import MIGRATIONS

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
class LinkConfig:
    """
    NetBBS Link's own transport (design doc §11/§12) --
    distinct from `TransportConfig` because "am I dialable" and "what
    do I claim about how to reach me" are two independent questions
    for Link in a way they aren't for an interactive transport: a node
    can run `LinkServer` (`enabled`, bound to `host`/`port`) purely so
    peers *it* dials can reply over the same connection, while still
    being unreachable from anywhere else (`outgoing_only=True`, §12's
    common NAT/residential case) -- `outgoing_only` controls what this
    node's own `endpoint_descriptor` claims, never whether
    the local listener runs at all.

    `advertised_host`/`advertised_port` are only meaningful when
    `outgoing_only` is false (a full peer): what a peer should be told
    to dial, which may differ from `host`/`port` (a router port-forward
    to a different external port, or `host="0.0.0.0"` — a valid bind
    wildcard, never a valid address to hand another node). `advertised_
    port` defaults to `port` when unset; `advertised_host` has no
    default -- see `NodeConfig.validate`.

    `seeds` (design doc §12) is this node's operator-
    configured seed list -- a plain list of base URLs (e.g.
    `"http://198.51.100.7:7862"`) `netbbs.link.sync`'s background loop
    dials every `sync_interval_seconds`. Just the fixed/operator-
    configured half of §12's bootstrap model -- `netbbs.link.seedlist.
    run_scheduled_seed_refresh` fetches a live supplementary
    list over the same channel `netbbs.selfupdate` uses and
    `run_link_sync` merges it in every pass, "a supplement to -- never a
    replacement for" this list, exactly as that design framed
    it. Empty by default -- Link can run accepting inbound traffic
    with nothing configured here at all, relying entirely on
    the live-fetched list (or peer-list-exchange-discovered candidates,
    once something consumes those) to ever reach the network.

    Defaults to disabled, matching §15's "Phase 3 is explicitly
    private/experimental federation" framing -- an operator opts in.

    `relay_serving_enabled`/`max_relay_clients` (design doc §12,
    issue #58) govern this node's own willingness to *act as a
    relay* for other outgoing-only nodes -- entirely separate from
    `outgoing_only` above, which governs whether *this* node needs a
    relay itself. Defaults to serving enabled with a conservative cap
    ("relay-serving defaults to on, with a conservative
    resource cap... and an easy opt-out — confirmed with Thiesi over
    defaulting off," since an opt-in-only default would leave a young
    or small Link without enough relays for outgoing-only nodes to ever
    reliably reach anyone). Neither setting has any effect on this
    node's own outgoing relay *selection* (`netbbs.link.sync`'s own
    `_maintain_relay_selection`, gated purely on `outgoing_only`) --
    they only gate `netbbs.link.transport.LinkServer`'s consent-request
    route, i.e. whether *other* nodes may successfully ask this one to
    relay for them.

    `max_peers`/`max_carried_boards`/`request_rate_*` (design doc §13.9,
    issue #60's third operational slice): issue #60's own "configurable
    with safe defaults" wording for every remotely influenced resource,
    applied to the three gaps that slice found with no bound at all --
    `LinkNode.peers` (any completed hello became a permanent peer,
    unconditionally), locally materialized carried-board count, and
    per-source Link HTTP request rate (no throttling on any Link route
    before this, including the two unauthenticated ones). `request_rate_
    capacity`/`request_rate_refill_per_minute` size one `netbbs.net.
    throttle.LinkRequestThrottle` bucket per source address;
    `request_rate_max_tracked_sources` bounds how many distinct source
    addresses it remembers at once (same LRU-eviction-under-attack
    trade-off `ThrottleConfig.max_tracked_keys` already documents for
    login throttling).

    `diagnostic_log_max_age_days`/`diagnostic_log_max_rows` (design doc
    §13.11, issue #60's remaining pieces): bound `netbbs.link.
    diagnostics.LinkDiagnosticLogHandler`'s own bounded, non-permanent
    `link_diagnostic_log` table -- whichever limit is stricter in
    practice actually governs, both are enforced independently on every
    write.
    """

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 7862
    outgoing_only: bool = True
    advertised_host: str | None = None
    advertised_port: int | None = None
    seeds: list[str] = field(default_factory=list)
    sync_interval_seconds: float = 300.0
    relay_serving_enabled: bool = True
    max_relay_clients: int = 20
    max_peers: int = 1000
    max_carried_boards: int = 500
    # Design doc §9.6, issue #87: same shape as max_carried_boards above,
    # the channel-side counterpart.
    max_carried_channels: int = 500
    request_rate_capacity: float = 20.0
    request_rate_refill_per_minute: float = 60.0
    request_rate_max_tracked_sources: int = 10_000
    diagnostic_log_max_age_days: int = 30
    diagnostic_log_max_rows: int = 5_000


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
    """Design doc: how long a *graceful* shutdown (SIGTERM)
    waits, after broadcasting the warning, before forcibly disconnecting
    everyone still connected — an immediate shutdown (SIGINT) skips this
    wait entirely. Operator-overridable via `[shutdown]`, matching
    `[throttle]`'s precedent."""

    graceful_delay_seconds: float = 60.0


@dataclass(frozen=True)
class NodeConfig:
    db_path: Path = Path("netbbs.db")
    # Design doc: the node's own key-lifecycle state (root
    # key + signing/transport operational keys + transition history,
    # see netbbs.link.node_identity) — a directory, not a single file,
    # since it holds three key files plus a transition-history file.
    # `node_name` is purely the human-readable label attached to the
    # generated keys (Identity.label) -- it has no effect on the
    # fingerprint, which is derived from the key material alone.
    identity_dir: Path = Path("netbbs_identity")
    node_name: str = "netbbs-node"
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
    link: LinkConfig = field(default_factory=LinkConfig)
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    shutdown: ShutdownConfig = field(default_factory=ShutdownConfig)

    def validate(self) -> None:
        for name, transport in (("telnet", self.telnet), ("ssh", self.ssh), ("web", self.web)):
            if not (1 <= transport.port <= 65535):
                raise ConfigError(f"{name}.port must be between 1 and 65535, got {transport.port}")
            if not transport.host.strip():
                raise ConfigError(f"{name}.host must not be empty")

        if self.link.enabled:
            if not (1 <= self.link.port <= 65535):
                raise ConfigError(f"link.port must be between 1 and 65535, got {self.link.port}")
            if not self.link.host.strip():
                raise ConfigError("link.host must not be empty")
            if not self.link.outgoing_only:
                if not self.link.advertised_host or not self.link.advertised_host.strip():
                    raise ConfigError(
                        "link.advertised_host must be set when link.outgoing_only is false -- "
                        "a full peer must know what address to tell others to dial"
                    )
                advertised_port = (
                    self.link.advertised_port if self.link.advertised_port is not None else self.link.port
                )
                if not (1 <= advertised_port <= 65535):
                    raise ConfigError(
                        f"link.advertised_port must be between 1 and 65535, got {advertised_port}"
                    )
            if self.link.sync_interval_seconds <= 0:
                raise ConfigError(
                    "link.sync_interval_seconds must be greater than 0, got "
                    f"{self.link.sync_interval_seconds}"
                )
            for seed in self.link.seeds:
                if not seed.strip():
                    raise ConfigError("link.seeds must not contain an empty entry")
            if self.link.max_relay_clients <= 0:
                raise ConfigError(
                    f"link.max_relay_clients must be greater than 0, got {self.link.max_relay_clients}"
                )
            _require_positive_link = {
                "max_peers": self.link.max_peers,
                "max_carried_boards": self.link.max_carried_boards,
                "max_carried_channels": self.link.max_carried_channels,
                "request_rate_capacity": self.link.request_rate_capacity,
                "request_rate_refill_per_minute": self.link.request_rate_refill_per_minute,
                "request_rate_max_tracked_sources": self.link.request_rate_max_tracked_sources,
                "diagnostic_log_max_age_days": self.link.diagnostic_log_max_age_days,
                "diagnostic_log_max_rows": self.link.diagnostic_log_max_rows,
            }
            for name, value in _require_positive_link.items():
                if value <= 0:
                    raise ConfigError(f"link.{name} must be greater than 0, got {value}")

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
        if self.link.enabled and not self.link.outgoing_only:
            warnings.append(
                f"NetBBS Link is configured as a full peer, advertising "
                f"{self.link.advertised_host}:{self.link.advertised_port or self.link.port} to other "
                "nodes -- design doc §15: Phase 3 remains explicitly private/experimental federation "
                "with no public trust/reputation or quarantine model yet (issue #55), even though the "
                "WAN/NAT trust-boundary work (issue #58) and operational controls (issue #60) have "
                "landed. Not a plaintext-password risk the way Telnet/web are (Link traffic is signed, "
                "not password-authenticated), but an externally reachable Link listener accepts hellos "
                "from any node that dials it, with no reputation/quarantine model to fall back on yet. "
                "Prefer outgoing_only (the default) for anything but a small, trusted, invite-your-"
                "friends deployment."
            )
        return warnings


_TRANSPORTS = ("telnet", "ssh", "web")


def _version_string() -> str:
    """`netbbs <release version> (schema version N)` -- issue #82: an
    operator upgrading a package-managed install needs a fast way to
    confirm what they actually have installed and what database schema
    it expects, without starting a node. The schema number is this
    build's own `len(MIGRATIONS)` (`netbbs.storage.migrations`), the
    exact value `Database.__init__` compares a database's `PRAGMA
    user_version` against -- independent of the release version
    string, which is why both are shown rather than just one."""
    return f"netbbs {__version__} (schema version {len(MIGRATIONS)})"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="netbbs", description="Run a NetBBS node.")
    parser.add_argument("--version", action="version", version=_version_string())
    parser.add_argument("--config", type=Path, default=None, help="path to a TOML config file")
    parser.add_argument("--db", type=Path, default=None, help="path to the node's SQLite database")
    parser.add_argument(
        "--identity-dir", type=Path, default=None, help="directory holding the node's Link key-lifecycle state"
    )
    parser.add_argument("--node-name", type=str, default=None, help="human-readable label for this node's keys")
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

    # Link: special-cased, not folded into the _TRANSPORTS
    # loop above -- LinkConfig carries outgoing_only/advertised_host/
    # advertised_port beyond bare TransportConfig's enabled/host/port.
    link_group = parser.add_mutually_exclusive_group()
    link_group.add_argument("--enable-link", dest="link_enabled", action="store_true", default=None)
    link_group.add_argument("--disable-link", dest="link_enabled", action="store_false", default=None)
    parser.add_argument("--link-host", dest="link_host", default=None)
    parser.add_argument("--link-port", dest="link_port", type=int, default=None)
    outgoing_group = parser.add_mutually_exclusive_group()
    outgoing_group.add_argument(
        "--link-outgoing-only", dest="link_outgoing_only", action="store_true", default=None
    )
    outgoing_group.add_argument(
        "--link-full-peer", dest="link_outgoing_only", action="store_false", default=None
    )
    parser.add_argument("--link-advertised-host", dest="link_advertised_host", default=None)
    parser.add_argument("--link-advertised-port", dest="link_advertised_port", type=int, default=None)
    # --link-seed is repeatable (netbbs --link-seed
    # http://a:7862 --link-seed http://b:7862 ...) -- when given at all,
    # it *replaces* the config file's [link] seeds list entirely,
    # matching every other setting's "CLI wins, full override" behavior
    # in this module (see _apply_cli_overrides) rather than merging.
    parser.add_argument("--link-seed", dest="link_seeds", action="append", default=None)
    parser.add_argument(
        "--link-sync-interval-seconds", dest="link_sync_interval_seconds", type=float, default=None
    )
    # issue #58: relay-serving opt-out + resource cap.
    relay_serving_group = parser.add_mutually_exclusive_group()
    relay_serving_group.add_argument(
        "--link-relay-serving", dest="link_relay_serving_enabled", action="store_true", default=None
    )
    relay_serving_group.add_argument(
        "--link-no-relay-serving", dest="link_relay_serving_enabled", action="store_false", default=None
    )
    parser.add_argument(
        "--link-max-relay-clients", dest="link_max_relay_clients", type=int, default=None
    )
    # Design doc §13.9 (issue #60's third operational slice).
    parser.add_argument("--link-max-peers", dest="link_max_peers", type=int, default=None)
    parser.add_argument(
        "--link-max-carried-boards", dest="link_max_carried_boards", type=int, default=None
    )
    parser.add_argument(
        "--link-max-carried-channels", dest="link_max_carried_channels", type=int, default=None
    )
    parser.add_argument(
        "--link-request-rate-capacity", dest="link_request_rate_capacity", type=float, default=None
    )
    parser.add_argument(
        "--link-request-rate-refill-per-minute",
        dest="link_request_rate_refill_per_minute", type=float, default=None,
    )
    parser.add_argument(
        "--link-request-rate-max-tracked-sources",
        dest="link_request_rate_max_tracked_sources", type=int, default=None,
    )
    # Design doc §13.11 (issue #60's remaining pieces).
    parser.add_argument(
        "--link-diagnostic-log-max-age-days", dest="link_diagnostic_log_max_age_days", type=int, default=None
    )
    parser.add_argument(
        "--link-diagnostic-log-max-rows", dest="link_diagnostic_log_max_rows", type=int, default=None
    )
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


def _node_from_toml(data: dict, config: NodeConfig) -> tuple[Path, str]:
    table = data.get("node", {})
    if not isinstance(table, dict):
        raise ConfigError("[node] in the config file must be a table")
    unknown = set(table) - {"identity_dir", "name"}
    if unknown:
        raise ConfigError(f"[node] has unknown setting(s): {', '.join(sorted(unknown))}")
    identity_dir = Path(table["identity_dir"]) if "identity_dir" in table else config.identity_dir
    node_name = str(table["name"]) if "name" in table else config.node_name
    return identity_dir, node_name


def _link_from_toml(data: dict, current: LinkConfig) -> LinkConfig:
    table = data.get("link", {})
    if not isinstance(table, dict):
        raise ConfigError("[link] in the config file must be a table")
    unknown = set(table) - set(LinkConfig.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"[link] has unknown setting(s): {', '.join(sorted(unknown))}")
    seeds = table.get("seeds", current.seeds)
    if not isinstance(seeds, list) or not all(isinstance(item, str) for item in seeds):
        raise ConfigError("link.seeds must be a list of strings")
    return LinkConfig(
        enabled=bool(table.get("enabled", current.enabled)),
        host=str(table.get("host", current.host)),
        port=int(table.get("port", current.port)),
        outgoing_only=bool(table.get("outgoing_only", current.outgoing_only)),
        advertised_host=table.get("advertised_host", current.advertised_host),
        advertised_port=table.get("advertised_port", current.advertised_port),
        seeds=list(seeds),
        sync_interval_seconds=float(table.get("sync_interval_seconds", current.sync_interval_seconds)),
        relay_serving_enabled=bool(table.get("relay_serving_enabled", current.relay_serving_enabled)),
        max_relay_clients=int(table.get("max_relay_clients", current.max_relay_clients)),
        max_peers=int(table.get("max_peers", current.max_peers)),
        max_carried_boards=int(table.get("max_carried_boards", current.max_carried_boards)),
        max_carried_channels=int(table.get("max_carried_channels", current.max_carried_channels)),
        request_rate_capacity=float(table.get("request_rate_capacity", current.request_rate_capacity)),
        request_rate_refill_per_minute=float(
            table.get("request_rate_refill_per_minute", current.request_rate_refill_per_minute)
        ),
        request_rate_max_tracked_sources=int(
            table.get("request_rate_max_tracked_sources", current.request_rate_max_tracked_sources)
        ),
        diagnostic_log_max_age_days=int(
            table.get("diagnostic_log_max_age_days", current.diagnostic_log_max_age_days)
        ),
        diagnostic_log_max_rows=int(table.get("diagnostic_log_max_rows", current.diagnostic_log_max_rows)),
    )


def _apply_toml(config: NodeConfig, data: dict) -> NodeConfig:
    known_tables = {"database", "node", "telnet", "ssh", "web", "link", "throttle", "shutdown"}
    unknown = set(data) - known_tables
    if unknown:
        raise ConfigError(f"config file has unknown section(s): {', '.join(sorted(unknown))}")

    db_table = data.get("database", {})
    if not isinstance(db_table, dict):
        raise ConfigError("[database] in the config file must be a table")
    db_path = Path(db_table["path"]) if "path" in db_table else config.db_path

    identity_dir, node_name = _node_from_toml(data, config)

    return NodeConfig(
        db_path=db_path,
        identity_dir=identity_dir,
        node_name=node_name,
        telnet=_transport_from_toml(data, "telnet", config.telnet),
        ssh=_transport_from_toml(data, "ssh", config.ssh),
        web=_transport_from_toml(data, "web", config.web),
        link=_link_from_toml(data, config.link),
        throttle=_throttle_from_toml(data, config.throttle),
        shutdown=_shutdown_from_toml(data, config.shutdown),
    )


def _apply_cli_overrides(config: NodeConfig, args: argparse.Namespace) -> NodeConfig:
    if args.db is not None:
        config = replace(config, db_path=args.db)
    if args.identity_dir is not None:
        config = replace(config, identity_dir=args.identity_dir)
    if args.node_name is not None:
        config = replace(config, node_name=args.node_name)
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

    link = config.link
    link_overrides = (
        args.link_enabled,
        args.link_host,
        args.link_port,
        args.link_outgoing_only,
        args.link_advertised_host,
        args.link_advertised_port,
        args.link_seeds,
        args.link_sync_interval_seconds,
        args.link_relay_serving_enabled,
        args.link_max_relay_clients,
        args.link_max_peers,
        args.link_max_carried_boards,
        args.link_max_carried_channels,
        args.link_request_rate_capacity,
        args.link_request_rate_refill_per_minute,
        args.link_request_rate_max_tracked_sources,
        args.link_diagnostic_log_max_age_days,
        args.link_diagnostic_log_max_rows,
    )
    if any(value is not None for value in link_overrides):
        config = replace(
            config,
            link=LinkConfig(
                enabled=link.enabled if args.link_enabled is None else args.link_enabled,
                host=link.host if args.link_host is None else args.link_host,
                port=link.port if args.link_port is None else args.link_port,
                outgoing_only=(
                    link.outgoing_only if args.link_outgoing_only is None else args.link_outgoing_only
                ),
                advertised_host=(
                    link.advertised_host if args.link_advertised_host is None else args.link_advertised_host
                ),
                advertised_port=(
                    link.advertised_port if args.link_advertised_port is None else args.link_advertised_port
                ),
                seeds=link.seeds if args.link_seeds is None else args.link_seeds,
                sync_interval_seconds=(
                    link.sync_interval_seconds
                    if args.link_sync_interval_seconds is None
                    else args.link_sync_interval_seconds
                ),
                relay_serving_enabled=(
                    link.relay_serving_enabled
                    if args.link_relay_serving_enabled is None
                    else args.link_relay_serving_enabled
                ),
                max_relay_clients=(
                    link.max_relay_clients
                    if args.link_max_relay_clients is None
                    else args.link_max_relay_clients
                ),
                max_peers=(link.max_peers if args.link_max_peers is None else args.link_max_peers),
                max_carried_boards=(
                    link.max_carried_boards
                    if args.link_max_carried_boards is None
                    else args.link_max_carried_boards
                ),
                max_carried_channels=(
                    link.max_carried_channels
                    if args.link_max_carried_channels is None
                    else args.link_max_carried_channels
                ),
                request_rate_capacity=(
                    link.request_rate_capacity
                    if args.link_request_rate_capacity is None
                    else args.link_request_rate_capacity
                ),
                request_rate_refill_per_minute=(
                    link.request_rate_refill_per_minute
                    if args.link_request_rate_refill_per_minute is None
                    else args.link_request_rate_refill_per_minute
                ),
                request_rate_max_tracked_sources=(
                    link.request_rate_max_tracked_sources
                    if args.link_request_rate_max_tracked_sources is None
                    else args.link_request_rate_max_tracked_sources
                ),
                diagnostic_log_max_age_days=(
                    link.diagnostic_log_max_age_days
                    if args.link_diagnostic_log_max_age_days is None
                    else args.link_diagnostic_log_max_age_days
                ),
                diagnostic_log_max_rows=(
                    link.diagnostic_log_max_rows
                    if args.link_diagnostic_log_max_rows is None
                    else args.link_diagnostic_log_max_rows
                ),
            ),
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
