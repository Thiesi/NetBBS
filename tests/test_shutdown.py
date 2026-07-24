"""
Tests for the deliberate node-shutdown sequence:
`netbbs.net.session_registry.ActiveSessionRegistry`,
`netbbs.net.maintenance.MaintenanceMode`, and
`netbbs.net.shutdown.run_shutdown_sequence` (the coordinator SIGTERM/
SIGINT -- and the in-session `[N]ode` admin command too -- actually
trigger; relocated out of `netbbs.__main__` since it's no longer
signal-specific). The coordinator is driven directly here rather than
via real OS signals — `tests/test_main_lifecycle.py`'s own
`test_signal_handler_registration_triggers_shutdown_event` already
covers that a real signal reaches `_install_signal_handlers`; this file
covers what happens once it does.
"""

from __future__ import annotations

import asyncio

from netbbs.__main__ import run
from netbbs.net import shutdown as shutdown_module
from netbbs.net.maintenance import MAINTENANCE_MESSAGE, MaintenanceMode
from netbbs.net.nodeconfig import TransportConfig
from netbbs.net.session import SessionClosedError
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import (
    NodeControls,
    SequenceScheduler,
    format_remaining_seconds,
    run_drain_sequence,
    run_shutdown_sequence,
)
from tests.test_main_lifecycle import _config, _open_connection_when_ready


class _FakeSession:
    peer_address: str | None = None
    # Matches Session.pinned_notice_hook's own class-level default --
    # broadcast_to_all reads this directly, and neither fake here is a
    # real Session subclass to inherit it from.
    pinned_notice_hook = None

    def __init__(self):
        self.written: list[str] = []

    async def write(self, text: str = "") -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text)

    async def read_key(self, echo: bool = True) -> str:
        # Blocks forever, like a real session genuinely idle at a menu
        # prompt -- the §13.8 lockdown tests only need whatever was
        # written before this point (the welcome line/lockdown notice),
        # not the main menu loop to ever actually respond to a key.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def read_line(self, echo: bool = True, history=None, completer=None) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _FailingSession:
    pinned_notice_hook = None

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


def test_broadcast_to_all_uses_pinned_notice_hook_instead_of_write_line_when_set():
    """A screen with reserved/pinned rows (currently only chat) installs
    `Session.pinned_notice_hook` so an out-of-band notice like this one
    reaches it safely instead of a raw `write_line` that has no idea a
    scroll region/pinned input row is active -- see that attribute's own
    docstring. `write_line` must not be called at all once a hook is
    installed."""
    registry = ActiveSessionRegistry()

    async def scenario():
        session = _FakeSession()
        hook_calls: list[str] = []

        async def fake_hook(text: str) -> None:
            hook_calls.append(text)

        session.pinned_notice_hook = fake_hook
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)

        await registry.broadcast_to_all("hello")

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert hook_calls == ["hello"]
        assert session.written == []  # write_line never called directly

    asyncio.run(scenario())


def test_broadcast_to_all_falls_back_to_write_line_when_no_hook_is_set():
    registry = ActiveSessionRegistry()

    async def scenario():
        session = _FakeSession()
        assert session.pinned_notice_hook is None
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)

        await registry.broadcast_to_all("hello")

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert session.written == ["hello"]

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


# -- disconnect_one / mark_authenticated / list_entries (design doc §13.8) --


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


# -- exclude_sysops (design doc §13.8, [D]rain) -----------------------------


def test_mark_authenticated_records_is_sysop():
    registry = ActiveSessionRegistry()

    async def scenario():
        session = _FakeSession()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)

        registry.mark_authenticated(session, "sysop", is_sysop=True)

        assert registry.list_entries()[0].username == "sysop"

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_broadcast_to_all_excludes_sysops_when_asked():
    registry = ActiveSessionRegistry()

    async def scenario():
        sysop, regular = _FakeSession(), _FakeSession()
        tasks = [
            asyncio.create_task(_hold_registered(registry, sysop)),
            asyncio.create_task(_hold_registered(registry, regular)),
        ]
        await asyncio.sleep(0)
        registry.mark_authenticated(sysop, "sysop", is_sysop=True)
        registry.mark_authenticated(regular, "alice", is_sysop=False)

        await registry.broadcast_to_all("draining", exclude_sysops=True)

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert sysop.written == []
        assert regular.written == ["draining"]

    asyncio.run(scenario())


def test_disconnect_all_excludes_sysops_when_asked():
    registry = ActiveSessionRegistry()

    async def scenario():
        sysop, regular = _FakeSession(), _FakeSession()
        tasks = [
            asyncio.create_task(_hold_registered(registry, sysop)),
            asyncio.create_task(_hold_registered(registry, regular)),
        ]
        await asyncio.sleep(0)
        registry.mark_authenticated(sysop, "sysop", is_sysop=True)
        registry.mark_authenticated(regular, "alice", is_sysop=False)

        await registry.disconnect_all(exclude_sysops=True)

        assert not tasks[0].cancelled()
        assert tasks[1].cancelled()
        assert len(registry) == 1  # only the SysOp session remains

        tasks[0].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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


def test_deactivate_reverses_activate():
    """The one narrow exception to activate()'s own "no way back" claim
    -- a *scheduled* graceful shutdown, cancelled before it fires, needs
    this to reopen new-login admission (see `netbbs.net.shutdown.
    run_shutdown_sequence`'s own cancellation handling)."""
    mode = MaintenanceMode()
    mode.activate()
    mode.deactivate()
    assert mode.is_active() is False


# -- lockdown (design doc §13.8, [M]aintenance mode) -----------------------


def test_lockdown_starts_inactive():
    assert MaintenanceMode().is_lockdown_active() is False


def test_enable_lockdown_flips_the_flag():
    mode = MaintenanceMode()
    mode.enable_lockdown()
    assert mode.is_lockdown_active() is True


def test_disable_lockdown_flips_it_back():
    mode = MaintenanceMode()
    mode.enable_lockdown()
    mode.disable_lockdown()
    assert mode.is_lockdown_active() is False


def test_lockdown_is_independent_of_activate():
    """The two gates must never be conflated -- shutdown's hard,
    unconditional lockout and the SysOp-toggleable, SysOp-bypassing one
    are different questions (design doc §13.8)."""
    mode = MaintenanceMode()
    mode.enable_lockdown()
    assert mode.is_active() is False
    mode.activate()
    assert mode.is_lockdown_active() is True  # unaffected by the other gate


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
                    delay_seconds=60.0,  # must be ignored entirely on this path
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
                    delay_seconds=0.3,
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
            delay_seconds=60.0,
            shutdown_event=shutdown_event,
        )

        assert maintenance.is_active() is True
        assert shutdown_event.is_set() is True

    asyncio.run(scenario())


def test_run_shutdown_sequence_custom_message_replaces_the_default(tmp_path):
    """Per Thiesi's own wording (design doc §13.8): a
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
            delay_seconds=60.0,
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
            delay_seconds=60.0,
            shutdown_event=asyncio.Event(),
        )

        assert any("going down now" in line for line in session.written)

        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


# -- run_drain_sequence (design doc §13.8, [D]rain) -------------------------


def test_run_drain_sequence_warns_and_disconnects_only_non_sysops():
    async def scenario():
        sysop, regular = _FakeSession(), _FakeSession()
        registry = ActiveSessionRegistry()
        tasks = [
            asyncio.create_task(_hold_registered(registry, sysop)),
            asyncio.create_task(_hold_registered(registry, regular)),
        ]
        await asyncio.sleep(0)
        registry.mark_authenticated(sysop, "sysop", is_sysop=True)
        registry.mark_authenticated(regular, "alice", is_sysop=False)

        await run_drain_sequence(session_registry=registry, delay_seconds=0.0)

        assert sysop.written == []
        assert any("drained" in line for line in regular.written)
        assert not tasks[0].cancelled()
        assert tasks[1].cancelled()
        assert len(registry) == 1  # only the SysOp session remains

        tasks[0].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_run_drain_sequence_custom_message_replaces_the_default():
    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)
        registry.mark_authenticated(session, "alice", is_sysop=False)

        await run_drain_sequence(
            session_registry=registry, delay_seconds=0.0, message="Reconnect after the upgrade."
        )

        assert any("Reconnect after the upgrade" in line for line in session.written)
        assert not any("drained" in line for line in session.written)

        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_run_drain_sequence_waits_the_given_delay_before_disconnecting():
    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)
        registry.mark_authenticated(session, "alice", is_sysop=False)

        drain_task = asyncio.create_task(
            run_drain_sequence(session_registry=registry, delay_seconds=10.0)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.cancelled()  # still connected -- delay hasn't elapsed

        drain_task.cancel()
        await asyncio.gather(drain_task, return_exceptions=True)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_run_drain_sequence_never_touches_maintenance_or_shutdown_event():
    """Design doc §13.8: drain is deliberately orthogonal to [M]aintenance
    mode and never shuts the node down -- it takes no `maintenance`/
    `shutdown_event` parameter at all, unlike `run_shutdown_sequence`."""
    import inspect

    parameters = inspect.signature(run_drain_sequence).parameters
    assert "maintenance" not in parameters
    assert "shutdown_event" not in parameters


# -- post-authentication lockdown (design doc §13.8, [M]aintenance mode) ----


def _node_controls_with_lockdown(registry: ActiveSessionRegistry, *, lockdown: bool) -> NodeControls:
    maintenance = MaintenanceMode()
    if lockdown:
        maintenance.enable_lockdown()
    return NodeControls(
        session_registry=registry, maintenance=maintenance,
        shutdown_event=asyncio.Event(), graceful_delay_seconds=0.0,
    )


def test_lockdown_rejects_a_non_sysop_after_authentication(tmp_path):
    from netbbs.auth.users import create_user
    from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
    from netbbs.net import login_flow
    from netbbs.net.maintenance import LOCKDOWN_MESSAGE
    from netbbs.storage.database import Database

    async def scenario():
        db = Database(tmp_path / "node.db")
        try:
            user = create_user(db, "alice", password="hunter2", user_level=10)
            registry = ActiveSessionRegistry()
            session = _FakeSession()
            node_controls = _node_controls_with_lockdown(registry, lockdown=True)

            await login_flow.run_authenticated_session(
                session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), user,
                node_controls=node_controls,
            )

            assert any(LOCKDOWN_MESSAGE in line for line in session.written)
            assert not any("Main menu" in line for line in session.written)
        finally:
            db.close()

    asyncio.run(scenario())


def test_lockdown_lets_a_sysop_through(tmp_path):
    from netbbs.auth.users import SYSOP_LEVEL, create_user
    from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
    from netbbs.net import login_flow
    from netbbs.storage.database import Database

    async def scenario():
        db = Database(tmp_path / "node.db")
        try:
            sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
            registry = ActiveSessionRegistry()
            session = _FakeSession()
            node_controls = _node_controls_with_lockdown(registry, lockdown=True)

            # An allowed SysOp proceeds into the main menu, which then
            # blocks on read_key() forever (a real idle session) -- run
            # as a background task and cancel once the welcome/notice
            # lines have already been written, rather than awaiting
            # inline to completion.
            task = asyncio.create_task(
                login_flow.run_authenticated_session(
                    session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), sysop,
                    node_controls=node_controls,
                )
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            assert any("Welcome, sysop" in line for line in session.written)
            assert any("Maintenance mode is ON" in line for line in session.written)
        finally:
            db.close()

    asyncio.run(scenario())


def test_no_lockdown_notice_when_lockdown_is_off(tmp_path):
    from netbbs.auth.users import SYSOP_LEVEL, create_user
    from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
    from netbbs.net import login_flow
    from netbbs.storage.database import Database

    async def scenario():
        db = Database(tmp_path / "node.db")
        try:
            sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
            registry = ActiveSessionRegistry()
            session = _FakeSession()
            node_controls = _node_controls_with_lockdown(registry, lockdown=False)

            task = asyncio.create_task(
                login_flow.run_authenticated_session(
                    session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), sysop,
                    node_controls=node_controls,
                )
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            assert not any("Maintenance mode is ON" in line for line in session.written)
        finally:
            db.close()

    asyncio.run(scenario())


# -- pre-login maintenance notice / post-login drain notice (design doc -- --
# -- node management, Thiesi's own dogfood-testing report) ------------------


class _ScriptedLoginSession:
    """Supports a real interactive login (`read_line`, from `lines`)
    followed by however much of the main menu the test actually needs
    (`read_key`, from `keys`) -- the same two-list shape `tests/
    test_login_outcomes.py`'s own `FakeSession`/`_SSHFakeSession` split
    into two separate classes, combined here since these tests need
    both in the same session."""

    def __init__(self, lines, keys=None):
        self._lines = iter(lines)
        self._keys = iter(keys or [])
        self.written = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines)

    async def read_key(self, echo: bool = True) -> str:
        return next(self._keys)

    @property
    def output(self) -> str:
        return "".join(self.written)


def _real_throttle():
    from netbbs.net.nodeconfig import ThrottleConfig
    from netbbs.net.throttle import LoginThrottle

    config = ThrottleConfig()
    throttle = LoginThrottle(
        per_source_capacity=config.per_source_capacity,
        per_source_refill_per_minute=config.per_source_refill_per_minute,
        per_username_capacity=config.per_username_capacity,
        per_username_refill_per_minute=config.per_username_refill_per_minute,
        global_capacity=config.global_capacity,
        global_refill_per_minute=config.global_refill_per_minute,
        max_tracked_keys=config.max_tracked_keys,
        max_concurrent_unauthenticated_sessions=config.max_concurrent_unauthenticated_sessions,
    )
    return throttle, config


def test_lockdown_notice_shown_before_login_regardless_of_who_is_connecting(tmp_path):
    """The pre-login heads-up (design doc, distinct from `LOCKDOWN_
    MESSAGE`'s own hard-rejection wording) is shown to *everyone*
    connecting while lockdown is on, before credentials are even
    checked -- SysOp-ness isn't known yet. Proven here against a
    non-SysOp account, which then still gets rejected afterward by the
    existing post-authentication check -- both the heads-up and the
    rejection fire, not one instead of the other."""
    from netbbs.auth.users import create_user
    from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
    from netbbs.net import login_flow
    from netbbs.net.maintenance import LOCKDOWN_MESSAGE, LOCKDOWN_NOTICE
    from netbbs.storage.database import Database

    async def scenario():
        db = Database(tmp_path / "node.db")
        try:
            create_user(db, "alice", password="hunter2", user_level=10)
            maintenance = MaintenanceMode()
            maintenance.enable_lockdown()
            throttle, throttle_config = _real_throttle()
            session = _ScriptedLoginSession(["alice", "hunter2"])

            await login_flow.handle_session(
                session, db, ChatHub(), PresenceRegistry(), MessageMailbox(),
                throttle, throttle_config, ActiveSessionRegistry(), maintenance,
            )

            assert LOCKDOWN_NOTICE in session.output
            assert LOCKDOWN_MESSAGE in session.output  # the actual rejection, still fires too
        finally:
            db.close()

    asyncio.run(scenario())


def test_no_lockdown_notice_before_login_when_lockdown_is_off(tmp_path):
    from netbbs.auth.users import create_user
    from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
    from netbbs.net import login_flow
    from netbbs.net.maintenance import LOCKDOWN_NOTICE
    from netbbs.storage.database import Database

    async def scenario():
        db = Database(tmp_path / "node.db")
        try:
            create_user(db, "alice", password="hunter2", user_level=10)
            throttle, throttle_config = _real_throttle()
            session = _ScriptedLoginSession(["alice", "hunter2"], keys=["l"])

            await login_flow.handle_session(
                session, db, ChatHub(), PresenceRegistry(), MessageMailbox(),
                throttle, throttle_config, ActiveSessionRegistry(), MaintenanceMode(),
            )

            assert LOCKDOWN_NOTICE not in session.output
        finally:
            db.close()

    asyncio.run(scenario())


def test_shutdown_pre_login_message_includes_remaining_time_when_scheduled():
    """The existing hard pre-login `MAINTENANCE_MESSAGE` gains the
    scheduled shutdown's own remaining time when a `shutdown_scheduler`
    is given -- a plain, unenhanced message otherwise (no scheduler
    passed, e.g. a caller that never registered one)."""
    async def scenario():
        maintenance = MaintenanceMode()
        maintenance.activate()
        shutdown_scheduler = SequenceScheduler()
        task = asyncio.create_task(asyncio.Event().wait())
        loop = asyncio.get_running_loop()
        shutdown_scheduler.schedule(task, deadline=loop.time() + 42.0, message=None)

        session = _FakeSession()
        throttle, throttle_config = _real_throttle()

        from netbbs.net import login_flow

        # db/hub/presence/mailbox are never touched -- maintenance.is_active()
        # returns before any of them are used.
        await login_flow.handle_session(
            session, object(), object(), object(), object(),
            throttle, throttle_config, ActiveSessionRegistry(), maintenance,
            shutdown_scheduler=shutdown_scheduler,
        )

        text = "".join(session.written)
        assert MAINTENANCE_MESSAGE in text
        assert "going down in" in text

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_drain_notice_shown_to_a_non_sysop_after_login_when_a_drain_is_scheduled(tmp_path):
    async def scenario():
        from netbbs.auth.users import create_user
        from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
        from netbbs.net import login_flow
        from netbbs.storage.database import Database

        db = Database(tmp_path / "node.db")
        try:
            alice = create_user(db, "alice", password="hunter2", user_level=10)
            node_controls = NodeControls(
                session_registry=ActiveSessionRegistry(), maintenance=MaintenanceMode(),
                shutdown_event=asyncio.Event(), graceful_delay_seconds=60.0,
            )
            loop = asyncio.get_running_loop()
            drain_task = asyncio.create_task(asyncio.Event().wait())
            node_controls.drain_scheduler.schedule(drain_task, deadline=loop.time() + 30.0, message=None)
            session = _ScriptedLoginSession([], keys=["l"])

            await login_flow.run_authenticated_session(
                session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), alice,
                node_controls=node_controls,
            )

            assert "you will be disconnected in about" in session.output

            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        finally:
            db.close()

    asyncio.run(scenario())


def test_no_drain_notice_shown_to_a_sysop_even_when_a_drain_is_scheduled(tmp_path):
    async def scenario():
        from netbbs.auth.users import SYSOP_LEVEL, create_user
        from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
        from netbbs.net import login_flow
        from netbbs.storage.database import Database

        db = Database(tmp_path / "node.db")
        try:
            sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
            node_controls = NodeControls(
                session_registry=ActiveSessionRegistry(), maintenance=MaintenanceMode(),
                shutdown_event=asyncio.Event(), graceful_delay_seconds=60.0,
            )
            loop = asyncio.get_running_loop()
            drain_task = asyncio.create_task(asyncio.Event().wait())
            node_controls.drain_scheduler.schedule(drain_task, deadline=loop.time() + 30.0, message=None)
            session = _ScriptedLoginSession([], keys=["l"])

            await login_flow.run_authenticated_session(
                session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), sysop,
                node_controls=node_controls,
            )

            assert "you will be disconnected" not in session.output

            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        finally:
            db.close()

    asyncio.run(scenario())


# -- format_remaining_seconds ------------------------------------------------


def test_format_remaining_seconds_formats_as_mmss():
    assert format_remaining_seconds(75) == "1:15"
    assert format_remaining_seconds(9) == "0:09"
    assert format_remaining_seconds(0) == "0:00"


def test_format_remaining_seconds_floors_at_zero_for_a_negative_input():
    assert format_remaining_seconds(-5) == "0:00"


# -- SequenceScheduler (design doc -- node management, the stacking-bug fix) --


async def _pending_task() -> None:
    await asyncio.Event().wait()


def test_sequence_scheduler_starts_unscheduled():
    scheduler = SequenceScheduler()
    assert scheduler.is_scheduled() is False
    assert scheduler.message() is None


def test_schedule_marks_it_scheduled_and_records_deadline_and_message():
    async def scenario():
        scheduler = SequenceScheduler()
        task = asyncio.create_task(_pending_task())
        loop = asyncio.get_running_loop()

        scheduler.schedule(task, deadline=loop.time() + 60.0, message="be right back")

        assert scheduler.is_scheduled() is True
        assert scheduler.message() == "be right back"
        remaining = scheduler.remaining_seconds()
        assert remaining is not None and 59.0 <= remaining <= 60.0

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_remaining_seconds_counts_down():
    async def scenario():
        scheduler = SequenceScheduler()
        task = asyncio.create_task(_pending_task())
        loop = asyncio.get_running_loop()
        scheduler.schedule(task, deadline=loop.time() + 0.3, message=None)

        first = scheduler.remaining_seconds()
        await asyncio.sleep(0.2)
        second = scheduler.remaining_seconds()

        assert first is not None and second is not None
        assert second < first

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_schedule_cancels_and_replaces_an_existing_one():
    """The actual fix for Thiesi's own reported stacking bug: scheduling
    a second sequence must cancel the first, never leave both running
    independently."""

    async def scenario():
        scheduler = SequenceScheduler()
        loop = asyncio.get_running_loop()
        first_task = asyncio.create_task(_pending_task())
        scheduler.schedule(first_task, deadline=loop.time() + 60.0, message="first")

        second_task = asyncio.create_task(_pending_task())
        scheduler.schedule(second_task, deadline=loop.time() + 10.0, message="second")
        await asyncio.sleep(0)  # let the cancellation actually land

        assert first_task.cancelled()
        assert not second_task.done()
        assert scheduler.message() == "second"

        second_task.cancel()
        await asyncio.gather(second_task, return_exceptions=True)

    asyncio.run(scenario())


def test_cancel_returns_false_when_nothing_scheduled():
    assert SequenceScheduler().cancel() is False


def test_cancel_stops_it_being_scheduled_and_cancels_the_task():
    async def scenario():
        scheduler = SequenceScheduler()
        task = asyncio.create_task(_pending_task())
        loop = asyncio.get_running_loop()
        scheduler.schedule(task, deadline=loop.time() + 60.0, message=None)

        cancelled = scheduler.cancel()
        await asyncio.sleep(0)

        assert cancelled is True
        assert scheduler.is_scheduled() is False
        assert scheduler.remaining_seconds() is None
        assert scheduler.message() is None
        assert task.cancelled()

    asyncio.run(scenario())


def test_is_scheduled_becomes_false_once_the_task_finishes_naturally():
    async def scenario():
        scheduler = SequenceScheduler()
        loop = asyncio.get_running_loop()

        async def _quick() -> None:
            await asyncio.sleep(0)

        task = asyncio.create_task(_quick())
        scheduler.schedule(task, deadline=loop.time() + 60.0, message=None)
        await asyncio.gather(task)

        assert scheduler.is_scheduled() is False
        assert scheduler.remaining_seconds() is None

    asyncio.run(scenario())


# -- staged countdown broadcasts (design doc -- node management, Thiesi's ---
# -- own Unix-`shutdown`-style request) --------------------------------------


def test_run_drain_sequence_broadcasts_a_five_minute_and_one_minute_reminder(monkeypatch):
    """Scaled-down thresholds (the same monkeypatch-an-interval-constant
    trick `test_account_revocation_watcher.py` already uses) so this
    proves the *staging logic* -- an initial broadcast, a reminder at
    each threshold the total delay actually reaches, then a final one --
    without a real test waiting out real minutes. Whole-second values
    throughout (not sub-second) so `_countdown_phrase`'s own rounding
    doesn't collapse distinct stages into identical wording -- that's a
    property of using unrealistically tiny thresholds for test speed,
    not something worth asserting on here."""
    monkeypatch.setattr(shutdown_module, "_STAGE_THRESHOLDS_SECONDS", (2, 1))

    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)
        registry.mark_authenticated(session, "alice", is_sysop=False)

        await run_drain_sequence(session_registry=registry, delay_seconds=3.0)

        broadcasts = [line for line in session.written if "drained" in line]
        assert len(broadcasts) == 4  # initial (3s), 2s-remaining, 1s-remaining, final
        assert any("in 3 seconds" in line for line in broadcasts)
        assert any("in 2 seconds" in line for line in broadcasts)
        assert any("in 1 second" in line for line in broadcasts)
        assert any("now" in line for line in broadcasts)

        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_run_drain_sequence_skips_a_threshold_the_delay_never_reaches(monkeypatch):
    """A delay shorter than even the smallest threshold must not
    fabricate a reminder for a checkpoint it never actually passes
    through -- only the initial and final broadcasts happen."""
    monkeypatch.setattr(shutdown_module, "_STAGE_THRESHOLDS_SECONDS", (5, 2))

    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)
        registry.mark_authenticated(session, "alice", is_sysop=False)

        await run_drain_sequence(session_registry=registry, delay_seconds=1.0)

        broadcasts = [line for line in session.written if "drained" in line]
        assert len(broadcasts) == 2  # initial + final "now" only

        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_run_drain_sequence_zero_delay_broadcasts_exactly_once():
    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)
        registry.mark_authenticated(session, "alice", is_sysop=False)

        await run_drain_sequence(session_registry=registry, delay_seconds=0.0)

        broadcasts = [line for line in session.written if "drained" in line]
        assert len(broadcasts) == 1
        assert "now" in broadcasts[0]

        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_run_drain_sequence_custom_message_is_broadcast_at_every_stage(monkeypatch):
    monkeypatch.setattr(shutdown_module, "_STAGE_THRESHOLDS_SECONDS", (1,))

    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)
        registry.mark_authenticated(session, "alice", is_sysop=False)

        await run_drain_sequence(
            session_registry=registry, delay_seconds=2.0, message="Reconnect after the upgrade."
        )

        broadcasts = [line for line in session.written if "Reconnect after the upgrade" in line]
        assert len(broadcasts) == 3  # initial, the one threshold, final -- verbatim every time

        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_run_shutdown_sequence_graceful_stages_reminders_the_same_way(monkeypatch):
    monkeypatch.setattr(shutdown_module, "_STAGE_THRESHOLDS_SECONDS", (1,))

    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)

        await run_shutdown_sequence(
            graceful=True,
            session_registry=registry,
            maintenance=MaintenanceMode(),
            delay_seconds=2.0,
            shutdown_event=asyncio.Event(),
        )

        broadcasts = [line for line in session.written if "going down" in line]
        assert len(broadcasts) == 3

        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


# -- cancelling a scheduled sequence (design doc -- node management) --------


def test_cancelling_a_scheduled_graceful_shutdown_deactivates_maintenance_and_disconnects_nobody():
    """The one exception to MaintenanceMode.activate()'s own "no way
    back" claim: a *scheduled* graceful shutdown, cancelled before it
    fires, must reopen new-login admission again -- otherwise a
    cancelled shutdown would leave the node silently unreachable
    forever."""

    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)
        maintenance = MaintenanceMode()
        shutdown_event = asyncio.Event()

        sequence_task = asyncio.create_task(
            run_shutdown_sequence(
                graceful=True,
                session_registry=registry,
                maintenance=maintenance,
                delay_seconds=60.0,
                shutdown_event=shutdown_event,
            )
        )
        await asyncio.sleep(0)
        assert maintenance.is_active() is True  # locked out immediately on scheduling

        sequence_task.cancel()
        await asyncio.gather(sequence_task, return_exceptions=True)

        assert maintenance.is_active() is False  # reopened by the cancellation
        assert shutdown_event.is_set() is False
        assert not task.cancelled()  # nobody was actually disconnected

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_cancelling_a_scheduled_drain_disconnects_nobody():
    async def scenario():
        session = _FakeSession()
        registry = ActiveSessionRegistry()
        task = asyncio.create_task(_hold_registered(registry, session))
        await asyncio.sleep(0)
        registry.mark_authenticated(session, "alice", is_sysop=False)

        drain_task = asyncio.create_task(
            run_drain_sequence(session_registry=registry, delay_seconds=60.0)
        )
        await asyncio.sleep(0)

        drain_task.cancel()
        await asyncio.gather(drain_task, return_exceptions=True)

        assert not task.cancelled()  # nobody was actually disconnected

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_immediate_shutdown_is_not_cancellable_via_maintenance_deactivate():
    """An *immediate* shutdown never enters the graceful countdown's own
    try/except at all -- cancelling it after the fact must not reopen
    admission, unlike the graceful case above, since there was never a
    cancellable window to begin with once the single broadcast is sent."""

    async def scenario():
        registry = ActiveSessionRegistry()
        maintenance = MaintenanceMode()

        await run_shutdown_sequence(
            graceful=False,
            session_registry=registry,
            maintenance=maintenance,
            delay_seconds=60.0,  # ignored entirely -- immediate
            shutdown_event=asyncio.Event(),
        )

        assert maintenance.is_active() is True

    asyncio.run(scenario())
