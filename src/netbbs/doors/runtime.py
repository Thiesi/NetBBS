"""Supervised doors. Same-user execution is NOT filesystem/network isolation.

Only operator-trusted programs belong here. No shell, real caller socket, parent
environment, database path, or credentials are passed to local doors. Unprofiled
doors retain the original JSON metadata and raw UTF-8 stdin/stdout API.
"""
from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from netbbs.doors.endpoints import NodeLease, StreamEndpoint, pty_endpoint, socket_endpoint
from netbbs.doors.dropfiles import write_drop_files
from netbbs.doors.profiles import preflight
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.session import SessionClosedError
from netbbs.moderation.log import record_action

_logger = logging.getLogger(__name__)
DOOR_CPU_LIMIT_SECONDS = 300
DOOR_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
# RLIMIT_NPROC is shared by the real UID, not a per-door quota.
DOOR_MAX_PROCESSES = 16
WALL_TIME_LIMIT_SECONDS = 3600
_TERMINATE_GRACE_SECONDS = 0.5
_DIAGNOSTIC_BYTES = 8192


@dataclass(frozen=True)
class DoorRunResult:
    exit_code: int | None
    duration_seconds: float
    reason: str
    diagnostic: str = ""


def _write_door_info(db, workdir, session, player):
    info = {"handle": player.username, "user_id": player.id,
            "terminal_width": session.terminal_width, "terminal_height": session.terminal_height,
            "color_depth": "truecolor" if effective_truecolor(session, db, player) else "256",
            "node_name": session.node_display_name}
    path = workdir / "door_info.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


def _door_environment(info_path):
    env = {"NETBBS_DOOR_INFO": str(info_path)}
    try:
        env["USERPROFILE" if os.name == "nt" else "HOME"] = str(Path.home())
    except RuntimeError:
        pass
    return env


class DoorTerminal:
    """NetBBS terminals speak UTF-8; legacy door streams can speak CP437."""
    def __init__(self, session, encoding):
        self.session, self.encoding = session, encoding
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending = deque()

    async def read_byte(self):
        if self.encoding != "cp437":
            return await self.session.read_byte()
        while not self.pending:
            value = await self.session.read_byte()
            if value is not None:
                self.pending.extend(self.decoder.decode(bytes([value])).encode("cp437", errors="replace"))
        return self.pending.popleft()

    async def write_raw(self, data):
        if self.encoding == "cp437":
            data = data.decode("cp437").encode("utf-8")
        await self.session.write_raw(data)


async def _pump_input(session, endpoint):
    try:
        while True:
            value = await session.read_byte()
            if value is not None:
                await endpoint.write(bytes([value]))
    except SessionClosedError:
        return "caller_disconnected"
    except (BrokenPipeError, ConnectionResetError):
        return "door_exited"


async def _pump_output(session, endpoint):
    try:
        while chunk := await endpoint.read(4096):
            await session.write_raw(chunk)
        return "door_exited"
    except SessionClosedError:
        return "caller_disconnected"


async def _wait_leader(proc):
    # asyncio Process.wait() can wait for PIPE closure as well as child exit.
    # Descendants may retain those pipes. The child watcher sets returncode
    # independently, so watch that signal before tearing down the owned group.
    while proc.returncode is None:
        await asyncio.sleep(0.01)
    return proc.returncode


async def _relay(session, endpoint, proc=None):
    input_task = asyncio.create_task(_pump_input(session, endpoint))
    output_task = asyncio.create_task(_pump_output(session, endpoint))
    exit_task = asyncio.create_task(_wait_leader(proc)) if proc else None
    tasks = [input_task, output_task] + ([exit_task] if exit_task else [])
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        results = [task.result() for task in done]
        if "caller_disconnected" in results:
            return "caller_disconnected"
        if exit_task in done and not output_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(output_task), timeout=0.25)
            except asyncio.TimeoutError:
                pass
        return "door_exited"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _diagnostics(reader, tail):
    while chunk := await reader.read(4096):
        tail.extend(chunk)
        del tail[:-_DIAGNOSTIC_BYTES]


async def _finish_owned(task):
    """Retrieve an owned operation even under repeated shutdown cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _stop_process(proc):
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif proc.returncode is None:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
    except asyncio.TimeoutError:
        pass
    finally:
        # A reaped leader does not mean that its descendants have exited.
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.returncode is None:
            proc.kill()
        await proc.wait()


def _record_door_session(db, *, actor, door, duration_seconds, reason, exit_code, diagnostic=""):
    db.connection.execute("UPDATE doors SET last_diagnostic = ? WHERE id = ?", (diagnostic[-8192:], door.id))
    db.connection.commit()
    record_action(db, actor=actor, action="play_door", object_type="door", object_id=door.id,
                  detail=f"door={door.name!r} duration={duration_seconds:.1f}s reason={reason} exit_code={exit_code}")


async def run_door(session, lane, door, player, *, wall_time_limit_seconds=WALL_TIME_LIMIT_SECONDS):
    profile = door.profile
    start = time.monotonic()
    proc = endpoint = lease = child_socket = None
    slave = workdir = None
    diagnostic_tasks = []
    tail = bytearray()
    reason, exit_code = "failed_to_start", None
    mode_entered = False
    try:
        problems = await asyncio.to_thread(preflight, door, session)
        if problems:
            raise ValueError("\n".join(problems))
        if profile:
            root = await lane.run(lambda db: db.path.parent / "door-nodes")
            identity = str(Path(profile.install_dir).resolve()) if profile.install_dir else f"door-{door.id}"
            # Small local lock operation; no await that could lose an acquired lease on cancellation.
            lease = NodeLease(root, identity, profile.max_sessions)
        workdir = Path(tempfile.mkdtemp(prefix="netbbs-door-"))
        info_path = await lane.run(_write_door_info, workdir, session, player)
        info = json.loads(info_path.read_text(encoding="utf-8"))
        width = profile.width if profile and profile.width else session.terminal_width
        height = profile.height if profile and profile.height else session.terminal_height
        env = _door_environment(info_path)
        encoding = profile.encoding if profile else "utf-8"
        terminal = DoorTerminal(session, encoding)
        mode_entered = True
        await session.enter_door_mode(encoding=encoding, width=(profile.width or None) if profile else None,
                                      height=(profile.height or None) if profile else None)
        if profile and profile.adapter == "rlogin":
            from netbbs.doors.remote import connect_remote
            endpoint = await connect_remote(profile, info, width, height)
        else:
            kind = profile.endpoint if profile else "stdio"
            stdin = stdout = asyncio.subprocess.PIPE
            pass_fds = ()
            if kind == "socketpair":
                endpoint, child_socket = socket_endpoint()
                pass_fds = (child_socket.fileno(),)
                stdin = asyncio.subprocess.DEVNULL
            elif kind == "pty":
                endpoint, slave = pty_endpoint(width, height)
                stdin = stdout = slave
            argv = [door.executable_path, *door.args]
            cwd = Path(profile.install_dir) if profile and profile.install_dir else workdir
            if profile:
                info.update(terminal_width=width, terminal_height=height)
                info_path.write_text(json.dumps(info), encoding="utf-8")
                drops = write_drop_files(workdir, profile, info, lease.number,
                                         descriptor=child_socket.fileno() if child_socket and profile.adapter == "native" else 0)
                lower = profile.filename_case == "lower"
                substitutions = {"node_dir": str(drops), "install_dir": str(cwd), "node": str(lease.number),
                                 "door32": str(drops / ("door32.sys" if lower else "DOOR32.SYS")),
                                 "door_sys": str(drops / ("door.sys" if lower else "DOOR.SYS"))}
                argv = [argv[0], *(arg.format_map(substitutions) for arg in argv[1:])]
                env.update(profile.environment)
                env.update(NETBBS_DOOR_NODE=str(lease.number), NETBBS_DOOR_NODE_DIR=str(drops))
                env.setdefault("TERM", "ansi")
                if profile.adapter == "dosbox":
                    from netbbs.doors.dosbox import prepare_dosbox
                    argv, dos_env = prepare_dosbox(door, workdir, child_socket.fileno(), lease.number)
                    env.update(dos_env)
                    cwd = workdir
                if profile.runner:
                    argv = [*profile.runner, *argv]
            if os.name == "posix":
                setup = {"pty": kind == "pty", "limits": {"RLIMIT_CPU": DOOR_CPU_LIMIT_SECONDS,
                         "RLIMIT_AS": profile.memory_mb * 1024 * 1024 if profile else DOOR_MEMORY_LIMIT_BYTES,
                         "RLIMIT_NPROC": DOOR_MAX_PROCESSES}}
                argv = [sys.executable, "-I", str(Path(__file__).with_name("launcher.py")), json.dumps(setup), *argv]
            kwargs = {"start_new_session": True, "pass_fds": pass_fds} if os.name == "posix" else {}
            # Cancellation during spawn must not lose ownership of a live child.
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(*argv, stdin=stdin, stdout=stdout,
                         stderr=asyncio.subprocess.PIPE, cwd=str(cwd), env=env, **kwargs))
            proc, cancelled = await _finish_owned(spawn)
            if cancelled:
                raise asyncio.CancelledError
            if child_socket:
                child_socket.close()
                child_socket = None
            if slave is not None:
                os.close(slave)
                slave = None
            if endpoint is None:
                endpoint = StreamEndpoint(proc.stdout, proc.stdin)
            elif kind == "socketpair":
                diagnostic_tasks.append(asyncio.create_task(_diagnostics(proc.stdout, tail)))
            diagnostic_tasks.append(asyncio.create_task(_diagnostics(proc.stderr, tail)))
        try:
            reason = await asyncio.wait_for(_relay(terminal, endpoint, proc),
                            timeout=min(wall_time_limit_seconds, profile.time_limit if profile else WALL_TIME_LIMIT_SECONDS))
            if reason == "door_exited":
                if proc and proc.returncode is None:
                    try:
                        await asyncio.wait_for(_wait_leader(proc), timeout=2)
                    except asyncio.TimeoutError:
                        pass
                exit_code = proc.returncode if proc else 0
                if profile and profile.adapter == "dosbox" and exit_code == 0:
                    names = {p.name.upper(): p for p in workdir.iterdir()}
                    failed = "EXIT.ERR" in names and names["EXIT.ERR"].stat().st_size > 0
                    if failed or "RETURN.OK" not in names:
                        exit_code = 1
                        tail.extend(b"DOS command failed or did not return through the configured launcher.\n")
                reason = "exited" if exit_code == 0 else "crashed"
        except asyncio.TimeoutError:
            reason = "timed_out"
        except Exception as exc:
            reason = "relay_failed"
            tail.extend(str(exc).encode("utf-8", errors="replace")[:2048])
            _logger.exception("door %r terminal relay failed", door.name)
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    except SessionClosedError:
        reason = "caller_disconnected"
    except BlockingIOError as exc:
        reason = "busy"
        tail.extend(str(exc).encode())
    except (OSError, ValueError, KeyError) as exc:
        tail.extend(str(exc).encode("utf-8", errors="replace")[:4096])
        _logger.warning("door %r failed preflight/start: %s", door.name, exc)
    finally:
        primary = sys.exc_info()[1]

        async def cleanup():
            nonlocal exit_code
            errors = []
            for operation in (lambda: _stop_process(proc) if proc is not None else None,
                              lambda: endpoint.close() if endpoint is not None else None):
                try:
                    pending = operation()
                    if pending is not None:
                        await pending
                except Exception as exc:
                    errors.append(exc)
            if proc is not None and exit_code is None:
                exit_code = proc.returncode
            if child_socket:
                child_socket.close()
            if slave is not None:
                os.close(slave)
            for task in diagnostic_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*diagnostic_tasks, return_exceptions=True)
            try:
                if mode_entered:
                    await session.leave_door_mode()
            except Exception as exc:
                errors.append(exc)
            finally:
                if workdir is not None:
                    shutil.rmtree(workdir, ignore_errors=True)
                if lease:
                    lease.close()
            diagnostic = bytes(tail[-_DIAGNOSTIC_BYTES:]).decode("utf-8", errors="replace")
            try:
                await lane.run(_record_door_session, actor=player, door=door,
                           duration_seconds=time.monotonic() - start, reason=reason,
                           exit_code=exit_code, diagnostic=diagnostic)
            except Exception as exc:
                errors.append(exc)
            for exc in errors:
                _logger.error("door cleanup failed: %s", exc, exc_info=exc)
            if errors and primary is None:
                raise errors[0]
            return diagnostic

        diagnostic, cancelled = await _finish_owned(asyncio.create_task(cleanup()))
        if cancelled and primary is None:
            raise asyncio.CancelledError
    return DoorRunResult(exit_code, time.monotonic() - start, reason, diagnostic)
