"""
`FILE_ID.DIZ` extraction, and the shape of a file entry's description.

The BBS-era convention (issue #463): an archive carries its own
catalogue text as a member named `FILE_ID.DIZ` -- up to 10 lines of at
most 45 columns, written by whoever *made* the archive rather than by
whoever happened to upload it. A file area which reads it gets an
accurate description for free, which is exactly why the convention
outlived the software that invented it.

Two extraction paths, deliberately asymmetric:

- **ZIP is handled in-process** by the standard library's `zipfile`,
  recognised by content (`zipfile.is_zipfile`) rather than by the
  uploader-chosen extension -- so a `.zip` renamed to something else,
  or a self-extracting `.exe` with a ZIP payload appended, still gets
  read.
- **Legacy formats shell out to whichever unpacker the SysOp has
  installed** (`lha`, `unrar`, `7z`/`7za`/`7zz`). NetBBS never installs
  one, and their presence on `PATH` *is* the opt-in: a node without
  them simply gets no description from those formats, which is a
  perfectly good outcome and never an upload failure. `.arj` goes
  through the 7-Zip family only -- `arj`'s own `p` command prints a
  copyright banner onto the same stdout the member content comes out
  of, and a description with a banner glued to the front is worse than
  no description.

Every one of those unpackers parses attacker-supplied bytes, and their
history of parser bugs is exactly why this module is as narrow as it
is:

- member content is read from the tool's **stdout**, never extracted to
  disk, so the classic `../..` path-traversal write in a crafted
  archive has nowhere to land;
- argv lists only, never a shell, and the member name is a fixed
  constant of this module rather than anything derived from the upload;
- on POSIX the tool runs behind the door launcher's own post-exec
  `resource.setrlimit` helper (`netbbs.doors.launcher`, reached by path
  so this module never imports the doors package), with far tighter
  CPU/memory/process ceilings than a door gets -- an unpacker that
  spins or allocates is killed by the kernel, not merely by us;
- the read is bounded, each spawn has a timeout, the whole attempt has
  an overall budget, and the child is killed and reaped on every exit
  path including cancellation;
- output larger than `MAX_DIZ_BYTES` is discarded rather than
  truncated: a real DIZ is under a kilobyte, so anything bigger is not
  one, and guessing at where to cut someone's "description" is worse
  than admitting there isn't one.

`normalize_description`/`fit_description` also define what a file
description *is* generally, not just one read out of an archive --
`netbbs.files.entries.set_file_description` validates the hand-written
kind against the same rules, so a description can never be shaped in a
way the area listing can't render or the Link `file_descriptor` can't
carry (design doc §11.2's own 4096-byte cap, mirrored here).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
import time
import unicodedata
import zipfile
from pathlib import Path

_logger = logging.getLogger(__name__)

DIZ_MEMBER_NAME = "FILE_ID.DIZ"
DIZ_MEMBER_NAMES = (DIZ_MEMBER_NAME, DIZ_MEMBER_NAME.lower())
"""The member names looked for, in order. DOS-era archives store the
name uppercase; one repacked on a case-sensitive filesystem may not,
and none of the external unpackers can be relied on to match a member
name case-insensitively across every build."""

MAX_DIZ_BYTES = 8192
"""Read cap for one extracted member. The DIZ spec allows 10 x 45
characters; this leaves generous room for the CP437 art people actually
put in them while staying far below anything worth calling a bomb."""

MAX_ZIP_CENTRAL_DIRECTORY_BYTES = 2 * 1024 * 1024
"""How large a ZIP's central directory may be before this node declines
to parse it at all (Codex review). `ZipFile` builds a `ZipInfo` for
every member up front, so the cost is set by the directory rather than
by the upload's size.

Measured in bytes rather than in the entry count the end-of-central-
directory record also carries, because only this number bounds the
work: CPython reads `size_cd` bytes and walks entries until they are
consumed (`ZipFile._RealGetContents`), never consulting the count,
which a crafted archive is free to understate. `size_cd` cannot lie the
same way -- those bytes have to actually be in the file or parsing
fails immediately. At 46 bytes minimum per entry this still allows
~45,000 members, far past any real release archive."""

MAX_DESCRIPTION_LINES = 10
"""The DIZ spec's own line limit, applied to every description
regardless of where it came from -- the area listing renders each line,
so an unbounded description would be an unbounded screen."""

MAX_DESCRIPTION_COLUMNS = 80
"""Per-line truncation for *extracted* text only (see
`fit_description`). The spec says 45; real archives overrun it, and 80
is the width every terminal has. Hand-written descriptions are not
column-limited -- `Session.write_line` wraps them."""

MAX_DESCRIPTION_BYTES = 4096
"""Matches `netbbs.link.protocol._MAX_FILE_DESCRIPTOR_DESCRIPTION_BYTES`
exactly: a description this node accepts locally must always be one a
peer will accept in a `file_descriptor`, or a linked area would
silently stop propagating uploads."""

_SPAWN_TIMEOUT_SECONDS = 5
"""Per-spawn wall-clock ceiling. Reading one small member out of even a
large archive is a seek-and-inflate, not a scan, so this is already
generous by an order of magnitude."""

_TOTAL_BUDGET_SECONDS = 15
"""Ceiling across every candidate tool and member-name variant for one
upload. Kept short deliberately: this runs while a caller sits at a
finished transfer with nothing on screen yet, and the only thing a long
budget buys is a longer silence before the same "no description"."""

_EXTRACT_CPU_SECONDS = 10
_EXTRACT_MEMORY_BYTES = 256 * 1024 * 1024
_EXTRACT_MAX_PROCESSES = 16

_ARCHIVE_TOOLS: dict[str, tuple[tuple[str, ...], ...]] = {
    ".lzh": (("lha", "pq", "{archive}", "{member}"), ("7z", "e", "-so", "-y", "{archive}", "{member}")),
    ".lha": (("lha", "pq", "{archive}", "{member}"), ("7z", "e", "-so", "-y", "{archive}", "{member}")),
    ".arj": (("7z", "e", "-so", "-y", "{archive}", "{member}"),),
    ".rar": (("unrar", "p", "-inul", "-y", "{archive}", "{member}"),
             ("7z", "e", "-so", "-y", "{archive}", "{member}")),
    ".7z": (("7z", "e", "-so", "-y", "{archive}", "{member}"),
            ("7za", "e", "-so", "-y", "{archive}", "{member}"),
            ("7zz", "e", "-so", "-y", "{archive}", "{member}")),
}
"""Extension -> ordered candidate commands. `{archive}`/`{member}` are
substituted positionally into the argv list; nothing here ever reaches
a shell. Order is "quiet and format-native first, general-purpose
second" -- see the module docstring on why `.arj` has no `arj` entry."""


_BIDI_CONTROLS = frozenset(
    "‪‫‬‭‮⁦⁧⁨⁩"
)
"""The same 9 embedding/override/isolate controls `netbbs.rendering.
sanitize` strips -- real visual-reordering potential, and a description
is displayed next to a filename people judge before downloading."""


def normalize_description(raw: str) -> str | None:
    """
    Clean one description into the only shape the rest of the system
    handles: newline-separated lines, no carriage returns, no control
    or bidi-override characters, no trailing whitespace on any line,
    and no leading/trailing blank lines. Returns `None` when nothing
    printable is left -- a description of `""` and no description at
    all are the same thing, and only one of them should ever reach the
    database.

    Deliberately does *not* enforce `MAX_DESCRIPTION_LINES`/
    `MAX_DESCRIPTION_BYTES`: a hand-written description that exceeds
    them is rejected with the draft intact (`netbbs.files.entries.
    set_file_description`), while extracted text is cut down instead
    (`fit_description`). Silently truncating what a caller typed is
    the one behaviour neither wants.

    Control characters are stripped here, at the domain boundary,
    rather than left for `netbbs.rendering.sanitize` at render time:
    this text is also carried over Link, written into the search index,
    and shown by the local CLI, so storing it already-clean is the only
    version that holds everywhere. Rendering still sanitizes -- that
    boundary doesn't get to trust its input either.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Unicode's own line/paragraph separators become ordinary newlines
    # rather than surviving as content (Codex review): `str.splitlines`
    # -- which `netbbs.files.entries.validate_description` counts lines
    # with -- treats them as breaks, while splitting on "\n" does not,
    # and a DIZ full of them would otherwise be fitted to ten "lines"
    # here and then rejected as more than ten there, failing an upload
    # this module promises never to fail.
    text = text.replace(" ", "\n").replace(" ", "\n").expandtabs(8)
    lines = []
    for line in text.split("\n"):
        kept = "".join(
            char for char in line
            if unicodedata.category(char) != "Cc" and char not in _BIDI_CONTROLS
        )
        lines.append(kept.rstrip())
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    cleaned = "\n".join(lines)
    return cleaned if cleaned.strip() else None



def fit_description(raw: str) -> str | None:
    """
    `normalize_description`, then cut the result down to something a
    listing can render: at most `MAX_DESCRIPTION_LINES` lines, each at
    most `MAX_DESCRIPTION_COLUMNS` characters, and the whole thing
    within `MAX_DESCRIPTION_BYTES` of UTF-8.

    For text this node did not author -- a DIZ out of a stranger's
    archive -- where cutting is the right answer and refusing the whole
    upload is not.
    """
    normalized = normalize_description(raw)
    if normalized is None:
        return None
    lines = [line[:MAX_DESCRIPTION_COLUMNS] for line in normalized.split("\n")[:MAX_DESCRIPTION_LINES]]
    while lines and len("\n".join(lines).encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        lines.pop()
    return normalize_description("\n".join(lines))


def decode_diz(raw: bytes) -> str | None:
    """
    Decode raw DIZ bytes: UTF-8 first (a DIZ written this century),
    CP437 otherwise (a DIZ written in the one encoding the format
    actually grew up in -- and the reason `╔══╗`-style art survives the
    trip instead of arriving as mojibake). CP437 cannot fail, so there
    is no third fallback and no `errors="replace"` damage.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp437")
    return fit_description(text)


async def read_archive_description(archive_path: Path, filename: str) -> str | None:
    """
    The description `FILE_ID.DIZ` inside `archive_path` claims, or
    `None` -- for an archive with no DIZ, a format with no installed
    unpacker, a file that is not an archive at all, and every kind of
    corruption or unpacker failure alike. `filename` is the uploader's
    own name for the content (the stored path is content-addressed and
    carries no extension), used only to choose which external unpacker
    to try.

    Never raises for bad input: an upload is not a compilation, and a
    file that arrived intact should never be rejected because something
    inside it was malformed. Genuinely unexpected failures are logged,
    not surfaced.
    """
    deadline = time.monotonic() + _TOTAL_BUDGET_SECONDS
    try:
        zip_diz = await asyncio.to_thread(_read_zip_diz, archive_path)
    except Exception:  # pragma: no cover - defensive: zipfile raising something new
        _logger.exception("FILE_ID.DIZ: reading %r as a ZIP failed unexpectedly", filename)
        zip_diz = None
    if zip_diz is not None:
        return decode_diz(zip_diz)

    suffix = Path(filename).suffix.lower()
    for command in _ARCHIVE_TOOLS.get(suffix, ()):
        tool = shutil.which(command[0])
        if tool is None:
            continue
        for member in DIZ_MEMBER_NAMES:
            substitutions = {"{archive}": str(archive_path), "{member}": member}
            argv = [tool, *(substitutions.get(part, part) for part in command[1:])]
            extracted = await _run_extractor(argv, deadline=deadline)
            if extracted:
                return decode_diz(extracted)
            if time.monotonic() >= deadline:
                _logger.warning("FILE_ID.DIZ: gave up on %r after %ss", filename, _TOTAL_BUDGET_SECONDS)
                return None
    return None


def _read_zip_diz(archive_path: Path) -> bytes | None:
    """The in-process ZIP half of `read_archive_description`, run in a
    worker thread because a large central directory is real disk I/O.

    `ZipInfo.file_size` is the archive's own claim about the member and
    is never trusted: the read itself is capped one byte past
    `MAX_DIZ_BYTES`, so a member which lies about its size (or inflates
    from nothing at all) costs one bounded read rather than memory.

    Neither is the *size of the central directory* (Codex review):
    `ZipFile()` parses it eagerly and builds a `ZipInfo` per member
    before any of this can look for a DIZ, so an archive of nothing but
    empty entries turns a modest upload into hundreds of megabytes of
    objects. Its declared size is read out of the end-of-central-
    directory record first, and anything past
    `MAX_ZIP_CENTRAL_DIRECTORY_BYTES` is left unread -- an archive with
    a directory that large is not one somebody wrote a `FILE_ID.DIZ`
    for."""
    directory_bytes = _zip_central_directory_bytes(archive_path)
    if directory_bytes is None or directory_bytes > MAX_ZIP_CENTRAL_DIRECTORY_BYTES:
        if directory_bytes is not None:
            _logger.info(
                "FILE_ID.DIZ: %s declares a %d-byte central directory, more than the %d "
                "this node will parse",
                archive_path.name, directory_bytes, MAX_ZIP_CENTRAL_DIRECTORY_BYTES,
            )
        return None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                # Split on both separators rather than through `Path`:
                # a DOS-era archiver may have stored a backslash, which
                # `PurePosixPath` would keep as part of the name.
                if info.filename.replace("\\", "/").rsplit("/", 1)[-1].upper() != DIZ_MEMBER_NAME:
                    continue
                with archive.open(info) as member:
                    data = member.read(MAX_DIZ_BYTES + 1)
                return None if len(data) > MAX_DIZ_BYTES else data
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError, EOFError) as exc:
        # RuntimeError: an encrypted member. ValueError/EOFError: a
        # truncated or otherwise inconsistent entry. None of these say
        # anything about the upload itself, which is already stored.
        _logger.info("FILE_ID.DIZ: %s is not readable as a ZIP: %s", archive_path.name, exc)
    return None


def _zip_central_directory_bytes(archive_path: Path) -> int | None:
    """How many bytes of central directory this file's end-of-central-
    directory record commits to, or `None` if it is not a readable ZIP
    at all -- read directly rather than through `zipfile`, whose only
    way to answer the question is to parse the whole thing first, which
    is precisely what this exists to avoid.

    The EOCD is the last 22 bytes plus an optional comment of up to
    64 KiB, so a bounded tail read finds it. A ZIP64 archive stores
    `0xFFFFFFFF` here and keeps the real size elsewhere; that is
    reported as "too large" rather than chased, since a genuine ZIP64
    central directory is far past anything worth scanning for a DIZ.
    """
    try:
        with archive_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            tail_length = min(size, 22 + 0xFFFF)
            handle.seek(size - tail_length)
            tail = handle.read(tail_length)
    except OSError as exc:
        _logger.info("FILE_ID.DIZ: could not read %s: %s", archive_path.name, exc)
        return None
    marker = tail.rfind(b"PK")
    if marker < 0 or len(tail) - marker < 22:
        return None
    return int.from_bytes(tail[marker + 12:marker + 16], "little")


async def _run_extractor(argv: list[str], *, deadline: float) -> bytes | None:
    """
    Run one already-resolved unpacker command and return what it wrote
    to stdout, bounded and never trusted. `None` for a tool that
    failed, printed nothing, timed out, or produced more than
    `MAX_DIZ_BYTES`.

    The child is started in its own session on POSIX so a tool which
    spawns helpers can be killed as a group, and it is killed and
    reaped in `finally` -- including on cancellation, where leaving an
    unpacker running against a caller's upload would outlive the
    session that asked for it.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    if os.name == "posix":
        setup = {"pty": False, "limits": {"RLIMIT_CPU": _EXTRACT_CPU_SECONDS,
                                          "RLIMIT_AS": _EXTRACT_MEMORY_BYTES,
                                          "RLIMIT_NPROC": _EXTRACT_MAX_PROCESSES}}
        # Reached by path, not by import: netbbs.doors.launcher imports
        # fcntl/termios at module scope (it is exec'd, never imported),
        # and nothing about reading a DIZ should pull in the doors
        # package.
        launcher = Path(__file__).resolve().parent.parent / "doors" / "launcher.py"
        argv = [sys.executable, "-I", str(launcher), json.dumps(setup), *argv]
        spawn_kwargs = {"start_new_session": True}
    else:
        spawn_kwargs = {}

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **spawn_kwargs,
        )
    except (OSError, ValueError) as exc:
        _logger.info("FILE_ID.DIZ: could not run %s: %s", argv[0], exc)
        return None
    killed = True
    try:
        if proc.stdout is None:  # pragma: no cover - stdout=PIPE guarantees one
            return None
        deadline_left = min(remaining, _SPAWN_TIMEOUT_SECONDS)
        started = time.monotonic()
        data = await asyncio.wait_for(
            _read_bounded(proc.stdout, MAX_DIZ_BYTES + 1), timeout=deadline_left
        )
        if len(data) > MAX_DIZ_BYTES:
            return None
        # Output alone is not a result (Codex review): an unpacker that
        # streams a damaged member and *then* reports a CRC error, or
        # one that writes a "no such member" line to stdout, would
        # otherwise hand back rubbish as a description -- and, worse,
        # stop the lowercase-name retry and the next candidate tool from
        # ever running. Exit status is the tool's own verdict on what it
        # just printed, so wait for it and believe it. The kill path
        # below stays for the cases where there is no verdict coming.
        await asyncio.wait_for(proc.wait(), timeout=max(0.1, deadline_left - (time.monotonic() - started)))
        killed = False
        if proc.returncode != 0:
            _logger.info(
                "FILE_ID.DIZ: %s exited %s; ignoring its output",
                Path(argv[0]).name, proc.returncode,
            )
            return None
    except asyncio.TimeoutError:
        _logger.warning("FILE_ID.DIZ: %s timed out; killing it", Path(argv[0]).name)
        return None
    finally:
        if killed:
            await _stop(proc)
    return data or None


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    """Read at most `limit` bytes. `StreamReader.read(n)` returns as
    soon as *any* data is available, so a single call can't be trusted
    to have collected a whole small member yet."""
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = await stream.read(limit - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Kill and reap, unconditionally, on every exit path.

    `SIGKILL` rather than a polite `SIGTERM` first: by the time this
    runs the unpacker's output is already read (or already too big, or
    already too slow) and nothing it could still do is wanted -- and a
    tool wedged in a parser loop on a crafted archive is exactly the
    case a catchable signal would not end. On POSIX the whole process
    group goes, since `start_new_session` made this child its leader
    and a tool which spawned a helper must not leave it behind.

    A cancellation arriving mid-wait still leaves the child killed (the
    signal is sent before any await) and still being reaped -- the
    shielded `wait()` outlives the cancellation, which is what keeps a
    zombie from accumulating per abandoned upload."""
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
