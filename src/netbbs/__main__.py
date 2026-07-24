"""
`python -m netbbs [--config PATH] [options...]` — node entry point.

Configuration-driven (design doc, issue #15): what listens
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
from netbbs.backup import remove_pid_file, write_pid_file
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.files.storage import purge_incoming_staging
from netbbs.link.boards import LinkConfigSnapshot, LinkContext
from netbbs.link.diagnostics import LINK_LOGGER_NAME, LinkDiagnosticLogHandler
from netbbs.link.node_identity import NodeIdentityError, load_or_bootstrap_node_identity
from netbbs.link.protocol import HelloMessage, LinkNode
from netbbs.link.seedlist import run_scheduled_seed_refresh
from netbbs.link.store import load_link_node
from netbbs.net.daybreak import run_daybreak_announcer
from netbbs.net.login_flow import handle_session, handle_ssh_session
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.nodeconfig import ConfigError, LinkConfig, NodeConfig, load_config
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import run_shutdown_sequence
from netbbs.net.throttle import LinkRequestThrottle, LoginThrottle
from netbbs.selfupdate import run_scheduled_update_check
from netbbs.storage.database import Database, DatabaseIntegrityError
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import utc_now_iso

_logger = logging.getLogger(__name__)


class StartupError(Exception):
    """Raised for any startup failure `main()` should report as one
    clear, actionable message rather than a raw traceback: one or more
    enabled listeners failing to start (whatever did start is already
    stopped again by the time this is raised — see `run`'s partial-
    start cleanup), the database failing to open (wrong build/version
    paired with this database file, or a genuinely corrupt one), a
    missing/unloadable Link identity, or no SysOp account existing yet."""


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


def _build_link_throttle(link_config: LinkConfig) -> LinkRequestThrottle:
    """Design doc §13.9 (issue #60's third operational slice) -- same
    "build once, node-lifetime, threaded into the one real server"
    shape as `_build_throttle` above."""
    return LinkRequestThrottle(
        capacity=link_config.request_rate_capacity,
        refill_per_minute=link_config.request_rate_refill_per_minute,
        max_tracked_sources=link_config.request_rate_max_tracked_sources,
    )


def _build_own_hello_provider(link_node: LinkNode, link_config: LinkConfig):
    """
    Returns a plain callable producing this node's current `HelloMessage`
    on demand (design doc: `LinkServer`'s `own_hello_provider`
    — a transport-level concern deliberately kept out of `LinkNode`
    itself). `addresses` is only populated for a full peer
    (`not outgoing_only`) -- `config.validate()` already guarantees
    `advertised_host` is set whenever that's the case, so this can build
    the descriptor unconditionally rather than re-checking here.
    """

    def _provide() -> HelloMessage:
        addresses = None
        if not link_config.outgoing_only:
            advertised_port = (
                link_config.advertised_port if link_config.advertised_port is not None else link_config.port
            )
            addresses = [
                {"protocol": "http", "address": link_config.advertised_host, "port": advertised_port}
            ]
        return link_node.build_hello(
            addresses=addresses, outgoing_only=link_config.outgoing_only, created_at=utc_now_iso()
        )

    return _provide


async def _start_servers(
    config: NodeConfig,
    db: Database,
    session_handler,
    ssh_session_handler,
    throttle: LoginThrottle,
    link_node: LinkNode | None,
    link_lane: DatabaseLane,
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

    `ssh_session_handler`, distinct from `session_handler` (GitHub
    issue #25), wraps `netbbs.net.login_flow.handle_ssh_session` rather
    than `handle_session` -- SSH has already authenticated the
    connection through its own protocol-level handshake by the time
    either handler is ever called, so it must not be handed the
    Telnet/web handler, which unconditionally prompts for a
    username/password a second time.

    `link_node`, non-`None` exactly when `config.link.enabled` (`run()`
    constructs it, not this function -- the
    background sync task needs to share this *same* `LinkNode`
    instance, not a second one with its own independent, diverging
    peer table), is what `LinkServer` answers inbound traffic through.
    `link_lane` is the background `DatabaseLane` `LinkServer`
    persists accepted peers/events through.
    """
    started: list = []
    any_interactive_started = False

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
        any_interactive_started = True
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
                    session_handler=ssh_session_handler,
                    throttle=throttle,
                    login_timeout=config.throttle.login_deadline_seconds,
                ),
            )
            any_interactive_started = True
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
            any_interactive_started = True
            _logger.info("NetBBS listening on %s:%d (web)", config.web.host, config.web.port)

    if config.link.enabled:
        try:
            from netbbs.link.transport import LinkServer
        except ImportError:
            _logger.warning(
                "Link is enabled in configuration but aiohttp is not installed — "
                "skipping Link listener (pip install netbbs[web])"
            )
        else:
            assert link_node is not None  # run() constructs it exactly when config.link.enabled
            await _start_one(
                "link",
                LinkServer(
                    host=config.link.host,
                    port=config.link.port,
                    node=link_node,
                    own_hello_provider=_build_own_hello_provider(link_node, config.link),
                    lane=link_lane,
                    relay_serving_enabled=config.link.relay_serving_enabled,
                    max_relay_clients=config.link.max_relay_clients,
                    max_peers=config.link.max_peers,
                    max_carried_boards=config.link.max_carried_boards,
                    max_carried_channels=config.link.max_carried_channels,
                    max_carried_file_areas=config.link.max_carried_file_areas,
                    max_remote_files_per_area=config.link.max_remote_files_per_area,
                    max_concurrent_file_transfers_per_peer=config.link.max_concurrent_file_transfers_per_peer,
                    throttle=_build_link_throttle(config.link),
                ),
            )
            # Deliberately not counted toward any_interactive_started
            # below -- Link is a machine-to-machine peer listener, not
            # something a user connects to, so it must never be able to
            # satisfy "the node has nothing to serve" on its own with
            # every actual interactive transport having failed to bind.
            _logger.info(
                "NetBBS Link listening on %s:%d (fingerprint %s, %s)",
                config.link.host, config.link.port, link_node.identity.fingerprint,
                "outgoing-only" if config.link.outgoing_only else "full peer",
            )

    if not any_interactive_started:
        # A non-interactive listener (Link) may have started successfully
        # even though no *interactive* one did -- must still be stopped
        # here before raising, or it leaks a bound port. _start_one's own
        # cleanup only runs when a start() call itself fails; this check
        # fires after every attempt has already finished, so it needs the
        # same cleanup applied explicitly.
        for already_started in reversed(started):
            await already_started.stop()
        raise StartupError(
            "no interactive listener actually started — every enabled transport "
            "either failed to bind or is missing its optional dependency; the node "
            "has nothing to serve"
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
    SIGTERM/SIGINT handling to them (design doc) — by the time
    `shutdown_event` is set, whatever triggered it (a real signal, or a
    test setting it directly) has already had its chance to warn/
    disconnect connected sessions first; this function itself doesn't
    need to know anything about that, only that everything is already
    gone by the time it proceeds to `server.stop()`/`db.close()`.
    """
    for warning in config.describe_insecure_bindings():
        _logger.warning(warning)

    try:
        db = Database(config.db_path)
    except Exception as exc:
        # Wrapped into StartupError, not left as a raw sqlite3.Error/
        # RuntimeError, so main() has exactly one exception type to
        # catch for a clear, actionable message -- the concrete failure
        # this closes: opening a database file against the wrong build
        # (e.g. a database a newer version already migrated, opened by
        # an older one -- Database._apply_migrations' own version check
        # raises a plain RuntimeError for that specific case, but a
        # corrupted or genuinely foreign file raises a raw sqlite3.Error
        # instead) used to surface as a multi-frame traceback rather
        # than a message actually pointing at the mismatch.
        raise StartupError(
            f"could not open the database at {config.db_path}: {exc} -- this usually means "
            "the database file doesn't match this build of NetBBS (e.g. it was last migrated "
            "by a newer or older version). If you're testing multiple NetBBS versions side by "
            "side, make sure each one is paired with its own separate database file."
        ) from exc

    # Design doc §13.11, issue #60: a corrupted database file otherwise
    # surfaces only later, the first time some unlucky query touches the
    # damaged page, as a confusing raw error rather than a clear
    # diagnosis at the one point an operator can still act on it. Run
    # once, here, not inside Database.__init__ (see check_integrity's
    # own docstring for why every other caller -- admin scripts, the
    # whole test suite -- must not pay for a full scan they don't need).
    try:
        db.check_integrity()
    except DatabaseIntegrityError as exc:
        db.close()
        raise StartupError(
            f"{exc} -- restore from a known-good backup (see docs/NetBBS-disaster-recovery-"
            "drill.md) rather than starting against a corrupted database."
        ) from exc

    # Design doc/issue #57: the foreground DatabaseLane -- a
    # second, independent connection to the same database file (WAL
    # mode makes this safe), off the event loop, that migrated features
    # dispatch through instead of calling business logic directly via
    # `db`. Only `netbbs.net.mail_flow` (the first module migrated,
    # proof-of-pattern) actually uses it yet -- every other feature
    # still runs on `db` directly, unmigrated, per the design doc.
    # Opened lazily on first use, not here, so node startup doesn't pay
    # for a connection nothing may touch this run.
    foreground_lane = DatabaseLane(config.db_path)

    # Design doc: the background DatabaseLane named for
    # "peer inventory exchange, event
    # verification/ingestion, retry/outbox processing." First real use:
    # netbbs.link.transport persists accepted Link peers/events through
    # it, off the event loop, independent of foreground_lane's own
    # session-driven work.
    background_lane = DatabaseLane(config.db_path)

    # GitHub issue #34, reopened a third time: any file left under
    # .incoming staging is guaranteed stale at this exact point --
    # nothing has had a chance to start a legitimate upload yet, so
    # anything already there survived from a previous run that was
    # killed, crashed, or lost power mid-transfer, skipping
    # receive_file's own exception-based cleanup entirely. Must run
    # before _start_servers below, not after -- once listeners are up,
    # a genuinely in-progress upload's temp file would no longer be
    # safely distinguishable from an abandoned one.
    purged = purge_incoming_staging(db)
    if purged:
        _logger.info("removed %d stale upload staging file(s) from a previous run", purged)

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
    # handle_session, per the design doc's node management section) before
    # reaching that later line, the same ordering hazard
    # session_registry/maintenance's own None-check already avoids by
    # being resolved up here.
    if shutdown_event is None:
        shutdown_event = asyncio.Event()

    # Node-lifetime background task (design doc), not tied to
    # any one session -- see netbbs.net.daybreak's own module docstring
    # for what it does and why it's strictly local-only. Started here,
    # alongside hub/presence/mailbox/throttle, and cancelled in this
    # function's own `finally` below the same way every listener is
    # already stopped there.
    #
    # GitHub issue #48: an unexpected failure in this task (a database
    # write, timezone lookup, or future broadcast change raising) must
    # not go unnoticed, and must not be able to block the `finally`
    # block's listener/database cleanup below. The done-callback logs
    # the failure the moment it happens -- this is a purely cosmetic,
    # local-only chat announcement (design doc), not
    # something worth taking the whole node's listeners down for, so
    # the chosen policy is graceful degrade (log and keep serving,
    # feature silently retired for the rest of this node's uptime)
    # rather than fail-fast or an auto-restarting supervisor -- the
    # latter would need its own backoff/give-up policy for a repeatedly
    # crashing announcer, complexity this ancillary a feature doesn't
    # warrant.
    daybreak_task = asyncio.create_task(run_daybreak_announcer(db, hub))

    def _log_daybreak_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _logger.error(
                "daybreak announcer task failed -- the midnight chat "
                "announcement will not run again this node uptime "
                "(other functionality is unaffected)",
                exc_info=exc,
            )

    daybreak_task.add_done_callback(_log_daybreak_failure)

    # Design doc §17: the "startup" and "daily-background"
    # halves of the check-for-updates off switch admin_flow's own
    # screen already names and gates (`get_auto_update_check_enabled`)
    # -- previously nothing actually performed a scheduled check despite
    # that switch's own UI copy ("Daily automatic check: ON/off")
    # implying one existed. Runs regardless of Link configuration, same
    # as the daybreak announcer -- this is general node maintenance, not
    # a Link-specific concern.
    update_check_task = asyncio.create_task(run_scheduled_update_check(db))

    def _log_update_check_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _logger.error(
                "scheduled update-check task failed -- automatic release "
                "checks will not run again this node uptime (the manual "
                "admin-menu check is unaffected)",
                exc_info=exc,
            )

    update_check_task.add_done_callback(_log_update_check_failure)

    async def session_handler(session):
        await handle_session(
            session, db, hub, presence, mailbox, throttle, throttle_config, session_registry, maintenance,
            shutdown_event=shutdown_event,
            graceful_delay_seconds=config.shutdown.graceful_delay_seconds,
            lane=foreground_lane,
            # Design doc: None whenever this node has Link
            # disabled (Phase 3 is opt-in/experimental) --
            # `link_node` is only ever non-None in that same condition
            # (see its own construction below), so this mirrors it
            # directly rather than re-checking config.link.enabled here.
            link_context=(
                LinkContext(
                    node_identity=node_identity, link_node=link_node, link_config=link_config_snapshot
                ) if link_node is not None else None
            ),
        )

    async def ssh_session_handler(session):
        # GitHub issue #25: SSH has already authenticated the
        # connection through its own protocol-level handshake by this
        # point (see netbbs.net.ssh._NetBBSSSHServer) -- this skips
        # handle_session's interactive username/password prompt
        # entirely rather than asking again.
        await handle_ssh_session(
            session, db, hub, presence, mailbox, session_registry, maintenance,
            shutdown_event=shutdown_event,
            graceful_delay_seconds=config.shutdown.graceful_delay_seconds,
            lane=foreground_lane,
            link_context=(
                LinkContext(
                    node_identity=node_identity, link_node=link_node, link_config=link_config_snapshot
                ) if link_node is not None else None
            ),
        )

    servers: list = []
    link_sync_task: asyncio.Task | None = None
    link_sync_session = None
    # Design doc §13.11, issue #60: the graceful-drain half of run_link_
    # sync's own cooperative stop_event parameter -- created unconditionally
    # (harmless to .set() even when link_sync_task never ends up created)
    # so the shutdown finally block below never needs an extra None check
    # beyond the ones it already has for link_sync_task itself.
    link_sync_stop_event = asyncio.Event()
    seed_refresh_task: asyncio.Task | None = None
    link_diagnostic_log_handler: LinkDiagnosticLogHandler | None = None
    try:
        # Design doc §13.10, issue #75: this node's own PID, so a later
        # `netbbs.backup restore` can reliably refuse against an idle-
        # but-running node (the write-lock probe it also uses only ever
        # catches a transaction genuinely in flight, not a process
        # sitting idle between them). Written inside this try/finally,
        # not before it, so it's guaranteed removed by the matching
        # remove_pid_file() below on every exit path -- same reasoning
        # the node identity load just below already documents for
        # itself.
        write_pid_file(config.db_path)

        # Design doc: a node's Link identity (root key +
        # signing/transport operational keys) auto-generates silently on
        # first-ever startup and just loads on every one after that -- no
        # separate "init" step. Always loaded regardless of whether
        # config.link.enabled -- it must exist and be verified sound
        # before anything that signs with it does. Checked inside this
        # try/finally, not before it, so a failure here still goes
        # through the same db.close()/daybreak_task cleanup as every
        # other startup failure below.
        try:
            node_identity = load_or_bootstrap_node_identity(config.identity_dir, label=config.node_name)
        except NodeIdentityError as exc:
            raise StartupError(f"could not load or bootstrap this node's Link identity: {exc}") from exc
        _logger.info("node Link identity %r: fingerprint %s", config.node_name, node_identity.fingerprint)

        # Constructed here, once, rather than
        # inside _start_servers -- the background sync task below needs
        # to share this *same* LinkNode instance (its peer table, its
        # tracked transitions-per-peer), not a second one that would
        # silently diverge from what LinkServer sees.
        #
        # Hydrated from persisted storage via load_link_node,
        # not a bare LinkNode(...) construction -- so a restarted node
        # doesn't forget its peers or reprocess/re-forward events it has
        # already seen. Reads `db` directly (not background_lane): this
        # is a one-time synchronous read before the event loop is
        # serving any traffic or lane jobs exist to dispatch onto, the
        # same reasoning node_identity/count_sysops(db) already read
        # synchronously at this point in startup.
        link_node = load_link_node(db, node_identity) if config.link.enabled else None

        # Design doc §13.11, issue #60: attached once, here, only when
        # Link is actually enabled -- a disabled node has no netbbs.link
        # activity to ever log a warning about in the first place.
        # Removed in the matching finally block below via handler.close()
        # (also closes its own independent sqlite3 connection -- see that
        # class's own docstring for why it doesn't share db.connection).
        if config.link.enabled:
            link_diagnostic_log_handler = LinkDiagnosticLogHandler(
                config.db_path,
                max_age_days=config.link.diagnostic_log_max_age_days,
                max_rows=config.link.diagnostic_log_max_rows,
            )
            logging.getLogger(LINK_LOGGER_NAME).addHandler(link_diagnostic_log_handler)

        # Issue #60's SysOp Link-status screen needs a handful of
        # netbbs.net.nodeconfig.LinkConfig fields for display -- built
        # once here (config never changes at runtime), not per-session,
        # and as a primitives-only snapshot rather than the LinkConfig
        # object itself, so netbbs.link never has to import netbbs.net
        # (see LinkConfigSnapshot's own docstring).
        link_config_snapshot = (
            LinkConfigSnapshot(
                outgoing_only=config.link.outgoing_only,
                advertised_host=config.link.advertised_host,
                advertised_port=config.link.advertised_port,
                seeds=tuple(config.link.seeds),
                sync_interval_seconds=config.link.sync_interval_seconds,
                relay_serving_enabled=config.link.relay_serving_enabled,
                max_relay_clients=config.link.max_relay_clients,
                max_peers=config.link.max_peers,
                max_carried_boards=config.link.max_carried_boards,
                max_carried_channels=config.link.max_carried_channels,
            )
            if link_node is not None else None
        )

        if count_sysops(db) == 0:
            raise StartupError(
                "no SysOp-level account exists on this node -- run "
                "`python -m netbbs.admin` to create one before starting "
                "the network-facing server; a node with no SysOp could "
                "never be administered once it's running"
            )
        servers = await _start_servers(
            config, db, session_handler, ssh_session_handler, throttle, link_node, background_lane
        )

        # Design doc: the piece that makes this node
        # *originate* outbound Link activity, not just answer it. A
        # separate try/except ImportError from LinkServer's own,
        # since this runs here in run(), not inside _start_servers,
        # after _start_servers may have already decided (independently)
        # whether aiohttp was available for the inbound listener.
        #
        # Not gated on `config.link.seeds` being
        # non-empty -- a node started with zero operator-configured
        # seeds still starts this task, since `run_link_sync` itself
        # also merges in whatever `run_scheduled_seed_refresh` (below)
        # most recently cached, and a genuinely empty combined list is
        # already a harmless no-op pass (nothing to dial, nothing
        # crashes). This is precisely the "brand-new node with no
        # learned peers yet" case the live
        # seed-list refresh matters most for -- gating the task itself
        # on operator-configured seeds would have made a live-fetched
        # seed unreachable no matter how successfully it was fetched.
        if config.link.enabled and link_node is not None:
            try:
                import aiohttp

                from netbbs.link.sync import run_link_sync
            except ImportError:
                _logger.warning(
                    "Link is enabled but aiohttp is not installed — "
                    "skipping the Link sync task (pip install netbbs[web])"
                )
            else:
                link_sync_session = aiohttp.ClientSession()
                link_sync_task = asyncio.create_task(
                    run_link_sync(
                        link_node, link_sync_session, config.link.seeds,
                        _build_own_hello_provider(link_node, config.link),
                        background_lane,
                        interval_seconds=config.link.sync_interval_seconds,
                        stop_event=link_sync_stop_event,
                        max_carried_boards=config.link.max_carried_boards,
                        max_carried_channels=config.link.max_carried_channels,
                    )
                )

                def _log_link_sync_failure(task: asyncio.Task) -> None:
                    if task.cancelled():
                        return
                    exc = task.exception()
                    if exc is not None:
                        _logger.error(
                            "Link sync task failed -- outbound Link activity will not "
                            "resume this node uptime (inbound Link, if enabled, is "
                            "unaffected)",
                            exc_info=exc,
                        )

                link_sync_task.add_done_callback(_log_link_sync_failure)
                _logger.info(
                    "NetBBS Link sync started: %d operator-configured seed(s), every %.0fs",
                    len(config.link.seeds), config.link.sync_interval_seconds,
                )

                # Design doc: refreshes the supplementary seed
                # list `run_link_sync` above merges in every pass. Its
                # own off switch is `get_auto_update_check_enabled` --
                # deliberately the same flag as the release-check task,
                # not a second Link-specific toggle for a now explicitly
                # coupled feature (see that function's own docstring).
                seed_refresh_task = asyncio.create_task(run_scheduled_seed_refresh(db))

                def _log_seed_refresh_failure(task: asyncio.Task) -> None:
                    if task.cancelled():
                        return
                    exc = task.exception()
                    if exc is not None:
                        _logger.error(
                            "Seed-list refresh task failed -- the supplementary seed "
                            "list will no longer update this node uptime (operator-"
                            "configured seeds are unaffected)",
                            exc_info=exc,
                        )

                seed_refresh_task.add_done_callback(_log_seed_refresh_failure)

        await shutdown_event.wait()
    finally:
        # GitHub issue #48: cancelling an already-failed task is a
        # no-op, and awaiting it re-raises the original (non-
        # cancellation) exception -- which must not be allowed to skip
        # the listener/database cleanup below. That failure was already
        # logged by `_log_daybreak_failure` above the moment it
        # happened, so it's safe to swallow it here.
        daybreak_task.cancel()
        try:
            await daybreak_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        # Same cancel-await-swallow shape as
        # daybreak_task, for the same reason (issue #48) -- already
        # logged by _log_update_check_failure if it failed on its own.
        update_check_task.cancel()
        try:
            await update_check_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        # Design doc §13.11, issue #60: graceful drain, not an
        # unconditional hard cancel -- an in-flight dial/push otherwise
        # gets torn out of half-done at whatever await happens to be
        # outstanding the instant shutdown fires, unlike every other
        # task drained below (none of which talk to a Link peer, see
        # run_link_sync's own stop_event docstring for why only this
        # one gets this treatment). Setting the event lets run_link_
        # sync finish its *current* pass normally and exit its own loop
        # before the next one starts; bounded by the same graceful_
        # delay_seconds budget user-session shutdown already uses, so a
        # wedged dial can't hang shutdown indefinitely -- past that
        # bound, fall back to the same cancel-await-swallow shape as
        # daybreak_task above (already logged by _log_link_sync_failure
        # if it failed on its own, so safe to swallow here too).
        if link_sync_task is not None:
            link_sync_stop_event.set()
            try:
                await asyncio.wait_for(link_sync_task, timeout=config.shutdown.graceful_delay_seconds)
            except asyncio.TimeoutError:
                # wait_for already cancelled link_sync_task and awaited
                # that cancellation through to completion before raising
                # this -- nothing further to clean up, same end state as
                # every other task's cancel-await-swallow shape here.
                pass
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if link_sync_session is not None:
            await link_sync_session.close()
        # Same cancel-await-swallow shape, for the
        # same reason -- already logged by _log_seed_refresh_failure if
        # it failed on its own.
        if seed_refresh_task is not None:
            seed_refresh_task.cancel()
            try:
                await seed_refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        for server in reversed(servers):
            await server.stop()
        foreground_lane.close()
        background_lane.close()
        db.close()
        if link_diagnostic_log_handler is not None:
            logging.getLogger(LINK_LOGGER_NAME).removeHandler(link_diagnostic_log_handler)
            link_diagnostic_log_handler.close()
        remove_pid_file(config.db_path)
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
