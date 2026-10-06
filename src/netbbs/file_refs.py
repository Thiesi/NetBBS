"""
References to files in this node's file areas (issue #830).

A letter can point at a file that is already in a file area here. Nothing
is copied, and nothing new is stored but the reference. Whoever reads the
letter sees the file named in it and can download it with the file area's
own download (Zmodem or a browser link), if they may read that area.

This module is the part of that which is not mail's: what a reference
is, whether a given account can open one, the plain-text line that stands
in for it where the file cannot follow, and which files an account may
point at. Mail stores its references in `mail_file_refs`
(`write_mail_refs_without_commit`, `mail_refs`); the rendering and the
download are `netbbs.net.file_ref_view`. A board post referencing a file
(issue #842 F086) reuses all of it with a table of its own,
`post_file_refs` (`write_post_refs_without_commit`, `post_refs`).

The rules:

- **A reference names the file by `files.file_id`,** the content-addressed
  id, which no later upload reuses (`files.id`, a rowid, can be). Its name,
  area and size are kept beside it as they were when it was attached, so a
  file removed since is still named: "report.zip, no longer available".
- **Only a file its sender can open can be attached:** approved, not
  expired, in an area the sender may read and whose age requirement they
  meet (`open_ref`). At most `MAX_FILE_REFS` per letter.
- **Every recipient must be able to open it too, or the letter is not
  sent** (`recipient_ref_problem`, made for each local copy before any is
  written). A reader who can no longer open it later -- the area's read
  level raised, the file deleted, moved away or expired -- sees that in the
  letter. A reader who may not read the area is not told the file's name or
  the area's: the letter says only that a file in it is not available to
  them.
- **Local only.** Nothing about a file crosses Link but its description in
  text: a Link copy of the letter carries one `link_text_line` per file at
  the end of its body, and no reference.
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from netbbs.auth.users import User
from netbbs.age_requirement import VERIFIED
from netbbs.communities import (
    get_effective_age_requirement,
    get_effective_min_age,
    get_effective_min_read_level,
    meets_read_gate,
    meets_resource_age,
)
from netbbs.config import get_node_display_name
from netbbs.rendering.sanitize import sanitize_text
from netbbs.storage.database import Database

if TYPE_CHECKING:
    from netbbs.files.areas import FileArea
    from netbbs.files.entries import FileEntry

# `netbbs.files` is imported where it is used: it reaches `netbbs.mail`
# (through the boards' moderation notices), which imports this module.

#: The most files one letter can point at.
MAX_FILE_REFS = 5

#: The most files the attach picker lists from one area, newest first.
ATTACHABLE_LIST_LIMIT = 500

# What `OpenedRef.state` is.
AVAILABLE = "available"
GONE = "gone"
NO_ACCESS = "no_access"


@dataclass(frozen=True)
class FileRef:
    """One file a letter points at: its `file_id`, and its name, area and
    size as they were when it was attached."""
    file_id: str
    filename: str
    area_name: str
    size_bytes: int


@dataclass(frozen=True)
class OpenedRef:
    """A reference as one reader finds it now. `entry` and `area` are set
    only when `state` is `AVAILABLE`."""
    ref: FileRef
    state: str
    entry: FileEntry | None = None
    area: FileArea | None = None


def file_size_text(size_bytes: int) -> str:
    """A file's size as the file areas show it: bytes, then KiB, MiB, GiB."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = size_bytes / 1024
    for unit in ("KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"  # pragma: no cover -- the loop always returns


def _cutoff_iso(days: int) -> str:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _visible_area(db: Database, area_local_id: int) -> FileArea | None:
    """The area with this row id, unless it is gone or the SysOp hid it
    (issue #683)."""
    from netbbs.files.areas import _row_to_file_area

    row = db.connection.execute("SELECT * FROM file_areas WHERE id = ?", (area_local_id,)).fetchone()
    if row is None or row["link_hidden_at"] is not None:
        return None
    return _row_to_file_area(row)


def _past_its_age(entry: FileEntry, area: FileArea) -> bool:
    """Whether `entry` is past its area's maximum age: expired, though the
    area's lazy sweep may not have marked it yet. Worked out rather than
    swept, so that checking a reference never writes."""
    if area.max_file_age_days is None or entry.exempt_from_expiry:
        return False
    return entry.created_at < _cutoff_iso(area.max_file_age_days)


def may_read_area(db: Database, user: User, area: FileArea) -> bool:
    """Whether `user` may read `area`: the file areas' own read gate --
    level or grant, and the age requirement."""
    return meets_read_gate(db, user, area) and meets_resource_age(db, user, area)


def open_ref(db: Database, user: User, ref: FileRef) -> OpenedRef:
    """How `ref` stands for `user` now: `AVAILABLE` to download, `GONE` --
    deleted, expired, its area removed or hidden, its content no longer on
    this node -- or `NO_ACCESS` when the area is there but `user` may not
    read it."""
    from netbbs.files.entries import _row_to_file_entry

    row = db.connection.execute("SELECT * FROM files WHERE file_id = ?", (ref.file_id,)).fetchone()
    if row is None:
        return OpenedRef(ref, GONE)
    entry = _row_to_file_entry(row)
    area = _visible_area(db, entry.area_id)
    if area is None:
        return OpenedRef(ref, GONE)
    if not may_read_area(db, user, area):
        return OpenedRef(ref, NO_ACCESS)
    if entry.status != "approved" or _past_its_age(entry, area) or not Path(entry.storage_path).exists():
        return OpenedRef(ref, GONE)
    return OpenedRef(ref, AVAILABLE, entry=entry, area=area)


def ref_for_entry(entry: FileEntry, area: FileArea) -> FileRef:
    """A reference to `entry`, as it is now."""
    return FileRef(file_id=entry.file_id, filename=entry.filename, area_name=area.name, size_bytes=entry.size_bytes)


def attachable_files(db: Database, user: User, area: FileArea) -> list[FileEntry]:
    """The files in `area` that `user` can point a letter at, newest first
    (at most `ATTACHABLE_LIST_LIMIT`): approved and not past their age.
    Empty for an area `user` may not read."""
    from netbbs.files.entries import _row_to_file_entry

    if _visible_area(db, area.id) is None or not may_read_area(db, user, area):
        return []
    rows = db.connection.execute(
        "SELECT * FROM files WHERE area_id = ? AND status = 'approved' ORDER BY created_at DESC, file_id DESC LIMIT ?",
        (area.id, ATTACHABLE_LIST_LIMIT),
    ).fetchall()
    entries = [_row_to_file_entry(row) for row in rows]
    return [entry for entry in entries if not _past_its_age(entry, area)]


def sender_ref_problem(
    db: Database, sender: User, refs: list[FileRef], *, noun: str = "letter", verb: str = "send",
) -> str | None:
    """Why `sender` cannot send a letter pointing at `refs`, or `None`: too
    many, or one they cannot open (any more) themselves. Sanitized, like
    `recipient_ref_problem`: a name can come from another BBS. `noun` and
    `verb` name what is written and what finishes it -- "post" and
    "publish" for a board post (issue #842)."""
    if len(refs) > MAX_FILE_REFS:
        return f"A {noun} can point at {MAX_FILE_REFS} files at most; this one has {len(refs)}."
    for ref in refs:
        if open_ref(db, sender, ref).state != AVAILABLE:
            return (
                f"{sanitize_text(ref.filename)} is no longer available to you. "
                f"[R]emove it from the {noun}, then {verb} it."
            )
    return None


def recipient_ref_problem(db: Database, recipient: User, refs: list[FileRef]) -> str | None:
    """Why `recipient` cannot be sent a letter pointing at `refs`, or `None`:
    one of them is in a file area they may not read. Named by the area, which
    the sender can read.

    Returned sanitized, ready for the terminal: a carried file area's name,
    and a file's, come from another BBS, and a refusal is shown as it is
    (review on #912)."""
    for ref in refs:
        opened = open_ref(db, recipient, ref)
        if opened.state == NO_ACCESS:
            return sanitize_text(
                f"{recipient.username} can't open file area {ref.area_name!r}, so they couldn't "
                f"download {ref.filename}."
            )
    return None


def link_text_line(ref: FileRef, node_name: str) -> str:
    """The plain-text line that stands for `ref` where the file cannot
    follow -- in a letter to another BBS. Everything a reader needs to find
    it by hand, in words: its name, its size, its file area, and the BBS it
    is on. For example:

        File: report.zip (12.3 KiB) in file area "Uploads" on Farpoint
    """
    return f'File: {ref.filename} ({file_size_text(ref.size_bytes)}) in file area "{ref.area_name}" on {node_name}'


def body_with_link_text(db: Database, body: str, refs: list[FileRef]) -> str:
    """`body` as it goes over Link: one `link_text_line` per file at its end,
    after a blank line; `body` unchanged when there are none."""
    if not refs:
        return body
    node_name = get_node_display_name(db)
    lines = [link_text_line(ref, node_name) for ref in refs]
    return body.rstrip("\n") + "\n\n" + "\n".join(lines)


# -- mail's references ------------------------------------------------------
#
# One row per file per letter. The table has no foreign keys, deliberately:
# a foreign key to `mail_messages` would make it a parent table, and a later
# rebuild of it (as migration 97 did) would then cascade through
# `mail_file_refs` on the implicit DELETE that DROP TABLE does. Rows go with
# their letter through `netbbs.mail`'s one delete helper instead, beside the
# letter's search entry. Nor one to `files`: a file deleted since is still
# named in the letter, as no longer available.


def write_mail_refs_without_commit(db: Database, mail_id: int, refs: list[FileRef]) -> None:
    """Record that letter `mail_id` points at `refs`, in order. Called in the
    transaction that writes the letter."""
    db.connection.executemany(
        """
        INSERT INTO mail_file_refs (mail_id, position, file_id, filename, area_name, size_bytes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (mail_id, position, ref.file_id, ref.filename, ref.area_name, ref.size_bytes)
            for position, ref in enumerate(refs)
        ],
    )


def mail_refs(db: Database, mail_id: int) -> list[FileRef]:
    """The files letter `mail_id` points at, in the order they were attached."""
    rows = db.connection.execute(
        "SELECT * FROM mail_file_refs WHERE mail_id = ? ORDER BY position", (mail_id,)
    ).fetchall()
    return [_row_to_ref(row) for row in rows]


def forget_mail_refs_without_commit(db: Database, mail_ids) -> None:
    """Remove the references of letters `mail_ids`, being deleted for good."""
    db.connection.executemany("DELETE FROM mail_file_refs WHERE mail_id = ?", [(mail_id,) for mail_id in mail_ids])


def _row_to_ref(row: sqlite3.Row) -> FileRef:
    return FileRef(
        file_id=row["file_id"], filename=row["filename"], area_name=row["area_name"], size_bytes=row["size_bytes"],
    )


# -- a board post's references (issue #842) ---------------------------------
#
# The same rows as mail's, keyed by the revision's content-addressed
# `posts.post_id`: every revision of a post has its own set, so an edit can
# attach or remove a file and a held edit's files wait with it. No foreign
# keys, for mail's reason -- `posts` is rebuilt by migrations too -- and
# rows go when their revision does through `netbbs.boards.posts`' one
# cleanup, `forget_orphaned_post_refs_without_commit`, called by every hard
# delete of posts rows. A post carried from another node never has rows
# here: it names its files in its body.


def _has_post_refs_table(db: Database) -> bool:
    """Whether this schema has `post_file_refs`: posts are written and
    deleted by migrations older than the one that adds it."""
    return db.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'post_file_refs'"
    ).fetchone() is not None


def write_post_refs_without_commit(db: Database, post_id: str, refs: list[FileRef]) -> None:
    """Record that revision `post_id` points at `refs`, in order. Called in
    the transaction that writes the revision."""
    if not refs:
        return
    db.connection.executemany(
        """
        INSERT INTO post_file_refs (post_id, position, file_id, filename, area_name, size_bytes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (post_id, position, ref.file_id, ref.filename, ref.area_name, ref.size_bytes)
            for position, ref in enumerate(refs)
        ],
    )


def post_refs(db: Database, post_id: str) -> list[FileRef]:
    """The files revision `post_id` points at, in the order they were
    attached."""
    if not _has_post_refs_table(db):
        return []
    rows = db.connection.execute(
        "SELECT * FROM post_file_refs WHERE post_id = ? ORDER BY position", (post_id,)
    ).fetchall()
    return [_row_to_ref(row) for row in rows]


def forget_orphaned_post_refs_without_commit(db: Database) -> None:
    """Remove the references of post revisions no longer in `posts`: the one
    cleanup every hard delete of posts rows calls before it commits."""
    if not _has_post_refs_table(db):
        return
    db.connection.execute(
        "DELETE FROM post_file_refs WHERE post_id NOT IN (SELECT post_id FROM posts)"
    )


def refs_some_readers_cannot_open(db: Database, refs: list[FileRef], resource) -> list[FileRef]:
    """Those of `refs` in a file area with a stricter read gate than
    `resource` -- a board a post is written to (issue #842): its effective
    read level is higher, or it asks an age the board does not, or asks for
    a verified age where the board accepts a self-entered one (issue #1082).
    Some who can read the post will see only that a file is there. A file
    gone already is left out; its row says so."""
    read_level = get_effective_min_read_level(db, resource)
    min_age = get_effective_min_age(db, resource) or 0
    # Only with an age to check: without one a requirement has no effect,
    # so it must not make the board count as strict as a gated area.
    verified = bool(min_age) and get_effective_age_requirement(db, resource) == VERIFIED
    narrower = []
    for ref in refs:
        row = db.connection.execute("SELECT area_id FROM files WHERE file_id = ?", (ref.file_id,)).fetchone()
        area = _visible_area(db, row["area_id"]) if row is not None else None
        if area is None:
            continue
        area_age = get_effective_min_age(db, area) or 0
        area_verified = bool(area_age) and get_effective_age_requirement(db, area) == VERIFIED
        if (
            get_effective_min_read_level(db, area) > read_level
            or area_age > min_age
            or (area_verified and not verified)
        ):
            narrower.append(ref)
    return narrower
