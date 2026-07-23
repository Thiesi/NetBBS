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
import logging
import os
import signal
import sqlite3
import sys

import pytest

from netbbs.__main__ import StartupError, _install_signal_handlers, run
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.link.transport import dial_hello
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.nodeconfig import LinkConfig, NodeConfig, ShutdownConfig, TransportConfig
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


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
        identity_dir=tmp_path / "netbbs_identity",
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


# -- node Link identity bootstrap (design doc round 89/111) ------------------


def test_run_bootstraps_node_identity_on_first_startup(tmp_path):
    config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12401))
    assert not config.identity_dir.exists()

    asyncio.run(_run_until_ready_then_shut_down(config))

    assert (config.identity_dir / "root.identity").exists()
    assert (config.identity_dir / "signing.identity").exists()
    assert (config.identity_dir / "transport.identity").exists()
    assert (config.identity_dir / "transitions.json").exists()


def test_run_reuses_existing_node_identity_on_second_startup(tmp_path):
    from netbbs.link.node_identity import NodeIdentity

    config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12402))
    asyncio.run(_run_until_ready_then_shut_down(config))
    first_fingerprint = NodeIdentity.load(config.identity_dir).fingerprint

    asyncio.run(_run_until_ready_then_shut_down(config))
    second_fingerprint = NodeIdentity.load(config.identity_dir).fingerprint

    assert first_fingerprint == second_fingerprint


def test_run_logs_node_identity_fingerprint(tmp_path, caplog):
    config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12403))
    with caplog.at_level(logging.INFO, logger="netbbs.__main__"):
        asyncio.run(_run_until_ready_then_shut_down(config))
    assert any("node Link identity" in record.message for record in caplog.records)


def test_startup_fails_cleanly_on_corrupted_node_identity(tmp_path):
    from netbbs.link.node_identity import load_or_bootstrap_node_identity

    config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12404))
    load_or_bootstrap_node_identity(config.identity_dir, label=config.node_name)
    # Corrupt the transition history so loading it fails on next startup.
    (config.identity_dir / "transitions.json").write_text("not valid json")

    async def scenario():
        shutdown_event = asyncio.Event()
        with pytest.raises(StartupError, match="Link identity"):
            await run(config, shutdown_event=shutdown_event)

    asyncio.run(scenario())


def test_startup_fails_cleanly_on_a_database_from_a_newer_build(tmp_path):
    """The concrete scenario this closes: pairing a database file with
    the wrong build/version of NetBBS (e.g. switching between checkouts
    for a before/after comparison and grabbing the wrong db). Without
    this, `Database._apply_migrations`' own version-mismatch `RuntimeError`
    propagated straight out of `run()` as a raw traceback -- now it's
    wrapped into the one exception type `main()` already knows how to
    report cleanly."""
    config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12405))
    conn = sqlite3.connect(str(config.db_path))
    conn.execute("PRAGMA user_version = 999999")
    conn.close()

    async def scenario():
        shutdown_event = asyncio.Event()
        with pytest.raises(StartupError, match="could not open the database"):
            await run(config, shutdown_event=shutdown_event)

    asyncio.run(scenario())


def test_startup_fails_cleanly_on_a_corrupted_database(tmp_path):
    """Design doc §13.11, issue #60: a corrupted database must be
    refused loudly at startup, not left to surface later as a
    confusing raw error the first time some unlucky query touches the
    damaged page."""
    config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12406))

    # Enough real rows, spread across several pages, that a late-offset
    # corruption lands in table data rather than the header/schema page
    # -- same technique as tests/test_storage.py's own integrity-check
    # coverage, see that test for why the offset matters.
    conn = sqlite3.connect(str(config.db_path))
    for i in range(500):
        conn.execute(
            "INSERT INTO node_config (key, value) VALUES (?, ?)", (f"key-{i}", "x" * 200)
        )
    conn.commit()
    conn.close()
    with config.db_path.open("r+b") as handle:
        handle.seek(0, 2)
        file_size = handle.tell()
        handle.seek(file_size - 500)
        handle.write(b"\xff" * 200)

    async def scenario():
        shutdown_event = asyncio.Event()
        with pytest.raises(StartupError, match="failed integrity check"):
            await run(config, shutdown_event=shutdown_event)

    asyncio.run(scenario())

    # The database connection was closed on the way out, not leaked --
    # a fresh, unrelated connection can still open the (still-corrupt,
    # but not locked) file immediately afterward.
    conn = sqlite3.connect(str(config.db_path))
    conn.close()


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


def test_configured_link_listener_completes_a_real_hello(tmp_path):
    """design doc round 118: a real running node's Link listener
    actually answers a genuine dial_hello -- not just "something is
    listening" (test_configured_telnet_listener above), but a
    verified, signed handshake against the node's own real, loaded
    NodeIdentity."""
    import aiohttp

    async def scenario():
        config = _config(
            tmp_path,
            telnet=TransportConfig(True, "127.0.0.1", 12401),
            link=LinkConfig(enabled=True, host="127.0.0.1", port=12402),
        )
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        try:
            await _open_connection_when_ready("127.0.0.1", 12401)  # node fully up

            dialer = LinkNode(identity=bootstrap_node_identity("dialer"))
            dialer_hello = dialer.build_hello(
                addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00"
            )
            dialer_db = Database(tmp_path / "dialer.db")
            dialer_lane = DatabaseLane(dialer_db.path)
            try:
                async with aiohttp.ClientSession() as session:
                    record = await dial_hello(dialer, session, "http://127.0.0.1:12402", dialer_hello, dialer_lane)
            finally:
                dialer_lane.close()
                dialer_db.close()

            from netbbs.link.node_identity import load_or_bootstrap_node_identity

            real_identity = load_or_bootstrap_node_identity(
                config.identity_dir, label=config.node_name
            )
            assert record.fingerprint == real_identity.fingerprint
        finally:
            shutdown_event.set()
            await task

    asyncio.run(scenario())


def test_configured_link_seed_is_dialed_by_a_real_running_node(tmp_path):
    """design doc round 119: a real running node with [link] seeds
    configured actually *originates* a hello to that seed on its own,
    unprompted -- not just answering one (the test above). Drives a
    bare LinkServer as the "seed" (not a second full netbbs node --
    tests/test_link_sync.py already covers the sync loop's own
    behavior in isolation; this test's job is only confirming run()
    actually wires it up and starts it for real)."""
    from netbbs.link.transport import LinkServer

    async def scenario():
        seed_node = LinkNode(identity=bootstrap_node_identity("seed"))
        seed_db = Database(tmp_path / "seed.db")
        seed_lane = DatabaseLane(seed_db.path)
        seed_server = LinkServer(
            host="127.0.0.1", port=0, node=seed_node,
            own_hello_provider=lambda: seed_node.build_hello(
                addresses=None, outgoing_only=True, created_at="2026-01-01T00:00:00+00:00"
            ),
            lane=seed_lane,
        )
        await seed_server.start()

        config = _config(
            tmp_path,
            telnet=TransportConfig(True, "127.0.0.1", 12403),
            link=LinkConfig(
                enabled=True, host="127.0.0.1", port=12404,
                seeds=[f"http://127.0.0.1:{seed_server.port}"], sync_interval_seconds=60.0,
            ),
        )
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        try:
            await _open_connection_when_ready("127.0.0.1", 12403)  # node fully up

            deadline = asyncio.get_event_loop().time() + 5.0
            while not seed_node.peers:
                if asyncio.get_event_loop().time() >= deadline:
                    raise AssertionError("the running node's sync task never dialed the seed")
                await asyncio.sleep(0.05)

            from netbbs.link.node_identity import load_or_bootstrap_node_identity

            real_identity = load_or_bootstrap_node_identity(
                config.identity_dir, label=config.node_name
            )
            assert real_identity.fingerprint in seed_node.peers
        finally:
            shutdown_event.set()
            await task
            await seed_server.stop()
            seed_lane.close()
            seed_db.close()

    asyncio.run(scenario())


def test_link_sync_failures_reach_the_bounded_diagnostic_log(tmp_path):
    """Design doc §13.11, issue #60: LinkDiagnosticLogHandler is
    actually attached during a real run() -- not just unit-tested in
    isolation (tests/test_link_diagnostics.py) -- confirmed here against
    a genuine dial failure a real running node produces on its own."""
    async def scenario():
        # A closed port -- nothing is listening, so every dial attempt
        # fails immediately with a real connection error, exactly the
        # sync.py call site (`_logger.warning("Link sync: could not
        # complete hello with seed %s: %s", ...)`) this test means to
        # exercise.
        config = _config(
            tmp_path,
            telnet=TransportConfig(True, "127.0.0.1", 12407),
            link=LinkConfig(
                enabled=True, host="127.0.0.1", port=12408,
                seeds=["http://127.0.0.1:1"], sync_interval_seconds=60.0,
            ),
        )
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        try:
            await _open_connection_when_ready("127.0.0.1", 12407)

            # Other Link background tasks (e.g. the scheduled seed-list
            # refresh, which also logs a warning trying to reach its own
            # real endpoint) can legitimately write to the same log
            # concurrently -- poll for the *specific* sync.py dial
            # failure this test means to exercise, not just "any row."
            deadline = asyncio.get_event_loop().time() + 5.0
            matching_row = None
            while matching_row is None:
                if asyncio.get_event_loop().time() >= deadline:
                    raise AssertionError("no diagnostic log entry appeared for the failed dial")
                conn = sqlite3.connect(str(config.db_path))
                try:
                    rows = conn.execute(
                        "SELECT level, logger_name, message FROM link_diagnostic_log"
                    ).fetchall()
                finally:
                    conn.close()
                matching_row = next((row for row in rows if row[1] == "netbbs.link.sync"), None)
                if matching_row is None:
                    await asyncio.sleep(0.05)
        finally:
            shutdown_event.set()
            await task

        assert matching_row[0] == "WARNING"
        assert "could not complete hello" in matching_row[2]

    asyncio.run(scenario())


def test_link_sync_task_is_drained_promptly_on_shutdown_even_mid_sleep(tmp_path):
    """Design doc §13.11, issue #60's graceful-drain piece: proves the
    fix through a real run(), not just the run_link_sync-level unit
    test (tests/test_link_sync.py) -- a first version of this feature
    only interrupted the loop's *top-of-pass* check, leaving the
    trailing `asyncio.sleep(sync_interval_seconds)` to run to
    completion regardless, which meant shutdown would silently wait out
    however much of a five-minute default interval remained (routinely
    longer than `graceful_delay_seconds` itself) before ever falling
    back to a hard cancel. `sync_interval_seconds` here is deliberately
    far longer than `graceful_delay_seconds` -- if shutdown were still
    waiting out that sleep, this test's own generous ceiling below
    would catch it."""
    async def scenario():
        config = _config(
            tmp_path,
            telnet=TransportConfig(True, "127.0.0.1", 12409),
            link=LinkConfig(
                enabled=True, host="127.0.0.1", port=12410,
                seeds=["http://127.0.0.1:1"], sync_interval_seconds=120.0,
            ),
            shutdown=ShutdownConfig(graceful_delay_seconds=20.0),
        )
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        try:
            await _open_connection_when_ready("127.0.0.1", 12409)
            # Let the first pass finish and the sync task settle into its
            # (long) trailing sleep before signalling shutdown.
            await asyncio.sleep(0.3)

            start = asyncio.get_event_loop().time()
            shutdown_event.set()
            await asyncio.wait_for(task, timeout=5.0)
            elapsed = asyncio.get_event_loop().time() - start
        finally:
            if not task.done():
                shutdown_event.set()
                await task

        # Well under both graceful_delay_seconds (20s) and
        # sync_interval_seconds (120s) -- the sleep was woken early, not
        # waited out.
        assert elapsed < 2.0

    asyncio.run(scenario())


def test_link_sync_task_is_hard_cancelled_if_a_pass_hangs_past_the_grace_period(tmp_path, monkeypatch):
    """The fallback half of the same graceful-drain piece: a pass that
    never returns on its own (a wedged dial) must still not hang
    shutdown forever -- past `graceful_delay_seconds`, today's
    unconditional hard `.cancel()` remains the backstop."""
    import netbbs.link.sync as sync_module

    async def _hang_forever(*args, **kwargs):
        await asyncio.sleep(999)

    monkeypatch.setattr(sync_module, "_sync_one_seed", _hang_forever)

    async def scenario():
        config = _config(
            tmp_path,
            telnet=TransportConfig(True, "127.0.0.1", 12411),
            link=LinkConfig(
                enabled=True, host="127.0.0.1", port=12412,
                seeds=["http://127.0.0.1:1"], sync_interval_seconds=120.0,
            ),
            shutdown=ShutdownConfig(graceful_delay_seconds=0.3),
        )
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        try:
            await _open_connection_when_ready("127.0.0.1", 12411)
            await asyncio.sleep(0.1)  # into the now-hanging first pass

            start = asyncio.get_event_loop().time()
            shutdown_event.set()
            await asyncio.wait_for(task, timeout=10.0)
            elapsed = asyncio.get_event_loop().time() - start
        finally:
            if not task.done():
                shutdown_event.set()
                await task

        # Waited out roughly the configured grace period (not an
        # instant cancel) before the hard-cancel fallback took over, and
        # didn't hang indefinitely on the wedged pass. The lower bound
        # tolerates a few milliseconds under the nominal 0.3s: real
        # asyncio timers (wait_for/call_later) are not perfectly
        # precise, and prior awaits before this node's own wait_for
        # starts its countdown add a small, variable amount of slack in
        # both directions -- confirmed flaky at the exact 0.3 threshold
        # (observed 0.297 in one run) before this tolerance was added.
        # The point of the lower bound is only "not an instant cancel,"
        # not pinning down sub-10ms scheduler jitter.
        assert 0.3 - 0.05 <= elapsed < 8.0

    asyncio.run(scenario())


def test_link_alone_does_not_count_as_an_interactive_listener(tmp_path):
    """A node configured with only Link enabled (no telnet/ssh/web) has
    nothing a *user* can connect to -- must still raise StartupError,
    even though the Link listener itself would start fine. Confirms
    round 118's any_interactive_started tracking is genuinely separate
    from the servers list Link also participates in."""

    async def scenario():
        config = _config(
            tmp_path,
            telnet=TransportConfig(False, "127.0.0.1", 0),
            ssh=TransportConfig(False, "127.0.0.1", 0),
            web=TransportConfig(False, "127.0.0.1", 0),
            link=LinkConfig(enabled=True, host="127.0.0.1", port=0),
        )
        with pytest.raises(StartupError, match="no interactive listener actually started"):
            await run(config)

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

    async def spy(
        session, db, hub, presence, mailbox, throttle, throttle_config,
        *, node_controls=None, lane=None, link_context=None,
    ):
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


# -- GitHub issue #34 (reopened a third time): startup staging purge --------


def test_run_purges_stale_incoming_staging_files_before_accepting_sessions(tmp_path):
    """A file left under .incoming from a previous run that was
    killed/crashed/lost power mid-upload must be gone by the time
    run() actually starts accepting connections -- confirms the purge
    wired into run() itself, not just netbbs.files.storage.
    purge_incoming_staging in isolation (already covered in
    test_file_storage.py)."""
    from netbbs.files.storage import new_incoming_temp_path

    config = _config(tmp_path)
    setup_db = Database(config.db_path)
    stray = new_incoming_temp_path(setup_db)
    stray.write_bytes(b"leftover from a crashed upload")
    setup_db.close()
    assert stray.exists()

    asyncio.run(_run_until_ready_then_shut_down(config))

    assert not stray.exists()


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


def test_run_writes_and_removes_its_own_pid_file(tmp_path):
    """Design doc §13.10, issue #75: netbbs.backup.restore_backup's
    liveness check depends on this existing across every real node
    lifetime and being gone again on a clean exit -- proven here
    against a real run(), not just the write_pid_file/remove_pid_file
    unit round trip in tests/test_backup.py."""
    captured_db_path = tmp_path / "node.db"
    pid_path = tmp_path / "node.pid"

    async def scenario():
        config = _config(tmp_path, db_path=captured_db_path)
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        await asyncio.sleep(0.1)

        assert pid_path.exists()
        assert int(pid_path.read_text().strip()) == os.getpid()  # this test process is what's actually running

        shutdown_event.set()
        await task

        assert not pid_path.exists()

    asyncio.run(scenario())


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


def test_a_pending_approval_sysop_does_not_count_and_still_raises_startup_error(tmp_path):
    """GitHub issue #44: a self-registered account sitting at SysOp
    level while still `pending_approval` can't actually log in through
    any auth path, so it must not satisfy the startup guard any more
    than a disabled one does (see the sibling
    `test_a_disabled_sysop_does_not_count...` above)."""
    config = _config(tmp_path, seed_sysop=False)
    db = Database(config.db_path)
    create_user(
        db, "pending", password="hunter2", user_level=SYSOP_LEVEL, pending_approval=True
    )
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
        with pytest.raises(StartupError, match="no interactive listener actually started"):
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


# -- daybreak task failure resilience (GitHub issue #48) --------------------


def test_failed_daybreak_task_is_logged(tmp_path, monkeypatch, caplog):
    """An unexpected exception in the daybreak announcer must be
    observed (logged), not silently leave the feature dead with no
    trace -- the original defect this issue reports."""

    async def failing_announcer(db, hub):
        raise RuntimeError("boom")

    monkeypatch.setattr("netbbs.__main__.run_daybreak_announcer", failing_announcer)

    async def scenario():
        config = _config(tmp_path)
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        await asyncio.sleep(0.1)
        shutdown_event.set()
        await task

    with caplog.at_level(logging.ERROR, logger="netbbs.__main__"):
        asyncio.run(scenario())

    assert any(
        "daybreak announcer task failed" in record.message and record.exc_info
        for record in caplog.records
    )


def test_failed_daybreak_task_does_not_prevent_listener_and_db_cleanup(tmp_path, monkeypatch):
    """The core defect: awaiting an already-failed (non-cancelled) task
    in the shutdown `finally` block used to re-raise its exception and
    skip stopping listeners / closing the database entirely."""

    async def failing_announcer(db, hub):
        raise RuntimeError("boom")

    monkeypatch.setattr("netbbs.__main__.run_daybreak_announcer", failing_announcer)

    captured_db_path = tmp_path / "node.db"

    async def scenario():
        config = _config(
            tmp_path, db_path=captured_db_path, telnet=TransportConfig(True, "127.0.0.1", 12394)
        )
        shutdown_event = asyncio.Event()
        task = asyncio.create_task(run(config, shutdown_event=shutdown_event))
        reader, writer = await _open_connection_when_ready("127.0.0.1", 12394)
        writer.close()
        await writer.wait_closed()

        shutdown_event.set()
        # Must return cleanly -- not re-raise the daybreak task's
        # RuntimeError -- and must actually finish stopping listeners
        # and closing the database before doing so.
        await asyncio.wait_for(task, timeout=5.0)

        with pytest.raises((ConnectionRefusedError, OSError)):
            await asyncio.open_connection("127.0.0.1", 12394)

    asyncio.run(scenario())

    conn = sqlite3.connect(str(captured_db_path))
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


def test_daybreak_task_cancellation_on_normal_shutdown_is_unaffected(tmp_path):
    """Sibling coverage for the untouched, already-correct path: a
    daybreak task that's still happily sleeping when shutdown happens
    is cancelled cleanly, same as before this fix."""
    asyncio.run(_run_until_ready_then_shut_down(_config(tmp_path)))


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
