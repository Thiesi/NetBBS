"""
On-demand, bounded, resumable chunk transfer (design doc §11.3, issue
#89) -- the content half of remote file areas, genuinely new rather than
mirroring boards/channels: a direct point-to-point pull against one
file's own origin, never gossiped, never passed through `netbbs.link.
protocol.LinkNode.handle_events`.

Deliberately `db`-first and free of any real network I/O, the same
separation `netbbs.link.boards`/`netbbs.link.channels` already establish
-- `netbbs.link.transport` is the only place that actually dials a peer
or answers an inbound request; this module is what it calls into on both
sides (building the next request, applying a verified response,
answering a request to serve one out).

One transfer, one `link_file_transfers` row, `transfer_id` deterministic
(a content hash of `file_id` + this node's own fingerprint) so a resumed
or retried fetch naturally reuses the same row instead of restarting.
Chunks are requested and applied strictly in order -- `chunk_index`
`0, 1, 2, ...` -- so resuming means asking for the next index past what
`link_file_transfer_chunks` already has, nothing more elaborate; a
duplicate/resent chunk (the same index arriving twice) is a no-op,
checked against that same table before ever touching the staging file,
never double-applied.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from netbbs.boards.content_id import compute_content_id
from netbbs.files.storage import move_temp_file_into_storage, new_incoming_temp_path
from netbbs.link.events import FileDescriptor
from netbbs.link.files import RemoteFile
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class FileTransferError(Exception):
    """Raised for a malformed/out-of-range chunk request, or a
    completed transfer whose reassembled content doesn't match its own
    catalogued hash."""


def compute_transfer_id(file_id: str, requester_fingerprint: str) -> str:
    """Deterministic `transfer_id` (design doc §11.3) -- a resumed or
    retried fetch of the same file by the same requester always
    computes the identical id, so `get_or_create_transfer` naturally
    finds and reuses the existing row rather than starting over."""
    return compute_content_id(
        {"type": "file_transfer", "file_id": file_id, "requester_fingerprint": requester_fingerprint}
    )


_DEFAULT_CHUNK_SIZE = 256 * 1024


@dataclass(frozen=True)
class TransferState:
    id: int
    transfer_id: str
    remote_file_id: str
    total_size: int
    chunk_size: int
    bytes_received: int
    status: str  # 'in_progress' | 'completed' | 'failed'
    temp_path: str | None
    created_at: str
    updated_at: str

    @property
    def next_chunk_index(self) -> int | None:
        """`None` once every byte has been received -- there is nothing
        further to request. Strict in-order chunking (module docstring)
        means this is just `bytes_received // chunk_size`, no separate
        row-scanning needed."""
        if self.status != "in_progress":
            return None
        return self.bytes_received // self.chunk_size


def _transfer_from_row(row) -> TransferState:
    return TransferState(
        id=row["id"], transfer_id=row["transfer_id"], remote_file_id=row["remote_file_id"],
        total_size=row["total_size"], chunk_size=row["chunk_size"], bytes_received=row["bytes_received"],
        status=row["status"], temp_path=row["temp_path"], created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_transfer(db: Database, transfer_id: str) -> TransferState | None:
    row = db.connection.execute(
        "SELECT * FROM link_file_transfers WHERE transfer_id = ?", (transfer_id,)
    ).fetchone()
    return None if row is None else _transfer_from_row(row)


def get_or_create_transfer(
    db: Database, remote_file: RemoteFile, *, requester_fingerprint: str, chunk_size: int = _DEFAULT_CHUNK_SIZE,
) -> TransferState:
    """
    The requester's own row tracking a fetch of `remote_file` -- created
    once, on first request, and found again (unchanged) on every
    subsequent call for the same `(remote_file, requester_fingerprint)`
    pair, since `transfer_id` is deterministic. A file with `size_bytes
    == 0` starts (and immediately reports) `'completed'` with no chunk
    ever requested -- there is nothing to fetch.
    """
    transfer_id = compute_transfer_id(remote_file.file_id, requester_fingerprint)
    existing = get_transfer(db, transfer_id)
    if existing is not None:
        return existing

    now = utc_now_iso()
    status = "completed" if remote_file.size_bytes == 0 else "in_progress"
    db.connection.execute(
        """
        INSERT INTO link_file_transfers
            (transfer_id, remote_file_id, total_size, chunk_size, bytes_received, status, temp_path,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, 0, ?, NULL, ?, ?)
        """,
        (transfer_id, remote_file.file_id, remote_file.size_bytes, chunk_size, status, now, now),
    )
    db.connection.commit()
    return get_transfer(db, transfer_id)


def apply_received_chunk(
    db: Database,
    transfer: TransferState,
    *,
    chunk_index: int,
    chunk_bytes: bytes,
    claimed_chunk_sha256: str,
    is_last: bool,
    remote_file: RemoteFile,
) -> TransferState:
    """
    Verify and apply one chunk already retrieved over the wire (its
    signature already checked by the caller against the origin's current
    signing key -- this function only checks the *content* actually
    matches what was claimed). Idempotent: a chunk index already
    recorded in `link_file_transfer_chunks` is a no-op, returning the
    transfer's current state unchanged -- the exact-dedup mechanism the
    design doc names for a duplicate/resent chunk request.

    Raises `FileTransferError` if the received bytes don't hash to
    `claimed_chunk_sha256` (the requester's own integrity check,
    independent of the signature covering the *claim*), if `chunk_index`
    doesn't match what this transfer actually expects next (out-of-order
    delivery isn't tolerated -- a fresh request for the correct index
    recovers, the same "no reordering" discipline every gossiped chain
    already applies), or if the reassembled whole-file content doesn't
    match `remote_file.sha256` once the last chunk lands.
    """
    already_applied = db.connection.execute(
        "SELECT 1 FROM link_file_transfer_chunks WHERE transfer_id = ? AND chunk_index = ?",
        (transfer.transfer_id, chunk_index),
    ).fetchone()
    if already_applied is not None:
        # Exact-dedup: a duplicate/resent chunk request for an index this
        # transfer already has -- a safe no-op, never re-hashed, re-applied,
        # or re-appended to the staging file (which would otherwise corrupt
        # the reassembly by double-writing bytes).
        return get_transfer(db, transfer.transfer_id)

    if hashlib.sha256(chunk_bytes).hexdigest() != claimed_chunk_sha256:
        raise FileTransferError(
            f"chunk {chunk_index} of transfer {transfer.transfer_id!r} does not hash to its own "
            "claimed chunk_sha256 -- refusing"
        )

    if chunk_index != transfer.next_chunk_index:
        raise FileTransferError(
            f"transfer {transfer.transfer_id!r} received chunk_index {chunk_index}, but expected "
            f"{transfer.next_chunk_index} -- refusing (reordering isn't tolerated, a fresh request "
            "for the correct index recovers)"
        )

    temp_path = transfer.temp_path
    if temp_path is None:
        temp_path = str(new_incoming_temp_path(db))

    with open(temp_path, "ab") as handle:
        handle.write(chunk_bytes)

    bytes_received = transfer.bytes_received + len(chunk_bytes)
    now = utc_now_iso()
    db.connection.execute(
        "INSERT INTO link_file_transfer_chunks (transfer_id, chunk_index, chunk_id, received_at) "
        "VALUES (?, ?, ?, ?)",
        (transfer.transfer_id, chunk_index, claimed_chunk_sha256, now),
    )
    db.connection.execute(
        "UPDATE link_file_transfers SET bytes_received = ?, temp_path = ?, updated_at = ? WHERE transfer_id = ?",
        (bytes_received, temp_path, now, transfer.transfer_id),
    )
    db.connection.commit()

    if not is_last:
        return get_transfer(db, transfer.transfer_id)

    return _finalize_transfer(db, transfer.transfer_id, temp_path=temp_path, remote_file=remote_file)


def _finalize_transfer(db: Database, transfer_id: str, *, temp_path: str, remote_file: RemoteFile) -> TransferState:
    """Reassembly verification and promotion into real storage, once the
    last chunk has landed -- a peer whose actual bytes don't match its
    own `file_descriptor`'s claimed hash is refused here, never silently
    accepted (design doc §11.3)."""
    reassembled_sha256 = _hash_file(temp_path)
    if reassembled_sha256 != remote_file.sha256:
        db.connection.execute(
            "UPDATE link_file_transfers SET status = 'failed', updated_at = ? WHERE transfer_id = ?",
            (utc_now_iso(), transfer_id),
        )
        db.connection.commit()
        os.remove(temp_path) if os.path.exists(temp_path) else None
        raise FileTransferError(
            f"transfer {transfer_id!r} completed but its reassembled content hashes to "
            f"{reassembled_sha256!r}, not the catalogued {remote_file.sha256!r} -- refusing"
        )

    storage_path = move_temp_file_into_storage(db, Path(temp_path), reassembled_sha256)

    db.connection.execute(
        """
        INSERT INTO files
            (file_id, area_id, filename, description, size_bytes, sha256, storage_path,
             uploader_user_id, uploader_label, uploader_fingerprint, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'approved')
        ON CONFLICT(file_id) DO NOTHING
        """,
        (
            remote_file.file_id, remote_file.area_id, remote_file.filename, remote_file.description,
            remote_file.size_bytes, reassembled_sha256, str(storage_path),
            f"remote@{remote_file.origin_fingerprint}", remote_file.origin_fingerprint,
            remote_file.created_at,
        ),
    )
    db.connection.execute(
        "UPDATE remote_files SET fetched_file_id = ? WHERE file_id = ?",
        (remote_file.file_id, remote_file.file_id),
    )
    db.connection.execute(
        "UPDATE link_file_transfers SET status = 'completed', updated_at = ? WHERE transfer_id = ?",
        (utc_now_iso(), transfer_id),
    )
    db.connection.commit()

    return get_transfer(db, transfer_id)


def _hash_file(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def build_chunk_for_serving(
    db: Database, *, file_id: str, chunk_index: int, max_chunk_size: int,
) -> tuple[bytes, int, int, bool]:
    """
    The serving side's own read: locate a *local* file this node
    actually has bytes for (a real `files` row, never `remote_files` --
    this node can only ever serve content it genuinely holds, matching
    §11's "remains owned and stored by its source node") and return the
    requested chunk's raw bytes plus `(chunk_size, total_size, is_last)`
    for the caller to sign into a `FileChunkDescriptor`.

    Raises `FileTransferError` for an unknown `file_id` or an
    out-of-range `chunk_index` -- a malformed/abusive request is refused
    outright, never silently served a truncated or empty chunk.
    """
    row = db.connection.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
    if row is None:
        raise FileTransferError(f"no such file_id known to this node: {file_id!r}")

    total_size = row["size_bytes"]
    chunk_size = max(1, min(max_chunk_size, _DEFAULT_CHUNK_SIZE))
    offset = chunk_index * chunk_size
    if offset > total_size or (offset == total_size and total_size != 0):
        raise FileTransferError(
            f"chunk_index {chunk_index} is out of range for file_id {file_id!r} ({total_size} bytes)"
        )

    with open(row["storage_path"], "rb") as handle:
        handle.seek(offset)
        chunk_bytes = handle.read(chunk_size)

    is_last = offset + len(chunk_bytes) >= total_size
    return chunk_bytes, len(chunk_bytes), total_size, is_last
