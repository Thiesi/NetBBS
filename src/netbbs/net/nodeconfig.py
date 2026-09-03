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
import math
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
    configured half of §12's bootstrap model -- `netbbs.link.
    reliable_nodes` supplies the project's reliable-nodes roster (a
    built-in fallback plus a daily live fetch from netbbs.org) and
    `run_link_sync` merges it in every pass once the SysOp accepts
    participation (`netbbs.link.onboarding`, design doc §16 issue #219),
    "a supplement to -- never a replacement for" this list. Empty by
    default -- Link can run accepting inbound traffic with nothing
    configured here at all, relying entirely on the reliable-nodes
    roster (or peer-list-exchange-discovered candidates) to ever reach
    the network.

    `enabled` is tri-state (design doc §16, issue #219): `True`/`False`
    when the operator set it explicitly (TOML `enabled = ...` or
    `--enable-link`/`--disable-link`), `None` when the config is silent.
    An explicit value always wins; a silent config defers to the SysOp's
    node-wide reliable-node participation decision (`netbbs.link.
    onboarding.resolve_link_enabled`, resolved once at startup by
    `netbbs.__main__.run`, which then replaces this field with the
    effective bool so every downstream consumer keeps reading a plain
    bool). A node whose config is silent and whose SysOp never accepted
    therefore stays off, exactly as the old `False` default did.

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

    `realtime_port`/`realtime_advertised_port` (design doc §8.10, issue
    #148): the persistent Noise-protected TCP listener for real-time
    Link chat, deliberately a second port rather than multiplexed onto
    `port` -- it is a different traffic family (raw length-prefixed
    Noise records, never HTTP+JSON) advertised as a separate `addresses`
    entry in this node's own `endpoint_descriptor` (see `netbbs.link.
    transport.LINK_REALTIME_PROTOCOL_TAG`). Shares `host`/`advertised_
    host` with the HTTP listener above -- same NIC/interface, just a
    different port -- so there is no separate `realtime_host` to keep in
    sync. `realtime_advertised_port` defaults to `realtime_port` when
    unset, mirroring `advertised_port`'s own default exactly. The
    real-time listener always starts whenever `enabled` is true,
    regardless of `outgoing_only` -- an outgoing-only node still needs it
    running so a peer *it* dials can reply over the same connection, the
    same reasoning `LinkServer`'s own `host`/`port` already apply to the
    HTTP listener.

    `realtime_port` defaults to `None` -- resolved to `port + 1000` by
    `effective_realtime_port` below, never a fixed constant and never
    merely `port + 1`. A fixed constant collides in exactly the
    deployment pattern this project's own README two-node quickstart
    documents: two loopback nodes told apart only by sequential HTTP
    ports (7862/7863) -- a fixed realtime default would silently equal
    node B's own `port`. `port + 1` is not safe either, for the same
    reason one level removed: node A's `port + 1` (7863) would then
    equal node B's own `port` (7863), a real OS-level bind collision
    the moment both run on one host, just moved from a clear config
    error to a confusing "address already in use" at startup. `+1000`
    keeps every single-node deployment (including every existing config
    predating this field) working unchanged, and gives sequential-port
    multi-node-per-host setups enough headroom that neither a node's own
    `port`/`realtime_port` pair nor two different nodes' pairs can
    collide from small, natural port increments alone.
    """

    enabled: bool | None = None
    host: str = "0.0.0.0"
    port: int = 7862
    outgoing_only: bool = True
    advertised_host: str | None = None
    advertised_port: int | None = None
    realtime_port: int | None = None
    realtime_advertised_port: int | None = None
    seeds: list[str] = field(default_factory=list)
    sync_interval_seconds: float = 300.0
    relay_serving_enabled: bool = True
    max_relay_clients: int = 20
    max_peers: int = 1000
    max_carried_boards: int = 500
    # Design doc §9.6, issue #87: same shape as max_carried_boards above,
    # the channel-side counterpart.
    max_carried_channels: int = 500
    # Design doc §11, issue #89: same shape, the file-area-side
    # counterpart (carried areas) and its own further per-area
    # catalogue-entry bound.
    max_carried_file_areas: int = 500
    max_remote_files_per_area: int = 5000
    # Design doc §11.3, issue #89: how many concurrent chunk-transfer
    # transfer_ids this node will serve for one requesting peer at a
    # time (§13.5's bounded-remote-influence principle).
    max_concurrent_file_transfers_per_peer: int = 4
    request_rate_capacity: float = 20.0
    request_rate_refill_per_minute: float = 60.0
    request_rate_max_tracked_sources: int = 10_000
    diagnostic_log_max_age_days: int = 30
    diagnostic_log_max_rows: int = 5_000
    # Design doc §16 issue #168 Decision 2: bounds for *serving* live
    # relay (only a full peer with relay_serving_enabled ever does).
    # A bridge is two live TCP legs for a whole conversation; the byte-
    # rate bound replaces the per-frame one (a relay never parses
    # frames); the idle timer is protocol-agnostic; pending rendezvous
    # are capped in count and time.
    live_relay_max_concurrent_pairs: int = 8
    live_relay_max_pending_rendezvous: int = 32
    live_relay_rendezvous_timeout_seconds: float = 30.0
    live_relay_idle_timeout_seconds: float = 120.0
    live_relay_max_bytes_per_second: int = 65_536


def effective_realtime_port(link_config: LinkConfig) -> int:
    """`link_config.realtime_port` if the operator set one, else `port +
    1000` -- the one place this default is computed; every reader
    (`validate()`, the real-time listener's own bind, the hello
    provider's advertised address) calls this rather than re-deriving
    it, so the fallback can never drift between them. See `LinkConfig`'s
    own docstring for why `+1000`, not a fixed constant or `+1`."""
    return link_config.realtime_port if link_config.realtime_port is not None else link_config.port + 1000


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
    # Real-world report: an operator's Ctrl+C (an *immediate*, SIGINT
    # shutdown, which this field's own sibling above explicitly promises
    # "skips this wait entirely") took about nine minutes to actually
    # exit. Tracing it found `netbbs.__main__.run`'s own teardown
    # `finally` block bounding its four background tasks' cancellation
    # inconsistently: `link_sync_task` had *a* bound, but the wrong
    # one -- reusing `graceful_delay_seconds` (60s), conflating two
    # unrelated concerns that happened to share one config value: how
    # long a *human* gets to notice a shutdown warning (already fully
    # spent by the time teardown starts -- a graceful shutdown's own
    # countdown already finished before `disconnect_all()`, and an
    # immediate shutdown never had one to spend), versus how long a
    # *background task* gets to notice cancellation before teardown
    # gives up on it and moves on. The other three tasks
    # (`daybreak_task`/`update_check_task`/`reliable_nodes_refresh_task`) had *no*
    # bound at all -- a bare `.cancel()` then an unbounded `await`,
    # which cancellation usually satisfies promptly but isn't
    # guaranteed to (two of the three reach a blocking `urllib.request.
    # urlopen` call via `asyncio.to_thread`; cancelling the *awaiting*
    # coroutine there does not stop the underlying worker thread, which
    # keeps running to completion regardless). All four cancellation
    # waits now share this one short, deliberately generic bound
    # instead -- not Link-specific despite the historical name of the
    # design-doc entry ("graceful drain of Link work") this field's
    # value first existed for.
    background_task_drain_seconds: float = 5.0


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

        # Only an *explicit* `enabled = true` validates the Link block at
        # config-load time. A silent config (`None`, design doc §16 issue
        # #219) may still become enabled via the SysOp's participation
        # decision -- `netbbs.__main__.run` calls `validate_link()` again
        # after resolving that, so a bad Link value never reaches a running
        # node either way, but a local-only node with a stray `[link]`
        # table keeps loading exactly as it did before `enabled` was
        # tri-state.
        if self.link.enabled:
            self.validate_link()

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

        if self.shutdown.background_task_drain_seconds <= 0:
            raise ConfigError(
                "shutdown.background_task_drain_seconds must be greater than 0, got "
                f"{self.shutdown.background_task_drain_seconds}"
            )

        if not self.telnet.enabled and not self.ssh.enabled and not self.web.enabled:
            raise ConfigError(
                "no transport is enabled -- a node with nothing listening can't serve "
                "anyone; enable at least one of telnet, ssh, or web"
            )

    def validate_link(self) -> None:
        """The Link-specific half of `validate()`, callable on its own so
        `netbbs.__main__.run` can re-run it once a silent `enabled` has
        been resolved to `True` from the participation decision."""
        if not (1 <= self.link.port <= 65535):
            raise ConfigError(f"link.port must be between 1 and 65535, got {self.link.port}")
        if not self.link.host.strip():
            raise ConfigError("link.host must not be empty")
        realtime_port = effective_realtime_port(self.link)
        if not (1 <= realtime_port <= 65535):
            raise ConfigError(
                f"link.realtime_port must be between 1 and 65535, got {realtime_port}"
            )
        if realtime_port == self.link.port:
            raise ConfigError(
                "link.realtime_port must differ from link.port -- they are two independent "
                "listeners (HTTP+JSON gossip vs. persistent Noise real-time chat)"
            )
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
            realtime_advertised_port = (
                self.link.realtime_advertised_port
                if self.link.realtime_advertised_port is not None else realtime_port
            )
            if not (1 <= realtime_advertised_port <= 65535):
                raise ConfigError(
                    "link.realtime_advertised_port must be between 1 and 65535, got "
                    f"{realtime_advertised_port}"
                )
        if self.link.sync_interval_seconds <= 0 or not math.isfinite(self.link.sync_interval_seconds):
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
            "max_carried_file_areas": self.link.max_carried_file_areas,
            "max_remote_files_per_area": self.link.max_remote_files_per_area,
            "max_concurrent_file_transfers_per_peer": self.link.max_concurrent_file_transfers_per_peer,
            "request_rate_capacity": self.link.request_rate_capacity,
            "request_rate_refill_per_minute": self.link.request_rate_refill_per_minute,
            "request_rate_max_tracked_sources": self.link.request_rate_max_tracked_sources,
            "diagnostic_log_max_age_days": self.link.diagnostic_log_max_age_days,
            "diagnostic_log_max_rows": self.link.diagnostic_log_max_rows,
            "live_relay_max_concurrent_pairs": self.link.live_relay_max_concurrent_pairs,
            "live_relay_max_pending_rendezvous": self.link.live_relay_max_pending_rendezvous,
            "live_relay_rendezvous_timeout_seconds": self.link.live_relay_rendezvous_timeout_seconds,
            "live_relay_idle_timeout_seconds": self.link.live_relay_idle_timeout_seconds,
            "live_relay_max_bytes_per_second": self.link.live_relay_max_bytes_per_second,
        }
        for name, value in _require_positive_link.items():
            # TOML accepts `inf`/`nan`; neither is a bound. `nan <= 0` is
            # False, so the finiteness check is what actually catches it.
            if value <= 0 or not math.isfinite(value):
                raise ConfigError(f"link.{name} must be a finite number greater than 0, got {value}")

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
                "nodes -- design doc §15/§12: Phase 3 remains a private, invite-your-friends federation, "
                "not a public one (issue #55). Local trust/probation/quarantine enforcement across Link "
                "boundaries is implemented and validated against adversarial scenarios (issues "
                "#126-131), not merely planned -- but an externally reachable Link listener still "
                "accepts a hello from any node that dials it (by design: a stranger has to be seen "
                "before it can be evaluated), and a freshly-seen node stays PROBATIONARY, with only "
                "bounded access, until your own configured trust signals/reporters or a manual SysOp "
                "decision quarantines or blocks it. There is no public, shared reputation network to "
                "lean on yet, and issue #83's independently-administered dogfood -- the remaining step "
                "before any public-readiness claim -- has not happened. Prefer outgoing_only (the "
                "default) for anything but a small, trusted, invite-your-friends deployment."
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
    parser.add_argument("--link-realtime-port", dest="link_realtime_port", type=int, default=None)
    parser.add_argument(
        "--link-realtime-advertised-port", dest="link_realtime_advertised_port", type=int, default=None
    )
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
        "--link-max-carried-file-areas", dest="link_max_carried_file_areas", type=int, default=None
    )
    parser.add_argument(
        "--link-max-remote-files-per-area", dest="link_max_remote_files_per_area", type=int, default=None
    )
    parser.add_argument(
        "--link-max-concurrent-file-transfers-per-peer",
        dest="link_max_concurrent_file_transfers_per_peer", type=int, default=None,
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
    enabled = table.get("enabled", current.enabled)
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"link.enabled must be true or false, got {enabled!r}")
    return LinkConfig(
        enabled=enabled,
        host=str(table.get("host", current.host)),
        port=int(table.get("port", current.port)),
        outgoing_only=bool(table.get("outgoing_only", current.outgoing_only)),
        advertised_host=table.get("advertised_host", current.advertised_host),
        advertised_port=table.get("advertised_port", current.advertised_port),
        realtime_port=table.get("realtime_port", current.realtime_port),
        realtime_advertised_port=table.get("realtime_advertised_port", current.realtime_advertised_port),
        seeds=list(seeds),
        sync_interval_seconds=float(table.get("sync_interval_seconds", current.sync_interval_seconds)),
        relay_serving_enabled=bool(table.get("relay_serving_enabled", current.relay_serving_enabled)),
        max_relay_clients=int(table.get("max_relay_clients", current.max_relay_clients)),
        max_peers=int(table.get("max_peers", current.max_peers)),
        max_carried_boards=int(table.get("max_carried_boards", current.max_carried_boards)),
        max_carried_channels=int(table.get("max_carried_channels", current.max_carried_channels)),
        max_carried_file_areas=int(table.get("max_carried_file_areas", current.max_carried_file_areas)),
        max_remote_files_per_area=int(
            table.get("max_remote_files_per_area", current.max_remote_files_per_area)
        ),
        max_concurrent_file_transfers_per_peer=int(
            table.get(
                "max_concurrent_file_transfers_per_peer", current.max_concurrent_file_transfers_per_peer
            )
        ),
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
        live_relay_max_concurrent_pairs=int(
            table.get("live_relay_max_concurrent_pairs", current.live_relay_max_concurrent_pairs)
        ),
        live_relay_max_pending_rendezvous=int(
            table.get("live_relay_max_pending_rendezvous", current.live_relay_max_pending_rendezvous)
        ),
        live_relay_rendezvous_timeout_seconds=float(
            table.get("live_relay_rendezvous_timeout_seconds", current.live_relay_rendezvous_timeout_seconds)
        ),
        live_relay_idle_timeout_seconds=float(
            table.get("live_relay_idle_timeout_seconds", current.live_relay_idle_timeout_seconds)
        ),
        live_relay_max_bytes_per_second=int(
            table.get("live_relay_max_bytes_per_second", current.live_relay_max_bytes_per_second)
        ),
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
        args.link_realtime_port,
        args.link_realtime_advertised_port,
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
        args.link_max_carried_file_areas,
        args.link_max_remote_files_per_area,
        args.link_max_concurrent_file_transfers_per_peer,
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
                realtime_port=(
                    link.realtime_port if args.link_realtime_port is None else args.link_realtime_port
                ),
                realtime_advertised_port=(
                    link.realtime_advertised_port
                    if args.link_realtime_advertised_port is None
                    else args.link_realtime_advertised_port
                ),
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
                max_carried_file_areas=(
                    link.max_carried_file_areas
                    if args.link_max_carried_file_areas is None
                    else args.link_max_carried_file_areas
                ),
                max_remote_files_per_area=(
                    link.max_remote_files_per_area
                    if args.link_max_remote_files_per_area is None
                    else args.link_max_remote_files_per_area
                ),
                max_concurrent_file_transfers_per_peer=(
                    link.max_concurrent_file_transfers_per_peer
                    if args.link_max_concurrent_file_transfers_per_peer is None
                    else args.link_max_concurrent_file_transfers_per_peer
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
                live_relay_max_concurrent_pairs=link.live_relay_max_concurrent_pairs,
                live_relay_max_pending_rendezvous=link.live_relay_max_pending_rendezvous,
                live_relay_rendezvous_timeout_seconds=link.live_relay_rendezvous_timeout_seconds,
                live_relay_idle_timeout_seconds=link.live_relay_idle_timeout_seconds,
                live_relay_max_bytes_per_second=link.live_relay_max_bytes_per_second,
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
