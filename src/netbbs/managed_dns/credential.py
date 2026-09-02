"""
On-disk storage for the managed-DNS registration credential (design doc
§16, issue #201, Decision 2).

This is a plain bearer secret, minted by the managed service at
registration time and returned once in that response -- not a keypair,
so it doesn't need `netbbs.identity.keys.Identity`'s full JSON/passphrase
machinery. What it does need, and gets here, is that class's same
durable-secret-on-disk handling: owner-only (0600) permissions and an
atomic tmp-file-then-rename write, so a crash mid-write can never leave
a half-written credential behind. Deliberately kept out of
`netbbs.config`'s `node_config` table -- that store is plaintext with no
at-rest protection at all (see its own module docstring), appropriate
for settings like a display name but not for a secret that authenticates
mutating calls against project-operated infrastructure.

`credential_path_for(db_path)` follows the exact naming convention
`netbbs.backup._ssh_host_key_path_for` already uses for its own
derived-path artifacts, so `netbbs.backup` can adopt this file as its
13th backup artifact (design doc §16 Decision 7) by importing this
function rather than re-deriving the path a second time.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def credential_path_for(db_path: Path) -> Path:
    """Mirrors `netbbs.backup._ssh_host_key_path_for`'s own derived-path
    convention: a single file, sibling to the database, named from the
    database's own stem."""
    return db_path.parent / f"{db_path.stem}_managed_dns_credential"


def save_credential(path: Path, secret: str) -> None:
    """Write `secret` to `path`, owner-only (0600), atomically.

    Same tmp-file-then-rename-then-chmod discipline `netbbs.identity.
    keys.Identity.save` already uses for the same reason: a reader must
    never observe a partially-written file, and the permission bits must
    never have a window where the file is briefly world/group readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        # O_CREAT's mode is ignored when a stale temp inode already exists.
        # Reassert the bearer secret's permissions before writing any bytes.
        if hasattr(os, "fchmod"):
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        else:  # Windows has no fchmod; chmod the still-open temp path.
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(secret)
    finally:
        if fd >= 0:
            os.close(fd)
    tmp_path.replace(path)


def load_credential(path: Path) -> str | None:
    """The stored secret, or `None` if this node has never registered
    (no file on disk) -- never raises for the "not registered yet" case,
    since that's an ordinary, expected state, not an error."""
    if not path.exists():
        return None
    return path.read_text()


def delete_credential(path: Path) -> None:
    """Remove a previously-saved credential, e.g. after a confirmed
    release. A no-op if nothing is there -- matches `load_credential`'s
    own "missing file is a normal state" treatment."""
    path.unlink(missing_ok=True)
