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
(design doc §13/§15) mirror
`netbbs.boards.posts`'s treatment structurally — see that
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
from netbbs.files.diz import (
    MAX_DESCRIPTION_BYTES,
    MAX_DESCRIPTION_LINES,
    normalize_description,
)
from netbbs.files.storage import move_temp_file_into_storage, read_bytes, store_bytes
from netbbs.moderation import BoardPermission, has_permission, record_action
from netbbs.permissions import require_level
from netbbs.search import reindex_file
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

    Takes the complete file as one in-memory `bytes` object — the right
    shape for a caller that already has the whole thing in hand (dev
    scripts, most tests; see `scripts/create_test_file.py`). The real
    Zmodem upload path (`netbbs.net.file_flow._handle_upload`) uses
    `upload_file_from_temp` instead (GitHub issue #34): once
    `receive_file` streamed the content incrementally rather than
    handing over a complete buffer, this function's own `store_bytes`
    call would be the one remaining place still requiring the whole
    file in memory at once, defeating the point.

    Starts `'pending'` if `area.moderated`, else `'approved'` — see
    `approve_file`/`delete_file` for how a pending upload gets
    resolved, and `list_pending_files` for the moderation queue view.
    """
    require_level(uploader, area.min_write_level)
    # Before the bytes are stored, not after (Codex review): a
    # description this node will refuse should never leave a blob in
    # content-addressed storage with no row referencing it, waiting for
    # `netbbs.files.gc` to notice.
    description = validate_description(description)
    sha256, path = store_bytes(db, data)
    return _finalize_upload(
        db, area, uploader, filename,
        sha256=sha256, size_bytes=len(data), storage_path=path, description=description,
    )


def upload_file_from_temp(
    db: Database,
    area: FileArea,
    uploader: User,
    filename: str,
    *,
    temp_path: Path,
    sha256: str,
    size_bytes: int,
    description: str | None = None,
) -> FileEntry:
    """
    Like `upload_file`, but for a caller that already streamed the
    content to `temp_path` (`netbbs.files.storage.
    new_incoming_temp_path`) and computed `sha256`/`size_bytes`
    incrementally while doing so (GitHub issue #34's streaming Zmodem
    receive path) — the full content is never held in memory here
    either, only moved into place by
    `netbbs.files.storage.move_temp_file_into_storage`.

    `temp_path` is always cleaned up one way or another: moved into
    content-addressed storage on success, or deleted here if anything
    fails before that move happens (e.g. `require_level` rejecting the
    uploader) — never silently leaked as an orphaned staging file
    either way.
    """
    try:
        require_level(uploader, area.min_write_level)
        # Validated before the move, for the same reason `upload_file`
        # validates before storing (Codex review) -- and here a refusal
        # after the move would strand the staging file's content in
        # storage while this function's own cleanup only ever removes
        # the *staging* path.
        description = validate_description(description)
        storage_path = move_temp_file_into_storage(db, temp_path, sha256)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return _finalize_upload(
        db, area, uploader, filename,
        sha256=sha256, size_bytes=size_bytes, storage_path=storage_path, description=description,
    )


def _finalize_upload(
    db: Database,
    area: FileArea,
    uploader: User,
    filename: str,
    *,
    sha256: str,
    size_bytes: int,
    storage_path: Path,
    description: str | None,
) -> FileEntry:
    """The database-row half of an upload, shared by `upload_file` and
    `upload_file_from_temp` (GitHub issue #34) once each has already
    placed the content in storage its own way and knows its hash/size —
    everything from here on is identical regardless of how the bytes
    got there."""
    description = validate_description(description)
    status = "pending" if area.moderated else "approved"
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
                size_bytes,
                sha256,
                str(storage_path),
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

    reindex_file(db, area.id, file_id)
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
    alongside `list_files_page` specifically so
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
    a holder of `BoardPermission.APPROVE` on `area`; passing no
    `requesting_user` treats it the same as an unauthorized one, the
    safe default. `'expired'` matches
    are always returned — expiry is a delisting, not an access
    restriction (see `netbbs.boards.posts`'s equivalent treatment).
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
    doc, issue #10's file-area follow-up), which this module
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
    Fetch one bounded page of files in `area` (design doc,
    issue #10's file-area follow-up to the board-post
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

    Only `status = 'approved'` entries are ever included here (mirroring
    `netbbs.boards.posts`'s treatment) —
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


def count_visible_files(db: Database, area: FileArea) -> tuple[int, str | None]:
    """
    Total visible (approved) files in `area`, plus the most recent
    one's `created_at` (`None` if there are none).

    For admin/reporting surfaces (`netbbs.net.admin_flow`'s file-area
    detail screen -- dogfood follow-up: a SysOp had no way to tell a
    dead area from an active one without leaving admin and browsing it
    as an ordinary caller, mirroring `netbbs.boards.posts.
    count_visible_posts`'s same gap for boards) -- not gated by
    `min_read_level` since only a SysOp already inside the admin
    console reaches this.
    """
    _sweep_expired_files(db, area)
    row = db.connection.execute(
        "SELECT COUNT(*), MAX(created_at) FROM files WHERE area_id = ? AND status = 'approved'",
        (area.id,),
    ).fetchone()
    return row[0], row[1]


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
    reindex_file(db, entry.area_id, entry.file_id)
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
    a separate concern handled by `netbbs.files.gc`, not by this function.
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
    reindex_file(db, entry.area_id, entry.file_id)


def validate_description(description: str | None) -> str | None:
    """
    The single gate every stored description passes through, whether it
    came out of an uploaded archive's `FILE_ID.DIZ`
    (`netbbs.files.diz.read_archive_description`) or from a caller
    typing one: normalized to clean, newline-separated text (blank
    becomes `None`), then bounded to what a listing can render and a
    Link `file_descriptor` can carry.

    Raises rather than truncating: `read_archive_description` has
    already cut a DIZ down to fit before it gets here, so anything
    still over the limit is a description someone wrote deliberately,
    and quietly deleting half of it is the one outcome they did not
    ask for. `netbbs.net.file_flow`'s editor catches this and keeps the
    draft.
    """
    normalized = normalize_description(description) if description else None
    if normalized is None:
        return None
    line_count = len(normalized.splitlines())
    if line_count > MAX_DESCRIPTION_LINES:
        raise FileEntryError(
            f"a description may be at most {MAX_DESCRIPTION_LINES} lines -- this one is {line_count}"
        )
    byte_count = len(normalized.encode("utf-8"))
    if byte_count > MAX_DESCRIPTION_BYTES:
        raise FileEntryError(
            f"a description may be at most {MAX_DESCRIPTION_BYTES} bytes -- this one is {byte_count}"
        )
    return normalized


def set_file_description(
    db: Database, entry: FileEntry, description: str | None, *, changed_by: User
) -> FileEntry:
    """
    Replace `entry`'s description (issue #463). Allowed for the file's
    own uploader, no permission grant needed -- the same "you may act on
    it because you own it" rule `netbbs.boards.posts.edit_post`
    establishes for a post's author -- or for anyone holding
    `BoardPermission.EDIT` on the area, matching every other
    moderator-side file mutation here.

    An in-place `UPDATE`, unlike `edit_post`'s new-revision chain: a
    `file_id` is a content hash of the *bytes* plus their upload
    metadata (`_finalize_upload`), not of the description, so amending
    the description leaves it just as valid as it was -- and nothing
    references a file by description the way a reply references a
    `parent_post_id`.

    Local-only, deliberately. A file whose `file_descriptor` has already
    been signed and pushed keeps the description its peers were told
    about: rewriting a signed event would mean either re-signing the
    same `file_id` with different content (which every peer would
    correctly ignore, having already recorded it) or inventing a
    `file_descriptor_edit` event type, which is a protocol change and a
    separate decision. A description edited *before* the descriptor is
    built is simply the one that propagates.

    `entry` is only as fresh as the screen it came from, so the row is
    re-read by `file_id` before anything is decided (Codex review). Two
    things follow: a file deleted while its editor sat open fails
    honestly here instead of reporting a save that updated nothing, and
    the ownership question can never be answered from a stale row while
    the `UPDATE` lands on a different one -- `files.id` is a plain
    SQLite rowid, which a later upload may reuse after a delete, while
    `file_id` is content-addressed and never is.
    """
    current = get_file(db, entry.file_id)
    if current.uploader_user_id != changed_by.id:
        _require_area_permission(db, current, changed_by, BoardPermission.EDIT)

    normalized = validate_description(description)
    db.connection.execute("UPDATE files SET description = ? WHERE id = ?", (normalized, current.id))
    db.connection.commit()
    record_action(
        db,
        actor=changed_by,
        action="describe",
        object_type="file_area",
        object_id=current.area_id,
        target_user_id=current.uploader_user_id,
        detail=current.file_id,
    )
    reindex_file(db, current.area_id, current.file_id)
    return get_file(db, current.file_id)


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
    # Captured before either bulk statement below (issue #56's search
    # index) -- same reasoning as netbbs.boards.posts._sweep_expired_
    # posts, though simpler here since files have no edit chain: every
    # affected file_id just needs its (now stale) file_search entry
    # recomputed via reindex_file, which will remove it once its row is
    # no longer 'approved'.
    expiring_ids = {
        row["file_id"]
        for row in db.connection.execute(
            """
            SELECT file_id FROM files
            WHERE area_id = ? AND status = 'approved' AND exempt_from_expiry = 0
                  AND created_at < ?
            """,
            (area.id, expiry_cutoff),
        ).fetchall()
    }
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
    deleting_ids = {
        row["file_id"]
        for row in db.connection.execute(
            """
            SELECT file_id FROM files
            WHERE area_id = ? AND status = 'expired' AND exempt_from_expiry = 0
                  AND created_at < ?
            """,
            (area.id, deletion_cutoff),
        ).fetchall()
    }
    db.connection.execute(
        """
        DELETE FROM files
        WHERE area_id = ? AND status = 'expired' AND exempt_from_expiry = 0
              AND created_at < ?
        """,
        (area.id, deletion_cutoff),
    )
    db.connection.commit()

    for file_id in expiring_ids | deleting_ids:
        reindex_file(db, area.id, file_id)


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
