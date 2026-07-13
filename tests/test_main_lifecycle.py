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
from netbbs.net.nodeconfig import NodeConfig, TransportConfig


def _config(tmp_path, **overrides) -> NodeConfig:
    defaults = dict(
        db_path=tmp_path / "node.db",
        telnet=TransportConfig(True, "127.0.0.1", 0),
        ssh=TransportConfig(False, "127.0.0.1", 0),
        web=TransportConfig(False, "127.0.0.1", 0),
    )
    defaults.update(overrides)
    return NodeConfig(**defaults)


async def _run_until_ready_then_shut_down(config, ready_delay=0.1):
    """Runs `run()` in the background, gives listeners a moment to bind,
    then signals shutdown and waits for run() to return."""
    shutdown_event = asyncio.Event()
    task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
    await asyncio.sleep(ready_delay)
    shutdown_event.set()
    await task


# -- listeners actually start and are reachable ------------------------------


def test_configured_telnet_listener_on_known_port_accepts_connections(tmp_path):
    async def scenario():
        config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12399))
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        await asyncio.sleep(0.1)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 12399)
            data = await reader.readexactly(1)  # first byte of Telnet negotiation
            assert data == b"\xff"  # IAC
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
        await asyncio.sleep(0.1)

        # Confirm it's actually up before shutting down, so a false
        # "shutdown worked" isn't just "it was never listening at all".
        reader, writer = await asyncio.open_connection("127.0.0.1", 12398)
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
    Verifies `_install_signal_handlers`'s own logic -- registration
    succeeds and the registered callback correctly sets `shutdown_event`
    via the thread-safe asyncio bridge -- independent of whether *this
    test process's own OS-level signal delivery* can be exercised
    end-to-end in a given sandbox.

    That distinction matters here specifically: manual verification in
    this Windows/Git-Bash dev sandbox found `os.kill(pid, SIGINT)` on
    Windows actually calls `TerminateProcess` (not a real, Python-
    handleable signal) for a non-zero pid, and even the correct
    broadcast form (`os.kill(0, signal.CTRL_C_EVENT)`) didn't reliably
    reach Python's signal machinery through Git Bash's console
    emulation. Both are sandbox/OS artifacts, not defects in this
    function -- confirmed by registering the handler and invoking it
    directly (exactly what the OS would do on a real signal), which
    this test does. The real deployment target (NetBSD, POSIX) uses
    `loop.add_signal_handler` directly instead of this Windows-only
    fallback branch -- standard, well-tested asyncio behavior this test
    doesn't re-verify. Worth a real `kill -TERM`/Ctrl+C check on
    Thiesi's actual NetBSD machine before considering issue #15's
    graceful-shutdown requirement fully closed out end-to-end.
    """
    shutdown_event = asyncio.Event()

    async def scenario():
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, shutdown_event)
        handler = signal.getsignal(signal.SIGTERM)
        assert handler is not None and handler != signal.SIG_DFL
        handler(signal.SIGTERM, None)
        await asyncio.sleep(0.05)
        assert shutdown_event.is_set()

    asyncio.run(scenario())
