"""
Reference-aware garbage collection for content-addressed file storage
(GitHub issue #35).

`netbbs.files.entries.delete_file()`, the expiry sweep, and
`netbbs.files.areas.delete_file_area()` all only ever remove the
SQLite `files` row -- the underlying blob in `netbbs.files.storage` is
deliberately left on disk, since another entry might reference the
same bytes (see that module's docstring on content-addressing). That's
correct as far as it goes, but nothing ever actually reclaims a blob
once its last reference is gone, so repeated upload/delete cycles grow
storage forever with no way to recover the space through the BBS's own
tooling.

Mark-and-sweep, not reference-counted deletion inside `delete_file()`
itself: enumerating every live `sha256` and every stored blob, then
deleting only blobs with no live reference, is what correctly handles
two entries sharing one blob without needing every call site that
might drop the last reference to a blob (delete, expiry, area
deletion) to individually know how to reference-count it.

Explicit, SysOp-triggered maintenance (design doc's existing posture:
no background scheduler exists anywhere in this codebase) -- not a
periodic background job. `dry_run=True` by default on the actual
reclaim step specifically because this is a destructive, one-way
operation on the filesystem, unlike everything else in this codebase
that's undoable via the database.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from netbbs.files.storage import storage_root
from netbbs.storage.database import Database

# How long a blob must sit with no live reference before a real (non-
# dry-run) pass will actually delete it. Guards the real, if narrow,
# race `upload_file()`'s own two-step contract creates (netbbs.files.
# storage.store_bytes() writes the blob *before* the referencing
# `files` row is ever inserted -- see entries.py's module docstring) --
# a blob can be transiently "orphaned" for the brief window between
# those two steps. An hour is comfortably longer than any real upload
# could take.
_DEFAULT_MIN_AGE_SECONDS = 3600.0


@dataclass(frozen=True)
class GCReport:
    """What a GC pass found/did. `dry_run` mirrors the same argument on
    the call that produced it, so a caller/UI can tell "these are the
    orphans that would be reclaimed" from "these were actually
    reclaimed" from the report alone, without threading the flag
    through separately."""

    dry_run: bool
    reclaimable_blobs: int
    reclaimable_bytes: int
    skipped_recent: int  # newer than min_age_seconds -- not touched, safety margin
    errors: list[str] = field(default_factory=list)


def _live_sha256_hashes(db: Database) -> set[str]:
    rows = db.connection.execute("SELECT DISTINCT sha256 FROM files").fetchall()
    return {row["sha256"] for row in rows}


def _is_content_addressed_name(name: str) -> bool:
    """Whether `name` looks like one of this module's own sha256
    blob filenames -- anything else found under the storage root is
    unexpected content this GC pass shouldn't touch (module docstring's
    "handled conservatively" contract), not assumed reclaimable just
    because it happens to live under the storage root."""
    return len(name) == 64 and all(c in "0123456789abcdef" for c in name)


def find_orphaned_blobs(db: Database) -> list[Path]:
    """Every blob under this node's storage root with no live
    `files.sha256` reference -- the "mark" half of mark-and-sweep."""
    root = storage_root(db)
    if not root.exists():
        return []
    live = _live_sha256_hashes(db)
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and _is_content_addressed_name(path.name) and path.name not in live
    ]


def reclaim_orphaned_blobs(
    db: Database, *, dry_run: bool = True, min_age_seconds: float = _DEFAULT_MIN_AGE_SECONDS
) -> GCReport:
    """
    The "sweep" half: deletes every blob `find_orphaned_blobs` finds
    and that has aged past `min_age_seconds`, unless `dry_run` (the
    default).

    Each candidate blob's liveness is re-checked individually
    immediately before it's actually unlinked (not just once, at the
    start, against the whole batch) -- narrows the window in which a
    fresh upload landing mid-sweep with byte-identical content (and
    therefore the same sha256) could have its blob deleted out from
    under its brand-new reference to the shortest practical span,
    rather than the whole scan-plus-delete pass.
    """
    orphans = find_orphaned_blobs(db)
    now = time.time()
    reclaimable_blobs = 0
    reclaimable_bytes = 0
    skipped_recent = 0
    errors: list[str] = []
    for path in orphans:
        try:
            stat = path.stat()
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if now - stat.st_mtime < min_age_seconds:
            skipped_recent += 1
            continue
        if not dry_run and path.name in _live_sha256_hashes(db):
            continue  # became referenced since the initial scan -- leave it alone
        reclaimable_blobs += 1
        reclaimable_bytes += stat.st_size
        if not dry_run:
            try:
                path.unlink()
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                reclaimable_blobs -= 1
                reclaimable_bytes -= stat.st_size
    return GCReport(
        dry_run=dry_run,
        reclaimable_blobs=reclaimable_blobs,
        reclaimable_bytes=reclaimable_bytes,
        skipped_recent=skipped_recent,
        errors=errors,
    )
