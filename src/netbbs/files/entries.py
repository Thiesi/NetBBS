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

Moderated-area approval and the maintenance/expiry state machine
(design doc §13/§15, sign-off round 36) mirror
`netbbs.boards.posts`'s round 35 treatment structurally — see that
module's docstring for the fuller reasoning, not repeated here. One
real difference: `get_file_by_name` is a second unbounded lookup path
(besides `get_file`) that posts don't have an equivalent of, so it
gets its own pending-visibility check — see that function's docstring.
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from netbbs.auth.users import User
from netbbs.boards.content_id import compute_content_id
from netbbs.config import get_expiry_grace_period_days
from netbbs.files.areas import FileArea
from netbbs.files.storage import read_bytes, store_bytes
from netbbs.moderation import BoardPermission, has_permission, record_action
from netbbs.permissions import require_level
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class FileEntryError(Exception):
    """Raised for file upload/lookup/moderation failures."""


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
    status: str
    pinned: bool
    exempt_from_expiry: bool


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

    Starts `'pending'` if `area.moderated`, else `'approved'` — see
    `approve_file`/`delete_file` for how a pending upload gets
    resolved, and `list_pending_files` for the moderation queue view.
    """
    require_level(uploader, area.min_write_level)

    status = "pending" if area.moderated else "approved"
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
                 uploader_fingerprint, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                status,
            ),
        )
        db.connection.commit()
    except sqlite3.IntegrityError as exc:
        raise FileEntryError(
            "could not record upload — identical content uploaded twice in the same instant?"
        ) from exc

    return get_file(db, file_id)


def get_file(db: Database, file_id: str) -> FileEntry:
    """
    Unbounded by-ID lookup — deliberately not status-filtered, unlike
    `list_files_page`, same reasoning as `netbbs.boards.posts.get_post`:
    used for `upload_file`'s own return path, and reaching a
    `'pending'` file this way requires already knowing its exact
    `file_id`, which isn't discoverable through any listing a
    non-uploader, non-moderator would see.
    """
    row = db.connection.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
    if row is None:
        raise FileEntryError(f"no such file: {file_id!r}")
    return _row_to_file_entry(row)


def get_file_by_name(
    db: Database, area: FileArea, filename: str, *, requesting_user: User | None = None
) -> FileEntry | None:
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

    Unlike `get_file`, this path is reachable by anyone who merely
    knows (or guesses) a filename — a real, practical route to a
    `'pending'` file, not the theoretical one `get_file`/`get_post`
    accept. So a `'pending'` match is only returned to its uploader or
    a holder of `BoardPermission.APPROVE` on `area` (design doc
    sign-off round 36); passing no `requesting_user` treats it the
    same as an unauthorized one, the safe default. `'expired'` matches
    are always returned — expiry is a delisting, not an access
    restriction (see `netbbs.boards.posts`'s round 35 treatment).
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
    if row is None:
        return None

    entry = _row_to_file_entry(row)
    if entry.status == "pending" and not _can_view_pending(db, entry, requesting_user):
        return None
    return entry


def _can_view_pending(db: Database, entry: FileEntry, requesting_user: User | None) -> bool:
    if requesting_user is None:
        return False
    if requesting_user.id == entry.uploader_user_id:
        return True
    return has_permission(
        db,
        requesting_user,
        object_type="file_area",
        object_id=entry.area_id,
        permission=BoardPermission.APPROVE,
    )


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

    Only `status = 'approved'` entries are ever included here (design
    doc sign-off round 36, mirroring round 35's post treatment) —
    `'pending'` files belong to the moderation queue
    (`list_pending_files`), and `'expired'` files are delisted from
    normal browsing though still individually reachable (see
    `get_file`/`get_file_by_name`). Sweeps the area's own files for
    expiry/deletion first (`_sweep_expired_files`).
    """
    require_level(requesting_user, area.min_read_level)
    if before is not None and after is not None:
        raise ValueError("specify at most one of before/after")

    _sweep_expired_files(db, area)

    if after is not None:
        created_at, file_id = after
        rows = db.connection.execute(
            """
            SELECT * FROM files
            WHERE area_id = ? AND status = 'approved' AND (created_at, file_id) > (?, ?)
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
            WHERE area_id = ? AND status = 'approved' AND (created_at, file_id) < (?, ?)
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
            WHERE area_id = ? AND status = 'approved'
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
            WHERE area_id = ? AND status = 'approved' AND (created_at, file_id) < (?, ?)
        )
        """,
        (area.id, oldest.created_at, oldest.file_id),
    ).fetchone()[0]
    has_newer = db.connection.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM files
            WHERE area_id = ? AND status = 'approved' AND (created_at, file_id) > (?, ?)
        )
        """,
        (area.id, newest.created_at, newest.file_id),
    ).fetchone()[0]
    return FileEntryPage(entries=entries, has_older=bool(has_older), has_newer=bool(has_newer))


def download_file(entry: FileEntry) -> bytes:
    """Read a file entry's bytes back from storage."""
    return read_bytes(Path(entry.storage_path))


def approve_file(db: Database, entry: FileEntry, *, approved_by: User) -> FileEntry:
    """Approve a `'pending'` file, requiring `approved_by` to hold
    `BoardPermission.APPROVE` on its area. Logged via
    `netbbs.moderation.log.record_action`."""
    _require_area_permission(db, entry, approved_by, BoardPermission.APPROVE)

    db.connection.execute("UPDATE files SET status = 'approved' WHERE id = ?", (entry.id,))
    db.connection.commit()
    record_action(
        db,
        actor=approved_by,
        action="approve",
        object_type="file_area",
        object_id=entry.area_id,
        target_user_id=entry.uploader_user_id,
        detail=entry.file_id,
    )
    return get_file(db, entry.file_id)


def delete_file(db: Database, entry: FileEntry, *, deleted_by: User) -> None:
    """
    Delete a file outright, requiring `deleted_by` to hold
    `BoardPermission.DELETE` on its area. Doubles as "reject" for a
    still-`'pending'` upload — no separate rejected status, mirroring
    `netbbs.boards.posts.delete_post` exactly, including which of the
    two the moderation log records.

    Only removes the database row — the underlying bytes in
    `netbbs.files.storage` are deliberately left alone. Storage-level
    garbage collection of orphaned content-addressed blobs (a
    different file entry could in principle share the same bytes) is
    out of scope for this round, same as it was never in scope for
    file areas before moderation existed.
    """
    _require_area_permission(db, entry, deleted_by, BoardPermission.DELETE)

    action = "reject" if entry.status == "pending" else "delete"
    db.connection.execute("DELETE FROM files WHERE id = ?", (entry.id,))
    db.connection.commit()
    record_action(
        db,
        actor=deleted_by,
        action=action,
        object_type="file_area",
        object_id=entry.area_id,
        target_user_id=entry.uploader_user_id,
        detail=entry.file_id,
    )


def set_file_pinned(db: Database, entry: FileEntry, pinned: bool, *, changed_by: User) -> FileEntry:
    """
    Pin or unpin a file within its own area's listing — a distinct
    concept from `netbbs.files.areas.FileArea.pinned` (which area sorts
    first among *all* areas). Requires `BoardPermission.EDIT`.

    Does not reorder `list_files_page`'s cursor-paginated feed itself
    (would break keyset pagination's stability guarantees, exactly the
    reason `netbbs.boards.posts.set_post_pinned` doesn't either) — see
    `list_pinned_files` for the dedicated pinned view.
    """
    _require_area_permission(db, entry, changed_by, BoardPermission.EDIT)

    db.connection.execute("UPDATE files SET pinned = ? WHERE id = ?", (int(pinned), entry.id))
    db.connection.commit()
    record_action(
        db,
        actor=changed_by,
        action="pin" if pinned else "unpin",
        object_type="file_area",
        object_id=entry.area_id,
        target_user_id=entry.uploader_user_id,
        detail=entry.file_id,
    )
    return get_file(db, entry.file_id)


def set_file_exempt(db: Database, entry: FileEntry, exempt: bool, *, changed_by: User) -> FileEntry:
    """Exempt or unexempt a file from the expiry sweep. Requires
    `BoardPermission.EDIT`."""
    _require_area_permission(db, entry, changed_by, BoardPermission.EDIT)

    db.connection.execute(
        "UPDATE files SET exempt_from_expiry = ? WHERE id = ?", (int(exempt), entry.id)
    )
    db.connection.commit()
    record_action(
        db,
        actor=changed_by,
        action="exempt" if exempt else "unexempt",
        object_type="file_area",
        object_id=entry.area_id,
        target_user_id=entry.uploader_user_id,
        detail=entry.file_id,
    )
    return get_file(db, entry.file_id)


def list_pending_files(db: Database, area: FileArea, *, requesting_user: User) -> list[FileEntry]:
    """
    The moderation queue for `area`: every pending file if
    `requesting_user` holds `BoardPermission.APPROVE`, otherwise only
    their own pending uploads. Not cursor-paginated, same reasoning as
    `netbbs.boards.posts.list_pending_posts`.
    """
    if has_permission(
        db, requesting_user, object_type="file_area", object_id=area.id, permission=BoardPermission.APPROVE
    ):
        rows = db.connection.execute(
            "SELECT * FROM files WHERE area_id = ? AND status = 'pending' ORDER BY created_at",
            (area.id,),
        ).fetchall()
    else:
        rows = db.connection.execute(
            """
            SELECT * FROM files WHERE area_id = ? AND status = 'pending' AND uploader_user_id = ?
            ORDER BY created_at
            """,
            (area.id, requesting_user.id),
        ).fetchall()
    return [_row_to_file_entry(row) for row in rows]


def list_pinned_files(db: Database, area: FileArea, *, requesting_user: User) -> list[FileEntry]:
    """Every currently-pinned, approved file in `area`, oldest first.
    Requires only `area.min_read_level` — see
    `netbbs.boards.posts.list_pinned_posts` for the identical
    reasoning."""
    require_level(requesting_user, area.min_read_level)
    rows = db.connection.execute(
        """
        SELECT * FROM files WHERE area_id = ? AND status = 'approved' AND pinned = 1
        ORDER BY created_at
        """,
        (area.id,),
    ).fetchall()
    return [_row_to_file_entry(row) for row in rows]


def _require_area_permission(
    db: Database, entry: FileEntry, user: User, permission: BoardPermission
) -> None:
    if not has_permission(db, user, object_type="file_area", object_id=entry.area_id, permission=permission):
        raise FileEntryError(
            f"{user.username!r} does not hold {permission.name} permission on this area"
        )


def _cutoff_iso(days: int) -> str:
    """Mirrors `netbbs.boards.posts._cutoff_iso` exactly."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sweep_expired_files(db: Database, area: FileArea) -> None:
    """
    Lazily bring `area`'s file statuses up to date — mirrors
    `netbbs.boards.posts._sweep_expired_posts` exactly, including the
    "no background scheduler exists" reasoning behind running this at
    the top of `list_files_page` rather than on a timer.
    """
    if area.max_file_age_days is None:
        return

    expiry_cutoff = _cutoff_iso(area.max_file_age_days)
    db.connection.execute(
        """
        UPDATE files SET status = 'expired'
        WHERE area_id = ? AND status = 'approved' AND exempt_from_expiry = 0
              AND created_at < ?
        """,
        (area.id, expiry_cutoff),
    )

    grace_days = get_expiry_grace_period_days(db)
    deletion_cutoff = _cutoff_iso(area.max_file_age_days + grace_days)
    db.connection.execute(
        """
        DELETE FROM files
        WHERE area_id = ? AND status = 'expired' AND exempt_from_expiry = 0
              AND created_at < ?
        """,
        (area.id, deletion_cutoff),
    )
    db.connection.commit()


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
        status=row["status"],
        pinned=bool(row["pinned"]),
        exempt_from_expiry=bool(row["exempt_from_expiry"]),
    )
