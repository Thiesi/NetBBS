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

from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.net.login_flow import handle_session
from netbbs.net.nodeconfig import ConfigError, NodeConfig, load_config
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


async def run(config: NodeConfig, *, shutdown_event: asyncio.Event | None = None) -> None:
    """
    Run one node's lifetime: open the database, start every configured
    listener, and block until `shutdown_event` is set — then stop every
    listener and close the database, in that order, before returning.

    `shutdown_event` is injectable specifically so this coordinated
    shutdown path is testable without sending real OS signals (see
    `tests/test_main_lifecycle.py`); the real top-level `main()` below
    wires actual SIGTERM/SIGINT handling to it.
    """
    for warning in config.describe_insecure_bindings():
        _logger.warning(warning)

    db = Database(config.db_path)
    hub = ChatHub()
    presence = PresenceRegistry()
    mailbox = MessageMailbox()
    throttle = _build_throttle(config)
    throttle_config = config.throttle

    async def session_handler(session):
        await handle_session(session, db, hub, presence, mailbox, throttle, throttle_config)

    servers: list = []
    try:
        servers = await _start_servers(config, db, session_handler, throttle)

        if shutdown_event is None:
            shutdown_event = asyncio.Event()
        await shutdown_event.wait()
    finally:
        for server in reversed(servers):
            await server.stop()
        db.close()
        _logger.info("NetBBS node shut down cleanly")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event) -> None:
    def _request_shutdown() -> None:
        _logger.info("shutdown requested")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            # add_signal_handler is Unix-only (see the stdlib asyncio
            # docs); the intended deployment target is NetBSD (see
            # CLAUDE.md), where this branch never runs. Windows dev
            # environments fall back to signal.signal, which can't
            # safely touch asyncio state directly from the handler —
            # call_soon_threadsafe hands the actual event-setting back
            # to the loop instead.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(_request_shutdown))


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    try:
        config = load_config(sys.argv[1:])
    except ConfigError as exc:
        _logger.error("configuration error: %s", exc)
        raise SystemExit(1) from exc

    shutdown_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), shutdown_event)

    try:
        await run(config, shutdown_event=shutdown_event)
    except StartupError as exc:
        _logger.error("startup failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    asyncio.run(main())
