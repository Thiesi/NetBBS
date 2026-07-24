"""
Local-origination bridge for remote file areas (design doc §11, issue
#89) -- turns an existing local file area/upload into a signed
`file_area_genesis`/`file_descriptor` Link event, persists it on the
area's/file's own row, and (for an area's genesis specifically)
registers it with the running node the same two ways `netbbs.link.
boards.link_board` already does for a board.

Mirrors `netbbs.link.boards`/`netbbs.link.channels` as closely as the
underlying local resources allow, with one structural difference that
follows from §11 itself, not invented for Link: a linked file area's
*content* is never eagerly replicated the way a carried board's posts or
a carried channel's messages are. A carried file's catalogue entry
(metadata: name/size/hash) lives in a new `remote_files` table, not the
real local `files` table -- see `materialize_carried_file_descriptor`'s
own docstring for why. Actual bytes are fetched on demand by
`netbbs.link.file_transfer`, a separate module for the genuinely new
point-to-point chunk-transfer mechanism this issue also adds.

No `file_area_origin_transfer_offer`/`_accepted` event types in this
issue -- same deliberate deferral `netbbs.link.channels` already applies
to channel origin succession; §9.4's model applies unchanged by
reference if a future issue ever needs it.

Every function here is plain and synchronous, `db`-first, same calling
convention as `netbbs.link.boards`/`netbbs.link.channels`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from netbbs.files.areas import FileArea
from netbbs.files.entries import FileEntry
from netbbs.link.events import (
    FILE_DESCRIPTOR_OBJECT_TYPE,
    FileAreaGenesis,
    FileDescriptor,
    build_file_area_genesis,
    build_file_descriptor,
)
from netbbs.link.node_identity import NodeIdentity
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class LinkFilesError(Exception):
    """Raised for re-Linking an already-Linked file area."""


class FileAreaCarryLimitError(Exception):
    """Raised by `materialize_carried_file_area` when this node's own
    `max_carried_file_areas` quota (design doc §11/§13.9) is already
    reached -- the exact file-area-side counterpart to `netbbs.link.
    boards.BoardCarryLimitError`; see that exception's own docstring for
    why the caller must treat this differently from every other
    rejection: the underlying `file_area_genesis` is already verified,
    accepted, and persisted, and keeps gossiping normally regardless --
    only this node's own local materialization is refused."""


class RemoteFileCatalogueLimitError(Exception):
    """Raised by `materialize_carried_file_descriptor` when this node's
    own `max_remote_files_per_area` quota (design doc §11.2/§13.5's
    bounded-remote-influence principle) is already reached for the
    file's own area -- the catalogue-side counterpart to
    `FileAreaCarryLimitError`: the underlying `file_descriptor` is
    already verified, accepted, and persisted, and keeps gossiping
    normally regardless -- only this node's own local catalogue entry is
    refused, the same 'accept the event, decline to carry it locally'
    split every other carry-limit error in this codebase already makes."""


def is_area_linked(db: Database, area: FileArea) -> bool:
    """Whether `area` already has a `file_area_genesis` on file -- the
    single source of truth for "is this file area Linked," mirroring
    `netbbs.link.boards.is_board_linked` exactly."""
    row = db.connection.execute(
        "SELECT link_genesis_json FROM file_areas WHERE id = ?", (area.id,)
    ).fetchone()
    return row is not None and row["link_genesis_json"] is not None


def link_file_area(
    db: Database,
    area: FileArea,
    *,
    node_identity: NodeIdentity,
    default_min_read_level: int | None = None,
    default_min_write_level: int | None = None,
    default_moderated: bool | None = None,
    default_max_file_age_days: int | None = None,
    default_min_age: int | None = None,
    default_name_requirement: str | None = None,
) -> FileAreaGenesis:
    """
    Put `area` into Link scope: build and sign a `file_area_genesis`
    event referencing its existing `area_id` and persist it on the
    area's own row -- mirrors `netbbs.link.boards.link_board` exactly.

    Raises `LinkFilesError` if `area` is already Linked.

    Deliberately does **not** register the result with a live `LinkNode`
    -- same division of responsibility `link_board` already documents;
    callers (the `[L]ink` admin command) do that themselves right after
    this function returns.
    """
    if is_area_linked(db, area):
        raise LinkFilesError(f"file area {area.name!r} is already Linked")

    genesis = build_file_area_genesis(
        signing_identity=node_identity.signing_key,
        origin_fingerprint=node_identity.fingerprint,
        area_id=area.area_id,
        name=area.name,
        created_at=utc_now_iso(),
        description=area.description,
        default_min_read_level=default_min_read_level,
        default_min_write_level=default_min_write_level,
        default_moderated=default_moderated,
        default_max_file_age_days=default_max_file_age_days,
        default_min_age=default_min_age,
        default_name_requirement=default_name_requirement,
    )

    db.connection.execute(
        "UPDATE file_areas SET link_genesis_json = ? WHERE id = ?",
        (json.dumps(genesis.to_dict()), area.id),
    )
    db.connection.commit()

    return genesis


def _file_area_from_row(row) -> FileArea:
    """Local row->`FileArea` mapping, mirroring `netbbs.link.boards.
    _board_from_row`'s own "duplicate rather than reach into another
    module's private helper" reasoning."""
    return FileArea(
        id=row["id"], area_id=row["area_id"], name=row["name"], description=row["description"],
        min_read_level=row["min_read_level"], min_write_level=row["min_write_level"],
        category_id=row["category_id"], pinned=bool(row["pinned"]), created_at=row["created_at"],
        moderated=bool(row["moderated"]), max_file_age_days=row["max_file_age_days"],
        min_age=row["min_age"], name_requirement=row["name_requirement"], community_id=row["community_id"],
    )


def carried_file_area_count(db: Database, own_fingerprint: str) -> int:
    """How many file areas this node currently carries -- mirrors
    `netbbs.link.boards.carried_board_count` exactly."""
    count = 0
    for row in db.connection.execute(
        "SELECT link_genesis_json FROM file_areas WHERE link_genesis_json IS NOT NULL"
    ):
        genesis = json.loads(row["link_genesis_json"])
        if genesis["envelope"]["payload"].get("origin_fingerprint") != own_fingerprint:
            count += 1
    return count


def materialize_carried_file_area(
    db: Database,
    genesis: FileAreaGenesis,
    *,
    own_fingerprint: str | None = None,
    max_carried_file_areas: int | None = None,
) -> FileArea:
    """
    Turn a *received* (not self-originated) `file_area_genesis` into a
    real, locally browsable `FileArea` row -- mirrors `netbbs.link.
    boards.materialize_carried_board` exactly: a direct insert using the
    genesis's own `area_id` verbatim, never `netbbs.files.areas.
    create_file_area` (which mints a fresh content-addressed id, wrong
    for carried content). Idempotent, keyed on
    `genesis.payload["area_id"]`.
    """
    existing = db.connection.execute(
        "SELECT * FROM file_areas WHERE area_id = ?", (genesis.payload["area_id"],)
    ).fetchone()
    if existing is not None:
        return _file_area_from_row(existing)

    if (
        max_carried_file_areas is not None
        and carried_file_area_count(db, own_fingerprint) >= max_carried_file_areas
    ):
        raise FileAreaCarryLimitError(
            f"cannot carry file area {genesis.payload['area_id']!r}: already at this node's own "
            f"max_carried_file_areas limit ({max_carried_file_areas})"
        )

    payload = genesis.payload
    db.connection.execute(
        """
        INSERT INTO file_areas
            (area_id, name, description, min_read_level, min_write_level, category_id,
             pinned, created_at, moderated, max_file_age_days, min_age, name_requirement,
             community_id, link_genesis_json)
        VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            payload["area_id"],
            payload["name"],
            payload.get("description"),
            payload.get("default_min_read_level", 0),
            payload.get("default_min_write_level", 0),
            payload["created_at"],
            int(payload.get("default_moderated", False)),
            payload.get("default_max_file_age_days"),
            payload.get("default_min_age"),
            payload.get("default_name_requirement"),
            json.dumps(genesis.to_dict()),
        ),
    )
    db.connection.commit()

    return _file_area_from_row(
        db.connection.execute("SELECT * FROM file_areas WHERE area_id = ?", (payload["area_id"],)).fetchone()
    )


@dataclass(frozen=True)
class RemoteFile:
    """One carried file's catalogue entry (design doc §11.2, issue #89)
    -- metadata only, never content. `fetched_file_id` is `None` until
    `netbbs.link.file_transfer` completes and verifies a chunk transfer
    for it, at which point it references the resulting real `files`
    row's own `file_id`."""

    id: int
    file_id: str
    area_id: int
    origin_fingerprint: str
    filename: str
    description: str | None
    size_bytes: int
    sha256: str
    created_at: str
    fetched_file_id: str | None


def _remote_file_from_row(row) -> RemoteFile:
    return RemoteFile(
        id=row["id"], file_id=row["file_id"], area_id=row["area_id"],
        origin_fingerprint=row["origin_fingerprint"], filename=row["filename"],
        description=row["description"], size_bytes=row["size_bytes"], sha256=row["sha256"],
        created_at=row["created_at"], fetched_file_id=row["fetched_file_id"],
    )


def remote_file_count_for_area(db: Database, area_id: int) -> int:
    row = db.connection.execute(
        "SELECT COUNT(*) AS n FROM remote_files WHERE area_id = ?", (area_id,)
    ).fetchone()
    return row["n"]


def list_remote_files(db: Database, area: FileArea) -> list[RemoteFile]:
    """Every catalogued file for `area`, fetched or not -- the acceptance
    criterion's own "discover and list... without fetching any file
    content." Ordered newest first, matching `netbbs.files.entries.
    list_files`' own default ordering."""
    return [
        _remote_file_from_row(row)
        for row in db.connection.execute(
            "SELECT * FROM remote_files WHERE area_id = ? ORDER BY created_at DESC", (area.id,)
        )
    ]


def get_remote_file(db: Database, file_id: str) -> RemoteFile | None:
    row = db.connection.execute("SELECT * FROM remote_files WHERE file_id = ?", (file_id,)).fetchone()
    return None if row is None else _remote_file_from_row(row)


def materialize_carried_file_descriptor(
    db: Database,
    descriptor: FileDescriptor,
    *,
    sender_fingerprint: str,
    max_remote_files_per_area: int | None = None,
) -> RemoteFile | None:
    """
    Turn a *received* `file_descriptor` into a new `remote_files` row --
    catalogue metadata only, deliberately never a `files` row (see module
    docstring). Idempotent, keyed on `descriptor.payload["file_id"]` --
    the file's own local content-addressed id (`remote_files.file_id`),
    **not** `descriptor.content_id` (the signed *event's* own envelope
    hash, a different value -- see `FileDescriptor`'s own docstring for
    why the payload carries `file_id` explicitly rather than reusing the
    event's identity the way `BoardPost` does). Using the wrong one here
    would leave `netbbs.link.file_transfer.build_chunk_for_serving`
    unable to ever resolve the origin's own `files` row by the id this
    node then asks for.

    Returns `None` (not an error) if `descriptor.payload["area_id"]`
    names an area this node does not carry -- the same "not carried on
    this node" honest exclusion §9.3 already establishes for a board_post
    whose board is unknown.

    Raises `RemoteFileCatalogueLimitError` once `max_remote_files_per_
    area` is reached (§13.5's bounded-remote-influence principle) -- the
    underlying event is already accepted/persisted by the time this is
    called; only this node's own local catalogue entry is refused, the
    same split `FileAreaCarryLimitError` already documents. An idempotent
    resend of an already-catalogued file is exempt from the cap, same
    "resend never blocked by a cap reached after the first materialization"
    rule `materialize_carried_board`'s own tests establish.

    Issue #93: the `link_events` row this function inserts directly
    (rather than through `netbbs.link.store.save_event`, same shape
    `materialize_carried_post`/`materialize_carried_channel_message`
    already use) now also carries `file_area_id` -- the file-area-scoped
    counterpart to `board_id`/`channel_id`, feeding `netbbs.link.store.
    _all_file_area_events`'s own inventory-diff query the identical way
    those columns already feed the board/channel diffs.
    """
    file_id = descriptor.payload["file_id"]
    existing = db.connection.execute(
        "SELECT * FROM remote_files WHERE file_id = ?", (file_id,)
    ).fetchone()
    if existing is not None:
        return _remote_file_from_row(existing)

    area_row = db.connection.execute(
        "SELECT id, link_genesis_json FROM file_areas WHERE area_id = ?", (descriptor.payload["area_id"],)
    ).fetchone()
    if area_row is None:
        return None
    area_local_id = area_row["id"]
    # The area's own origin, resolved from its locally-materialized
    # genesis -- never sender_fingerprint (the wire-level relay this
    # particular envelope happened to arrive from, possibly not the same
    # node at all -- see FileAreaGenesis's own "no origin succession
    # built yet" scope note for why this is always just the genesis's
    # own claim, no further resolution needed).
    origin_fingerprint = json.loads(area_row["link_genesis_json"])["envelope"]["payload"]["origin_fingerprint"]

    if (
        max_remote_files_per_area is not None
        and remote_file_count_for_area(db, area_local_id) >= max_remote_files_per_area
    ):
        raise RemoteFileCatalogueLimitError(
            f"cannot catalogue file {file_id!r}: already at this node's own "
            f"max_remote_files_per_area limit ({max_remote_files_per_area}) for area_id "
            f"{descriptor.payload['area_id']!r}"
        )

    payload = descriptor.payload

    db.connection.execute(
        """
        INSERT INTO link_events
            (content_id, sender_fingerprint, object_type, envelope_json, received_at, file_area_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(content_id) DO NOTHING
        """,
        (
            descriptor.content_id, sender_fingerprint, FILE_DESCRIPTOR_OBJECT_TYPE,
            json.dumps(descriptor.to_dict()), utc_now_iso(), descriptor.payload["area_id"],
        ),
    )
    db.connection.execute(
        """
        INSERT INTO remote_files
            (file_id, area_id, origin_fingerprint, filename, description, size_bytes, sha256,
             created_at, link_event_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id, area_local_id, origin_fingerprint, payload["filename"],
            payload.get("description"), payload["size_bytes"], payload["sha256"], payload["created_at"],
            json.dumps(descriptor.to_dict()),
        ),
    )
    db.connection.commit()

    return _remote_file_from_row(
        db.connection.execute("SELECT * FROM remote_files WHERE file_id = ?", (file_id,)).fetchone()
    )


def queue_file_descriptor_if_linked(
    db: Database,
    file_entry: FileEntry,
    area: FileArea,
    *,
    node_identity: NodeIdentity,
) -> FileDescriptor | None:
    """
    If `area` is Linked and `file_entry` is currently `'approved'`, build
    and sign a `file_descriptor` event for it and store it on the file's
    own row for `netbbs.link.sync` to push -- a no-op returning `None`
    otherwise. Mirrors `netbbs.link.boards.queue_board_post_if_linked`
    exactly, minus the reply/`parent_post_id` linking logic files have no
    equivalent of.

    Idempotent: a file that already has a queued event returns it as-is
    rather than building (and re-signing, with a fresh `nonce`) a second,
    different one for the same logical upload.
    """
    if file_entry.status != "approved":
        return None
    if not is_area_linked(db, area):
        return None

    existing = db.connection.execute(
        "SELECT link_event_json FROM files WHERE file_id = ?", (file_entry.file_id,)
    ).fetchone()
    if existing is not None and existing["link_event_json"] is not None:
        return FileDescriptor.from_dict(json.loads(existing["link_event_json"]))

    descriptor = build_file_descriptor(
        signing_identity=node_identity.signing_key,
        area_id=area.area_id,
        file_id=file_entry.file_id,
        filename=file_entry.filename,
        size_bytes=file_entry.size_bytes,
        sha256=file_entry.sha256,
        created_at=file_entry.created_at,
        description=file_entry.description,
    )

    db.connection.execute(
        "UPDATE files SET link_event_json = ? WHERE file_id = ?",
        (json.dumps(descriptor.to_dict()), file_entry.file_id),
    )
    db.connection.commit()

    return descriptor


def load_own_file_area_events(db: Database, own_fingerprint: str) -> list[FileAreaGenesis | FileDescriptor]:
    """
    This node's own originated `file_area_genesis`/`file_descriptor`
    events, read directly off the `file_areas`/`files` tables' own
    columns -- mirrors `netbbs.link.channels.load_own_channel_events`
    exactly, minus the lifecycle-event half (no origin succession for
    file areas yet, see module docstring).
    """
    events: list[FileAreaGenesis | FileDescriptor] = []
    for row in db.connection.execute(
        "SELECT link_genesis_json FROM file_areas WHERE link_genesis_json IS NOT NULL"
    ):
        genesis = FileAreaGenesis.from_dict(json.loads(row["link_genesis_json"]))
        if genesis.payload["origin_fingerprint"] == own_fingerprint:
            events.append(genesis)
    for row in db.connection.execute(
        "SELECT link_event_json FROM files WHERE link_event_json IS NOT NULL"
    ):
        events.append(FileDescriptor.from_dict(json.loads(row["link_event_json"])))
    return events
