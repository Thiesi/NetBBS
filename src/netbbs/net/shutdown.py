"""
The deliberate node-shutdown sequence (design doc), the
`[D]rain` sequence (design doc §13.8) that borrows its session-
management shape without actually ending the node process, and
`NodeControls`, the bundle an in-session SysOp command needs to trigger
either (design doc -- node management).

Split out of `netbbs.__main__` into its own module for the same reason
`netbbs.net.session_registry`/`netbbs.net.maintenance` were themselves
split out: this is shared, not entry-point-specific.
Originally only the signal handler in `__main__.py` called this; the
in-session `[N]ode` admin menu (`netbbs.net.admin_flow`) now does too,
so it needed a home outside the top-level process-bootstrap module.

`SequenceScheduler` (design doc -- node management, Thiesi's own
dogfood-testing report) fixes a real bug the original single-shot
`asyncio.create_task(run_drain_sequence(...))`/`run_shutdown_sequence(
...)` calls had: nothing tracked "is one of these already scheduled",
so a SysOp running `[D]rain` (or `[S]hutdown`) twice launched two
independent, uncoordinated sequences racing each other -- a second,
shorter delay could disconnect everyone, and a reconnecting user would
then wait out whatever remained of the *first* delay too, with zero
visibility into any of it. One `SequenceScheduler` per sequence kind
(drain, shutdown -- `netbbs.__main__` constructs both, alongside
`session_registry`/`maintenance`) now tracks at most one in-flight
sequence: scheduling a new one always cancels-and-replaces any
existing one first, and its tracked deadline/message is exactly what
lets a SysOp re-running the command explicitly choose to cancel
instead, what `netbbs.net.login_flow` reads to warn an already-
connecting or freshly-logged-in user, and what a live session's own
main-menu prompt reads to show a visual indicator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.rendering import sanitize_text

# Design doc -- node management, Thiesi's own request: mirrors the
# Unix `shutdown` command's own staged-reminder convention (an initial
# broadcast, then again as the deadline gets close) rather than the
# original single "warn once, then silently wait" behavior. Seconds
# remaining at which a reminder fires -- only reached at all if the
# operator-chosen total delay is itself at least this long (a 30-second
# drain never gets a "5 minutes remaining" reminder it could never
# actually reach).
_STAGE_THRESHOLDS_SECONDS: tuple[int, ...] = (300, 60)


def format_remaining_seconds(seconds: float) -> str:
    """
    `M:SS`, floored at zero -- shared formatting for anything that shows
    a live countdown (the main-menu prompt's own status tag, the
    `[N]ode` admin menu's schedule status line, a freshly-connecting
    user's drain/shutdown notice). Deliberately not the phrase-based
    "`in 5 minutes`" wording `_countdown_phrase` below uses for
    broadcast text -- a compact, glanceable `M:SS` reads better packed
    into a prompt or a status line than a full sentence fragment would.
    """
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def _countdown_phrase(remaining_seconds: float) -> str:
    """
    The human phrase for one broadcast stage: `"now"`, `"in 5 minutes"`,
    `"in 1 minute"`, or `"in N seconds"` for the operator-chosen initial
    delay itself (an arbitrary value, never one of the two fixed
    thresholds) -- deliberately only special-cased for the exact values
    `_run_staged_countdown` below ever actually calls this with (`0`,
    `60`, `300`, and the caller's own initial delay), not a general
    unit-conversion formatter that would need to handle every possible
    remainder.

    The `<= 0` check runs against the *unrounded* value -- a genuinely
    positive but sub-second delay (a fast test's `delay_seconds=0.3`,
    say) must still read as a real, if brief, wait ("in 1 second"), not
    collapse to "now" just because rounding to a whole second would
    otherwise floor it to zero.
    """
    if remaining_seconds <= 0:
        return "now"
    seconds = int(round(remaining_seconds))
    if seconds == 300:
        return "in 5 minutes"
    if seconds == 60:
        return "in 1 minute"
    seconds = max(seconds, 1)
    return f"in {seconds} second{'s' if seconds != 1 else ''}"


async def _run_staged_countdown(
    session_registry: ActiveSessionRegistry,
    *,
    deadline: float,
    message: str | None,
    default_text: Callable[[str], str],
    exclude_sysops: bool,
) -> None:
    """
    Broadcasts now, then again at each of `_STAGE_THRESHOLDS_SECONDS`
    remaining (only the ones the total delay actually reaches), then
    once more right before the caller disconnects anyone -- the shared
    engine both `run_drain_sequence`/`run_shutdown_sequence` (graceful)
    stage their own broadcasts through, parameterized by `default_text`
    (given the countdown phrase, e.g. `"in 5 minutes"`, returns this
    sequence's own full broadcast sentence).

    A custom `message`, if given, is broadcast **verbatim at every
    stage** rather than varying with the remaining-time phrase --
    consistent with `message` already meaning "replaces the default
    text entirely" for a single broadcast (Thiesi's own wording, design
    doc), now just applied at more than one point in time rather than
    changing that meaning.

    Deadlines are `asyncio.get_running_loop().time()`-based (monotonic,
    process-local) -- nothing here needs to survive a restart or be
    compared across processes.

    Cancellable: this coroutine is always run inside a task the caller
    may `.cancel()` at any point before it returns (a SysOp re-running
    the same command, via `SequenceScheduler.schedule`/`.cancel`) --
    `CancelledError` propagates normally through whichever broadcast or
    `asyncio.sleep` it's currently paused on. Nothing here has an
    irreversible side effect to unwind on the way out; the caller's own
    disconnect step, which only ever runs *after* this returns
    normally, is where "no longer safely cancellable" begins.
    """
    loop = asyncio.get_running_loop()

    async def _broadcast(remaining_seconds: float) -> None:
        text = (
            f"\r\n*** {sanitize_text(message)} ***"
            if message is not None
            else default_text(_countdown_phrase(remaining_seconds))
        )
        await session_registry.broadcast_to_all(text, exclude_sysops=exclude_sysops)

    initial_remaining = deadline - loop.time()
    await _broadcast(initial_remaining)
    if initial_remaining <= 0:
        return  # zero/near-zero delay -- one broadcast is the whole sequence

    for threshold in _STAGE_THRESHOLDS_SECONDS:
        remaining = deadline - loop.time()
        if remaining > threshold:
            await asyncio.sleep(remaining - threshold)
            await _broadcast(threshold)

    remaining = deadline - loop.time()
    if remaining > 0:
        await asyncio.sleep(remaining)
    await _broadcast(0)


@dataclass
class _ScheduledSequence:
    task: asyncio.Task
    deadline: float
    message: str | None
    # Issue #108: which authority created this sequence, and whether the
    # in-BBS SysOp UI may cancel/replace it. Both default to the shape
    # every existing caller (SysOp `[D]rain`/`[S]hutdown`) already had --
    # only `netbbs.__main__._install_signal_handlers` passes anything
    # else, for a signal-triggered shutdown.
    source: str = "sysop"
    cancellable: bool = True


class SequenceScheduler:
    """
    Tracks at most one currently in-flight drain or shutdown sequence
    (design doc -- node management). One instance per node *per
    sequence kind* -- `netbbs.__main__` constructs two (one for drain,
    one for shutdown), alongside `session_registry`/`maintenance`, and
    threads both down through `handle_session`/`handle_ssh_session` the
    same way.

    This is the fix for the stacking bug Thiesi's own dogfood testing
    found: `schedule()` always cancels-and-replaces any existing
    sequence first, so a SysOp re-running `[D]rain`/`[S]hutdown` can
    never end up with two independent, uncoordinated countdowns running
    at once. It's also the one piece of state that makes several other
    asks possible for free once it exists: `netbbs.net.admin_flow` reads
    it to offer an explicit cancel choice and to show the `[N]ode` menu's
    own schedule status; `netbbs.net.login_flow` reads it to warn a
    freshly-connecting or freshly-logged-in user; a live session's main-
    menu prompt reads it for its own visual indicator.

    Deadlines are `asyncio.get_running_loop().time()`-based (monotonic,
    process-local) -- there is nothing here that needs to survive a
    restart or be compared across processes, so wall-clock/timezone
    concerns never enter this class at all. `is_scheduled()` needs no
    running loop (`asyncio.Task.done()` doesn't either); `schedule()`/
    `cancel()` are plain synchronous methods -- `Task.cancel()` only
    *requests* cancellation, it doesn't block waiting for it to take
    effect, so nothing here needs to be a coroutine itself.

    **Issue #108: `schedule()` and `cancel()` are deliberately asymmetric
    about cancellability.** `schedule()` stays unconditional -- it always
    cancels-and-replaces whatever is currently tracked, regardless of
    that existing sequence's own `cancellable` flag, because the *signal
    handler* path needs to keep working exactly this way too (a second
    SIGTERM must still be able to reset an in-flight SIGTERM countdown).
    `cancel()`, by contrast, is specifically the SysOp-facing "cancel it?"
    action (see its own docstring) and refuses outright for a non-
    cancellable sequence. Closing the loophole this leaves -- a SysOp
    who can't explicitly cancel a signal-triggered shutdown otherwise
    silently *replacing* it by scheduling a new one -- is therefore the
    caller's job: `netbbs.net.admin_flow._shutdown_screen` checks
    `is_cancellable()` itself before ever reaching its own `schedule()`
    call, refusing to offer a new shutdown at all while a non-cancellable
    one is in flight, rather than this class refusing on its behalf.
    """

    def __init__(self) -> None:
        self._current: _ScheduledSequence | None = None

    def is_scheduled(self) -> bool:
        return self._current is not None and not self._current.task.done()

    def is_cancellable(self) -> bool:
        """Whether the SysOp-facing "cancel it?"/replace path may act on
        the currently-scheduled sequence right now (issue #108). `True`
        when nothing is scheduled at all (nothing to refuse), or when
        what's scheduled was itself SysOp-created. `False` for an
        externally triggered shutdown (`source="sigterm"`/`"sigint"`) --
        the service supervisor's own authority to stop this process
        outranks an in-BBS choice to keep it running."""
        return not self.is_scheduled() or self._current.cancellable

    def source(self) -> str | None:
        """Provenance of the currently-scheduled sequence ("sysop",
        "sigterm", "sigint"), or `None` if nothing is scheduled -- lets a
        status screen explain *why* cancellation is refused, not merely
        that it is."""
        return self._current.source if self.is_scheduled() else None

    def remaining_seconds(self) -> float | None:
        """`None` if nothing is currently scheduled -- never a negative
        number otherwise, even if queried in the narrow window after the
        deadline has technically passed but the task hasn't finished
        disconnecting everyone yet."""
        if not self.is_scheduled():
            return None
        loop = asyncio.get_running_loop()
        return max(0.0, self._current.deadline - loop.time())

    def message(self) -> str | None:
        return self._current.message if self.is_scheduled() else None

    def schedule(
        self,
        task: asyncio.Task,
        *,
        deadline: float,
        message: str | None,
        source: str = "sysop",
        cancellable: bool = True,
    ) -> None:
        """Registers `task` (already created by the caller via
        `asyncio.create_task`) as the currently-scheduled sequence,
        cancelling and discarding any existing one first -- the actual
        fix for the stacking bug this class exists for: a second call
        always fully replaces the first, never runs alongside it.

        This replacement is deliberately unconditional regardless of the
        outgoing sequence's own `cancellable` flag -- see this class's
        own docstring for why (the signal-handler path must always be
        able to reassert itself). `source`/`cancellable` (issue #108)
        default to the shape every pre-existing caller already has."""
        self._cancel_unconditionally()
        self._current = _ScheduledSequence(
            task=task, deadline=deadline, message=message, source=source, cancellable=cancellable
        )

    def cancel(self) -> bool:
        """Cancels the currently-scheduled sequence, if any *and if it is
        cancellable* (issue #108, `is_cancellable()`) -- callers offering
        a SysOp an explicit "cancel it?" choice use this to confirm
        something really was there to cancel. Returns `False` without
        cancelling anything for a non-cancellable (externally triggered)
        sequence, same as when nothing is scheduled at all."""
        if not self.is_scheduled() or not self._current.cancellable:
            return False
        return self._cancel_unconditionally()

    def _cancel_unconditionally(self) -> bool:
        """Shared by the public `cancel()` (gated on `is_cancellable()`)
        and `schedule()`'s own unconditional replace-any-existing-
        sequence step (never gated -- see that method's own docstring)."""
        if not self.is_scheduled():
            return False
        self._current.task.cancel()
        self._current = None
        return True


@dataclass(frozen=True)
class NodeControls:
    """Everything an in-session node-management command needs, bundled
    as one optional parameter rather than six separate ones threaded
    through `netbbs.net.login_flow`'s whole call chain down to
    `netbbs.net.admin_flow.admin_menu`. `drain_scheduler`/`shutdown_
    scheduler` default to a fresh, empty `SequenceScheduler` each --
    every existing test constructing `NodeControls` directly (none of
    which exercise scheduling) needs no changes; `netbbs.__main__.run()`
    is the only caller that passes its own node-lifetime real ones. The
    optional configured identity path also gives live SysOp backup creation
    the same degrade-gracefully shape as the other live-only controls."""

    session_registry: ActiveSessionRegistry
    maintenance: MaintenanceMode
    shutdown_event: asyncio.Event
    graceful_delay_seconds: float
    drain_scheduler: SequenceScheduler = field(default_factory=SequenceScheduler)
    shutdown_scheduler: SequenceScheduler = field(default_factory=SequenceScheduler)
    # Issue #275: the node's one MRC bridge (`netbbs.mrc.bridge.
    # MrcBridge`) -- live in-memory node state that only a running node
    # has, `None` from the standalone `python -m netbbs.admin` CLI,
    # exactly like `session_registry`. Typed `Any` so this module (the
    # bottom of the `netbbs.net` import graph) never imports the chat
    # stack; the real type is annotated at every consumer.
    mrc_bridge: Any = None
    # Issue #277: the node's one in-process `netbbs.chat.hub.ChatHub` --
    # live channel membership only a running node has, `None` from the
    # standalone `python -m netbbs.admin` CLI, exactly like
    # `session_registry`. Typed `Any` so this module (the bottom of the
    # `netbbs.net` import graph) never imports the chat stack; the real
    # type is annotated at every consumer.
    chat_hub: Any = None
    # The effective identity directory from NodeConfig.  The live SysOp
    # Backup screen needs the configured path so a custom identity location
    # cannot be silently omitted.  None keeps standalone admin and direct
    # test callers status-only; the database path comes from their existing
    # DatabaseLane rather than being duplicated here.
    backup_identity_dir: Path | None = None


async def run_shutdown_sequence(
    *,
    graceful: bool,
    session_registry: ActiveSessionRegistry,
    maintenance: MaintenanceMode,
    delay_seconds: float,
    shutdown_event: asyncio.Event,
    message: str | None = None,
) -> None:
    """
    What a signal -- or the in-session `[N]ode` admin menu -- actually
    triggers (design doc) — locks out new logins, warns everyone
    already connected (regardless of what screen they're on, or even
    whether they've logged in yet — see `netbbs.net.session_registry.
    ActiveSessionRegistry`), then (a *graceful* request only) gives them
    a staged countdown to notice before forcibly disconnecting; an
    *immediate* request skips straight to disconnecting. Either way,
    `shutdown_event` is set last, so `run()`'s existing `finally: server.
    stop() -> db.close()` sequence only ever runs once every session is
    already gone.

    `delay_seconds` (design doc -- node management): now a genuine
    per-invocation choice, the same shape `run_drain_sequence`'s own
    `delay_seconds` already had -- unifies the two commands' UX (Thiesi's
    own request) rather than shutdown alone being pinned to a fixed
    config default with no override. Ignored entirely when `graceful`
    is `False` -- an immediate shutdown has no countdown to schedule at
    any length.

    `message`, if given, *replaces* the default "going down in N
    seconds"/"going down now" text entirely rather than appending to it
    (design doc -- node management, Thiesi's own wording) —
    sanitized, since it's free text a SysOp typed, same discipline as
    anything else reaching the terminal. Broadcast verbatim at every
    stage of a graceful countdown (see `_run_staged_countdown`'s own
    docstring), not just the first.

    Cancellable while graceful and still counting down (design doc --
    node management): if the caller's own task is cancelled before the
    countdown finishes (a SysOp cancelling a scheduled shutdown via
    `SequenceScheduler`), `maintenance.deactivate()` reopens new-login
    admission before re-raising -- a cancelled shutdown must not leave
    the node silently unreachable forever. Once the countdown finishes
    normally and `disconnect_all()` is reached, there is no more turning
    back, matching `MaintenanceMode.activate()`'s own documented "no way
    back" claim from that point onward.

    Callers triggering this from *within* a live session (the admin
    menu) must not `await` it inline from that session's own call
    stack: the calling session's own task is one of the ones
    `disconnect_all()` cancels, and a task cancelling itself while
    awaiting a `gather()` that includes itself is the same species of
    hazard already hit and fixed elsewhere
    (`netbbs.net.chat_flow._chat_loop`). Fire this as an independent
    background task (`asyncio.create_task`) instead, exactly as the
    signal-handler path in `netbbs.__main__` already does — the calling
    session then just gets cancelled from *outside*, the same
    already-proven-safe shape every other connected session goes
    through.
    """
    maintenance.activate()
    if graceful:
        deadline = asyncio.get_running_loop().time() + delay_seconds
        try:
            await _run_staged_countdown(
                session_registry,
                deadline=deadline,
                message=message,
                default_text=lambda phrase: f"\r\n*** This node is going down {phrase}. ***",
                exclude_sysops=False,
            )
        except asyncio.CancelledError:
            maintenance.deactivate()
            raise
    else:
        text = f"\r\n*** {sanitize_text(message)} ***" if message is not None else "\r\n*** This node is going down now. ***"
        await session_registry.broadcast_to_all(text)
    await session_registry.disconnect_all()
    shutdown_event.set()


async def run_drain_sequence(
    *, session_registry: ActiveSessionRegistry, delay_seconds: float, message: str | None = None
) -> None:
    """
    Warn every non-SysOp connected session (regardless of what screen
    it's on, same as `run_shutdown_sequence`), with the same staged-
    countdown broadcasts (see `_run_staged_countdown`), then disconnect
    them -- design doc §13.8's `[D]rain`, the piece that lets a SysOp
    clear ordinary users off the node for a change that needs a
    reconnect to take effect, without shutting the node down at all:
    unlike `run_shutdown_sequence`, this never touches `maintenance`/
    `shutdown_event`, and `exclude_sysops=True` on both calls below
    means a SysOp session (including the one that issued this command)
    is never warned or disconnected by it.

    Deliberately doesn't also enable maintenance-mode lockdown itself --
    the two are meant to be composed explicitly by the SysOp (design doc
    §13.8's own two-step workflow: turn on `[M]aintenance mode` first if
    new non-SysOp logins should be blocked too, then `[D]rain` if anyone
    already connected also needs to go), not silently implied by each
    other. `netbbs.net.admin_flow._drain_screen` says so explicitly, the
    same way its own `[M]aintenance mode` screen already explains its own
    limits.

    `message`, if given, replaces the default text entirely at every
    stage, same reasoning and same sanitization as
    `run_shutdown_sequence`'s own `message` parameter.

    Cancellable at any point before it returns (a SysOp re-running
    `[D]rain`) with zero cleanup needed -- unlike shutdown, drain never
    flips any flag that would need undoing on cancellation, so a bare
    `CancelledError` propagating straight out is already fully safe.

    Callers triggering this from within a live session must fire it as
    an independent background task (`asyncio.create_task`), never
    `await`ed inline -- identical self-referential-cancellation hazard
    `run_shutdown_sequence`'s own docstring explains, since
    `disconnect_all` here can still reach the calling session's own
    task if it somehow isn't SysOp-attributed; the admin screen that
    triggers this is always a real SysOp session, so in practice this
    is defense-in-depth, not a hazard this specific caller can hit.
    """
    deadline = asyncio.get_running_loop().time() + delay_seconds
    await _run_staged_countdown(
        session_registry,
        deadline=deadline,
        message=message,
        default_text=lambda phrase: (
            f"\r\n*** This node is being drained for maintenance. You will be disconnected {phrase}. ***"
        ),
        exclude_sysops=True,
    )
    await session_registry.disconnect_all(exclude_sysops=True)
