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
import re
import secrets
import shutil
import signal
import stat
import socket
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
from netbbs.doors.profiles import preflight, terminal_too_small
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.timeutil import resolve_display_preferences
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.net.session import SessionClosedError, physical_terminal_width
from netbbs.net.session_activity import records_activity
from netbbs.net.shared_account import signed_in_without_credential
from netbbs.guest_call import current_guest_call, discard_guest_call, new_guest_call
from netbbs.moderation.log import record_action
from netbbs.rendering.charset import CP437, UTF8, encode_text, input_codec

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
#: How often a running door's outbound requests are picked up (issue #520,
#: decision A2), and how many one pick-up answers. The rate ceiling, not the
#: tick, bounds how much a door publishes; the tick bounds how late it
#: appears, and the cap keeps each pick-up a short job on the shared lane.
_OUTBOUND_TICK_SECONDS = 2.0
_OUTBOUND_TICK_LIMIT = 16


class _TerminalTooSmall(Exception):
    """The caller's terminal is smaller than the door's fixed geometry (issue #956)."""


@dataclass(frozen=True)
class DoorRunResult:
    exit_code: int | None
    duration_seconds: float
    reason: str
    diagnostic: str = ""


#: Version of the `door_info.json` contract (issue #469). 1 was the original
#: six fields; 2 adds the caller/node metadata below; 3 adds the optional
#: `outbound` object (issue #520), present only for a door whose SysOp has
#: switched its outbound hook on; 4 adds `outbound.channels` and
#: `outbound.chat_lines_per_hour`, and requests naming a `channel`. A door may
#: refuse a platform it does not understand instead of probing for fields.
DOOR_API_VERSION = 4


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
                     door_id=None, rehearsal=False, user_id=None):
    # `user_id` is a guest call's own door identity (issue #1075), never an
    # account's; otherwise the player's.
    info = {"handle": player.username, "user_id": player.id if user_id is None else user_id,
            "terminal_width": physical_terminal_width(session), "terminal_height": session.terminal_height,
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
    if door_id is not None and (
            outbound := door_info_block(db, door_id, rehearsal=rehearsal)) is not None:
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


#: Where a Voidrunner save lands, as resolved *by the node process*.
#: Recorded so `netbbs.backup` does not have to guess (issue #555).
VOIDRUNNER_SAVE_DIR_CONFIG_KEY = "voidrunner_save_dir"


#: The node's own Voidrunner directory, beside War Dialer's world (issue #648).
VOIDRUNNER_DIRNAME = "voidrunner"


def node_voidrunner_save_dir(db_path: Path) -> Path:
    """`<db path>.doors/voidrunner/`: this node's careers, by construction."""
    node_path = Path(db_path).resolve()
    return node_path.parent / (node_path.name + ".doors") / VOIDRUNNER_DIRNAME


def legacy_voidrunner_save_dir() -> Path | None:
    """`~/.netbbs/voidrunner_saves`, where careers lived before issue #648.

    Keyed by the OS account rather than the node, so two nodes run by one
    user shared every career by user id -- which is why it is no longer
    the default. `None` when this process has no home directory at all.
    """
    try:
        return Path.home().resolve() / ".netbbs" / "voidrunner_saves"
    except RuntimeError:
        return None


def _careers_in(directory: Path | None) -> bool | None:
    """Whether a directory holds Voidrunner data: `True` for careers,
    scores or retained copies -- anything the backup component would
    capture -- `False` for nothing (absent, empty, lock files only), and
    `None` when it cannot be trusted either way: unreadable, or holding
    entries Voidrunner never writes.

    The third answer is why this is not a bool (#759 review). A node moves
    off the legacy directory only for a `True` from its own, and an
    unreadable legacy directory is not `False`: treating it as empty would
    record the empty target and forget the careers were ever this node's.
    """
    if directory is None:
        return False
    from netbbs.backup import BackupError, _voidrunner_files

    try:
        # `stat`, not `is_dir`: `is_dir` answers False for a directory it could
        # not stat, which would make an I/O error look like an absent one.
        if not stat.S_ISDIR(directory.stat().st_mode):
            return None
        return bool(_voidrunner_files(directory))
    except FileNotFoundError:
        return False
    except (BackupError, OSError) as exc:
        _logger.warning("Voidrunner save directory %s cannot be used as it stands: %s", directory, exc)
        return None


def _recorded_voidrunner_save_dir(db) -> str | None:
    row = db.connection.execute(
        "SELECT value FROM node_config WHERE key = ?", (VOIDRUNNER_SAVE_DIR_CONFIG_KEY,)
    ).fetchone()
    return row[0] if row is not None and row[0] else None


def _legacy_owned_by(db) -> Path | None:
    """The legacy directory, if *this node* used it -- and `None` otherwise.

    The evidence is the node's own record: every node since #555 wrote down
    the directory it handed its doors, and one that played from the home
    default wrote down exactly that path. A brand-new node has no such
    record, so it never adopts, or copies, careers another node left in the
    account's home directory: they are keyed by user id, and its user 5 is
    not theirs (#648 review). A node too old to have recorded anything and
    not started since is treated the same way; its careers stay on disk.
    """
    recorded, legacy = _recorded_voidrunner_save_dir(db), legacy_voidrunner_save_dir()
    if recorded is None or legacy is None:
        return None
    return legacy if Path(recorded).resolve() == legacy else None


def voidrunner_save_dir(db) -> Path:
    """Where this node's Voidrunner careers live.

    `VOIDRUNNER_SAVE_DIR` wins when the SysOp set it. Otherwise the node's
    own `<db path>.doors/voidrunner/`, except while this node's careers
    remain only in the legacy home directory -- a node whose first-start
    copy (`migrate_voidrunner_saves`) could not run yet keeps playing the
    careers it has rather than opening an empty directory beside them.
    """
    if override := os.environ.get("VOIDRUNNER_SAVE_DIR"):
        return Path(override).expanduser().resolve()
    own = node_voidrunner_save_dir(db.path)
    if _careers_in(own) is True:
        return own
    legacy = _legacy_owned_by(db)
    return legacy if _careers_in(legacy) is not False else own


def migrate_voidrunner_saves(db) -> Path | None:
    """Copy this node's legacy careers into its own directory once; return
    the directory copied from, or `None` when there was nothing to do.

    Runs at startup before the node records its save directory, and only for
    a node whose record says it played from the legacy directory
    (`_legacy_owned_by`). A copy, not a move: the legacy directory belongs
    to the OS account, and a second node -- or standalone Voidrunner -- may
    be reading it; moving it would hand those callers an empty career list.
    Each node takes its own copy and they stop sharing from then on.

    The copy is made under the legacy directory's maintenance gate, so no
    pilot is mid-checkpoint, into a staging directory renamed into place
    whole: a node killed half-way leaves no partial directory that
    `voidrunner_save_dir` would mistake for the real one. A target that
    exists but holds only lock files -- pre-created for ownership, say --
    is replaced; one that holds careers of its own is never overwritten,
    and wins; one holding anything else is left alone with a warning. A
    busy or unreadable legacy directory is left alone too. Each of those
    keeps the node on the legacy directory, and its record naming it, so
    the next start tries again.
    """
    if os.environ.get("VOIDRUNNER_SAVE_DIR"):
        return None
    own = node_voidrunner_save_dir(db.path)
    legacy = _legacy_owned_by(db)
    if legacy is None or _careers_in(legacy) is False:
        return None
    if _careers_in(own) is True:
        _logger.warning("Voidrunner careers exist both in %s and in %s; this node uses %s and copies nothing.",
                        own, legacy, own)
        return None
    from netbbs.backup import BackupError, _voidrunner_files
    from netbbs.doors.bundled.voidrunner import PilotBusy, maintenance_session

    staging = own.with_name(own.name + ".migrating")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        if own.exists() and _voidrunner_files(own):
            raise BackupError(f"{own} is not empty")
        with maintenance_session(legacy):
            for relative in _voidrunner_files(legacy):
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy / relative, target)
            staging.mkdir(parents=True, exist_ok=True)
            if own.exists():
                shutil.rmtree(own)  # lock files only, checked above
            os.replace(staging, own)
    except (PilotBusy, BackupError, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        _logger.warning("Voidrunner careers were not copied from %s to %s (%s); this node keeps using %s "
                        "and will try again at its next start.", legacy, own, exc, legacy)
        return None
    _logger.warning("Voidrunner careers copied from %s to %s. This node no longer reads %s; remove it once "
                    "no other NetBBS node and no standalone Voidrunner run by this account uses it.",
                    legacy, own, legacy)
    return legacy


def record_voidrunner_save_dir(db) -> Path:
    """Store where this node's doors keep their Voidrunner saves, and
    return it.

    The location is `voidrunner_save_dir`, which `run_door` also hands
    every launched door, so the node and the backup CLI agree by
    construction. That is the whole problem this exists to fix (issue
    #555): the saves used to default to `Path.home()`, and a node started
    by `examples/netbbs.rc` runs with `HOME=<state dir>` while
    `python -m netbbs.backup` run from a SysOp's shell has their own HOME,
    so the documented backup command quietly captured no careers at all.
    The default is now derived from the database path (issue #648), which
    the CLI has too; the record still decides a `VOIDRUNNER_SAVE_DIR`
    override and a legacy directory not yet copied, and it is the evidence
    `migrate_voidrunner_saves` needs that the legacy careers are this node's.

    Read before write, and written only when it has actually changed --
    an unconditional write would take SQLite's write lock on a path that
    runs at every startup, for a value that changes approximately never.
    See `_minted_once` for the same reasoning at greater length.
    """
    resolved = voidrunner_save_dir(db)
    if _recorded_voidrunner_save_dir(db) != str(resolved):
        db.connection.execute(
            "INSERT INTO node_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (VOIDRUNNER_SAVE_DIR_CONFIG_KEY, str(resolved)),
        )
        db.connection.commit()
    return resolved


#: The most Hall of Fame records a guest call copies into its own Voidrunner
#: directory -- the cap `netbbs.backup` puts on the whole directory.
_GUEST_SCORES_COPIED = 10000


def _prepare_guest_sandbox(call, world_path, voidrunner_dir):
    """Where a guest call plays the bundled doors (issue #1075): its own
    Voidrunner directory and its own copy of War Dialer's world, inside the
    call's directory and gone when the call ends. Returns `(voidrunner_dir,
    world_path)` for the door's environment.

    Made once, at the call's first launch, and played from for the rest of
    it, so a guest who leaves a game and comes back finds it where they left
    it. The Voidrunner directory starts with a copy of the node's Hall of
    Fame records, so a guest sees the real standings; what the guest scores
    is written to the copy. War Dialer's world is copied whole, through
    SQLite's backup API so a live game's writes cannot tear it: the guest
    plays against the node's real crews and exchanges, and none of it
    reaches them. A node whose world does not exist yet gives the guest a
    fresh one, as the door makes for anyone."""
    doors = call.subdirectory("doors")
    sandbox_voidrunner = doors / VOIDRUNNER_DIRNAME
    if not sandbox_voidrunner.exists():
        staging = doors / (VOIDRUNNER_DIRNAME + ".copying")
        shutil.rmtree(staging, ignore_errors=True)
        (staging / "scores").mkdir(parents=True)
        copied = 0
        try:
            records = sorted((Path(voidrunner_dir) / "scores").iterdir())
        except OSError:
            records = []
        for record in records:
            if copied >= _GUEST_SCORES_COPIED:
                break
            if record.is_symlink() or not record.is_file() or re.fullmatch(r"[0-9]+\.json", record.name) is None:
                continue
            try:
                shutil.copyfile(record, staging / "scores" / record.name)
            except OSError:
                continue
            copied += 1
        os.replace(staging, sandbox_voidrunner)
    sandbox_world = None
    if world_path is not None:
        sandbox_world = doors / "war-dialer.db"
        if not sandbox_world.exists() and Path(world_path).is_file():
            partial = doors / "war-dialer.db.copying"
            partial.unlink(missing_ok=True)
            source = sqlite3.connect(str(world_path), timeout=5)
            try:
                target = sqlite3.connect(str(partial))
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
            os.replace(partial, sandbox_world)
    return sandbox_voidrunner, sandbox_world


def _door_environment(info_path, war_dialer_path=None, voidrunner_dir=None):
    env = {"NETBBS_DOOR_INFO": str(info_path)}
    try:
        env["USERPROFILE" if os.name == "nt" else "HOME"] = str(Path.home())
    except RuntimeError:
        pass
    # A deliberate, narrow persistent-data location, resolved by the node
    # (`voidrunner_save_dir`) before entering the disposable door cwd. Never
    # forward the complete parent environment.
    if voidrunner_dir is not None:
        env["VOIDRUNNER_SAVE_DIR"] = str(voidrunner_dir)
    if war_dialer_path is not None:
        env["WAR_DIALER_DB_PATH"] = str(war_dialer_path)
    return env


class DoorTerminal:
    """A door's byte stream, transcoded between the door's encoding (UTF-8,
    CP437, or raw bytes passed through untouched) and the caller's
    terminal (`Session.output_charset`: UTF-8, CP437 or ASCII, issue
    #929). A CP437 door on a CP437 terminal needs nothing at all."""
    def __init__(self, session, encoding):
        self.session, self.encoding = session, encoding
        self.charset = getattr(session, "output_charset", UTF8)
        # Caller keystrokes, decoded before re-encoding for the door.
        self.decoder = codecs.getincrementaldecoder(input_codec(session))("replace")
        # A UTF-8 door's output, decoded before mapping for a narrower terminal.
        self.output_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending = deque()

    def _input_passes_through(self) -> bool:
        # What the terminal *types* is CP437 on a CP437 terminal and UTF-8
        # on every other one, an ASCII one included (review on #940).
        if self.encoding == "raw":
            return True
        door = "cp437" if self.encoding == "cp437" else "utf-8"
        return door == ("cp437" if self.charset == CP437 else "utf-8")

    async def read_byte(self):
        if self._input_passes_through():
            return await self.session.read_byte()
        door = "cp437" if self.encoding == "cp437" else "utf-8"
        while not self.pending:
            value = await self.session.read_byte()
            if value is not None:
                self.pending.extend(self.decoder.decode(bytes([value])).encode(door, errors="replace"))
        return self.pending.popleft()

    async def write_raw(self, data):
        if self.encoding == "raw":
            pass
        elif self.encoding == "cp437":
            if self.charset != CP437:
                data = encode_text(data.decode("cp437"), self.charset)
        elif self.charset != UTF8:
            data = encode_text(self.output_decoder.decode(data), self.charset)
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


def resize_mode(profile, endpoint_kind, *, bundled_follows_resize=False):
    """How a running door is told the caller resized, or None for not at all.

    Policy only, so it can be checked without a POSIX host; the caller adds
    the platform gate. A profile which pins width/height asked for a fixed
    screen, and DOS geometry is fixed by design, so only a native door
    following the caller's own terminal is ever notified.

    `bundled_follows_resize` is NetBBS vouching for its own door: the script
    being launched is this install's copy and the catalogue says it handles the
    signal. That needs no opt-in from the SysOp, and it is what lets a bundled
    door registered with no profile at all -- the gallery's default -- follow
    a resize (issue #645). For anybody else's door the profile still decides.
    """
    if profile is None:
        return "signal" if bundled_follows_resize and endpoint_kind in ("stdio", "socketpair") else None
    if profile.adapter != "native" or profile.width:
        return None
    if endpoint_kind == "pty":
        return "pty"
    if (profile.resize_signal or bundled_follows_resize) and endpoint_kind in ("stdio", "socketpair"):
        return "signal"
    return None


_CHILDREN_IGNORE_RESIZE_SIGNAL = False


def children_start_ignoring_resize_signal() -> bool:
    """Make every process this node spawns begin life ignoring `SIGUSR1`.

    The signal's default action ends a process, and a door cannot install its
    handler before its interpreter has started and its script has been compiled:
    most of a second on a small host, during which a caller who is still dragging
    a window, or a phone that rotates, would have the host signal a process with
    nothing to catch it (issue #645 review). An *ignored* disposition is inherited
    across `fork` and `exec`, so ignoring the signal here closes that window for
    the launcher and the door alike; a door that handles the signal replaces the
    disposition when it says so, and one that never does simply never hears it.
    That also covers a wrapper which does not `exec`, and a bundled door run as a
    module by an interpreter holding an older copy.

    The node itself has no use for `SIGUSR1`. If something else in this process
    has claimed it, or this is not the main thread, nothing is changed and the
    caller must not signal on NetBBS's own say-so.
    """
    global _CHILDREN_IGNORE_RESIZE_SIGNAL
    if _CHILDREN_IGNORE_RESIZE_SIGNAL:
        return True
    if not hasattr(signal, "SIGUSR1"):
        return False
    import threading

    if threading.current_thread() is not threading.main_thread():
        return False
    if signal.getsignal(signal.SIGUSR1) not in (signal.SIG_DFL, signal.SIG_IGN):
        return False
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    _CHILDREN_IGNORE_RESIZE_SIGNAL = True
    return True


def _is_bundled(door) -> bool:
    """Whether `door` launches this install's own copy of a bundled door."""
    from netbbs.doors.bundled import launched_bundled_door

    install_dir = door.profile.install_dir if door.profile else None
    return launched_bundled_door(door.executable_path, tuple(door.args), install_dir) is not None


def bundled_follows_resize(door) -> bool:
    """Whether `door` launches this install's own copy of a door that handles
    the resize signal."""
    from netbbs.doors.bundled import launched_bundled_door

    install_dir = door.profile.install_dir if door.profile else None
    entry = launched_bundled_door(door.executable_path, tuple(door.args), install_dir)
    return bool(entry and entry.follows_resize)


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
        current = (physical_terminal_width(session), session.terminal_height)
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


async def _publish_chat_lines(chat_fanout, published, door) -> None:
    """Deliver the chat lines a drain recorded to whoever is in the channel now."""
    if chat_fanout is None or not published:
        return
    try:
        await chat_fanout(published)
    except Exception as exc:
        # Already in the channel's scrollback and queued for Link: a failed
        # live delivery costs the moment, not the line.
        _logger.warning("door %r chat lines could not be delivered live: %s", door.name, exc)


async def _drain_while_running(lane, door, workdir, node_identity, rehearsal, *, rehearsed=None,
                               guest_receipts=False, chat_fanout=None,
                               interval=None):
    """Answer a door's outbound requests while it is still running (#520).

    A post made at the moment something happens is the point of a live hook;
    one made when the player leaves is a Chronicle. Each pass is bounded and
    non-final, so leftovers wait for the next one and only the drain at exit
    refuses anything. A failure is logged and the next pass tries again: it
    must never end the caller's session.

    A cancellation lands only between passes. A pass works in threads --
    the lane's and `to_thread`'s -- which a cancelled await does not stop,
    so a pass interrupted mid-way would keep writing into the working
    directory while the caller deleted it, and the chat lines it had just
    recorded would never be delivered. The caller waits for the pass instead,
    and a pass is bounded.
    """
    from netbbs.doors.outbound import RESULTS_KEPT, has_requests, results_dir

    receipts = await lane.run(lambda db: results_dir(db, door.id)) if guest_receipts else None

    async def one_pass():
        try:
            if await asyncio.to_thread(has_requests, workdir):
                published = []
                await lane.run(drain_outbound, door, workdir,
                               node_identity=node_identity() if callable(node_identity) else node_identity,
                               rehearsal=rehearsal, rehearsed=rehearsed,
                               limit=_OUTBOUND_TICK_LIMIT, final=False, published=published)
                await _publish_chat_lines(chat_fanout, published, door)
            if receipts is not None:
                # Every tick, not only after this session drained: another
                # session of the same door may have been answered, and a native
                # door would see that receipt in the shared directory at once.
                from netbbs.doors.vm import copy_receipts
                await asyncio.to_thread(copy_receipts, receipts, workdir, RESULTS_KEPT)
        except Exception as exc:
            _logger.warning("door %r in-session outbound drain failed: %s", door.name, exc)

    while True:
        await asyncio.sleep(interval or _OUTBOUND_TICK_SECONDS)
        _, cancelled = await _finish_owned(asyncio.ensure_future(one_pass()))
        if cancelled:
            raise asyncio.CancelledError


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


@records_activity(lambda args: args["door"].name)
async def run_door(session, lane, door, player, *, wall_time_limit_seconds=None,
                   output_check=None, node_identity=None, rehearsal=False, chat_fanout=None):
    """Supervise and record one run; an optional synchronous probe check returns an error string.

    `chat_fanout`, an async callable taking `[(channel, message), ...]`,
    delivers the chat lines a door sent through its outbound hook to the
    people in those channels right now, and to live-subscribed Link peers.
    Without it a line still reaches the channel's scrollback and the Link
    queue, so a reader sees it on their next visit rather than as it happens.

    `node_identity`, when this node has Link running, is what lets a post a
    door made through its outbound hook (issue #520) reach the peers a
    Linked board is linked to -- the same `queue_board_post_if_linked` call
    the interactive posting path makes. `None` (Link off, or the standalone
    admin CLI) simply keeps the post local. It may be a callable returning
    the identity, read when the drain runs, so a run that outlasts a key
    rotation signs with the new key (issue #624).

    `rehearsal` marks a launch a SysOp made to *check* the door -- the
    compatibility screen's test launch, or the DOS probe -- rather than a
    caller playing it. Such a launch never publishes through the outbound
    hook: a SysOp trying a door out must not post its content to a real
    board, and a probe which runs the game on every preflight would do it
    repeatedly. Its requests are still answered, with what *would* have
    happened, so the test exercises the door's posting logic too (#520).
    """
    profile = door.profile
    stop_grace = profile.stop_grace_seconds if profile else DOOR_STOP_GRACE_SECONDS
    start = time.monotonic()
    proc = endpoint = lease = child_socket = None
    # A VM door's control channel (issue #474): the parent end asks the guest
    # to power down; the child end is qemu's, closed here once it is spawned.
    qmp_socket = qmp_child = None
    # A rehearsal's would-be spend, for the whole session: rehearsal posts are
    # never persisted, so without this every drain would start from zero.
    rehearsal_spend = {"posts": 0, "chat": 0}
    slave = workdir = None
    diagnostic_tasks = []
    resize_task = None
    outbound_task = None
    tail = bytearray()
    reason, exit_code = "failed_to_start", None
    mode_entered = False
    handled_failure = False
    # A guest call made here, when the session has none of its own to play in
    # (issue #1075); removed with the run.
    own_guest_call = None
    try:
        problems = await asyncio.to_thread(preflight, door, session, check_terminal=False)
        if problems:
            raise ValueError("\n".join(problems))
        # Checked after setup, so a door that is broken as well says so first.
        if profile and terminal_too_small(profile, session):
            raise _TerminalTooSmall(f"Terminal is {physical_terminal_width(session)}x{session.terminal_height}; "
                                    f"the door needs at least {profile.width}x{profile.height}.")
        world_path = await lane.run(war_dialer_world_path, door)
        if problem := await asyncio.to_thread(war_dialer_path_problem, door, world_path):
            raise ValueError(problem)
        if profile:
            root = await lane.run(lambda db: db.path.parent / "door-nodes")
            identity = str(Path(profile.install_dir).resolve()) if profile.install_dir else f"door-{door.id}"
            # Small local lock operation; no await that could lose an acquired lease on cancellation.
            lease = NodeLease(root, identity, profile.max_sessions)
        voidrunner_dir = await lane.run(voidrunner_save_dir)
        # Issue #1075: a session that signed in without a credential is one of
        # every anonymous caller sharing the guest account, and the bundled
        # doors key their saves on the account. Such a session plays them as
        # an identity of its own, with saves that are thrown away at hang-up
        # and never reach the Hall of Fame or the shared world. A SysOp's own
        # doors keep the account: what they store is theirs, and is gated by
        # level.
        guest_call = None
        if signed_in_without_credential(session) and await asyncio.to_thread(_is_bundled, door):
            guest_call = current_guest_call()
            if guest_call is None:
                guest_call = own_guest_call = new_guest_call()
            voidrunner_dir, world_path = await asyncio.to_thread(
                _prepare_guest_sandbox, guest_call, world_path, voidrunner_dir)
            # Nothing a guest's game does is published through the outbound
            # hook either: it answers as a rehearsal (issue #520) does.
            rehearsal = True
        workdir = Path(tempfile.mkdtemp(prefix="netbbs-door-"))
        info_path = await lane.run(_write_door_info, workdir, session, player, world_path is not None,
                                   effective_wall_limit(profile, wall_time_limit_seconds), door.id,
                                   rehearsal, guest_call.door_user_id if guest_call is not None else None)
        info = json.loads(info_path.read_text(encoding="utf-8"))
        width = profile.width if profile and profile.width else physical_terminal_width(session)
        height = profile.height if profile and profile.height else session.terminal_height
        env = _door_environment(info_path, world_path, voidrunner_dir)
        # Decided before the spawn, because the guard has to be in place before
        # there is a child to inherit it.
        vouched = (os.name == "posix" and bundled_follows_resize(door)
                   and children_start_ignoring_resize_signal())
        encoding = profile.encoding if profile else "utf-8"
        terminal = DoorTerminal(session, encoding)
        mode_entered = True
        session.door_active = True
        await session.enter_door_mode(encoding=encoding, width=(profile.width or None) if profile else None,
                                      height=(profile.height or None) if profile else None)
        if profile and profile.adapter == "rlogin":
            from netbbs.doors.remote import connect_remote
            endpoint = await connect_remote(profile, info, width, height)
        elif profile and profile.adapter == "bbslink":
            from netbbs.doors.bbslink import connect_bbslink
            endpoint = await connect_bbslink(profile, info, width, height)
        else:
            kind = profile.endpoint if profile else "stdio"
            stdin = stdout = asyncio.subprocess.PIPE
            pass_fds = ()
            if kind == "socketpair":
                endpoint, child_socket = socket_endpoint()
                pass_fds = (child_socket.fileno(),)
                stdin = asyncio.subprocess.DEVNULL
                if profile and profile.adapter == "vm":
                    qmp_socket, qmp_child = socket.socketpair()
                    pass_fds += (qmp_child.fileno(),)
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
                if guest_call is not None:
                    # A profile's own save directory is the account's, not
                    # this guest call's (issue #1075).
                    env["VOIDRUNNER_SAVE_DIR"] = str(voidrunner_dir)
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
                elif profile.adapter == "vm":
                    from netbbs.doors.outbound import RESULTS_KEPT
                    from netbbs.doors.vm import prepare_vm, publish_guest_info
                    argv = prepare_vm(door, workdir, lease.number, width, height,
                                      child_socket.fileno(), qmp_child.fileno())
                    info = await asyncio.to_thread(publish_guest_info, workdir, info_path, info, RESULTS_KEPT)
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
            if qmp_child:
                qmp_child.close()
                qmp_child = None
            if slave is not None:
                os.close(slave)
                slave = None
            if endpoint is None:
                endpoint = StreamEndpoint(proc.stdout, proc.stdin)
            elif kind == "socketpair":
                diagnostic_tasks.append(asyncio.create_task(_diagnostics(proc.stdout, tail)))
            diagnostic_tasks.append(asyncio.create_task(_diagnostics(proc.stderr, tail)))
            if profile and profile.adapter == "vm":
                from netbbs.doors.vm import options as vm_options, watch_boot
                diagnostic_tasks.append(asyncio.create_task(watch_boot(
                    workdir, proc, vm_options(profile)["boot_timeout_seconds"], tail)))
            if "outbound" in info:
                outbound_task = asyncio.create_task(_drain_while_running(
                    lane, door, workdir, node_identity, rehearsal, rehearsed=rehearsal_spend,
                    guest_receipts=bool(profile and profile.adapter == "vm"), chat_fanout=chat_fanout))
            mode = resize_mode(profile, kind, bundled_follows_resize=vouched)
            if os.name == "posix" and mode is not None:
                resize_task = asyncio.create_task(_forward_resize(
                    session, proc, info_path, info, published=(width, height),
                    pty_fd=endpoint.fd if mode == "pty" else None, signal_door=mode == "signal"))
        try:
            relay = asyncio.create_task(_relay(terminal, endpoint, proc, stop_grace))
            try:
                if profile and profile.adapter == "vm":
                    # A guest's boot is bounded by its own watchdog; the caller's
                    # time limit is for playing, and starts when the door does.
                    from netbbs.doors.vm import wait_booted
                    await wait_booted(workdir, relay)
                reason = await asyncio.wait_for(relay, timeout=effective_wall_limit(profile, wall_time_limit_seconds))
            finally:
                if not relay.done():
                    relay.cancel()
                    await asyncio.gather(relay, return_exceptions=True)
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
                elif profile and profile.adapter == "vm" and exit_code == 0:
                    from netbbs.doors.vm import guest_exit_code
                    exit_code, problem = guest_exit_code(profile, workdir)
                    tail.extend(problem.encode())
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
    except _TerminalTooSmall as exc:
        reason = "terminal_too_small"
        handled_failure = True
        tail.extend(str(exc).encode())
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
            # Before the final drain below. A pass already under way finishes
            # first (see `_drain_while_running`), so nothing it started is
            # still writing into the working directory once this returns.
            if outbound_task is not None:
                outbound_task.cancel()
                await asyncio.gather(outbound_task, return_exceptions=True)
            # A full StreamReader can pause the underlying pipe. After timeout
            # or disconnect the terminal pump is gone; drain without forwarding
            # so process reaping/pipe closure cannot depend on that slow caller.
            drains = []
            if proc is not None:
                if proc.stdout is not None and (endpoint is None or isinstance(endpoint, StreamEndpoint)):
                    drains.append(asyncio.create_task(_discard_output(proc.stdout)))
                if proc.stderr is not None and not diagnostic_tasks:
                    drains.append(asyncio.create_task(_discard_output(proc.stderr)))
            kill_grace = stop_grace
            if qmp_socket is not None and proc is not None:
                # Before the group is signalled: qemu exits on SIGTERM without
                # telling its guest, so the game would never hear the hangup.
                from netbbs.doors.vm import power_down
                began = time.monotonic()
                try:
                    await power_down(qmp_socket, proc, stop_grace)
                except Exception as exc:
                    errors.append(exc)
                # One deadline for the whole stop, as documented: whatever the
                # power-down used is not granted again after SIGTERM.
                kill_grace = max(1, stop_grace - (time.monotonic() - began))
            for operation in (lambda: _stop_process(proc, kill_grace) if proc is not None else None,
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
            for sock in (qmp_socket, qmp_child):
                if sock is not None:
                    sock.close()
            if slave is not None:
                os.close(slave)
            for task in diagnostic_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*diagnostic_tasks, return_exceptions=True)
            try:
                if mode_entered:
                    session.door_active = False
                    await session.leave_door_mode()
            except Exception as exc:
                errors.append(exc)
            finally:
                if workdir is not None:
                    # Issue #520, and strictly before the workdir goes: the
                    # door has already been stopped above, so nothing races
                    # its own writes here, and a request left un-drained
                    # would be deleted along with the directory rather than
                    # answered. A failure is logged, never turned into an
                    # error: a door which exited cleanly must not be
                    # reported as having crashed because its drop directory
                    # was unreadable.
                    published = []
                    try:
                        await lane.run(
                            drain_outbound, door, workdir,
                            node_identity=node_identity() if callable(node_identity) else node_identity,
                            rehearsal=rehearsal, rehearsed=rehearsal_spend, published=published,
                        )
                    except Exception as exc:
                        _logger.warning("door %r outbound drain failed: %s", door.name, exc)
                    await _publish_chat_lines(chat_fanout, published, door)
                if workdir is not None:
                    shutil.rmtree(workdir, ignore_errors=True)
                if own_guest_call is not None:
                    discard_guest_call(own_guest_call)
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
