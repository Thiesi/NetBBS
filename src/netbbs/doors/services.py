"""One supervised long-lived companion process per door (issue #466).

Deliberately not a general process manager: at most one service per door,
started with the node or on the first caller, restarted with bounded backoff
behind a circuit breaker, and stopped by a bounded SIGTERM/SIGKILL sequence.

Every wait here has a ceiling. PRs #228 and #283 both traced a roughly
nine-minute shutdown to a cleanup step which cancelled something and then
awaited it without one, so nothing in `stop_all` may wait on a process
agreeing to exit -- it waits on a deadline and then kills.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from netbbs.doors.runtime import DOOR_MAX_PROCESSES, _finish_owned

_logger = logging.getLogger(__name__)

_BACKOFF_START_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 60.0
#: Enough restarts inside this window and the door is misconfigured rather
#: than unlucky; stop burning CPU respawning it and tell the SysOp instead.
_CIRCUIT_FAILURES = 5
_CIRCUIT_WINDOW_SECONDS = 300.0
_HEALTH_TIMEOUT_SECONDS = 2.0
#: How long a freshly started service must stay up before a caller is let in.
#: Short enough to be invisible on a healthy door, long enough that a service
#: which exits on startup is never mistaken for a working one.
_SETTLE_SECONDS = 0.25
#: A process which survives SIGKILL is stuck in the kernel. Waiting longer
#: cannot help, and shutdown must not stall behind it.
_KILL_DEADLINE_SECONDS = 5.0
_DIAGNOSTIC_BYTES = 8192

STOPPED, RUNNING, BACKOFF, FAILED = "stopped", "running", "backoff", "failed"


@dataclass(frozen=True)
class ServiceSpec:
    argv: tuple[str, ...]
    start: str
    stop_grace_seconds: int
    memory_mb: int
    health_kind: str
    health_path: str


def service_spec(profile) -> ServiceSpec | None:
    """Parse an already-validated profile's service block, or None."""
    if profile is None or not profile.service:
        return None
    service = profile.service
    install = str(Path(profile.install_dir).resolve())
    substitutions = {"install_dir": install}
    health = service.get("health", {})
    path = health.get("path", "").format_map(substitutions) if health else ""
    if path and not Path(path).is_absolute():
        # The service creates it relative to its own cwd, which is the
        # installation directory -- not NetBBS's working directory, which is
        # where `healthy()` would otherwise look and never find it.
        path = str(Path(install) / path)
    return ServiceSpec(
        argv=tuple(argument.format_map(substitutions) for argument in service["argv"]),
        start=service.get("start", "with_node"),
        stop_grace_seconds=service.get("stop_grace_seconds", 10),
        memory_mb=service.get("service_memory_mb", 512),
        health_kind=health.get("kind", "pid") if health else "pid",
        health_path=path,
    )


def launch_identity(door, spec: ServiceSpec | None):
    """Everything which decides how a service is launched, comparable as a key."""
    if spec is None:
        return None
    return (spec, door.executable_path, door.profile.install_dir,
            tuple(sorted(door.profile.environment.items())))


@dataclass
class ServiceStatus:
    """What the SysOp screens show; a snapshot, never the live object."""
    state: str = STOPPED
    since: float | None = None
    restarts: int = 0
    last_exit_code: int | None = None
    diagnostic: str = ""

    def uptime_seconds(self) -> float | None:
        return None if self.since is None or self.state != RUNNING else time.monotonic() - self.since

    def summary(self) -> str:
        if self.state == RUNNING:
            seconds = int(self.uptime_seconds() or 0)
            return f"running, up {seconds // 3600}h{seconds % 3600 // 60:02d}m, {self.restarts} restart(s)"
        if self.state == BACKOFF:
            return f"restarting after exit code {self.last_exit_code}, {self.restarts} restart(s)"
        if self.state == FAILED:
            return f"stopped after repeated failures (last exit code {self.last_exit_code})"
        return "not running"


class DoorService:
    """The supervisor for one door's companion process."""

    def __init__(self, door, spec: ServiceSpec):
        self.door_id, self.door_name, self.spec = door.id, door.name, spec
        self.executable = door.executable_path
        self.install_dir = Path(door.profile.install_dir)
        self.environment = dict(door.profile.environment)
        #: Everything this supervisor captured at construction. The service
        #: block alone is not the identity: a SysOp who changes only the
        #: executable, installation directory or environment would otherwise
        #: keep a supervisor which relaunches the old ones, even on Restart.
        self.identity = launch_identity(door, spec)
        self.status = ServiceStatus()
        self._proc = None
        self._task = None
        self._diagnostics = None
        self._stopping = False
        self._tail = bytearray()
        self._failures: deque[float] = deque()
        #: Serialises start/stop/restart. The manager's adoption lock does not
        #: cover these: a SysOp's Halt racing a caller's lazy start would
        #: otherwise let one kill the other's process, or leave a replacement
        #: running that nobody is stopping.
        self._lifecycle = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Begin supervising. Returns once the supervisor task exists."""
        async with self._lifecycle:
            self._start_locked()

    def _start_locked(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._failures.clear()
        self._task = asyncio.create_task(self._supervise(), name=f"door-service-{self.door_id}")
        self._task.add_done_callback(self._supervisor_finished)

    def _supervisor_finished(self, task) -> None:
        """Never let a supervisor die with its failure unretrieved."""
        if task.cancelled():
            return
        exception = task.exception()
        if exception is None:
            return
        self.status.state = FAILED
        self._note(f"supervisor failed: {exception}")
        _logger.error("door service %r supervisor failed", self.door_name, exc_info=exception)

    async def wait_until_running(self, timeout: float) -> bool:
        """Wait for the process to be up, giving up early once it cannot be.

        Polled rather than awaiting the event alone so a service already in its
        backoff or circuit-broken state answers the caller immediately instead
        of making them wait out the whole timeout for a certain "no".
        """
        deadline = time.monotonic() + timeout
        while True:
            # Up, and up long enough to mean it. A service which dies on
            # startup passes briefly through RUNNING on every restart, and a
            # caller who arrives inside that window would otherwise be let
            # through to a door whose companion process is already gone.
            if self.status.state == RUNNING and (self.status.uptime_seconds() or 0) >= _SETTLE_SECONDS:
                return True
            if self.status.state == FAILED or self._task is None or self._task.done():
                return False
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        """Stop supervising and the process, within a bounded deadline."""
        async with self._lifecycle:
            await self._protected(self._stop_locked())

    async def restart(self) -> None:
        """Stop then start again, under one lock so nothing interleaves."""
        async with self._lifecycle:
            await self._protected(self._restart_locked())

    async def _protected(self, coroutine) -> None:
        """Run a lifecycle transition to completion even while cancelled.

        The whole sequence, not only the termination at its end: a cancel
        landing on the supervisor-cancellation gather would otherwise leave
        `stop()` with the process still live, and by then the manager may
        already have dropped this service, so nothing could reach it again.
        """
        _, cancelled = await _finish_owned(asyncio.create_task(coroutine))
        if cancelled:
            raise asyncio.CancelledError

    async def _stop_locked(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            # Cancelling only unblocks the supervisor's own await; the child is
            # still ours to end, below.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._terminate()
        self.status.state = STOPPED
        self.status.since = None

    async def _restart_locked(self) -> None:
        await self._stop_locked()
        self.status.restarts = 0
        self._start_locked()

    # -- supervision -------------------------------------------------------

    async def _supervise(self) -> None:
        delay = _BACKOFF_START_SECONDS
        while not self._stopping:
            try:
                started = await self._spawn()
            except OSError as exc:
                self._note(f"could not start: {exc}")
                exit_code = None
            else:
                self.status.state, self.status.since = RUNNING, time.monotonic()
                exit_code = await started
                self.status.last_exit_code = exit_code
                self._note(f"exited with code {exit_code}")
                # The leader is reaped, but anything it left in its own process
                # group is not. Without this, every crash strands another group
                # which the next spawn's `_proc` can no longer reach and which
                # `stop_all` would never see.
                await self._reap_group()
            if self._stopping:
                return
            now = time.monotonic()
            aged_out = bool(self._failures) and now - self._failures[0] > _CIRCUIT_WINDOW_SECONDS
            self._failures.append(now)
            while self._failures and now - self._failures[0] > _CIRCUIT_WINDOW_SECONDS:
                self._failures.popleft()
            if aged_out:
                # A service which ran stably for the whole window is not
                # respawning rapidly, so it should not inherit the previous
                # incident's minute-long wait for one isolated crash.
                delay = _BACKOFF_START_SECONDS
            if len(self._failures) >= _CIRCUIT_FAILURES:
                self.status.state = FAILED
                _logger.error("door service %r failed %d times in %d seconds; not restarting it again",
                              self.door_name, len(self._failures), int(_CIRCUIT_WINDOW_SECONDS))
                return
            self.status.state = BACKOFF
            self.status.restarts += 1
            _logger.warning("door service %r exited (%s); restarting in %.0fs",
                            self.door_name, exit_code, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _BACKOFF_CAP_SECONDS)

    async def _spawn(self):
        """Launch the process; returns an awaitable for its exit code."""
        argv = [self.executable, *self.spec.argv]
        if os.name == "posix":
            # RLIMIT_CPU is explicitly removed rather than left unset: a service
            # is long-lived by definition, so accrued CPU seconds are not the
            # runaway signal they are for one caller's run -- and omitting the
            # key would inherit whatever soft limit NetBBS itself runs under,
            # eventually killing the service and tripping its circuit breaker.
            setup = {"pty": False, "limits": {"RLIMIT_AS": self.spec.memory_mb * 1024 * 1024,
                                              "RLIMIT_NPROC": DOOR_MAX_PROCESSES,
                                              "RLIMIT_CPU": None}}
            argv = [sys.executable, "-I", str(Path(__file__).with_name("launcher.py")), json.dumps(setup), *argv]
        # Cancellation between fork and assignment would leave a live process
        # nobody owns, so the spawn is finished even while being cancelled.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, cwd=str(self.install_dir), env=self._environment(),
            **({"start_new_session": True} if os.name == "posix" else {})))
        proc, cancelled = await _finish_owned(spawn)
        self._proc = proc
        self._diagnostics = asyncio.create_task(self._collect_diagnostics(proc))
        if cancelled:
            raise asyncio.CancelledError
        return self._wait(proc)

    def _environment(self) -> dict:
        """The same narrow rules as a door launch: never the parent environment."""
        env = {"NETBBS_DOOR_SERVICE": "1"}
        try:
            env["USERPROFILE" if os.name == "nt" else "HOME"] = str(Path.home())
        except RuntimeError:
            pass
        env.update(self.environment)
        env.setdefault("TERM", "dumb")
        return env

    async def _wait(self, proc) -> int | None:
        # Process.wait() can also wait for PIPE closure, which a descendant may
        # hold open long after the leader is gone; the child watcher sets
        # returncode independently, so watch that instead.
        while proc.returncode is None:
            await asyncio.sleep(0.05)
        return proc.returncode

    async def _collect_diagnostics(self, proc) -> None:
        try:
            while chunk := await proc.stderr.read(4096):
                self._tail.extend(chunk)
                del self._tail[:-_DIAGNOSTIC_BYTES]
                # Published as it arrives, not only on exit: a service which is
                # running but wedged is exactly when a SysOp opens its log.
                self._publish()
        except (OSError, ValueError):
            pass

    def _publish(self) -> None:
        self.status.diagnostic = bytes(self._tail).decode("utf-8", errors="replace")

    def _note(self, text: str) -> None:
        self._tail.extend(f"[netbbs] {text}\n".encode("utf-8", errors="replace"))
        del self._tail[:-_DIAGNOSTIC_BYTES]
        self._publish()

    async def _reap_group(self) -> None:
        """End whatever the exited leader left behind in its process group.

        Only meaningful on POSIX, where the service was given its own session,
        so the group id is the leader's pid. There is nothing left to wait on
        once the leader is reaped, so this is a bounded signal pair rather than
        a wait: anything still running was never supervised in its own right.
        """
        proc = self._proc
        if proc is None or os.name != "posix":
            self._proc = None
            return
        self._signal(proc, signal.SIGTERM)
        try:
            await asyncio.sleep(min(self.spec.stop_grace_seconds, 1))
        except asyncio.CancelledError:
            # Ownership is kept on purpose: `_terminate` still owes this group
            # the SIGKILL the cancel just skipped, and it can only send it
            # while `_proc` still names the group.
            raise
        self._signal(proc, signal.SIGKILL, kill=True)
        self._proc = None

    async def _terminate(self) -> None:
        # Ownership is released only once the process is actually gone, and the
        # stderr reader stays alive until then: a service writing more than a
        # pipe holds while handling SIGTERM would otherwise block in that write,
        # never finish its graceful flush, and be SIGKILLed with its own game
        # state half-written.
        proc = self._proc
        if proc is None:
            await self._stop_reader()
            return
        if proc.returncode is None:
            self._signal(proc, signal.SIGTERM if os.name == "posix" else None)
            try:
                await asyncio.wait_for(self._wait(proc), timeout=self.spec.stop_grace_seconds)
            except asyncio.TimeoutError:
                _logger.warning("door service %r ignored SIGTERM for %ds; killing it",
                                self.door_name, self.spec.stop_grace_seconds)
                self._signal(proc, signal.SIGKILL if os.name == "posix" else None, kill=True)
                try:
                    await asyncio.wait_for(self._wait(proc), timeout=_KILL_DEADLINE_SECONDS)
                except asyncio.TimeoutError:
                    # Unkillable means stuck in the kernel. Leaving it behind is
                    # worse than a stalled shutdown is; say so loudly, keep
                    # ownership so a later attempt can still find it, and go on.
                    _logger.error("door service %r survived SIGKILL; abandoning it so shutdown can finish",
                                  self.door_name)
                    await self._stop_reader()
                    return
        # The leader is gone, gracefully or not. Its group may still hold
        # descendants which ignored the same SIGTERM, so they are always reaped
        # -- including any a cancelled `_reap_group` never got to SIGKILL.
        await self._reap_group()
        await self._stop_reader()

    async def _stop_reader(self) -> None:
        reader, self._diagnostics = self._diagnostics, None
        if reader is None:
            return
        reader.cancel()
        # Gathered, not merely cancelled: an exception raised outside
        # `_collect_diagnostics`' own narrow catches would otherwise go
        # unretrieved, and stop/restart would report completion while the
        # task it owned was still unwinding.
        await asyncio.gather(reader, return_exceptions=True)

    def _signal(self, proc, number, *, kill=False) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, number)
            elif kill:
                proc.kill()
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # -- health ------------------------------------------------------------

    async def healthy(self) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None or self.status.state != RUNNING:
            return False
        if self.spec.health_kind != "socket":
            return True
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.spec.health_path), timeout=_HEALTH_TIMEOUT_SECONDS)
        except NotImplementedError:
            # The platform has no Unix sockets at all -- Windows development.
            # `asyncio.open_unix_connection` still exists there, so this is
            # where that is actually discovered; fall back to the pid check,
            # rather than reporting every live service as unhealthy.
            return True
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass
        return True


class DoorServiceManager:
    """Every door service this node owns; one per door at most."""

    def __init__(self):
        self._services: dict[int, DoorService] = {}
        #: One per door. `adopt` awaits a stop in the middle of replacing a
        #: supervisor, and two callers reconciling the same edited profile
        #: across that await would otherwise each install one -- leaving a
        #: companion nobody tracks, and two servers on one game's state.
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, door_id: int) -> DoorService | None:
        return self._services.get(door_id)

    def status(self, door_id: int) -> ServiceStatus | None:
        service = self._services.get(door_id)
        return service.status if service else None

    async def adopt(self, door) -> DoorService | None:
        """Register (without starting) the service a door's profile declares.

        A profile the SysOp has since edited produces a different spec, and the
        superseded supervisor is stopped before being replaced -- dropping it
        from the map alone would leave its process running with nothing owning
        it and nothing able to stop it again.
        """
        lock = self._locks.setdefault(door.id, asyncio.Lock())
        async with lock:
            spec = service_spec(door.profile)
            existing = self._services.get(door.id)
            if existing is not None and existing.identity == launch_identity(door, spec):
                # A rename does not change how the service launches, so the
                # supervisor is kept -- but every later exit, restart and
                # shutdown diagnostic names the door, and that name may since
                # have been given to a different one.
                existing.door_name = door.name
                return existing
            if existing is not None:
                del self._services[door.id]
                await existing.stop()
            if spec is None:
                return None
            service = DoorService(door, spec)
            self._services[door.id] = service
            return service

    async def start_node_services(self, doors) -> None:
        """Start everything declared `with_node`; a failure never blocks startup."""
        for door in doors:
            service = await self.adopt(door)
            if service is not None and service.spec.start == "with_node":
                await service.start()

    async def ensure_running(self, door, *, wait_seconds: float = 10.0) -> str | None:
        """Make a door's service available, returning a caller-facing problem.

        `on_first_caller` services are started here; a `with_node` one which
        died is not silently revived, because its supervisor already decided.
        """
        # Always reconcile against the door as it is now. Trusting a cached
        # entry would gate callers on a profile the SysOp has since edited, and
        # a service they removed would keep running with its controls hidden.
        service = await self.adopt(door)
        if service is None:
            return None
        problem = None
        if service.status.state == FAILED:
            problem = f"{door.name}'s service has stopped after repeated failures. Ask the SysOp to check it."
        else:
            if service.spec.start == "on_first_caller" and service.status.state == STOPPED:
                await service.start()
            deadline = time.monotonic() + wait_seconds
            if not await service.wait_until_running(wait_seconds):
                problem = f"{door.name}'s service is not running. Ask the SysOp to start it."
            else:
                # Health gets the rest of the same readiness budget, not one
                # probe: a server which binds its socket a second after it
                # starts is starting normally, not failing.
                while not await service.healthy():
                    if time.monotonic() >= deadline:
                        problem = f"{door.name}'s service is not answering. Ask the SysOp to check it."
                        break
                    await asyncio.sleep(0.1)
        if problem is not None:
            _logger.warning("refused to launch door %r: service state %s", door.name, service.status.state)
        return problem

    async def forget(self, door_id: int) -> None:
        """Stop and drop one door's service, for a door which no longer exists."""
        lock = self._locks.setdefault(door_id, asyncio.Lock())
        async with lock:
            service = self._services.pop(door_id, None)
        if service is not None:
            await service.stop()

    async def stop_all(self) -> None:
        """Stop every service concurrently, each within its own deadline."""
        services, self._services = list(self._services.values()), {}
        if services:
            await asyncio.gather(*(service.stop() for service in services), return_exceptions=True)
