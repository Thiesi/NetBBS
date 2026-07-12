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


def list_files(db: Database, area: FileArea, requesting_user: User) -> list[FileEntry]:
    """List all files in `area`, oldest first, after checking the
    requesting user meets the area's `min_read_level` — mirrors
    `netbbs.boards.posts.list_posts` exactly."""
    require_level(requesting_user, area.min_read_level)
    rows = db.connection.execute(
        "SELECT * FROM files WHERE area_id = ? ORDER BY created_at", (area.id,)
    ).fetchall()
    return [_row_to_file_entry(row) for row in rows]


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
