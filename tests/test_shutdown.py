"""
Tests for the deliberate node-shutdown sequence (design doc round 51):
`netbbs.net.session_registry.ActiveSessionRegistry`,
`netbbs.net.maintenance.MaintenanceMode`, and
`netbbs.net.shutdown.run_shutdown_sequence` (the coordinator SIGTERM/
SIGINT -- and, as of the node-management round, the in-session `[N]ode`
admin command too -- actually trigger; relocated out of
`netbbs.__main__` in that same round since it's no longer signal-
specific). The coordinator is driven directly here rather than via real
OS signals — `tests/test_main_lifecycle.py`'s own
`test_signal_handler_registration_triggers_shutdown_event` already
covers that a real signal reaches `_install_signal_handlers`; this file
covers what happens once it does.
"""

from __future__ import annotations

import asyncio

from netbbs.__main__ import run
from netbbs.net.maintenance import MAINTENANCE_MESSAGE, MaintenanceMode
from netbbs.net.nodeconfig import TransportConfig
from netbbs.net.session import SessionClosedError
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import run_shutdown_sequence
from tests.test_main_lifecycle import _config, _open_connection_when_ready


class _FakeSession:
    peer_address: str | None = None

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


# -- disconnect_one / mark_authenticated / list_entries (node management round) --


def test_disconnect_one_cancels_just_that_session():
    registry = ActiveSessionRegistry()

    async def scenario():
        sessions = [_FakeSession(), _FakeSession()]
        tasks = [asyncio.create_task(_hold_registered(registry, s)) for s in sessions]
        await asyncio.sleep(0)

        disconnected = await registry.disconnect_one(sessions[0])

        assert disconnected is True
        assert tasks[0].cancelled()
        assert len(registry) == 1  # the other session is untouched

        tasks[1].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_disconnect_one_returns_false_for_an_unregistered_session():
    registry = ActiveSessionRegistry()

    async def scenario():
        result = await registry.disconnect_one(_FakeSession())
        assert result is False

    asyncio.run(scenario())


def test_mark_authenticated_updates_list_entries():
    registry = ActiveSessionRegistry()

    async def scenario():
        session = _FakeSession()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)

        before = registry.list_entries()
        assert before[0].username is None

        registry.mark_authenticated(session, "alice")

        after = registry.list_entries()
        assert after[0].username == "alice"

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_mark_authenticated_on_an_unregistered_session_does_not_raise():
    registry = ActiveSessionRegistry()

    async def scenario():
        registry.mark_authenticated(_FakeSession(), "alice")  # must not raise

    asyncio.run(scenario())


def test_list_entries_reflects_peer_address_and_connected_at():
    registry = ActiveSessionRegistry()

    class _AddressedSession(_FakeSession):
        peer_address = "203.0.113.7"

    async def scenario():
        session = _AddressedSession()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)

        entries = registry.list_entries()
        assert len(entries) == 1
        assert entries[0].session is session
        assert entries[0].peer_address == "203.0.113.7"
        assert entries[0].connected_at  # a real timestamp was recorded

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


# -- sessions_for_username / disconnect_username (GitHub issue #29) --------


def test_sessions_for_username_finds_every_matching_session():
    registry = ActiveSessionRegistry()

    async def scenario():
        sessions = [_FakeSession(), _FakeSession(), _FakeSession()]
        tasks = [asyncio.create_task(_hold_registered(registry, s)) for s in sessions]
        await asyncio.sleep(0)
        registry.mark_authenticated(sessions[0], "alice")
        registry.mark_authenticated(sessions[1], "bob")
        registry.mark_authenticated(sessions[2], "alice")

        found = registry.sessions_for_username("alice")

        assert set(found) == {sessions[0], sessions[2]}

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_disconnect_username_ends_every_matching_session():
    registry = ActiveSessionRegistry()

    async def scenario():
        sessions = [_FakeSession(), _FakeSession(), _FakeSession()]
        tasks = [asyncio.create_task(_hold_registered(registry, s)) for s in sessions]
        await asyncio.sleep(0)
        registry.mark_authenticated(sessions[0], "alice")
        registry.mark_authenticated(sessions[1], "bob")
        registry.mark_authenticated(sessions[2], "alice")

        disconnected = await registry.disconnect_username("alice")

        assert disconnected == 2
        assert tasks[0].cancelled()
        assert tasks[2].cancelled()
        assert len(registry) == 1  # bob's session is untouched

        tasks[1].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_disconnect_username_excludes_the_given_session():
    """The acting SysOp's own current session must be skipped, not
    cancelled-and-awaited from within itself (GitHub issue #29)."""
    registry = ActiveSessionRegistry()

    async def scenario():
        sessions = [_FakeSession(), _FakeSession()]
        tasks = [asyncio.create_task(_hold_registered(registry, s)) for s in sessions]
        await asyncio.sleep(0)
        registry.mark_authenticated(sessions[0], "alice")
        registry.mark_authenticated(sessions[1], "alice")

        disconnected = await registry.disconnect_username("alice", exclude_session=sessions[0])

        assert disconnected == 1
        assert not tasks[0].cancelled()
        assert tasks[1].cancelled()
        assert len(registry) == 1

        tasks[0].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_disconnect_username_with_no_matching_sessions_returns_zero():
    registry = ActiveSessionRegistry()

    async def scenario():
        result = await registry.disconnect_username("nobody-here")
        assert result == 0

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
                run_shutdown_sequence(
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
                run_shutdown_sequence(
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

        await run_shutdown_sequence(
            graceful=False,
            session_registry=registry,
            maintenance=maintenance,
            graceful_delay_seconds=60.0,
            shutdown_event=shutdown_event,
        )

        assert maintenance.is_active() is True
        assert shutdown_event.is_set() is True

    asyncio.run(scenario())


def test_run_shutdown_sequence_custom_message_replaces_the_default(tmp_path):
    """Per Thiesi's own wording (design doc -- node management round): a
    supplied message *replaces* the default "going down" text, it
    doesn't append to it."""

    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)

        await run_shutdown_sequence(
            graceful=False,
            session_registry=registry,
            maintenance=MaintenanceMode(),
            graceful_delay_seconds=60.0,
            shutdown_event=asyncio.Event(),
            message="Emergency maintenance, back in five minutes.",
        )

        assert any("Emergency maintenance" in line for line in session.written)
        assert not any("going down now" in line for line in session.written)

        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_run_shutdown_sequence_without_a_message_uses_the_default():
    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)

        await run_shutdown_sequence(
            graceful=False,
            session_registry=registry,
            maintenance=MaintenanceMode(),
            graceful_delay_seconds=60.0,
            shutdown_event=asyncio.Event(),
        )

        assert any("going down now" in line for line in session.written)

        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
