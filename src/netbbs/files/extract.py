"""
Sandboxed external unpackers, shared by `FILE_ID.DIZ` reading
(`netbbs.files.diz`, design doc §6.2) and FTN mail bundles and nodelist
archives (`netbbs.ftn.bundle`, `netbbs.ftn.nodelist_feed`, §6.8).

NetBBS never installs an unpacker. A tool on `PATH` is the SysOp's opt-in,
and a node without one simply can't open that format. Every one of them
parses attacker-supplied bytes, so each run is narrow:

- argv lists only, never a shell;
- member content is read from the tool's **stdout**, never extracted to
  disk, so a crafted `../..` member name has nowhere to land; the tool
  runs in a private temporary folder all the same;
- on POSIX the tool runs behind the door launcher's post-exec
  `resource.setrlimit` helper (`netbbs.doors.launcher`, reached by path so
  this module never imports the doors package), with tight CPU, memory and
  process ceilings;
- the read is bounded, each spawn has a timeout, the whole attempt has an
  overall budget, and the child is killed and reaped on every exit path,
  cancellation included;
- a tool's exit status is its verdict on what it printed: output from a
  tool that then failed is not a result.

`run_extractor` is that one primitive. On top of it, `unpack_with_tools`
reads the members of an LHA, ARC, 7-Zip, RAR or ARJ archive held in
memory: it lists them, refuses a listing over the member limit or with an
unsafe name, lets the caller choose which members it wants, and reads
those one by one within a byte limit for all of them together, each
checked against the size the listing gave. ARC has no tool that lists
reliably, so its directory is walked here (a 29-byte header per member)
and the tool's whole output split by the sizes it names.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_logger = logging.getLogger(__name__)

EXTRACT_CPU_SECONDS = 10
EXTRACT_MEMORY_BYTES = 256 * 1024 * 1024
EXTRACT_MAX_PROCESSES = 16


@dataclass(frozen=True)
class ToolRun:
    """One unpacker run: its stdout when it exited 0 within the bounds,
    else `None` and why (`problem`)."""

    data: bytes | None
    problem: str | None = None


async def run_extractor(argv: list[str], *, deadline: float, limit: int, spawn_timeout: float,
                        cwd: str | None = None, label: str = "unpacker") -> ToolRun:
    """Run one already-resolved unpacker command and return what it wrote
    to stdout, at most `limit` bytes. `label` starts its log lines.

    The child is started in its own session on POSIX so a tool which
    spawns helpers can be killed as a group, and it is killed and reaped in
    `finally` -- including on cancellation, where leaving an unpacker
    running would outlive whatever asked for it."""
    tool = Path(argv[0]).name
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ToolRun(None, "out of time")
    if os.name == "posix":
        setup = {"pty": False, "limits": {"RLIMIT_CPU": EXTRACT_CPU_SECONDS,
                                          "RLIMIT_AS": EXTRACT_MEMORY_BYTES,
                                          "RLIMIT_NPROC": EXTRACT_MAX_PROCESSES}}
        # Reached by path, not by import: netbbs.doors.launcher imports
        # fcntl/termios at module scope (it is exec'd, never imported).
        launcher = Path(__file__).resolve().parent.parent / "doors" / "launcher.py"
        argv = [sys.executable, "-I", str(launcher), json.dumps(setup), *argv]
        spawn_kwargs = {"start_new_session": True}
    else:
        spawn_kwargs = {}

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            **spawn_kwargs,
        )
    except (OSError, ValueError) as exc:
        _logger.info("%s: could not run %s: %s", label, tool, exc)
        return ToolRun(None, f"could not run {tool}: {exc}")
    killed = True
    try:
        if proc.stdout is None:  # pragma: no cover - stdout=PIPE guarantees one
            return ToolRun(None, f"{tool} gave no output")
        deadline_left = min(remaining, spawn_timeout)
        started = time.monotonic()
        data = await asyncio.wait_for(_read_bounded(proc.stdout, limit + 1), timeout=deadline_left)
        if len(data) > limit:
            return ToolRun(None, f"{tool} printed more than {limit} bytes")
        # Exit status is the tool's own verdict on what it just printed
        # (Codex review): wait for it and believe it.
        await asyncio.wait_for(proc.wait(), timeout=max(0.1, deadline_left - (time.monotonic() - started)))
        killed = False
        if proc.returncode != 0:
            _logger.info("%s: %s exited %s; ignoring its output", label, tool, proc.returncode)
            return ToolRun(None, f"{tool} exited {proc.returncode}")
    except asyncio.TimeoutError:
        _logger.warning("%s: %s timed out; killing it", label, tool)
        return ToolRun(None, f"{tool} timed out")
    finally:
        if killed:
            await _stop(proc)
    return ToolRun(data)


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    """Read at most `limit` bytes. `StreamReader.read(n)` returns as soon
    as *any* data is available, so one call can't be trusted to have
    collected a whole member yet."""
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = await stream.read(min(limit - total, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Kill and reap, unconditionally, on every exit path.

    `SIGKILL` rather than a polite `SIGTERM` first: a tool wedged in a
    parser loop on a crafted archive is exactly the case a catchable signal
    would not end. On POSIX the whole process group goes, since
    `start_new_session` made this child its leader. A cancellation arriving
    mid-wait still leaves the child killed (the signal is sent before any
    await) and reaped -- the shielded `wait()` outlives the cancellation."""
    if proc.returncode is None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=2)


# --- archive members ----------------------------------------------------------


class ArchiveError(Exception):
    """An archive that can't be read: damaged, over a limit, an unsafe
    member name, or a tool that failed. The message says which."""


class ArchiveToolMissing(ArchiveError):
    """No unpacker for this format is installed. The message names the
    tools, and the pkgsrc packages that provide them."""


@dataclass(frozen=True)
class ArchiveTool:
    command: str  # looked up on PATH
    package: str  # the pkgsrc package that provides it
    style: str  # how it is driven: "7z", "unrar", "lha" or "nomarch"


_P7ZIP = (ArchiveTool("7z", "archivers/p7zip", "7z"), ArchiveTool("7za", "archivers/p7zip", "7z"),
          ArchiveTool("7zz", "archivers/7-zip", "7z"))

TOOLS: dict[str, tuple[ArchiveTool, ...]] = {
    "lha": (ArchiveTool("lha", "archivers/lha", "lha"), *_P7ZIP),
    "arc": (ArchiveTool("nomarch", "archivers/nomarch", "nomarch"),),
    "7z": _P7ZIP,
    "rar": (ArchiveTool("unrar", "archivers/unrar", "unrar"), *_P7ZIP),
    "arj": _P7ZIP,
}
"""Archive kind (`netbbs.ftn.bundle.archive_kind`) -> candidate tools, the
format's own first. ARC has no 7-Zip support; `nomarch` reads every ARC
method. ARJ goes through 7-Zip only, as for `FILE_ID.DIZ` (`arj` prints a
banner onto the stdout the member comes out of)."""

KIND_LABELS = {"lha": "LHA", "arc": "ARC", "7z": "7-Zip", "rar": "RAR", "arj": "ARJ"}

SPAWN_TIMEOUT_SECONDS = 60
LIST_LIMIT_BYTES = 1024 * 1024
_SUFFIX = {"lha": ".lzh", "arc": ".arc", "7z": ".7z", "rar": ".rar", "arj": ".arj"}
_UNSAFE_CHARACTERS = frozenset("*?[]")


@dataclass(frozen=True)
class Member:
    """One file in an archive: its path as the tool names it, its base
    name, and the size the listing gives (None when it gives none)."""

    path: str
    name: str
    size: int | None


def missing_tool_text(kind: str) -> str:
    """"install lha (pkgsrc archivers/lha) or 7z (archivers/p7zip)", for a
    reason a SysOp reads."""
    tools = TOOLS.get(kind, ())
    seen: list[str] = []
    for tool in tools:
        if tool.command in ("7za", "7zz"):
            continue  # 7z stands for the 7-Zip family
        seen.append(f"{tool.command} (pkgsrc {tool.package})")
    return "install " + " or ".join(seen)


def _resolve(command: str) -> list[str] | None:
    """The argv prefix that runs `command`, or None when it isn't on PATH.
    Tests point it at a stand-in."""
    found = shutil.which(command)
    return [found] if found else None


def _installed(kind: str) -> list[tuple[ArchiveTool, list[str]]]:
    found = []
    for tool in TOOLS.get(kind, ()):
        prefix = _resolve(tool.command)
        if prefix is not None:
            found.append((tool, prefix))
    return found


async def unpack_with_tools(
    data: bytes, kind: str, *, choose: Callable[[list[Member]], list[Member]], max_members: int,
    max_bytes: int, budget_seconds: float, label: str,
) -> list[tuple[Member, bytes]]:
    """The members `choose` picks from the archive `data`, with their
    content, in archive order. `choose` sees every file member (not
    folders) once the listing passed its checks, and may return none.
    Raises `ArchiveToolMissing` when no tool for `kind` is installed, and
    `ArchiveError` for everything else that goes wrong. Each installed
    tool is tried in turn until one lists the archive."""
    if kind not in TOOLS:
        raise ArchiveError(f"{kind} archives can't be opened")
    installed = _installed(kind)
    if not installed:
        raise ArchiveToolMissing(f"no unpacker for {KIND_LABELS[kind]} is installed: {missing_tool_text(kind)}")
    deadline = time.monotonic() + budget_seconds
    with tempfile.TemporaryDirectory(prefix="netbbs-unpack-") as workdir:
        archive = Path(workdir) / ("archive" + _SUFFIX[kind])
        archive.write_bytes(data)
        problems: list[str] = []
        for tool, prefix in installed:
            listed = await _list(tool, prefix, archive, data, deadline, workdir, label)
            if isinstance(listed, str):
                problems.append(listed)
                continue
            if len(listed) > max_members:
                raise ArchiveError(f"the archive holds {len(listed)} files, more than {max_members}")
            wanted = choose(listed)
            if not wanted:
                return []
            contents = await _read(tool, prefix, archive, listed, wanted, max_bytes, deadline, workdir, label)
            return list(zip(wanted, contents))
        raise ArchiveError("; ".join(problems))


async def _list(tool: ArchiveTool, prefix: list[str], archive: Path, data: bytes, deadline: float, workdir: str,
                label: str) -> list[Member] | str:
    """The archive's file members, or why the tool couldn't list them."""
    if tool.style == "nomarch":
        return _arc_directory(data)
    if tool.style == "7z":
        argv = [*prefix, "l", "-slt", "--", str(archive)]
    elif tool.style == "unrar":
        argv = [*prefix, "lt", "-p-", "-y", "--", str(archive)]
    else:
        argv = [*prefix, "l", str(archive)]
    run = await run_extractor(argv, deadline=deadline, limit=LIST_LIMIT_BYTES, spawn_timeout=SPAWN_TIMEOUT_SECONDS,
                              cwd=workdir, label=label)
    if run.data is None:
        return f"{tool.command} could not list it ({run.problem})"
    text = run.data.decode("utf-8", errors="replace")
    parse = {"7z": _parse_7z, "unrar": _parse_unrar, "lha": _parse_lha}[tool.style]
    return parse(text)


async def _read(tool: ArchiveTool, prefix: list[str], archive: Path, listed: list[Member], wanted: list[Member],
                max_bytes: int, deadline: float, workdir: str, label: str) -> list[bytes]:
    if tool.style == "nomarch":
        return await _read_arc(prefix, archive, listed, wanted, max_bytes, deadline, workdir, label)
    contents: list[bytes] = []
    total = 0
    for member in wanted:
        if member.size is not None and total + member.size > max_bytes:
            raise ArchiveError(f"the archive unpacks to more than {max_bytes} bytes")
        if tool.style == "7z":
            # -spd: the name is matched exactly, never as a wildcard.
            argv = [*prefix, "e", "-so", "-y", "-spd", "--", str(archive), member.path]
        elif tool.style == "unrar":
            argv = [*prefix, "p", "-inul", "-p-", "-y", "--", str(archive), member.path]
        else:
            argv = [*prefix, "pq", str(archive), member.path]
        run = await run_extractor(argv, deadline=deadline, limit=max_bytes - total,
                                  spawn_timeout=SPAWN_TIMEOUT_SECONDS, cwd=workdir, label=label)
        if run.data is None:
            if run.problem and "printed more than" in run.problem:
                raise ArchiveError(f"the archive unpacks to more than {max_bytes} bytes")
            raise ArchiveError(f"cannot unpack {member.name!r}: {run.problem}")
        _check_size(member, len(run.data))
        total += len(run.data)
        contents.append(run.data)
    return contents


def _check_size(member: Member, got: int) -> None:
    # 7-Zip exits 0 with nothing printed for a name it didn't find; the
    # listing's size is what tells that from an empty member.
    if member.size is not None and got != member.size:
        raise ArchiveError(f"{member.name!r} unpacked to {got} bytes, but the archive lists {member.size}")


def _member(path: str, size: int | None) -> Member | str:
    """A listed file, or why its name is refused: only printable ASCII,
    no wildcard characters, nothing that reads as an option, and a real
    base name. FTN bundles hold 8.3 names, so nothing real is refused."""
    if not path or not all(" " <= character <= "~" for character in path):
        return f"a member name {path!r} is not printable ASCII"
    if path.startswith("-") or _UNSAFE_CHARACTERS & set(path):
        return f"a member name {path!r} is unsafe"
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if name in ("", ".", ".."):
        return f"a member name {path!r} has no file name"
    return Member(path, name, size)


def _collect(entries: list[tuple[str, int | None]]) -> list[Member]:
    members = []
    for path, size in entries:
        member = _member(path, size)
        if isinstance(member, str):
            raise ArchiveError(member)
        members.append(member)
    return members


def _parse_7z(text: str) -> list[Member]:
    """`7z l -slt`: one block of `Key = value` lines per entry, after a
    block describing the archive itself (it has `Type`), which is skipped.
    `-ba` would drop that block, but not every 7-Zip build knows it."""
    entries: list[tuple[str, int | None]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields = {}
        for line in block.split("\n"):
            key, sep, value = line.partition(" = ")
            if sep:
                fields[key.strip()] = value
        if "Path" not in fields or "Type" in fields or "Physical Size" in fields:
            continue
        attributes = fields.get("Attributes", "").split()
        if fields.get("Folder") == "+" or (attributes and "D" in attributes[0]):
            continue
        if len(attributes) > 1 and attributes[1][:1] not in ("-", ""):
            raise ArchiveError(f"{fields['Path']!r} is not a plain file (a link or a device)")
        size = fields.get("Size", "")
        entries.append((fields["Path"], int(size) if size.isdigit() else None))
    return _collect(entries)


def _parse_unrar(text: str) -> list[Member]:
    """`unrar lt`: `Name:` and `Type:` lines per entry."""
    entries: list[tuple[str, int | None]] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if "Name" in current:
            kind = current.get("Type", "File")
            if kind == "File":
                size = current.get("Size", "")
                entries.append((current["Name"], int(size) if size.isdigit() else None))
            elif kind != "Directory":
                raise ArchiveError(f"{current['Name']!r} is not a plain file ({kind})")
        current.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        key, sep, value = line.strip().partition(": ")
        if not sep:
            continue
        if key == "Name":
            flush()
        current[key] = value
    flush()
    return _collect(entries)


_LHA_ROW = re.compile(r"^(?P<attr>\[[^\]]*\]|\S+)\s.*?(?P<size>\d+)\s+(?:\*+|[\d.]+%|-+)\s+"
                      r"[A-Z][a-z]{2} [ \d]\d (?: \d{4}|[ \d]\d:\d\d)\s(?P<name>.+)$")
"""One row of `lha l`: the attributes (`-rw-r--r--`, or `[MS-DOS]` for an
archive made on DOS), owner, size, ratio, a date, and the name."""


def _parse_lha(text: str) -> list[Member]:
    """`lha l` (LHa for UNIX and Lhasa print the same table): the rows
    between the two dashed rules, each ending in a date and the name."""
    row = _LHA_ROW
    lines = text.replace("\r\n", "\n").split("\n")
    rules = [index for index, line in enumerate(lines) if line.startswith("----------")]
    body = lines[rules[0] + 1:rules[1]] if len(rules) >= 2 else lines
    entries: list[tuple[str, int | None]] = []
    for line in body:
        if not line.strip():
            continue
        match = row.match(line)
        if match is None:
            raise ArchiveError(f"lha listed a line this node can't read: {line[:60]!r}")
        attr, name = match["attr"], match["name"]
        if attr.startswith("d") or name.endswith("/"):
            continue
        if attr.startswith("l") or " -> " in name:
            raise ArchiveError(f"{name!r} is a link, not a plain file")
        entries.append((name, int(match["size"])))
    return _collect(entries)


def _arc_directory(data: bytes) -> list[Member]:
    """An ARC archive's members, walked from its headers: `0x1A`, a method
    byte (0 ends the archive), a 13-byte name, the packed size, date, time,
    CRC-16 and (for every method but 1) the unpacked size, then the data."""
    entries: list[tuple[str, int | None]] = []
    position = 0
    while True:
        if position + 2 > len(data) or data[position] != 0x1A:
            raise ArchiveError("damaged ARC archive: a header is missing")
        method = data[position + 1]
        if method == 0:
            break
        if method >= 20:
            raise ArchiveError(f"ARC archive with a header of type {method} (comments or folders) can't be read")
        header = 25 if method == 1 else 29
        if position + header > len(data):
            raise ArchiveError("damaged ARC archive: a header is cut off")
        raw_name = data[position + 2:position + 15].split(b"\0", 1)[0]
        packed = int.from_bytes(data[position + 15:position + 19], "little")
        size = packed if method == 1 else int.from_bytes(data[position + 25:position + 29], "little")
        entries.append((raw_name.decode("latin-1"), size))
        position += header + packed
        if len(entries) > 100_000:
            raise ArchiveError("damaged ARC archive: too many headers")
    return _collect(entries)


async def _read_arc(prefix: list[str], archive: Path, listed: list[Member], wanted: list[Member], max_bytes: int,
                    deadline: float, workdir: str, label: str) -> list[bytes]:
    """`nomarch -p` prints every member in archive order; the stream is
    split by the sizes the headers name, and must add up to them."""
    expected = sum(member.size or 0 for member in listed)
    if expected > max_bytes:
        raise ArchiveError(f"the archive unpacks to more than {max_bytes} bytes")
    run = await run_extractor([*prefix, "-p", str(archive)], deadline=deadline, limit=expected,
                              spawn_timeout=SPAWN_TIMEOUT_SECONDS, cwd=workdir, label=label)
    if run.data is None:
        raise ArchiveError(f"cannot unpack it: {run.problem}")
    if len(run.data) != expected:
        raise ArchiveError(f"nomarch unpacked {len(run.data)} bytes, but the archive lists {expected}")
    pieces: dict[int, bytes] = {}
    offset = 0
    for index, member in enumerate(listed):
        pieces[index] = run.data[offset:offset + (member.size or 0)]
        offset += member.size or 0
    wanted_ids = {id(member) for member in wanted}
    return [pieces[index] for index, member in enumerate(listed) if id(member) in wanted_ids]


def run_blocking(coroutine_factory: Callable[[], object]):
    """Run an `unpack_with_tools` call from synchronous code (the nodelist
    import, on the database lane): in this thread when no event loop runs
    in it, else in a short-lived thread of its own."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine_factory())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coroutine_factory())).result()
