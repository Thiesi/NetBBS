"""
Tests for the deliberate node-shutdown sequence (design doc round 51):
`netbbs.net.session_registry.ActiveSessionRegistry`,
`netbbs.net.maintenance.MaintenanceMode`, and
`netbbs.__main__._run_shutdown_sequence` (the coordinator SIGTERM/
SIGINT actually trigger). The coordinator is driven directly here
rather than via real OS signals — `tests/test_main_lifecycle.py`'s own
`test_signal_handler_registration_triggers_shutdown_event` already
covers that a real signal reaches `_install_signal_handlers`; this file
covers what happens once it does.
"""

from __future__ import annotations

import asyncio

from netbbs.__main__ import _run_shutdown_sequence, run
from netbbs.net.maintenance import MAINTENANCE_MESSAGE, MaintenanceMode
from netbbs.net.nodeconfig import TransportConfig
from netbbs.net.session import SessionClosedError
from netbbs.net.session_registry import ActiveSessionRegistry
from tests.test_main_lifecycle import _config, _open_connection_when_ready


class _FakeSession:
    def __init__(self):
        self.written: list[str] = []

    async def write_line(self, text: str = "") -> None:
        self.written.append(text)


class _FailingSession:
    async def write_line(self, text: str = "") -> None:
        raise SessionClosedError("already gone")


async def _hold_registered(registry: ActiveSessionRegistry, session) -> None:
    """Registers `session` and stays "connected" (blocked) until
    cancelled -- the shape a real `handle_session` connection has while
    idle, needed so `disconnect_all` has something real to cancel."""
    registry.enter(session)
    try:
        await asyncio.Event().wait()
    finally:
        registry.leave(session)


# -- ActiveSessionRegistry ---------------------------------------------


def test_len_reflects_registered_sessions():
    registry = ActiveSessionRegistry()

    async def scenario():
        registry.enter(_FakeSession())
        assert len(registry) == 1

    asyncio.run(scenario())


def test_leave_removes_the_session():
    registry = ActiveSessionRegistry()
    session = _FakeSession()

    async def scenario():
        registry.enter(session)
        registry.leave(session)
        assert len(registry) == 0

    asyncio.run(scenario())


def test_leave_never_registered_does_not_raise():
    registry = ActiveSessionRegistry()

    async def scenario():
        registry.leave(_FakeSession())  # must not raise

    asyncio.run(scenario())


def test_broadcast_to_all_writes_to_every_registered_session():
    registry = ActiveSessionRegistry()

    async def scenario():
        sessions = [_FakeSession(), _FakeSession()]
        tasks = [asyncio.create_task(_hold_registered(registry, s)) for s in sessions]
        await asyncio.sleep(0)  # let both register before broadcasting

        await registry.broadcast_to_all("hello")

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert sessions[0].written == ["hello"]
        assert sessions[1].written == ["hello"]

    asyncio.run(scenario())


def test_broadcast_to_all_skips_a_session_that_raises_session_closed_error():
    registry = ActiveSessionRegistry()

    async def scenario():
        failing, ok = _FailingSession(), _FakeSession()
        tasks = [
            asyncio.create_task(_hold_registered(registry, failing)),
            asyncio.create_task(_hold_registered(registry, ok)),
        ]
        await asyncio.sleep(0)

        await registry.broadcast_to_all("hello")  # must not raise

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert ok.written == ["hello"]

    asyncio.run(scenario())


def test_disconnect_all_cancels_every_registered_task():
    registry = ActiveSessionRegistry()

    async def scenario():
        sessions = [_FakeSession(), _FakeSession()]
        tasks = [asyncio.create_task(_hold_registered(registry, s)) for s in sessions]
        await asyncio.sleep(0)

        await registry.disconnect_all()

        for task in tasks:
            assert task.cancelled()
        assert len(registry) == 0  # each task's own finally: leave() ran

    asyncio.run(scenario())


def test_disconnect_all_on_an_empty_registry_returns_immediately():
    registry = ActiveSessionRegistry()

    async def scenario():
        await asyncio.wait_for(registry.disconnect_all(), timeout=1.0)  # must not hang

    asyncio.run(scenario())


# -- MaintenanceMode ------------------------------------------------------


def test_maintenance_mode_starts_inactive():
    assert MaintenanceMode().is_active() is False


def test_activate_flips_the_flag():
    mode = MaintenanceMode()
    mode.activate()
    assert mode.is_active() is True


def test_maintenance_mode_rejects_a_new_connection_before_login():
    from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
    from netbbs.net import login_flow
    from netbbs.net.nodeconfig import ThrottleConfig
    from netbbs.net.throttle import LoginThrottle

    class _FakeLoginSession:
        def __init__(self):
            self.written: list[str] = []

        async def write_line(self, text: str = "") -> None:
            self.written.append(text)

        async def read_line(self, echo: bool = True) -> str:
            raise AssertionError("read_line should never be reached once maintenance mode is active")

    async def scenario():
        maintenance = MaintenanceMode()
        maintenance.activate()
        session_registry = ActiveSessionRegistry()
        session = _FakeLoginSession()
        throttle_config = ThrottleConfig()
        throttle = LoginThrottle(
            per_source_capacity=throttle_config.per_source_capacity,
            per_source_refill_per_minute=throttle_config.per_source_refill_per_minute,
            per_username_capacity=throttle_config.per_username_capacity,
            per_username_refill_per_minute=throttle_config.per_username_refill_per_minute,
            global_capacity=throttle_config.global_capacity,
            global_refill_per_minute=throttle_config.global_refill_per_minute,
            max_tracked_keys=throttle_config.max_tracked_keys,
            max_concurrent_unauthenticated_sessions=throttle_config.max_concurrent_unauthenticated_sessions,
        )

        await login_flow.handle_session(
            session, object(), ChatHub(), PresenceRegistry(), MessageMailbox(),
            throttle, throttle_config, session_registry, maintenance,
        )

        assert any(MAINTENANCE_MESSAGE in line for line in session.written)
        assert len(session_registry) == 0  # never registered -- rejected before that point

    asyncio.run(scenario())


# -- the real coordinator, over a real socket ------------------------------


def test_immediate_shutdown_broadcasts_and_disconnects_without_waiting(tmp_path):
    async def scenario():
        config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12394))
        shutdown_event = asyncio.Event()
        session_registry = ActiveSessionRegistry()
        maintenance = MaintenanceMode()
        task = asyncio.create_task(
            run(
                config,
                shutdown_event=shutdown_event,
                session_registry=session_registry,
                maintenance=maintenance,
            )
        )
        try:
            reader, writer = await _open_connection_when_ready("127.0.0.1", 12394)
            await reader.readexactly(9)  # initial Telnet negotiation triplets

            deadline = asyncio.get_event_loop().time() + 2.0
            while len(session_registry) == 0:
                if asyncio.get_event_loop().time() >= deadline:
                    raise AssertionError("connection never registered itself")
                await asyncio.sleep(0.01)

            await asyncio.wait_for(
                _run_shutdown_sequence(
                    graceful=False,
                    session_registry=session_registry,
                    maintenance=maintenance,
                    graceful_delay_seconds=60.0,  # must be ignored entirely on this path
                    shutdown_event=shutdown_event,
                ),
                timeout=5.0,
            )

            data = await reader.read(4096)
            assert b"going down now" in data

            # The connection was actually force-closed, not left dangling.
            assert await reader.read(4096) == b""

            await asyncio.wait_for(task, timeout=5.0)
        finally:
            writer.close()

    asyncio.run(scenario())


def test_graceful_shutdown_actually_waits_before_disconnecting(tmp_path):
    async def scenario():
        config = _config(tmp_path, telnet=TransportConfig(True, "127.0.0.1", 12393))
        shutdown_event = asyncio.Event()
        session_registry = ActiveSessionRegistry()
        maintenance = MaintenanceMode()
        task = asyncio.create_task(
            run(
                config,
                shutdown_event=shutdown_event,
                session_registry=session_registry,
                maintenance=maintenance,
            )
        )
        try:
            reader, writer = await _open_connection_when_ready("127.0.0.1", 12393)
            await reader.readexactly(9)

            deadline = asyncio.get_event_loop().time() + 2.0
            while len(session_registry) == 0:
                if asyncio.get_event_loop().time() >= deadline:
                    raise AssertionError("connection never registered itself")
                await asyncio.sleep(0.01)

            start = asyncio.get_event_loop().time()
            await asyncio.wait_for(
                _run_shutdown_sequence(
                    graceful=True,
                    session_registry=session_registry,
                    maintenance=maintenance,
                    graceful_delay_seconds=0.3,
                    shutdown_event=shutdown_event,
                ),
                timeout=5.0,
            )
            elapsed = asyncio.get_event_loop().time() - start
            # Genuinely waited close to the configured delay, not skipped
            # -- a lower bound only, so this can't flake on a slow machine.
            assert elapsed >= 0.25

            data = await reader.read(4096)
            assert b"going down in" in data

            await asyncio.wait_for(task, timeout=5.0)
        finally:
            writer.close()

    asyncio.run(scenario())


def test_shutdown_activates_maintenance_mode():
    async def scenario():
        maintenance = MaintenanceMode()
        registry = ActiveSessionRegistry()
        shutdown_event = asyncio.Event()

        await _run_shutdown_sequence(
            graceful=False,
            session_registry=registry,
            maintenance=maintenance,
            graceful_delay_seconds=60.0,
            shutdown_event=shutdown_event,
        )

        assert maintenance.is_active() is True
        assert shutdown_event.is_set() is True

    asyncio.run(scenario())
