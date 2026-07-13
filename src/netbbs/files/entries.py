"""
Individual files within a file area.

Content-addressed IDs (§7) computed from metadata *and* the uploaded
content's sha256 — unlike a post, where two different posts with
identical text are a real (if unusual) possibility that should still get
different IDs from their timestamps alone, a file's actual bytes are
central to what a file *is*, so its hash is folded into the ID
computation directly rather than relying only on an incidentally
differing timestamp.

A file row is only ever created after its bytes are already safely
written to storage (see `netbbs.files.storage`) — never the other way
around — so there's never a database row referencing storage that
doesn't exist.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from netbbs.auth.users import User
from netbbs.boards.content_id import compute_content_id
from netbbs.files.areas import FileArea
from netbbs.files.storage import read_bytes, store_bytes
from netbbs.permissions import require_level
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class FileEntryError(Exception):
    """Raised for file upload/lookup failures."""


@dataclass(frozen=True)
class FileEntry:
    id: int
    file_id: str
    area_id: int
    filename: str
    description: str | None
    size_bytes: int
    sha256: str
    storage_path: str
    uploader_user_id: int
    uploader_label: str
    uploader_fingerprint: str | None
    created_at: str


def upload_file(
    db: Database,
    area: FileArea,
    uploader: User,
    filename: str,
    data: bytes,
    *,
    description: str | None = None,
) -> FileEntry:
    """
    Store `data` in `area`, enforcing `area.min_write_level` via the same
    level-gating plumbing as `netbbs.boards.posts.create_post`.

    Takes the complete file as one in-memory `bytes` object — appropriate
    for this project's scale (design doc §14: dozens–low hundreds of
    concurrent users, not large-file streaming at volume) and for how
    files reach this function today (a dev script reading a local file;
    see `scripts/create_test_file.py`). Revisit if/when the actual
    upload transfer protocol (still unbuilt — see design doc) streams
    bytes incrementally rather than handing over a complete buffer.
    """
    require_level(uploader, area.min_write_level)

    sha256, path = store_bytes(db, data)
    created_at = utc_now_iso()
    uploader_identifier = uploader.fingerprint or uploader.username
    file_id = compute_content_id(
        {
            "type": "file",
            "area_id": area.area_id,
            "filename": filename,
            "sha256": sha256,
            "uploader": uploader_identifier,
            "created_at": created_at,
        }
    )

    try:
        db.connection.execute(
            """
            INSERT INTO files
                (file_id, area_id, filename, description, size_bytes, sha256,
                 storage_path, uploader_user_id, uploader_label,
                 uploader_fingerprint, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                area.id,
                filename,
                description,
                len(data),
                sha256,
                str(path),
                uploader.id,
                uploader.username,
                uploader.fingerprint,
                created_at,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise FileEntryError(
            "could not record upload — identical content uploaded twice in the same instant?"
        ) from exc

    return get_file(db, file_id)


def get_file(db: Database, file_id: str) -> FileEntry:
    row = db.connection.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
    if row is None:
        raise FileEntryError(f"no such file: {file_id!r}")
    return _row_to_file_entry(row)


def get_file_by_name(db: Database, area: FileArea, filename: str) -> FileEntry | None:
    """
    Look up a file in `area` by its exact stored `filename` — added
    alongside `list_files_page` (design doc round 31) specifically so
    `/download <filename>` (see `netbbs.net.file_flow._handle_download`)
    keeps working for a file that isn't on the *currently displayed*
    page. Pagination bounds what's fetched for browsing; it was never
    meant to bound what can be *referenced by name*, and the previous,
    unbounded `list_files` happened to make that distinction invisible
    since the full listing was always in memory anyway.

    `filename` is not unique within an area (unlike `file_id`) — two
    uploads can share a name (e.g. re-uploads/versions). Returns the
    *oldest* match, preserving exactly the tie-breaking behavior the
    old in-memory `next(entry for entry in files if entry.filename ==
    filename)` scan had, which always saw entries oldest-first.
    """
    row = db.connection.execute(
        """
        SELECT * FROM files
        WHERE area_id = ? AND filename = ?
        ORDER BY created_at ASC, file_id ASC
        LIMIT 1
        """,
        (area.id, filename),
    ).fetchone()
    return _row_to_file_entry(row) if row is not None else None


_DEFAULT_PAGE_SIZE = 5

FileEntryCursor = tuple[str, str]  # (created_at, file_id) -- see FileEntryPage/list_files_page


@dataclass(frozen=True)
class FileEntryPage:
    """One bounded page of file entries, always in chronological
    (oldest-first) order *within the page* — matches
    `netbbs.boards.posts.PostPage`'s shape and reasoning exactly (design
    doc round 31, issue #10's file-area follow-up), which this module
    mirrors deliberately rather than inventing a parallel design."""

    entries: list[FileEntry]
    has_older: bool
    has_newer: bool


def list_files_page(
    db: Database,
    area: FileArea,
    requesting_user: User,
    *,
    before: FileEntryCursor | None = None,
    after: FileEntryCursor | None = None,
    limit: int = _DEFAULT_PAGE_SIZE,
) -> FileEntryPage:
    """
    Fetch one bounded page of files in `area` (design doc round 31,
    issue #10's file-area follow-up to round 30's board-post
    pagination) — never the whole area's listing, however large its
    history. Enforces `area.min_read_level`, same as the unbounded
    function this replaces.

    Deliberately mirrors `netbbs.boards.posts.list_posts_page` byte for
    byte in approach — same cursor-based (keyset) pagination over
    `OFFSET`/`LIMIT` for the same stability-under-concurrent-inserts
    and no-growing-scan-cost reasons, same `(created_at, file_id)`
    ordering with `file_id` (content-addressed, globally unique) as a
    deterministic tie-breaker for the rare same-timestamp case, same
    three mutually exclusive `before`/`after`/neither modes, and the
    same `has_older`/`has_newer` semantics — see that function's
    docstring for the full reasoning, not repeated here to avoid the
    two copies drifting out of sync in what they claim rather than just
    in what they say (same reasoning `netbbs.net.chat_flow`'s and
    `netbbs.net.file_flow`'s own category-browsing docstrings already
    use for not re-explaining `netbbs.net.login_flow`'s pattern).
    """
    require_level(requesting_user, area.min_read_level)
    if before is not None and after is not None:
        raise ValueError("specify at most one of before/after")

    if after is not None:
        created_at, file_id = after
        rows = db.connection.execute(
            """
            SELECT * FROM files
            WHERE area_id = ? AND (created_at, file_id) > (?, ?)
            ORDER BY created_at ASC, file_id ASC
            LIMIT ?
            """,
            (area.id, created_at, file_id, limit),
        ).fetchall()
        entries = [_row_to_file_entry(row) for row in rows]
    elif before is not None:
        created_at, file_id = before
        rows = db.connection.execute(
            """
            SELECT * FROM files
            WHERE area_id = ? AND (created_at, file_id) < (?, ?)
            ORDER BY created_at DESC, file_id DESC
            LIMIT ?
            """,
            (area.id, created_at, file_id, limit),
        ).fetchall()
        entries = [_row_to_file_entry(row) for row in reversed(rows)]
    else:
        rows = db.connection.execute(
            """
            SELECT * FROM files
            WHERE area_id = ?
            ORDER BY created_at DESC, file_id DESC
            LIMIT ?
            """,
            (area.id, limit),
        ).fetchall()
        entries = [_row_to_file_entry(row) for row in reversed(rows)]

    if not entries:
        return FileEntryPage(entries=[], has_older=False, has_newer=False)

    oldest, newest = entries[0], entries[-1]
    has_older = db.connection.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM files
            WHERE area_id = ? AND (created_at, file_id) < (?, ?)
        )
        """,
        (area.id, oldest.created_at, oldest.file_id),
    ).fetchone()[0]
    has_newer = db.connection.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM files
            WHERE area_id = ? AND (created_at, file_id) > (?, ?)
        )
        """,
        (area.id, newest.created_at, newest.file_id),
    ).fetchone()[0]
    return FileEntryPage(entries=entries, has_older=bool(has_older), has_newer=bool(has_newer))


def download_file(entry: FileEntry) -> bytes:
    """Read a file entry's bytes back from storage."""
    return read_bytes(Path(entry.storage_path))


def _row_to_file_entry(row: sqlite3.Row) -> FileEntry:
    return FileEntry(
        id=row["id"],
        file_id=row["file_id"],
        area_id=row["area_id"],
        filename=row["filename"],
        description=row["description"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        storage_path=row["storage_path"],
        uploader_user_id=row["uploader_user_id"],
        uploader_label=row["uploader_label"],
        uploader_fingerprint=row["uploader_fingerprint"],
        created_at=row["created_at"],
    )
