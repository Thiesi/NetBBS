"""
Door execution engine (issue #172; locked design record: issues #63/#167).

**Sandbox model**: subprocess isolation, same OS user as the main NetBBS
process -- no containers, no dedicated door-runner user, no privilege
drop, no root requirement anywhere. This is a deliberate choice, not a
placeholder for something stronger later: operational frictionlessness
for a SysOp was judged to outweigh the defense-in-depth a privilege-
separated model would buy, at the real cost that model would impose
(NetBBS running as root at startup, or a setuid helper, plus correct
user/permission/firewall setup on every install). See issue #63's own
comment thread for the full reasoning. Consequently: a door process
technically *can* reach anything the main NetBBS process can at the OS
level -- filesystem, network -- nothing here enforces otherwise. What
*is* enforced, unconditionally, regardless of a door's own behavior:
CPU/memory/process-count ceilings (`resource.setrlimit`, POSIX only --
see `_apply_resource_limits`) and wall-clock session length (this
module's own async watchdog, since `setrlimit`'s CPU-time limit doesn't
catch a process that's alive but simply not consuming CPU).

**Door output is trusted, not sanitized** -- relayed to the caller's
real terminal exactly as the door emits it, the same "SysOp vouches for
what they chose to run" posture `netbbs.net.welcome_banner` already
takes for a hand-placed `.ans` file. NetBBS provides the interface and
this module's own resource/lifetime bounds as best-effort abuse
prevention; it does not claim an airtight guarantee against a door's own
content.

**v1 API surface is drop-file-shaped, not a live protocol** -- static
session metadata (handle, stable numeric user ID, terminal size, color
depth, node name) is written to a small JSON file in a scratch directory
created fresh for this one launch, and its path is handed to the door
via the `NETBBS_DOOR_INFO` environment variable, before the process ever
starts. From then on stdin/stdout are a pure raw byte relay for the
session's whole duration -- no framing, no control messages interleaved
with terminal output, matching the classic DOS-door drop-file convention
rather than a custom structured handshake. (Deliberate side benefit: a
later DOSBox/dos-door adapter only has to translate this same metadata
into an actual DOOR.SYS-format file, not out of some NetBBS-specific
protocol.) No live terminal-resize propagation -- size is captured once,
at launch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from netbbs.auth.users import User
from netbbs.doors.registry import Door
from netbbs.moderation.log import record_action
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.session import Session, SessionClosedError
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_logger = logging.getLogger(__name__)

try:
    import resource

    _RESOURCE_AVAILABLE = True
except ImportError:  # Windows dev environment -- not a supported deployment
    # target (design doc §2.1); doors still run here for local development,
    # just without the POSIX resource ceilings a real NetBSD/Linux node
    # would enforce. The wall-time watchdog below is pure asyncio and
    # applies on every platform regardless.
    resource = None  # type: ignore[assignment]
    _RESOURCE_AVAILABLE = False

# Not admin-configurable in v1 -- same "sane hardcoded ceiling, revisit if
# real usage demands it" precedent as netbbs.net.welcome_banner's own
# MAX_BANNER_SIZE_BYTES. CPU/memory are generous since most doors spend
# almost all their time blocked on caller input, not computing,
DOOR_CPU_LIMIT_SECONDS = 300
DOOR_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
# RLIMIT_NPROC is a per-*real-UID* ceiling, not per-process-tree -- since
# every door (and NetBBS itself) shares one OS user by design (see this
# module's own docstring), this bounds one door's own runaway forking but
# also stacks across every concurrent door session and NetBBS's own
# process count. A coarse safety net against fork-bombing, not a precise
# per-door quota -- a known, accepted limitation of the same-OS-user model,
# not an oversight.
DOOR_MAX_PROCESSES = 16
WALL_TIME_LIMIT_SECONDS = 3600
_TERMINATE_GRACE_SECONDS = 5


@dataclass(frozen=True)
class DoorRunResult:
    exit_code: int | None
    duration_seconds: float
    reason: str  # "exited" | "crashed" | "timed_out" | "caller_disconnected" | "failed_to_start"


def _apply_resource_limits() -> None:
    """Runs inside the forked child, before exec -- `setrlimit` only ever
    lowers a process's own ceiling, never raises it, so a door cannot
    escape these regardless of what it tries."""
    resource.setrlimit(resource.RLIMIT_CPU, (DOOR_CPU_LIMIT_SECONDS, DOOR_CPU_LIMIT_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (DOOR_MEMORY_LIMIT_BYTES, DOOR_MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_NPROC, (DOOR_MAX_PROCESSES, DOOR_MAX_PROCESSES))


def _write_door_info(db: Database, workdir: Path, session: Session, player: User) -> Path:
    """The v1 "drop-file" -- see this module's own docstring. Deliberately
    narrow (design doc §-equivalent least-privilege framing from issue
    #63): a stable numeric ID and coarse display info, never a password
    hash, session token, or node identity key."""
    info = {
        "handle": player.username,
        "user_id": player.id,
        "terminal_width": session.terminal_width,
        "terminal_height": session.terminal_height,
        "color_depth": "truecolor" if effective_truecolor(session, db, player) else "256",
        "node_name": session.node_display_name,
    }
    path = workdir / "door_info.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


def _door_environment(info_path: Path) -> dict[str, str]:
    """Build the door's deliberately minimal execution environment.

    A platform's home-directory locator is execution context rather than a
    door capability: the child already runs as the same OS user and can reach
    that user's files.  Supplying it explicitly keeps ``Path.home()`` stable
    when the rest of the parent environment is intentionally not inherited.
    In particular, Windows has no password-database fallback when
    ``USERPROFILE`` is absent, so a persistent door would otherwise resolve a
    tempfile fallback relative to its disposable working directory.
    """
    env = {"NETBBS_DOOR_INFO": str(info_path)}
    try:
        home = str(Path.home())
    except RuntimeError:
        return env
    env["USERPROFILE" if os.name == "nt" else "HOME"] = home
    return env


async def _pump_input(session: Session, proc: asyncio.subprocess.Process) -> None:
    """Caller keystrokes -> door stdin, byte for byte, until the session
    closes or the door's stdin pipe does. Mirrors `netbbs.net.zmodem.
    _read_raw_byte`'s own "loop past a transport-level None" contract
    against the same `Session.read_byte` primitive."""
    assert proc.stdin is not None
    try:
        while True:
            b = await session.read_byte()
            if b is None:
                continue
            proc.stdin.write(bytes([b]))
            await proc.stdin.drain()
    except SessionClosedError:
        return
    except (BrokenPipeError, ConnectionResetError):
        return


async def _pump_output(session: Session, proc: asyncio.subprocess.Process) -> None:
    """Door stdout -> caller, relayed with `write_raw` (exactly as
    emitted, no sanitization -- see this module's own docstring) until
    EOF, which is how a door signals "I'm about to exit" in this v1
    surface -- there is no separate explicit "I'm done" message."""
    assert proc.stdout is not None
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            return
        try:
            await session.write_raw(chunk)
        except SessionClosedError:
            return


async def _relay(session: Session, proc: asyncio.subprocess.Process) -> str:
    """Runs until either side ends -- the door closing its stdout (it is
    exiting) or the caller's session disconnecting -- whichever comes
    first tears the whole relay down; returns which one it was. The
    loser is cancelled and awaited so nothing leaks."""
    input_task = asyncio.create_task(_pump_input(session, proc))
    output_task = asyncio.create_task(_pump_output(session, proc))
    done, pending = await asyncio.wait({input_task, output_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return "door_exited" if output_task in done else "caller_disconnected"


def _record_door_session(
    db: Database, *, actor: User, door: Door, duration_seconds: float, reason: str, exit_code: int | None
) -> None:
    record_action(
        db, actor=actor, action="play_door", object_type="door", object_id=door.id,
        detail=f"door={door.name!r} duration={duration_seconds:.1f}s reason={reason} exit_code={exit_code}",
    )


async def run_door(
    session: Session,
    lane: DatabaseLane,
    door: Door,
    player: User,
    *,
    wall_time_limit_seconds: float = WALL_TIME_LIMIT_SECONDS,
) -> DoorRunResult:
    """Launch `door`, relay the session interactively until it ends, and
    unconditionally clean up afterward -- the one function every door
    launch, from any transport, goes through.

    No permission check here -- same "gating is the calling screen's
    job, not this function's" convention as `netbbs.files.entries.
    upload_file` and everything else in this codebase that separates
    "is this allowed" from "do the thing".

    `wall_time_limit_seconds` defaults to the real ceiling
    (`WALL_TIME_LIMIT_SECONDS`) -- overridable so a test can exercise the
    timeout path in milliseconds rather than an hour."""
    workdir_str = tempfile.mkdtemp(prefix="netbbs-door-")
    workdir = Path(workdir_str)
    start = time.monotonic()
    try:
        info_path = await lane.run(_write_door_info, workdir, session, player)
        env = _door_environment(info_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                door.executable_path,
                *door.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=workdir_str,
                env=env,
                preexec_fn=_apply_resource_limits if _RESOURCE_AVAILABLE else None,
            )
        except OSError as exc:
            _logger.warning("door %r failed to start: %s", door.name, exc)
            await lane.run(
                _record_door_session, actor=player, door=door, duration_seconds=0.0,
                reason="failed_to_start", exit_code=None,
            )
            return DoorRunResult(exit_code=None, duration_seconds=0.0, reason="failed_to_start")

        relay_task = asyncio.create_task(_relay(session, proc))
        try:
            end_reason = await asyncio.wait_for(relay_task, timeout=wall_time_limit_seconds)
        except asyncio.TimeoutError:
            end_reason = "timed_out"

        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

        duration = time.monotonic() - start
        exit_code = proc.returncode
        if end_reason == "door_exited":
            final_reason = "exited" if exit_code == 0 else "crashed"
        else:
            final_reason = end_reason  # "timed_out" or "caller_disconnected"

        await lane.run(
            _record_door_session, actor=player, door=door, duration_seconds=duration,
            reason=final_reason, exit_code=exit_code,
        )
        return DoorRunResult(exit_code=exit_code, duration_seconds=duration, reason=final_reason)
    finally:
        shutil.rmtree(workdir_str, ignore_errors=True)
