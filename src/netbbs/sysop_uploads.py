"""Files a SysOp sends to the node from inside NetBBS (issue #728).

Two SysOp screens work only with files already on the host: a banner or
masthead's **From disk**, and the doors' **From disk**. Until this module a
SysOp who reached their node only through NetBBS had to find an OS-level
SSH/SCP account to get anything there. Now the same transfer machinery that
carries file-area uploads (Zmodem, or a single-use browser link) can deliver
one file to one fixed destination:

- a banner or masthead piece's exact file (`BANNER`), capped at the size
  `[E]nable` accepts; never enabled by the upload itself;
- a named file in the node's own doors directory (`DOOR_FILE`), capped at the
  node's upload limit; never registered as a door by the upload itself.

**Destination first.** The SysOp chooses where the file goes -- the piece, or
the door file's name -- and confirms replacing an existing file *before* any
transfer is offered. The name the sending client reports is ignored: a
browser upload finishes somewhere the terminal cannot ask a follow-up
question, so nothing about where the bytes land may depend on the upload
itself.

**No new trust boundary.** A SysOp can already point a door at any program on
the host and run it with **Test as SysOp**; this makes placing that program
easier, not possible. Every upload is audit-logged with its size and
destination.

**Atomic.** The bytes are written to a temporary file beside the destination
and moved over it, so a failed or interrupted upload leaves the previous file
as it was, and a caller never sees half a banner.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

BANNER = "banner"
DOOR_FILE = "door_file"

#: A door file's name: long enough for any real script name, short enough to
#: fit a picker row.
MAX_DOOR_FILENAME_LENGTH = 100


class SysOpUploadError(Exception):
    """An upload that cannot be installed; the message is for the SysOp."""


@dataclass(frozen=True)
class SysOpUploadTarget:
    """Where one SysOp upload goes, decided before the transfer is offered."""

    kind: str
    destination: Path
    max_bytes: int
    #: What the screen calls it ("the welcome banner", "door file foo.py").
    label: str
    #: The `moderation.log` action recorded when it lands.
    audit_action: str
    #: Whether the SysOp agreed to replace a file already there. Checked again
    #: when the bytes arrive: a file that appeared at the destination since
    #: (another session's upload) is not overwritten on a consent never given.
    replaces: bool = False


def door_filename_error(name: str) -> str | None:
    """Why `name` cannot be a file in the doors directory, or `None`.

    A plain name only: no directory part, nothing hidden, nothing that
    means "this directory" or "the one above", no control characters."""
    if not name:
        return "Give the file a name."
    if len(name) > MAX_DOOR_FILENAME_LENGTH:
        return f"Keep the name to {MAX_DOOR_FILENAME_LENGTH} characters."
    if "/" in name or "\\" in name or name in (".", ".."):
        return "Give a plain file name, without a folder."
    if name.startswith("."):
        return "The name cannot start with a dot."
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return "The name cannot contain control characters."
    if name != name.strip():
        return "The name cannot start or end with a space."
    if ":" in name:
        return "The name cannot contain a colon."
    return None


def destination_problem(destination: Path) -> str | None:
    """Why nothing can be written at `destination`, or `None`. A symlink or
    a directory there is refused rather than followed or replaced."""
    if destination.is_symlink():
        return f"{destination.name} is a symbolic link; NetBBS will not write through it."
    if destination.exists() and not destination.is_file():
        return f"{destination.name} exists and is not a file."
    return None


def install_upload(target: SysOpUploadTarget, source: Path) -> int:
    """Move a received upload at `source` into place; return its size.

    `source` is always consumed (removed), whether this succeeds or not.
    Copied rather than renamed into the destination directory, because the
    staging area and the doors directory need not share a filesystem; the
    copy lands under a temporary name beside the destination and is then
    renamed over it, which is atomic on one filesystem. A replaced file's
    permission bits carry over to its successor, so a door that was
    executable stays executable.

    Cleanup never replaces the outcome: a leftover temporary or staging file
    is logged, not raised, whether the install succeeded or failed."""
    try:
        size = source.stat().st_size
        if size == 0:
            raise SysOpUploadError("No file was sent.")
        if size > target.max_bytes:
            raise SysOpUploadError(
                f"That file is {size} bytes, over the {target.max_bytes} byte limit for {target.label}."
            )
        problem = destination_problem(target.destination)
        if problem is not None:
            raise SysOpUploadError(problem)
        if target.destination.exists() and not target.replaces:
            raise SysOpUploadError(
                f"{target.destination.name} appeared since you started this upload; nothing was replaced. "
                "Upload again to replace it."
            )
        target.destination.parent.mkdir(parents=True, exist_ok=True)
        partial = target.destination.with_name(
            f".{target.destination.name}.upload-{secrets.token_hex(4)}"
        )
        try:
            shutil.copyfile(source, partial)
            if target.destination.exists():
                shutil.copymode(target.destination, partial)
            os.replace(partial, target.destination)
        except BaseException:
            _discard(partial)
            raise
        return size
    finally:
        _discard(source)


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _logger.warning("could not remove %s: %s", path, exc)
