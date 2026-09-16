"""
`python -m services.managed_dns` -- runs one instance of the managed
netbbs.org subdomain + dynamic DNS service (design doc §16, issue #201).

Configuration is environment variables, not a TOML file -- this is a
small, single-purpose standalone service (unlike the installable
`netbbs` node package, which has real config-surface reasons for
`netbbs.net.nodeconfig`'s richer TOML+CLI model), and env vars are the
lower-friction convention for something this size to deploy under
systemd/rc.d/a container without inventing a second config format. See
`services/managed_dns/README.md` for the full deployment runbook,
including the BIND-side TSIG/`allow-update` configuration this process
does not (and cannot) perform on its own.

`MANAGED_DNS_ADMIN_TOKEN` gates `/admin/revoke` (design doc §16
Decision 4, issue #599) and is unset by default, which leaves that route
refusing every request -- an instance whose operator has not set one
simply has no administrative surface.

`MANAGED_DNS_CONTACT` is the channel a SysOp refused by the rate limit
or the cumulative cap is told to use for a legitimate exception (design
doc §16 Decision 3, issue #598). Unset, the refusals say the operator has
not named one; `_build_server` warns at startup so that is a choice
rather than an oversight.

`MANAGED_DNS_DB_PATH` is the only required variable. Everything else has
a reasoned default; `MANAGED_DNS_TSIG_KEYNAME`/`MANAGED_DNS_TSIG_SECRET`
default to unset, in which case this falls back to `LoggingDnsProvider`
(see that class's own docstring: "the default until BIND is actually
configured") with a startup warning, rather than refusing to start --
the rest of the service (registration bookkeeping, the age-gate, abuse
controls) is still fully real and useful before a DNS provider is wired
up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from services.managed_dns.dns_provider import DnsProvider, LoggingDnsProvider, Rfc2136DnsProvider
from services.managed_dns.server import ManagedDnsServer
from services.managed_dns.store import Database

_logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s:%(name)s:%(message)s"


class ConfigError(Exception):
    """Raised for a missing/invalid required environment variable --
    `main()` reports this as one clear line, not a raw traceback."""


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _build_dns_provider() -> DnsProvider:
    keyname = os.environ.get("MANAGED_DNS_TSIG_KEYNAME")
    secret = os.environ.get("MANAGED_DNS_TSIG_SECRET")
    if not keyname or not secret:
        _logger.warning(
            "MANAGED_DNS_TSIG_KEYNAME/MANAGED_DNS_TSIG_SECRET not set -- "
            "using LoggingDnsProvider (no real DNS records will be published). "
            "See services/managed_dns/README.md to configure a real BIND server."
        )
        return LoggingDnsProvider()
    return Rfc2136DnsProvider(
        server=os.environ.get("MANAGED_DNS_BIND_SERVER", "127.0.0.1"),
        zone=os.environ.get("MANAGED_DNS_ZONE", "netbbs.org"),
        keyname=keyname,
        secret=secret,
        algorithm=os.environ.get("MANAGED_DNS_TSIG_ALGORITHM", "hmac-sha256"),
        port=_env_int("MANAGED_DNS_BIND_PORT", 53),
    )


def _build_server() -> ManagedDnsServer:
    db_path_raw = os.environ.get("MANAGED_DNS_DB_PATH")
    if not db_path_raw:
        raise ConfigError("MANAGED_DNS_DB_PATH is required")
    db = Database(Path(db_path_raw))
    if not (os.environ.get("MANAGED_DNS_CONTACT") or "").strip():
        _logger.warning(
            "MANAGED_DNS_CONTACT not set -- a SysOp refused by the rate limit or the "
            "cumulative cap will be told no contact channel is configured. "
            "See services/managed_dns/README.md section 4."
        )

    return ManagedDnsServer(
        os.environ.get("MANAGED_DNS_HOST", "127.0.0.1"),
        _env_int("MANAGED_DNS_PORT", 8080),
        db,
        dns_provider=_build_dns_provider(),
        trust_x_forwarded_for=_env_bool("MANAGED_DNS_TRUST_X_FORWARDED_FOR", False),
        min_age_seconds=_env_float("MANAGED_DNS_MIN_AGE_SECONDS", 24 * 60 * 60),
        cooldown_seconds=_env_float("MANAGED_DNS_COOLDOWN_SECONDS", 90 * 24 * 60 * 60),
        abandonment_seconds=_env_float("MANAGED_DNS_ABANDONMENT_SECONDS", 7 * 24 * 60 * 60),
        rate_limit_capacity=_env_float("MANAGED_DNS_RATE_LIMIT_CAPACITY", 5.0),
        rate_limit_refill_per_minute=_env_float("MANAGED_DNS_RATE_LIMIT_REFILL_PER_MINUTE", 5.0 / 60.0),
        cumulative_cap=_env_int("MANAGED_DNS_CUMULATIVE_CAP", 1000),
        admin_token=os.environ.get("MANAGED_DNS_ADMIN_TOKEN"),
        contact=os.environ.get("MANAGED_DNS_CONTACT"),
    )


async def _run() -> None:
    server = _build_server()
    await server.start()
    primary_exception: BaseException | None = None
    try:
        _logger.info("managed-DNS service listening on %s:%d", server.host, server.port)
        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, shutdown_event.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(shutdown_event.set))
        await shutdown_event.wait()
        _logger.info("shutting down")
    except BaseException as exc:
        primary_exception = exc
        raise
    finally:
        await _stop_server_preserving_primary(server, primary_exception)


async def _stop_server_preserving_primary(
    server: ManagedDnsServer, primary_exception: BaseException | None
) -> None:
    try:
        await asyncio.shield(server.stop())
    except BaseException:
        if primary_exception is None:
            raise
        _logger.exception("managed-DNS cleanup failed while handling an earlier error")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    try:
        asyncio.run(_run())
    except ConfigError as exc:
        _logger.error("configuration error: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
