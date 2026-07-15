"""
Filesystem-backed storage for uploaded file bytes.

Design doc §9: file areas are node-local in Phase 1, not synced across
NetBBS Link. Bytes live on the filesystem, not as SQLite blobs -- keeps
the database itself small and lets the filesystem handle what it's
already good at, rather than working against SQLite's blob-storage
overhead.

Laid out by content hash (sha256), sharded two hex characters deep to
avoid one huge flat directory (a well-worn content-addressable-storage
pattern -- e.g. git's own object store) -- `<root>/<aa>/<aabbccdd...>`. A
useful side effect, not the primary motivation: two uploads with
identical bytes share one stored blob regardless of filename, area, or
uploader, since the storage path is derived purely from content.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from netbbs.storage.database import Database


def storage_root(db: Database) -> Path:
    """
    Filesystem root for this node's uploaded file bytes, derived from its
    database path -- keeps a node's data (DB + files) rooted at one
    predictable location without a separate config setting.
    """
    return db.path.parent / f"{db.path.stem}_files"


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def storage_path_for(db: Database, sha256: str) -> Path:
    return storage_root(db) / sha256[:2] / sha256


def store_bytes(db: Database, data: bytes) -> tuple[str, Path]:
    """
    Write `data` to content-addressed storage, returning its sha256 and
    the path it was written to.

    A no-op write when identical content is already stored (the target
    path already existing implies the same bytes, since the path is
    derived from their hash) -- see module docstring.

    For a caller that already has the complete content as one in-memory
    `bytes` object (dev scripts, most tests). A caller receiving content
    incrementally over the wire (GitHub issue #34's streaming Zmodem
    upload path) should use `new_incoming_temp_path`/
    `move_temp_file_into_storage` instead, so the full content is never
    held in memory here either.
    """
    sha256 = compute_sha256(data)
    path = storage_path_for(db, sha256)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return sha256, path


def _incoming_dir(db: Database) -> Path:
    """A staging subdirectory *inside* `storage_root` (GitHub issue
    #34), not the platform temp directory (`tempfile.gettempdir()`):
    `move_temp_file_into_storage` finishes with `Path.replace()`, an
    atomic rename that POSIX only guarantees within a single filesystem
    -- staging here, alongside the final content-addressed layout,
    guarantees that rename is always same-filesystem regardless of
    where the platform temp directory happens to be mounted (a real
    concern on this project's NetBSD deployment target, where `/tmp` is
    commonly its own `tmpfs`/`mfs` mount, separate from a node's data
    volume)."""
    return storage_root(db) / ".incoming"


def new_incoming_temp_path(db: Database) -> Path:
    """
    A fresh, not-yet-existing path under `_incoming_dir` for a caller to
    stream not-yet-hashed content into (GitHub issue #34) -- e.g.
    `netbbs.net.zmodem.receive_file`'s streaming receive path, via
    `netbbs.net.file_flow._handle_upload`. The filename itself carries
    no meaning (unlike the final content-addressed path, its location
    is arbitrary staging); a random UUID avoids any collision between
    concurrent uploads on the same node.
    """
    directory = _incoming_dir(db)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / uuid.uuid4().hex


def move_temp_file_into_storage(db: Database, temp_path: Path, sha256: str) -> Path:
    """
    The streaming counterpart to `store_bytes`: places an already-
    written, already-hashed temp file (from `new_incoming_temp_path`)
    into content-addressed storage without ever reading its content
    back into memory here (GitHub issue #34) -- `store_bytes` needs the
    complete bytes up front specifically to compute the hash; a
    streaming caller already computed it incrementally while writing,
    so only a placement step remains.

    A no-op move (the temp file is discarded, the existing stored
    content kept as-is) when identical content is already stored, same
    "content hash already implies identical bytes" reasoning
    `store_bytes` uses. `Path.replace()`, not a copy, for the actual
    placement -- an atomic, same-filesystem rename (guaranteed by
    `new_incoming_temp_path` staging under this same `storage_root`),
    not a second full read-and-write of the content.
    """
    path = storage_path_for(db, sha256)
    if path.exists():
        temp_path.unlink(missing_ok=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_path, path)
    return path


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()
