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
import secrets
import shutil
import signal
import sqlite3
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from netbbs.doors.endpoints import NodeLease, StreamEndpoint, pty_endpoint, socket_endpoint
from netbbs.doors.dropfiles import write_drop_files
from netbbs.doors.outbound import OUTBOUND_DIRNAME, door_info_block, drain as drain_outbound
from netbbs.doors.profiles import preflight
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.timeutil import resolve_display_preferences
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.net.session import SessionClosedError
from netbbs.moderation.log import record_action

_logger = logging.getLogger(__name__)
DOOR_CPU_LIMIT_SECONDS = 300
DOOR_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
# RLIMIT_NPROC is shared by the real UID, not a per-door quota.
DOOR_MAX_PROCESSES = 16
WALL_TIME_LIMIT_SECONDS = 3600
#: How long a door gets to exit on its own after SIGTERM, before SIGKILL.
#: Was a fixed 0.5 s, which is long enough for a process that exits on the
#: signal and far too short for one which flushes anything first -- a DOS game
#: writing its scores through the emulator, say. A door which exits promptly
#: never waits this long, because the wait ends the moment it does; the only
#: doors that pay for a longer grace are the ones that need it.
DOOR_STOP_GRACE_SECONDS = 5
_DIAGNOSTIC_BYTES = 8192
# A human dragging a window edge; fine-grained enough to feel immediate
# without waking the event loop for a size which almost never changes.
_RESIZE_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class DoorRunResult:
    exit_code: int | None
    duration_seconds: float
    reason: str
    diagnostic: str = ""


#: Version of the `door_info.json` contract (issue #469). 1 was the original
#: six fields; 2 adds the caller/node metadata below; 3 adds the optional
#: `outbound` object (issue #520), present only for a door whose SysOp has
#: switched its outbound hook on. A door may refuse a platform it does not
#: understand instead of probing for fields.
DOOR_API_VERSION = 3


def node_opaque_id(db) -> str:
    """A stable, opaque identifier for this node, minted on first use.

    Not a credential and not derived from the display name, which a SysOp can
    change at will; a door which keys its world on the node needs something
    that survives a rename. Lives in the node database, so it survives a
    backup and restore with everything else.
    """
    return _minted_once(db, "node_opaque_id")


def _minted_once(db, key: str) -> str:
    """Read a node-scoped opaque value, minting it only on first use.

    Read before write deliberately: an unconditional INSERT OR IGNORE takes
    SQLite's write lock on *every* door launch, and a second connection holding
    a write transaction -- a live administrative process, say -- would make
    each launch wait out the busy timeout and then fail.
    """
    row = db.connection.execute("SELECT value FROM node_config WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row[0]
    db.connection.execute("INSERT OR IGNORE INTO node_config (key, value) VALUES (?, ?)",
                          (key, secrets.token_hex(16)))
    db.connection.commit()
    return db.connection.execute(
        "SELECT value FROM node_config WHERE key = ?", (key,)).fetchone()[0]


def _write_door_info(db, workdir, session, player, war_dialer=False, session_limit_seconds=None,
                     door_id=None):
    info = {"handle": player.username, "user_id": player.id,
            "terminal_width": session.terminal_width, "terminal_height": session.terminal_height,
            "color_depth": "truecolor" if effective_truecolor(session, db, player) else "256",
            "node_name": session.node_display_name,
            "door_api": DOOR_API_VERSION,
            # The caller's own display preference, so a door can match the
            # glyph style they already chose rather than guessing.
            "unicode_style": unicode_style_enabled(db, player),
            "transport": getattr(session, "transport_name", "unknown"),
            # Node-wide, not per-caller: NetBBS has one display timezone.
            "timezone": resolve_display_preferences(db)[1],
            # `node_fingerprint` is deliberately absent: the node's own Link
            # identity is not in the database, so supplying it would mean
            # threading the identity (or its directory and passphrase) down
            # into the door runtime. Every reader treats a missing field as
            # unknown, and `node_id` is what a door keying its world on the
            # node actually needs today.
            "node_id": node_opaque_id(db)}
    if session_limit_seconds is not None:
        # The effective wall clock for *this* launch, so a door can warn
        # before it is cut off rather than being surprised by it.
        info["session_limit_seconds"] = session_limit_seconds
    if war_dialer:
        # An opaque namespace belongs to the node database and survives its backup.
        # It is not a credential and does not depend on a mutable display name.
        info["war_dialer_owner"] = _minted_once(db, "war_dialer_owner")
    if door_id is not None and (outbound := door_info_block(db, door_id)) is not None:
        # Issue #520. Present only for a door whose SysOp switched the hook
        # on, so the overwhelming majority of doors see exactly what they saw
        # at door_api 2. The door is told its own label and which boards it
        # may name, because neither is discoverable any other way and a door
        # left to guess would guess wrong.
        (workdir / OUTBOUND_DIRNAME).mkdir(exist_ok=True)
        info["outbound"] = outbound
    path = workdir / "door_info.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


def war_dialer_world_path(db, door) -> Path | None:
    """Effective world locator: explicit profile, process override, then node DB sibling."""
    profile_override = door.profile.environment.get("WAR_DIALER_DB_PATH") if door.profile else None
    bundled = Path(__file__).with_name("bundled") / "war_dialer.py"
    argv = (door.executable_path, *door.args)
    # Match the script the profile actually launches, including relative argv
    # and the supported installation-directory substitution. An unprofiled
    # relative script runs in an empty temporary directory, not the server cwd.
    install = Path(door.profile.install_dir).resolve() if door.profile and door.profile.install_dir else None
    is_bundled = False
    for index, arg in enumerate(argv):
        if index and install is not None:
            arg = arg.replace("{install_dir}", str(install))
        candidate = Path(arg)
        if candidate.name != "war_dialer.py":
            continue
        if not candidate.is_absolute():
            if install is None:
                continue
            candidate = install / candidate
        if candidate.resolve() == bundled.resolve():
            is_bundled = True
            break
    is_module = any(argv[i:i + 2] == ("-m", "netbbs.doors.bundled.war_dialer") for i in range(len(argv) - 1))
    if not (is_bundled or is_module or profile_override is not None):
        return None
    override = profile_override if profile_override is not None else os.environ.get("WAR_DIALER_DB_PATH")
    if override is not None:
        if not override.strip():
            raise ValueError("WAR_DIALER_DB_PATH must name a database file")
        return Path(override).expanduser().resolve()
    node_path = db.path.resolve()
    return node_path.parent / (node_path.name + ".doors") / "war-dialer.db"


def war_dialer_path_problem(door, world_path: Path | None) -> str | None:
    if world_path is None:
        return None
    explicit = (door.profile and "WAR_DIALER_DB_PATH" in door.profile.environment) or "WAR_DIALER_DB_PATH" in os.environ
    if not explicit and not world_path.exists():
        try:
            legacy = Path.home() / ".netbbs" / "wardialer.db"
        except RuntimeError:
            return "Cannot check the legacy War Dialer home path. Set WAR_DIALER_DB_PATH explicitly."
        if legacy.exists():
            return (f"Legacy War Dialer world found at {legacy}. Stop old sessions and set WAR_DIALER_DB_PATH "
                    f"explicitly, or migrate a SQLite-consistent copy to {world_path}. No new world was created.")
    if world_path.exists() and not world_path.is_file():
        return f"War Dialer world path is not a file: {world_path}"
    return None


def _door_environment(info_path, war_dialer_path=None):
    env = {"NETBBS_DOOR_INFO": str(info_path)}
    try:
        env["USERPROFILE" if os.name == "nt" else "HOME"] = str(Path.home())
    except RuntimeError:
        pass
    # A deliberate, narrow persistent-data override; resolve before entering
    # the disposable door cwd. Never forward the complete parent environment.
    if save_dir := os.environ.get("VOIDRUNNER_SAVE_DIR"):
        env["VOIDRUNNER_SAVE_DIR"] = str(Path(save_dir).expanduser().resolve())
    if war_dialer_path is not None:
        env["WAR_DIALER_DB_PATH"] = str(war_dialer_path)
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


async def _relay(session, endpoint, proc=None, stop_grace=DOOR_STOP_GRACE_SECONDS):
    input_task = asyncio.create_task(_pump_input(session, endpoint))
    output_task = asyncio.create_task(_pump_output(session, endpoint))
    exit_task = asyncio.create_task(_wait_leader(proc)) if proc else None
    tasks = [input_task, output_task] + ([exit_task] if exit_task else [])
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            results = [task.result() for task in done]
            if "caller_disconnected" in results:
                return "caller_disconnected"
            if output_task in done:
                return "door_exited"
            if exit_task in done:
                # Kill lingering pipe/socket holders independently of draining.
                # A slow caller still gets every final byte, bounded by the
                # launch watchdog and caller disconnect, not a 250 ms cutoff.
                stop_task = asyncio.create_task(_stop_process(proc, stop_grace))
                tasks.append(stop_task)
                pending.add(stop_task)
            # Broken stdin does not imply stdout has finished delivering.
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def resize_mode(profile, endpoint_kind):
    """How a running door is told the caller resized, or None for not at all.

    Policy only, so it can be checked without a POSIX host; the caller adds
    the platform gate. A profile which pins width/height asked for a fixed
    screen, and DOS geometry is fixed by design, so only a native door
    following the caller's own terminal is ever notified.
    """
    if profile is None or profile.adapter != "native" or profile.width:
        return None
    if endpoint_kind == "pty":
        return "pty"
    if profile.resize_signal and endpoint_kind in ("stdio", "socketpair"):
        return "signal"
    return None


def _republish_terminal_size(info_path, info, width, height):
    """Rewrite the door's metadata with the caller's current geometry.

    Through a temporary file in the same directory: a door woken by the
    signal below may read this the instant it is notified, and must never
    catch a half-written file.
    """
    info = dict(info, terminal_width=width, terminal_height=height)
    temporary = info_path.parent / (info_path.name + ".new")
    temporary.write_text(json.dumps(info), encoding="utf-8")
    os.replace(temporary, info_path)
    return info


async def _forward_resize(session, proc, info_path, info, *, published, pty_fd=None, signal_door=False,
                          interval=_RESIZE_POLL_SECONDS):
    """Follow the caller's terminal size while the door runs (issue #468).

    Polled rather than pushed. Telnet NAWS, SSH's window-change message and
    the web client's resize event each update `Session.terminal_width`/
    `terminal_height` in place, with no notification in common, so one bounded
    poll here covers every transport — including any later one — instead of
    each transport growing a hook it must remember to call. `RemoteEndpoint.
    _urgent_loop` already reads its own out-of-band channel the same way.
    """
    # The baseline is what the door was actually told at launch, not the
    # session's size now: a caller who resized during door-mode entry or the
    # spawn would otherwise leave the door holding stale geometry until they
    # happened to resize a second time.
    last = published
    while True:
        await asyncio.sleep(interval)
        current = (session.terminal_width, session.terminal_height)
        if current == last or not all(current):
            continue
        last = current
        width, height = current
        try:
            info = _republish_terminal_size(info_path, info, width, height)
            if pty_fd is not None:
                import fcntl
                import struct
                import termios
                fcntl.ioctl(pty_fd, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
                # The kernel signals the terminal's foreground group itself;
                # this also reaches a door which never made the PTY its
                # controlling terminal.
                os.killpg(proc.pid, signal.SIGWINCH)
            elif signal_door:
                # The leader only, never the process group: SIGUSR1 terminates
                # a process which does not handle it, and only the door itself
                # opted in. Helper processes it spawned did not, and killing
                # them on the caller's first resize would break the game.
                os.kill(proc.pid, signal.SIGUSR1)
        except OSError:
            # The door or its terminal is gone. Ending the run is the relay's
            # job, not this task's; stop following rather than report.
            return


async def _diagnostics(reader, tail):
    while chunk := await reader.read(4096):
        tail.extend(chunk)
        del tail[:-_DIAGNOSTIC_BYTES]


async def _discard_output(reader):
    """Release pipe backpressure after terminal delivery has been cancelled."""
    while await reader.read(4096):
        pass


async def _finish_owned(task):
    """Retrieve an owned operation even under repeated shutdown cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _stop_process(proc, grace=DOOR_STOP_GRACE_SECONDS):
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif proc.returncode is None:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
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


def effective_wall_limit(profile, call_site_limit=None):
    """Tightest explicit wall-clock bound, or None when nothing bounds the run.

    A profile's `time_limit` of 0 is the SysOp's explicit opt-out, so it must
    not be folded in with `min()` as if it were the smallest bound. An
    unprofiled door keeps the original fixed ceiling; a caller-supplied bound
    (the capability probe's, say) still wins when it is tighter.
    """
    bounds = [value for value in (call_site_limit,
                                  profile.time_limit if profile else WALL_TIME_LIMIT_SECONDS) if value]
    return min(bounds) if bounds else None


async def run_door(session, lane, door, player, *, wall_time_limit_seconds=None,
                   output_check=None, node_identity=None, rehearsal=False):
    """Supervise and record one run; an optional synchronous probe check returns an error string.

    `node_identity`, when this node has Link running, is what lets a post a
    door made through its outbound hook (issue #520) reach the peers a
    Linked board is linked to -- the same `queue_board_post_if_linked` call
    the interactive posting path makes. `None` (Link off, or the standalone
    admin CLI) simply keeps the post local.

    `rehearsal` marks a launch a SysOp made to *check* the door -- the
    compatibility screen's test launch, or the DOS probe -- rather than a
    caller playing it. Such a launch does not drain the outbound hook: a
    SysOp trying a door out must not publish its content to a real board,
    and a probe which runs the game for twelve seconds on every preflight
    would do it repeatedly.
    """
    profile = door.profile
    stop_grace = profile.stop_grace_seconds if profile else DOOR_STOP_GRACE_SECONDS
    start = time.monotonic()
    proc = endpoint = lease = child_socket = None
    slave = workdir = None
    diagnostic_tasks = []
    resize_task = None
    tail = bytearray()
    reason, exit_code = "failed_to_start", None
    mode_entered = False
    handled_failure = False
    try:
        problems = await asyncio.to_thread(preflight, door, session)
        if problems:
            raise ValueError("\n".join(problems))
        world_path = await lane.run(war_dialer_world_path, door)
        if problem := await asyncio.to_thread(war_dialer_path_problem, door, world_path):
            raise ValueError(problem)
        if profile:
            root = await lane.run(lambda db: db.path.parent / "door-nodes")
            identity = str(Path(profile.install_dir).resolve()) if profile.install_dir else f"door-{door.id}"
            # Small local lock operation; no await that could lose an acquired lease on cancellation.
            lease = NodeLease(root, identity, profile.max_sessions)
        workdir = Path(tempfile.mkdtemp(prefix="netbbs-door-"))
        info_path = await lane.run(_write_door_info, workdir, session, player, world_path is not None,
                                   effective_wall_limit(profile, wall_time_limit_seconds), door.id)
        info = json.loads(info_path.read_text(encoding="utf-8"))
        width = profile.width if profile and profile.width else session.terminal_width
        height = profile.height if profile and profile.height else session.terminal_height
        env = _door_environment(info_path, world_path)
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
                if world_path is not None:
                    # Keep a profile's relative path anchored to the server cwd,
                    # never the temporary node directory or installation directory.
                    env["WAR_DIALER_DB_PATH"] = str(world_path)
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
                limits = {"RLIMIT_AS": profile.memory_mb * 1024 * 1024 if profile else DOOR_MEMORY_LIMIT_BYTES,
                          "RLIMIT_NPROC": DOOR_MAX_PROCESSES}
                # A profile may remove the CPU ceiling outright (0). That is sent
                # as null, not as zero -- a limit of zero would kill the door on
                # its first scheduler tick, and simply omitting the key would
                # leave whatever soft limit this service inherited, which is not
                # what the screen and the guide promise.
                limits["RLIMIT_CPU"] = (profile.cpu_seconds if profile else DOOR_CPU_LIMIT_SECONDS) or None
                setup = {"pty": kind == "pty", "limits": limits}
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
            mode = resize_mode(profile, kind)
            if os.name == "posix" and mode is not None:
                resize_task = asyncio.create_task(_forward_resize(
                    session, proc, info_path, info, published=(width, height),
                    pty_fd=endpoint.fd if mode == "pty" else None, signal_door=mode == "signal"))
        try:
            reason = await asyncio.wait_for(_relay(terminal, endpoint, proc, stop_grace),
                                            timeout=effective_wall_limit(profile, wall_time_limit_seconds))
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
                # A capability probe's success includes its wire assertions,
                # not merely the emulator's exit code. Persist one final verdict.
                if reason == "exited" and output_check is not None:
                    problem = output_check()
                    if problem:
                        reason = "relay_failed"
                        tail.extend(problem.encode("utf-8", errors="replace")[:2048])
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
        handled_failure = True
        tail.extend(str(exc).encode())
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        handled_failure = True
        tail.extend(str(exc).encode("utf-8", errors="replace")[:4096])
        _logger.warning("door %r failed preflight/start: %s", door.name, exc)
    finally:
        primary = sys.exc_info()[1]

        async def cleanup():
            nonlocal exit_code
            errors = []
            # First, before the endpoint below is closed: the resize follower
            # captured the PTY master descriptor by number, and a descriptor
            # number is reused. It must never ioctl one this run no longer owns.
            if resize_task is not None:
                resize_task.cancel()
                await asyncio.gather(resize_task, return_exceptions=True)
            # A full StreamReader can pause the underlying pipe. After timeout
            # or disconnect the terminal pump is gone; drain without forwarding
            # so process reaping/pipe closure cannot depend on that slow caller.
            drains = []
            if proc is not None:
                if proc.stdout is not None and (endpoint is None or isinstance(endpoint, StreamEndpoint)):
                    drains.append(asyncio.create_task(_discard_output(proc.stdout)))
                if proc.stderr is not None and not diagnostic_tasks:
                    drains.append(asyncio.create_task(_discard_output(proc.stderr)))
            for operation in (lambda: _stop_process(proc, stop_grace) if proc is not None else None,
                              lambda: asyncio.gather(*drains),
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
                if workdir is not None and not rehearsal:
                    # Issue #520, and strictly before the workdir goes: the
                    # door has already been stopped above, so nothing races
                    # its own writes here, and a request left un-drained
                    # would be deleted along with the directory rather than
                    # answered. A failure is logged, never turned into an
                    # error: a door which exited cleanly must not be
                    # reported as having crashed because its drop directory
                    # was unreadable.
                    try:
                        await lane.run(drain_outbound, door, workdir, node_identity=node_identity)
                    except Exception as exc:
                        _logger.warning("door %r outbound drain failed: %s", door.name, exc)
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
            # A failure already turned into a reported reason must not be
            # replaced by a secondary one from cleanup. The obvious case is a
            # locked database: the launch fails, is handled, and then the
            # audit write fails the same way -- and re-raising that would hand
            # the caller an exception instead of the failure result they were
            # about to be shown.
            if errors and primary is None and not handled_failure:
                raise errors[0]
            return diagnostic

        diagnostic, cancelled = await _finish_owned(asyncio.create_task(cleanup()))
        if cancelled and primary is None:
            raise asyncio.CancelledError
    return DoorRunResult(exit_code, time.monotonic() - start, reason, diagnostic)
