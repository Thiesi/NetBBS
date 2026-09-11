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
    return ServiceSpec(
        argv=tuple(argument.format_map(substitutions) for argument in service["argv"]),
        start=service.get("start", "with_node"),
        stop_grace_seconds=service.get("stop_grace_seconds", 10),
        memory_mb=service.get("service_memory_mb", 512),
        health_kind=health.get("kind", "pid") if health else "pid",
        health_path=health.get("path", "").format_map(substitutions) if health else "",
    )


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
        self.status = ServiceStatus()
        self._proc = None
        self._task = None
        self._diagnostics = None
        self._stopping = False
        self._tail = bytearray()
        self._failures: deque[float] = deque()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Begin supervising. Returns at once; the process starts in the task."""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._failures.clear()
        self._task = asyncio.create_task(self._supervise(), name=f"door-service-{self.door_id}")

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

    async def restart(self) -> None:
        await self.stop()
        self.status.restarts = 0
        self.start()

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
            if self._stopping:
                return
            now = time.monotonic()
            self._failures.append(now)
            while self._failures and now - self._failures[0] > _CIRCUIT_WINDOW_SECONDS:
                self._failures.popleft()
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
            setup = {"pty": False, "limits": {"RLIMIT_AS": self.spec.memory_mb * 1024 * 1024,
                                              "RLIMIT_NPROC": DOOR_MAX_PROCESSES}}
            # No RLIMIT_CPU: a service is long-lived by definition, so accrued
            # CPU seconds are not a runaway signal the way they are for one
            # caller's run.
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
        except (OSError, ValueError):
            pass

    def _note(self, text: str) -> None:
        self._tail.extend(f"[netbbs] {text}\n".encode("utf-8", errors="replace"))
        del self._tail[:-_DIAGNOSTIC_BYTES]
        self.status.diagnostic = bytes(self._tail).decode("utf-8", errors="replace")

    async def _terminate(self) -> None:
        reader, self._diagnostics = self._diagnostics, None
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        self._signal(proc, signal.SIGTERM if os.name == "posix" else None)
        try:
            await asyncio.wait_for(self._wait(proc), timeout=self.spec.stop_grace_seconds)
            return
        except asyncio.TimeoutError:
            _logger.warning("door service %r ignored SIGTERM for %ds; killing it",
                            self.door_name, self.spec.stop_grace_seconds)
        self._signal(proc, signal.SIGKILL if os.name == "posix" else None, kill=True)
        try:
            await asyncio.wait_for(self._wait(proc), timeout=_KILL_DEADLINE_SECONDS)
        except asyncio.TimeoutError:
            # Unkillable means stuck in the kernel. Leaving it behind is worse
            # than a stalled shutdown is; say so loudly and carry on.
            _logger.error("door service %r survived SIGKILL; abandoning it so shutdown can finish",
                          self.door_name)

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
        if not hasattr(asyncio, "open_unix_connection"):
            return True  # Windows development; the pid check above is all there is.
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.spec.health_path), timeout=_HEALTH_TIMEOUT_SECONDS)
        except (OSError, asyncio.TimeoutError, NotImplementedError):
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
        spec = service_spec(door.profile)
        existing = self._services.get(door.id)
        if existing is not None and existing.spec == spec:
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
                service.start()

    async def ensure_running(self, door, *, wait_seconds: float = 10.0) -> str | None:
        """Make a door's service available, returning a caller-facing problem.

        `on_first_caller` services are started here; a `with_node` one which
        died is not silently revived, because its supervisor already decided.
        """
        service = self._services.get(door.id) or await self.adopt(door)
        if service is None:
            return None
        problem = None
        if service.status.state == FAILED:
            problem = f"{door.name}'s service has stopped after repeated failures. Ask the SysOp to check it."
        else:
            if service.spec.start == "on_first_caller" and service.status.state == STOPPED:
                service.start()
            if not await service.wait_until_running(wait_seconds):
                problem = f"{door.name}'s service is not running. Ask the SysOp to start it."
            elif not await service.healthy():
                problem = f"{door.name}'s service is not answering. Ask the SysOp to check it."
        if problem is not None:
            _logger.warning("refused to launch door %r: service state %s", door.name, service.status.state)
        return problem

    async def stop_all(self) -> None:
        """Stop every service concurrently, each within its own deadline."""
        services, self._services = list(self._services.values()), {}
        if services:
            await asyncio.gather(*(service.stop() for service in services), return_exceptions=True)
