"""Bounded, read-only access to the node's own `netbbs.log` (issue #729).

The node writes its application log beside the database
(`netbbs.__main__`, a rotating handler of 10 MiB x 5). Until this module
the SysOp could read it only on the host; the SysOp console now shows its
tail. Everything here is synchronous file I/O -- callers on the event loop
run it through `asyncio.to_thread`.

Bounds, because a log is exactly the kind of file that grows while you are
looking at it:

- a read takes at most `MAX_READ_BYTES` from the end of the active file,
  topped up from the newest rotated file (`netbbs.log.1`) only while the
  active one is shorter than that, and never from older generations;
- at most `MAX_ENTRIES` parsed entries are kept, the newest;
- a follow poll reads at most `MAX_FOLLOW_BYTES` of new text.

Neither file is read through a symlink: the log lives in the node's state
directory, and a link there pointing elsewhere is not something a remote
SysOp session should be able to page through.

Transfer tokens are masked in everything this module returns. The web
listener's access log records request paths, and `/transfer/<token>` is a
bearer credential: a HEAD request is logged without spending it, so a
logged token can still be live. The file on disk is unchanged; only what
reaches a terminal is masked.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

NODE_LOG_FILENAME = "netbbs.log"

MAX_READ_BYTES = 512 * 1024
MAX_ENTRIES = 2000
MAX_FOLLOW_BYTES = 64 * 1024

#: Standard logging level names, lowest first.
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# `netbbs.__main__._LOG_FORMAT` is "%(asctime)s %(levelname)s:%(name)s:%(message)s"
# with datefmt "%Y-%m-%d %H:%M:%S". A line that does not start like this is a
# continuation of the entry before it -- a traceback, or a message with a
# newline in it.
_ENTRY_START = re.compile(
    r"^(?P<when>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:,\d+)?) "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL):(?P<logger>[^:]*):(?P<message>.*)$"
)


_TRANSFER_TOKEN = re.compile(r"(/transfer/)[^\s\"'?#]+")


def redact(text: str) -> str:
    """Mask bearer transfer tokens (see the module docstring)."""
    return _TRANSFER_TOKEN.sub(r"\1<token>", text)


def node_log_path(db_path: Path) -> Path:
    """Where the node writes its log: beside its database."""
    return Path(db_path).parent / NODE_LOG_FILENAME


def level_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


@dataclass(frozen=True)
class NodeLogEntry:
    #: Position in this read, oldest first -- only stable within one read.
    id: int
    when: str
    level: str
    logger: str
    message: str
    #: Continuation lines (a traceback), without the first line.
    continuation: tuple[str, ...] = ()

    @property
    def full_text(self) -> str:
        return "\n".join((self.message, *self.continuation))


@dataclass
class NodeLogRead:
    path: Path
    entries: list[NodeLogEntry] = field(default_factory=list)
    #: The file does not exist (yet): a node that has never started here, or
    #: one whose log was moved away.
    missing: bool = False
    #: Older text exists that this read did not reach.
    truncated: bool = False
    #: Why the log could not be read, for the screen to say as it is.
    error: str | None = None


def _refuse_symlink(path: Path) -> str | None:
    if path.is_symlink():
        return f"{path.name} is a symbolic link; NetBBS does not follow it."
    return None


class _NotRegularFile(OSError):
    pass


# O_NONBLOCK so that a FIFO put where the log belongs cannot block the
# open waiting for a writer; O_NOFOLLOW so a symlink swapped in after the
# check above is refused by the kernel. Neither exists on Windows, where
# neither risk does either.
_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
)


def _open_regular(path: Path):
    """Open `path` for binary reading only if it is a regular file."""
    fd = os.open(path, _OPEN_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _NotRegularFile(f"{path.name} is not a regular file; NetBBS does not read it.")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _describe(path: Path, exc: OSError) -> str:
    if isinstance(exc, _NotRegularFile):
        return str(exc)
    return f"could not read {path.name}: {exc.strerror or exc}"


def _read_tail(path: Path, budget: int) -> tuple[str, bool]:
    """The last `budget` bytes of `path` as text, and whether any were left
    out. A read that starts mid-file drops its first, partial line."""
    with _open_regular(path) as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - budget)
        handle.seek(start)
        data = handle.read(budget)
    text = data.decode("utf-8", errors="replace")
    if start > 0:
        _, _, text = text.partition("\n")
    return text, start > 0


def parse_log_lines(lines: list[str], *, first_id: int = 0) -> list[NodeLogEntry]:
    """Group raw lines into entries. Continuation lines before the first
    entry (the tail of one the read cut into) are dropped."""
    entries: list[NodeLogEntry] = []
    pending: dict | None = None

    def _flush() -> None:
        if pending is not None:
            entries.append(NodeLogEntry(
                id=first_id + len(entries), when=pending["when"], level=pending["level"],
                logger=pending["logger"], message=pending["message"],
                continuation=tuple(pending["continuation"]),
            ))

    for raw in lines:
        line = redact(raw.rstrip("\r"))
        match = _ENTRY_START.match(line)
        if match:
            _flush()
            pending = {**match.groupdict(), "continuation": []}
        elif pending is not None and line:
            pending["continuation"].append(line)
    _flush()
    return entries


def read_node_log(path: Path, *, max_bytes: int = MAX_READ_BYTES, max_entries: int = MAX_ENTRIES) -> NodeLogRead:
    """Read the newest part of the node log, bounded as the module says."""
    result = NodeLogRead(path=path)
    refusal = _refuse_symlink(path)
    if refusal is not None:
        result.error = refusal
        return result
    if not path.exists():
        result.missing = True
        return result
    try:
        active_text, active_cut = _read_tail(path, max_bytes)
        text = active_text
        truncated = active_cut
        rotated = path.with_name(path.name + ".1")
        remaining = max_bytes - len(active_text.encode("utf-8", errors="replace"))
        if not active_cut and remaining > 0 and rotated.exists() and _refuse_symlink(rotated) is None:
            try:
                older_text, older_cut = _read_tail(rotated, remaining)
            except _NotRegularFile:
                truncated = True
            else:
                text = older_text + text
                # Beyond `.1` there may be `.2` to `.5`; this read never reaches them.
                truncated = older_cut or rotated.with_name(path.name + ".2").exists()
        elif not active_cut and rotated.exists():
            truncated = True
    except OSError as exc:
        result.error = _describe(path, exc)
        return result
    entries = parse_log_lines(text.split("\n"))
    if len(entries) > max_entries:
        entries = entries[-max_entries:]
        truncated = True
        entries = [
            NodeLogEntry(id=index, when=e.when, level=e.level, logger=e.logger, message=e.message,
                         continuation=e.continuation)
            for index, e in enumerate(entries)
        ]
    result.entries = entries
    result.truncated = truncated
    return result


def entries_at_or_above(entries: list[NodeLogEntry], minimum: str) -> list[NodeLogEntry]:
    floor = level_rank(minimum)
    return [entry for entry in entries if level_rank(entry.level) >= floor]


class NodeLogFollower:
    """Reads what is appended to the log after it was created.

    Starts at the current end of the file. A file that shrinks, or is
    replaced by a new one, was rotated: reading restarts at its beginning.
    A line still being written is held back until its newline arrives."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._identity: tuple[int, int] | None = None
        self._partial = ""
        self._next_id = 0
        try:
            if _refuse_symlink(path) is None and path.exists():
                info = path.stat()
                self._offset = info.st_size
                self._identity = (info.st_dev, info.st_ino)
        except OSError:
            pass

    def poll(self) -> tuple[list[NodeLogEntry], str | None]:
        """New complete entries since the last poll, and an error to show
        if the file could not be read this time."""
        refusal = _refuse_symlink(self.path)
        if refusal is not None:
            return [], refusal
        try:
            if not self.path.exists():
                return [], None
            with _open_regular(self.path) as handle:
                info = os.fstat(handle.fileno())
                identity = (info.st_dev, info.st_ino)
                if identity != self._identity or info.st_size < self._offset:
                    self._identity = identity
                    self._offset = 0
                    self._partial = ""
                handle.seek(self._offset)
                data = handle.read(MAX_FOLLOW_BYTES)
        except OSError as exc:
            return [], _describe(self.path, exc)
        self._offset += len(data)
        text = self._partial + data.decode("utf-8", errors="replace")
        complete, newline, rest = text.rpartition("\n")
        if not newline:
            self._partial = text
            return [], None
        self._partial = rest
        entries = parse_log_lines(complete.split("\n"), first_id=self._next_id)
        self._next_id += len(entries)
        return entries, None
