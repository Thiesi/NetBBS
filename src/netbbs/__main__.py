"""
`python -m netbbs [--config PATH] [options...]` — node entry point.

Configuration-driven (design doc round 28, issue #15): what listens
where, and the login-throttling policy protecting it, come from
`netbbs.net.nodeconfig.NodeConfig` — an optional TOML file plus CLI
overrides — rather than the hardcoded constants this module used to
carry directly. See `netbbs.net.nodeconfig` for the file format and
`README.md` for a worked example, including the rc.d-friendly
foreground invocation NetBSD deployments should use (this process does
not daemonize itself; that's the service supervisor's job).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from netbbs.auth.users import count_sysops
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.net.login_flow import handle_session
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.nodeconfig import ConfigError, NodeConfig, load_config
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import run_shutdown_sequence
from netbbs.net.throttle import LoginThrottle
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)


class StartupError(Exception):
    """Raised when one or more enabled listeners fail to start.
    Whatever did start is already stopped again by the time this is
    raised — see `run`'s partial-start cleanup."""


def _build_throttle(config: NodeConfig) -> LoginThrottle:
    t = config.throttle
    return LoginThrottle(
        per_source_capacity=t.per_source_capacity,
        per_source_refill_per_minute=t.per_source_refill_per_minute,
        per_username_capacity=t.per_username_capacity,
        per_username_refill_per_minute=t.per_username_refill_per_minute,
        global_capacity=t.global_capacity,
        global_refill_per_minute=t.global_refill_per_minute,
        max_tracked_keys=t.max_tracked_keys,
        max_concurrent_unauthenticated_sessions=t.max_concurrent_unauthenticated_sessions,
    )


async def _start_servers(
    config: NodeConfig, db: Database, session_handler, throttle: LoginThrottle
) -> list:
    """
    Start every enabled, available listener. On any failure partway
    through, stop whatever already started before re-raising as
    `StartupError` (issue #15: "partial startup failures clean up
    already-started components" — an operator should never end up with
    an unintended subset of listeners silently running because the
    third one failed to bind).

    `throttle` is the *same* `LoginThrottle` instance `run()` hands to
    `session_handler` for Telnet/web — SSH's `validate_password` (see
    `netbbs.net.ssh`) must share it too, not get its own, or its
    per-source/per-username/global budgets would track each transport
    separately and an attacker could simply switch transports to reset
    them, defeating the entire point of a cross-connection budget.
    """
    started: list = []

    async def _start_one(name: str, server) -> None:
        try:
            await server.start()
        except Exception as exc:
            for already_started in reversed(started):
                await already_started.stop()
            # Wrapped into StartupError, not left as a raw OSError/etc.,
            # so main() has exactly one exception type to catch for a
            # clear, actionable message (issue #15: "fail clearly") —
            # an operator who fat-fingers a port number should see
            # "ssh listener failed to start: ... already in use", not a
            # multi-frame asyncssh/asyncio traceback.
            raise StartupError(f"{name} listener failed to start: {exc}") from exc
        started.append(server)

    if config.telnet.enabled:
        from netbbs.net.telnet import TelnetServer

        await _start_one(
            "telnet",
            TelnetServer(
                host=config.telnet.host, port=config.telnet.port, session_handler=session_handler
            ),
        )
        _logger.info("NetBBS listening on %s:%d (Telnet)", config.telnet.host, config.telnet.port)

    if config.ssh.enabled:
        try:
            from netbbs.net.ssh import SSHServer
        except ImportError:
            _logger.warning(
                "SSH is enabled in configuration but asyncssh is not installed — "
                "skipping SSH listener (pip install netbbs[ssh])"
            )
        else:
            await _start_one(
                "ssh",
                SSHServer(
                    host=config.ssh.host,
                    port=config.ssh.port,
                    db=db,
                    session_handler=session_handler,
                    throttle=throttle,
                    login_timeout=config.throttle.login_deadline_seconds,
                ),
            )
            _logger.info("NetBBS listening on %s:%d (SSH)", config.ssh.host, config.ssh.port)

    if config.web.enabled:
        try:
            from netbbs.net.web import WebServer
        except ImportError:
            _logger.warning(
                "web is enabled in configuration but aiohttp is not installed — "
                "skipping web listener (pip install netbbs[web])"
            )
        else:
            await _start_one(
                "web",
                WebServer(host=config.web.host, port=config.web.port, session_handler=session_handler),
            )
            _logger.info("NetBBS listening on %s:%d (web)", config.web.host, config.web.port)

    if not started:
        raise StartupError(
            "no listener actually started — every enabled transport either failed to "
            "bind or is missing its optional dependency; the node has nothing to serve"
        )

    return started


async def run(
    config: NodeConfig,
    *,
    shutdown_event: asyncio.Event | None = None,
    session_registry: ActiveSessionRegistry | None = None,
    maintenance: MaintenanceMode | None = None,
) -> None:
    """
    Run one node's lifetime: open the database, start every configured
    listener, and block until `shutdown_event` is set — then stop every
    listener and close the database, in that order, before returning.

    `shutdown_event`/`session_registry`/`maintenance` are all
    injectable specifically so this coordinated shutdown path is
    testable without sending real OS signals (see
    `tests/test_main_lifecycle.py`/`tests/test_shutdown.py`); the real
    top-level `main()` below constructs its own and wires actual
    SIGTERM/SIGINT handling to them (design doc round 51) — by the time
    `shutdown_event` is set, whatever triggered it (a real signal, or a
    test setting it directly) has already had its chance to warn/
    disconnect connected sessions first; this function itself doesn't
    need to know anything about that, only that everything is already
    gone by the time it proceeds to `server.stop()`/`db.close()`.
    """
    for warning in config.describe_insecure_bindings():
        _logger.warning(warning)

    db = Database(config.db_path)
    hub = ChatHub()
    presence = PresenceRegistry()
    mailbox = MessageMailbox()
    throttle = _build_throttle(config)
    throttle_config = config.throttle
    if session_registry is None:
        session_registry = ActiveSessionRegistry()
    if maintenance is None:
        maintenance = MaintenanceMode()
    # Constructed here, before session_handler is defined below, rather
    # than lazily right before `await shutdown_event.wait()` (as this
    # used to) -- a real connection could in principle reach
    # session_handler (and need a real shutdown_event to hand down to
    # handle_session, design doc -- node management round) before
    # reaching that later line, the same ordering hazard
    # session_registry/maintenance's own None-check already avoids by
    # being resolved up here.
    if shutdown_event is None:
        shutdown_event = asyncio.Event()

    async def session_handler(session):
        await handle_session(
            session, db, hub, presence, mailbox, throttle, throttle_config, session_registry, maintenance,
            shutdown_event=shutdown_event,
            graceful_delay_seconds=config.shutdown.graceful_delay_seconds,
        )

    servers: list = []
    try:
        if count_sysops(db) == 0:
            raise StartupError(
                "no SysOp-level account exists on this node -- run "
                "`python -m netbbs.admin` to create one before starting "
                "the network-facing server; a node with no SysOp could "
                "never be administered once it's running"
            )
        servers = await _start_servers(config, db, session_handler, throttle)

        await shutdown_event.wait()
    finally:
        for server in reversed(servers):
            await server.stop()
        db.close()
        _logger.info("NetBBS node shut down cleanly")


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    *,
    shutdown_event: asyncio.Event,
    session_registry: ActiveSessionRegistry,
    maintenance: MaintenanceMode,
    graceful_delay_seconds: float,
) -> None:
    def _request_shutdown(graceful: bool) -> None:
        _logger.info("shutdown requested (%s)", "graceful" if graceful else "immediate")
        loop.create_task(
            run_shutdown_sequence(
                graceful=graceful,
                session_registry=session_registry,
                maintenance=maintenance,
                graceful_delay_seconds=graceful_delay_seconds,
                shutdown_event=shutdown_event,
            )
        )

    # SIGTERM is the conventional "please shut down properly" signal
    # every process supervisor/rc.d script sends first, so it gets the
    # graceful (warn, wait, then disconnect) path. SIGINT -- Ctrl+C in
    # an attended terminal -- is immediate: warn, then disconnect right
    # away, no wait.
    for sig, graceful in ((signal.SIGTERM, True), (signal.SIGINT, False)):
        try:
            loop.add_signal_handler(sig, lambda graceful=graceful: _request_shutdown(graceful))
        except NotImplementedError:
            # add_signal_handler is Unix-only (see the stdlib asyncio
            # docs); the intended deployment target is NetBSD (see
            # CLAUDE.md), where this branch never runs. Windows dev
            # environments fall back to signal.signal, which can't
            # safely touch asyncio state directly from the handler —
            # call_soon_threadsafe hands the actual scheduling back to
            # the loop instead.
            signal.signal(
                sig,
                lambda *_, graceful=graceful: loop.call_soon_threadsafe(_request_shutdown, graceful),
            )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    try:
        config = load_config(sys.argv[1:])
    except ConfigError as exc:
        _logger.error("configuration error: %s", exc)
        raise SystemExit(1) from exc

    shutdown_event = asyncio.Event()
    session_registry = ActiveSessionRegistry()
    maintenance = MaintenanceMode()
    _install_signal_handlers(
        asyncio.get_running_loop(),
        shutdown_event=shutdown_event,
        session_registry=session_registry,
        maintenance=maintenance,
        graceful_delay_seconds=config.shutdown.graceful_delay_seconds,
    )

    try:
        await run(
            config,
            shutdown_event=shutdown_event,
            session_registry=session_registry,
            maintenance=maintenance,
        )
    except StartupError as exc:
        _logger.error("startup failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    asyncio.run(main())
