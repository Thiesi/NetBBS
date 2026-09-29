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
- a follow poll reads at most `MAX_FOLLOW_BYTES` of new text, and holds back
  at most `MAX_HELD_CHARS` of an entry that may still be growing.

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

import codecs
import logging
import os
import re
import stat
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

NODE_LOG_FILENAME = "netbbs.log"

MAX_READ_BYTES = 512 * 1024
MAX_ENTRIES = 2000
MAX_FOLLOW_BYTES = 64 * 1024
MAX_HELD_CHARS = 64 * 1024

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


#: Appended to a followed entry released at `MAX_HELD_CHARS`.
ENTRY_CUT_MARKER = "  [entry cut: longer than NetBBS shows]"

#: What the file log writes before every line of an entry after its first.
CONTINUATION_INDENT = "  "


class ContinuationSafeFormatter(logging.Formatter):
    """The node's file-log formatter: every line after an entry's first is
    indented, so only a real entry starts with a timestamp at column 0.

    Logged messages can carry text from outside -- a Link peer's error body,
    a caller's input -- and a newline in it followed by something shaped
    like `2026-09-27 10:00:00 CRITICAL:...` would otherwise read, in the
    file and in Operations -> Node log, as an entry the node never wrote."""

    def format(self, record: logging.LogRecord) -> str:
        first, *rest = super().format(record).split("\n")
        return "\n".join([first, *(CONTINUATION_INDENT + line for line in rest)])


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


def _read_tail(path: Path, budget: int) -> tuple[str, bool, int, tuple[int, int]]:
    """The last `budget` bytes of `path` as text, whether any were left out,
    how many bytes were read, and the file's identity. A read that starts
    mid-line drops that partial first line; one that starts exactly at a
    line keeps it."""
    with _open_regular(path) as handle:
        info = os.fstat(handle.fileno())
        identity = (info.st_dev, info.st_ino)
        size = info.st_size
        start = max(0, size - budget)
        # One byte more, to see whether `start` begins a line.
        handle.seek(max(0, start - 1))
        data = handle.read(budget + (1 if start > 0 else 0))
    if start > 0:
        starts_a_line = data[:1] == b"\n"
        data = data[1:]
    text = data.decode("utf-8", errors="replace")
    if start > 0 and not starts_a_line:
        _, _, text = text.partition("\n")
    return text, start > 0, len(data), identity


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
        active_text, active_cut, active_bytes, active_identity = _read_tail(path, max_bytes)
        text = active_text
        truncated = active_cut
        rotated = path.with_name(path.name + ".1")
        remaining = max_bytes - active_bytes
        if not active_cut and remaining > 0 and rotated.exists() and _refuse_symlink(rotated) is None:
            try:
                older_text, older_cut, _, older_identity = _read_tail(rotated, remaining)
            except OSError:
                # `.1` is a top-up: unreadable (or renamed to `.2` by a
                # rollover since the check) costs its lines, not the active
                # file's that were read fine.
                truncated = True
            else:
                if older_identity == active_identity:
                    # The log rolled over between the two reads: `.1` is the
                    # generation already read above. Show it once.
                    older_text, older_cut = "", True
                # A generation that ended mid-line (a crash before rotation)
                # must not glue its last fragment onto the active file's
                # first entry.
                if older_text and not older_text.endswith("\n"):
                    older_text += "\n"
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


class StableEntryIds:
    """Keeps an entry's number the same across re-reads of one screen.

    `read_node_log` numbers entries by position, which shifts whenever the
    retained window moves (new lines, a rotation). The picker identifies a
    row by this number (reopening the list on it), so an
    entry seen before keeps its number -- matched by its text and by how
    many identical entries precede it -- and only entries new to this
    screen get new ones."""

    def __init__(self) -> None:
        self._known: dict[tuple, int] = {}
        self._next = 1

    def apply(self, entries: list[NodeLogEntry]) -> list[NodeLogEntry]:
        seen: dict[tuple, int] = {}
        numbered = []
        current: dict[tuple, int] = {}
        for entry in entries:
            text = (entry.when, entry.level, entry.logger, entry.message, entry.continuation)
            occurrence = seen.get(text, 0)
            seen[text] = occurrence + 1
            key = (*text, occurrence)
            number = self._known.get(key)
            if number is None:
                number = self._next
                self._next += 1
            current[key] = number
            numbered.append(dataclasses.replace(entry, id=number))
        # Only what this read holds is remembered: the window only moves
        # forward, so an entry gone from it does not come back, and the map
        # stays as bounded as the read (`MAX_ENTRIES`). `_next` never goes
        # back, so a forgotten number is never reused.
        self._known = current
        return numbered


def entries_at_or_above(entries: list[NodeLogEntry], minimum: str) -> list[NodeLogEntry]:
    floor = level_rank(minimum)
    return [entry for entry in entries if level_rank(entry.level) >= floor]


class NodeLogFollower:
    """Reads what is appended to the log after it was created.

    Starts at the current end of the file. A file that shrinks, or is
    replaced by a new one, was rotated: reading restarts at its beginning.
    A line still being written is held back until its newline arrives, and
    the newest entry is held back until the next entry starts, a poll finds
    nothing new, or it outgrows `MAX_HELD_CHARS` -- so a traceback that
    arrives across two reads stays with its entry. Bytes are decoded
    incrementally, so a character split across two reads survives."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._identity: tuple[int, int] | None = None
        self._partial = ""
        self._held: list[str] = []
        self._discarding = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
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
        ready: list[str] = []
        notice: str | None = None
        try:
            if not self.path.exists():
                return self._emit(self._finish_generation()), (
                    f"{self.path.name} is gone; nothing is being followed until it is back.")
            with _open_regular(self.path) as handle:
                info = os.fstat(handle.fileno())
                identity = (info.st_dev, info.st_ino)
                if identity != self._identity or info.st_size < self._offset:
                    # Rotated. What the old generation gained since the last
                    # poll is read from where it went (`.1`) before the new
                    # file is started, then everything held from it is done.
                    drained, notice = self._drain_rotated()
                    ready = drained + self._finish_generation()
                    self._identity = identity
                    self._offset = 0
                handle.seek(self._offset)
                data = handle.read(MAX_FOLLOW_BYTES)
        except OSError as exc:
            return self._emit(ready), _describe(self.path, exc)
        self._offset += len(data)
        ready += self._consume(data, hold=True)
        return self._emit(ready), notice

    def _consume(self, data: bytes, *, hold: bool) -> list[str]:
        """Decode `data` onto what was pending and return the lines ready to
        parse, keeping the newest entry back when `hold` (see the class)."""
        text = self._decoder.decode(data)
        if self._discarding:
            # The rest of a line already cut at `MAX_HELD_CHARS`.
            _, newline, tail = text.partition("\n")
            if not newline:
                text = ""
            else:
                self._discarding = False
                text = "\n" + tail
        text = self._partial + text
        complete, newline, rest = text.rpartition("\n")
        if newline:
            self._partial = rest
            lines = self._held + complete.split("\n")
        else:
            self._partial = text
            lines = list(self._held)
        self._held = []
        if len(self._partial) > MAX_HELD_CHARS:
            self._partial = self._partial[:MAX_HELD_CHARS] + " [line cut: longer than NetBBS shows]"
            self._discarding = True
        # An idle poll releases the held entry only once no line of it is
        # still half-written; otherwise the rest of that line would arrive
        # later with no header to belong to.
        if hold and (data or self._partial):
            last_header = max(
                (index for index, line in enumerate(lines) if _ENTRY_START.match(redact(line.rstrip("\r")))),
                default=None,
            )
            if last_header is not None:
                if sum(len(line) for line in lines[last_header:]) <= MAX_HELD_CHARS:
                    self._held = lines[last_header:]
                    lines = lines[:last_header]
                else:
                    # Released at its size limit: say so, because the rest of
                    # its traceback arrives without a header and is dropped.
                    lines.append(ENTRY_CUT_MARKER)
        return lines

    def _drain_rotated(self) -> tuple[list[str], str | None]:
        """The unread end of the generation this follower was reading, if
        it is now `netbbs.log.1` -- bounded like any other poll, with a
        notice naming what that bound left unread."""
        rotated = self.path.with_name(self.path.name + ".1")
        if self._identity is None or _refuse_symlink(rotated) is not None or not rotated.exists():
            return [], None
        try:
            with _open_regular(rotated) as handle:
                info = os.fstat(handle.fileno())
                if (info.st_dev, info.st_ino) != self._identity:
                    return [], None
                handle.seek(self._offset)
                data = handle.read(MAX_FOLLOW_BYTES)
        except OSError as exc:
            return [], (f"The log rotated, and {_describe(rotated, exc)}; lines written just before the "
                        "rotation are not shown here.")
        skipped = info.st_size - self._offset - len(data)
        notice = (
            f"The log rotated after a burst; {skipped} bytes written just before it are not shown here "
            f"(they are in {rotated.name})." if skipped > 0 else None
        )
        return self._consume(data, hold=False), notice

    def _finish_generation(self) -> list[str]:
        """Everything still pending from a file that will not grow again."""
        tail = self._partial + self._decoder.decode(b"", final=True)
        lines = self._held + ([tail] if tail else [])
        self._held, self._partial, self._discarding = [], "", False
        self._decoder.reset()
        return lines

    def _emit(self, lines: list[str]) -> list[NodeLogEntry]:
        entries = parse_log_lines(lines, first_id=self._next_id)
        self._next_id += len(entries)
        return entries
