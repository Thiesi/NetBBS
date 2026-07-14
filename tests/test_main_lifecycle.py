"""
Tests for netbbs.__main__'s configuration-driven startup and graceful
shutdown (design doc round 28, issue #15).

`run()` is deliberately structured to be testable without real OS
signals or a real subprocess: it takes an injectable `shutdown_event`
and returns once that's set, after stopping every listener and closing
the database -- see its docstring. These tests exercise that directly,
plus real socket connections to confirm configured listeners are
actually reachable and that a partial-start failure doesn't leave a
listener behind.
"""

from __future__ import annotations

import asyncio
import signal
import sqlite3
import sys

import pytest

from netbbs.__main__ import StartupError, _install_signal_handlers, run
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.nodeconfig import NodeConfig, ShutdownConfig, TransportConfig
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.storage.database import Database


def _config(tmp_path, *, seed_sysop: bool = True, **overrides) -> NodeConfig:
    """
    `seed_sysop=True` by default: `run()` refuses to start at all with
    zero SysOp-level accounts (design doc -- SysOp foundation round),
    and every test in this file except the one that specifically
    exercises that refusal wants a normally-startable node -- seeding
    here once, centrally, avoids repeating a create_user call at every
    call site.
    """
    defaults = dict(
        db_path=tmp_path / "node.db",
        telnet=TransportConfig(True, "127.0.0.1", 0),
        ssh=TransportConfig(False, "127.0.0.1", 0),
        web=TransportConfig(False, "127.0.0.1", 0),
    )
    defaults.update(overrides)
    config = NodeConfig(**defaults)
    if seed_sysop:
        db = Database(config.db_path)
        create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
        db.close()
    return config


async def _run_until_ready_then_shut_down(config, ready_delay=0.1):
    """Runs `run()` in the background, gives listeners a moment to bind,
    then signals shutdown and waits for run() to return."""
    shutdown_event = asyncio.Event()
    task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
    await asyncio.sleep(ready_delay)
    shutdown_event.set()
    await task


async def _open_connection_when_ready(host: str, port: int, *, timeout: float = 5.0):
    """
    Repeatedly attempts a connection instead of assuming a fixed delay is
    enough to have started it.

    `run()` opens the database and applies every migration synchronously,
    with no `await` in between (see `Database.__init__`/
    `_apply_migrations`), before a listener ever gets a chance to bind --
    real disk I/O for that can legitimately take longer than a guessed
    sleep on some hardware. A fixed `asyncio.sleep(0.1)`-then-connect here
    previously passed reliably on the Windows dev sandbox but failed with
    `ConnectionRefusedError` on real NetBSD hardware. Treating a refused
    connection as "not listening yet, try again" rather than picking a
    bigger fixed delay avoids just moving the same race to whatever
    machine is slower next.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            return await asyncio.open_connection(host, port)
        except (ConnectionRefusedError, OSError):
            if asyncio.get_event_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.02)


# -- listeners actually start and are reachable ------------------------------


def test_configured_telnet_listener_on_known_port_accepts_connections(tmp_path):
    async def scenario():
        config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12399))
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        try:
            reader, writer = await _open_connection_when_ready("127.0.0.1", 12399)
            data = await reader.readexactly(1)  # first byte of Telnet negotiation
            assert data == b"\xff"  # IAC
            writer.close()
            await writer.wait_closed()
        finally:
            shutdown_event.set()
            await task

    asyncio.run(scenario())


def test_shutdown_event_and_graceful_delay_reach_handle_session(tmp_path, monkeypatch):
    """
    Confirms `run()`'s `session_handler` closure actually threads its
    real `shutdown_event`/`config.shutdown.graceful_delay_seconds` all
    the way into `handle_session` (design doc -- node management round)
    -- not just that `handle_session`'s own signature accepts them.

    Monkeypatches `_run_authenticated_session` to a spy that captures
    the `node_controls` it receives and returns immediately, rather
    than driving a real login over the socket -- `handle_session`
    itself (the part actually under test here) already constructs
    `node_controls` before calling that function.
    """
    from netbbs.net import login_flow

    captured: dict = {}

    async def spy(session, db, hub, presence, mailbox, throttle, throttle_config, *, node_controls=None):
        captured["node_controls"] = node_controls

    monkeypatch.setattr(login_flow, "_run_authenticated_session", spy)

    async def scenario():
        config = _config(
            tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12391),
            shutdown=ShutdownConfig(graceful_delay_seconds=42.0),
        )
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        try:
            reader, writer = await _open_connection_when_ready("127.0.0.1", 12391)
            await reader.readexactly(9)  # initial Telnet negotiation triplets

            deadline = asyncio.get_event_loop().time() + 2.0
            while "node_controls" not in captured:
                if asyncio.get_event_loop().time() >= deadline:
                    raise AssertionError("handle_session's spy was never reached")
                await asyncio.sleep(0.01)

            node_controls = captured["node_controls"]
            assert node_controls is not None
            assert node_controls.shutdown_event is shutdown_event
            assert node_controls.graceful_delay_seconds == 42.0

            writer.close()
            await writer.wait_closed()
        finally:
            shutdown_event.set()
            await task

    asyncio.run(scenario())


# -- graceful shutdown --------------------------------------------------------


def test_shutdown_stops_listeners_and_closes_database(tmp_path):
    async def scenario():
        config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12398))
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))

        # Confirm it's actually up before shutting down, so a false
        # "shutdown worked" isn't just "it was never listening at all".
        reader, writer = await _open_connection_when_ready("127.0.0.1", 12398)
        writer.close()
        await writer.wait_closed()

        shutdown_event.set()
        await asyncio.wait_for(task, timeout=5.0)

        # Listener is really gone -- a fresh connection attempt fails.
        with pytest.raises((ConnectionRefusedError, OSError)):
            await asyncio.open_connection("127.0.0.1", 12398)

    asyncio.run(scenario())


def test_shutdown_closes_the_database(tmp_path):
    captured_db_path = tmp_path / "node.db"

    async def scenario():
        config = _config(tmp_path, db_path=captured_db_path)
        await _run_until_ready_then_shut_down(config)

    asyncio.run(scenario())

    # The file exists (Database.__init__ created it) and is closed
    # cleanly enough that a fresh, unrelated connection can open it
    # immediately afterward with no lingering lock (WAL mode would
    # leave -wal/-shm files behind if uncleanly closed mid-write, but
    # a plain open+immediate-query is enough to prove nothing is
    # holding an exclusive lock from run()'s own connection).
    conn = sqlite3.connect(str(captured_db_path))
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


# -- zero-SysOp startup refusal (design doc -- SysOp foundation round) ------


def test_zero_sysops_raises_startup_error(tmp_path):
    """A node with no SysOp-level account could never be administered
    once it's running -- run() must refuse to start at all, before any
    listener binds, rather than starting degraded."""

    async def scenario():
        config = _config(tmp_path, seed_sysop=False)
        with pytest.raises(StartupError, match="no SysOp-level account"):
            await run(config)

    asyncio.run(scenario())


def test_zero_sysops_still_closes_the_database(tmp_path):
    captured_db_path = tmp_path / "node.db"

    async def scenario():
        config = _config(tmp_path, db_path=captured_db_path, seed_sysop=False)
        with pytest.raises(StartupError, match="no SysOp-level account"):
            await run(config)

    asyncio.run(scenario())

    conn = sqlite3.connect(str(captured_db_path))
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


def test_a_disabled_sysop_does_not_count_and_still_raises_startup_error(tmp_path):
    config = _config(tmp_path, seed_sysop=False)
    db = Database(config.db_path)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    # Raw SQL, not set_user_disabled: disabling the sole active SysOp
    # through the normal API is exactly what that function's own
    # lockout guard refuses -- this test needs the resulting state
    # (a disabled SysOp, zero *active* ones) to exist regardless of how
    # it came about, to confirm run()'s own check treats it correctly.
    db.connection.execute("UPDATE users SET disabled_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", sysop.id))
    db.connection.commit()
    db.close()

    async def scenario():
        with pytest.raises(StartupError, match="no SysOp-level account"):
            await run(config)

    asyncio.run(scenario())


# -- partial-start failure cleanup --------------------------------------------


def test_partial_start_failure_stops_already_started_listeners(tmp_path):
    """Telnet and web both enabled; web's port is already occupied by
    another socket, so its start() fails. Telnet, which starts first,
    must be stopped again rather than left running."""

    async def scenario():
        # Occupy a port so the web listener's bind fails.
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 12397)
        try:
            config = _config(
                tmp_path,
                telnet=TransportConfig(True, "127.0.0.1", 12396),
                web=TransportConfig(True, "127.0.0.1", 12397),
            )
            with pytest.raises(StartupError, match="web listener failed to start"):
                await run(config)

            # Telnet must have been stopped again, not left running.
            with pytest.raises((ConnectionRefusedError, OSError)):
                await asyncio.open_connection("127.0.0.1", 12396)
        finally:
            blocker.close()
            await blocker.wait_closed()

    asyncio.run(scenario())


def test_no_listener_started_raises_startup_error(tmp_path, monkeypatch):
    """Every enabled transport unavailable (here: SSH configured but
    asyncssh 'not installed') must fail clearly, not silently run with
    nothing listening."""
    monkeypatch.setitem(sys.modules, "netbbs.net.ssh", None)

    async def scenario():
        config = _config(
            tmp_path,
            telnet=TransportConfig(False, "127.0.0.1", 0),
            ssh=TransportConfig(True, "127.0.0.1", 0),
            web=TransportConfig(False, "127.0.0.1", 0),
        )
        with pytest.raises(StartupError, match="no listener actually started"):
            await run(config)

    asyncio.run(scenario())


def test_startup_failure_still_closes_the_database(tmp_path):
    captured_db_path = tmp_path / "node.db"

    async def scenario():
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 12395)
        try:
            config = _config(
                tmp_path, db_path=captured_db_path, telnet=TransportConfig(True, "127.0.0.1", 12395)
            )
            with pytest.raises(StartupError, match="telnet listener failed to start"):
                await run(config)
        finally:
            blocker.close()
            await blocker.wait_closed()

    asyncio.run(scenario())

    conn = sqlite3.connect(str(captured_db_path))
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


# -- signal handling -----------------------------------------------------


def test_signal_handler_registration_triggers_shutdown_event():
    """
    Sends a real signal via `signal.raise_signal` -- a genuine C-level
    `raise()`, not a manual Python-level invocation -- and confirms
    `shutdown_event` ends up set, regardless of which branch
    `_install_signal_handlers` actually took to get there.

    That "regardless of which branch" is the point, and the reason this
    version replaced an earlier one: on POSIX, `_install_signal_handlers`
    uses `loop.add_signal_handler`, which installs asyncio's own
    low-level no-op C handler and dispatches the real callback later via
    an internal self-pipe -- `signal.getsignal(SIGTERM)` in that case
    returns *asyncio's* placeholder, not this module's `_request_shutdown`,
    so manually invoking whatever `getsignal` returns (the previous
    version of this test) silently tested the wrong thing and always
    passed vacuously on Windows (where the `signal.signal` fallback
    really is installed directly) while doing nothing useful on real
    POSIX targets -- caught by an actual NetBSD pytest run, not by
    reasoning about it, which is exactly the "actually run it" lesson
    CLAUDE.md already calls out elsewhere in this project's history.

    `signal.raise_signal` was chosen over `os.kill(os.getpid(), sig)`
    specifically because `os.kill` with a real (non-zero) pid on Windows
    calls `TerminateProcess` instead of delivering a real, Python-
    handleable signal (confirmed by hand in this project's Windows dev
    sandbox) -- it would kill the test process outright rather than
    exercise the fallback branch. `raise_signal` goes through the actual
    C-level signal-raising mechanism on both platforms, so it correctly
    reaches whichever dispatch mechanism is actually installed.
    """
    shutdown_event = asyncio.Event()

    async def scenario():
        loop = asyncio.get_running_loop()
        _install_signal_handlers(
            loop,
            shutdown_event=shutdown_event,
            session_registry=ActiveSessionRegistry(),
            maintenance=MaintenanceMode(),
            # A tiny delay, not 0 -- confirms the graceful/SIGTERM path's
            # `asyncio.sleep` actually runs (not skipped entirely) without
            # this test waiting anywhere near the real default.
            graceful_delay_seconds=0.01,
        )
        signal.raise_signal(signal.SIGTERM)
        await asyncio.wait_for(shutdown_event.wait(), timeout=2.0)

    asyncio.run(scenario())
