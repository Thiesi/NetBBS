"""
The deliberate node-shutdown sequence (design doc round 51), and
`NodeControls`, the bundle an in-session SysOp command needs to trigger
it (design doc -- node management round).

Split out of `netbbs.__main__` into its own module for the same reason
`netbbs.net.session_registry`/`netbbs.net.maintenance` were themselves
split out in round 51: this is shared, not entry-point-specific.
Originally only the signal handler in `__main__.py` called this; the
in-session `[N]ode` admin menu (`netbbs.net.admin_flow`) now does too,
so it needed a home outside the top-level process-bootstrap module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.rendering import sanitize_text


@dataclass(frozen=True)
class NodeControls:
    """Everything an in-session node-management command needs, bundled
    as one optional parameter rather than four separate ones threaded
    through `netbbs.net.login_flow`'s whole call chain down to
    `netbbs.net.admin_flow.admin_menu`."""

    session_registry: ActiveSessionRegistry
    maintenance: MaintenanceMode
    shutdown_event: asyncio.Event
    graceful_delay_seconds: float


async def run_shutdown_sequence(
    *,
    graceful: bool,
    session_registry: ActiveSessionRegistry,
    maintenance: MaintenanceMode,
    graceful_delay_seconds: float,
    shutdown_event: asyncio.Event,
    message: str | None = None,
) -> None:
    """
    What a signal -- or now, the in-session `[N]ode` admin menu --
    actually triggers (design doc round 51) — locks out new logins,
    warns everyone already connected (regardless of what screen they're
    on, or even whether they've logged in yet — see
    `netbbs.net.session_registry.ActiveSessionRegistry`), then (a
    *graceful* request only) gives them `graceful_delay_seconds` to
    notice before forcibly disconnecting; an *immediate* request skips
    straight to disconnecting. Either way, `shutdown_event` is set last,
    so `run()`'s existing `finally: server.stop() -> db.close()`
    sequence only ever runs once every session is already gone.

    `message`, if given, *replaces* the default "going down in N
    seconds"/"going down now" text entirely rather than appending to it
    (design doc -- node management round, Thiesi's own wording) —
    sanitized, since it's free text a SysOp typed, same discipline as
    anything else reaching the terminal.

    Callers triggering this from *within* a live session (the admin
    menu) must not `await` it inline from that session's own call
    stack: the calling session's own task is one of the ones
    `disconnect_all()` cancels, and a task cancelling itself while
    awaiting a `gather()` that includes itself is the same species of
    hazard design doc round 58 already hit and fixed elsewhere
    (`netbbs.net.chat_flow._chat_loop`). Fire this as an independent
    background task (`asyncio.create_task`) instead, exactly as the
    signal-handler path in `netbbs.__main__` already does — the calling
    session then just gets cancelled from *outside*, the same
    already-proven-safe shape every other connected session goes
    through.
    """
    maintenance.activate()
    if message is not None:
        text = f"\r\n*** {sanitize_text(message)} ***"
    elif graceful:
        text = f"\r\n*** This node is going down in {int(graceful_delay_seconds)} seconds. ***"
    else:
        text = "\r\n*** This node is going down now. ***"
    await session_registry.broadcast_to_all(text)
    if graceful:
        await asyncio.sleep(graceful_delay_seconds)
    await session_registry.disconnect_all()
    shutdown_event.set()
