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
    """
    sha256 = compute_sha256(data)
    path = storage_path_for(db, sha256)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return sha256, path


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()
