"""
Local full-text search over this node's own carried content (design doc
§6.6, issue #56's last piece) -- board posts, files, and recent channel
scrollback. Never Link-wide: a search only ever queries this node's own
SQLite FTS5 tables, and a query string is never transmitted to any peer
or broadcast over Link, by design and without exception.

Three FTS5 virtual tables (`netbbs.storage.migrations`) are kept in sync
with `posts`/`files`/`channel_messages` by explicit calls from
`netbbs.boards.posts`, `netbbs.files.entries`, and
`netbbs.chat.scrollback` at every write path -- never SQL triggers,
matching this codebase's existing convention. `post_search` only ever
holds the *resolved current* approved revision of a post's edit chain
(mirroring `netbbs.boards.posts._resolve_current_version`): a superseded
revision, a still-pending edit, or a root with no approved revision left
is never indexed. `file_search` mirrors `files` one-to-one (files have
no edit chain). `channel_message_search` is pruned in lockstep with
`netbbs.chat.scrollback`'s own bounded ring-buffer trim, so a search can
never surface a message already gone from scrollback.

Query-time authorization reuses the exact same visibility gates normal
browsing already enforces (`netbbs.net.login_flow._new_scan_screen`'s own
pattern) -- a level/age/community gate for boards and file areas,
`netbbs.net.chat_flow.list_visible_channels_for` for channels -- so
search can never be a side-channel revealing a restricted resource's
existence or content.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from netbbs.attestation import meets_age
from netbbs.auth.users import User
from netbbs.communities import get_effective_min_age, get_effective_min_read_level
from netbbs.permissions import meets_level
from netbbs.storage.database import Database

if TYPE_CHECKING:
    # Deferred (not top-level) to avoid a real import cycle: `netbbs.
    # boards`/`netbbs.files`/`netbbs.chat`'s own `__init__.py` import
    # `posts`/`entries`/`scrollback`, which import *this* module for
    # `reindex_post`/`reindex_file`/`index_channel_message` -- a
    # top-level `from netbbs.boards.boards import Board` here would race
    # that against whichever of the two packages happens to start
    # importing first. `search_posts`/`search_files` below import
    # `list_boards`/`list_file_areas` locally, inside the function body,
    # for the same reason -- by the time either is actually called
    # (always after full application startup), both packages are long
    # since fully loaded, so the deferred import is free.
    from netbbs.boards.boards import Board
    from netbbs.chat.channels import Channel
    from netbbs.files.areas import FileArea

# Channel message kinds worth searching -- mirrors
# netbbs.activity._CHANNEL_CONTENT_KINDS exactly: join/leave/mute/etc.
# system notices are never content, so never indexed.
_CHANNEL_CONTENT_KINDS = ("message", "action")

# Raw FTS5 hits are fetched in excess of the caller's requested limit,
# then filtered by per-item visibility and truncated -- a board/area/
# channel a searching user can't currently access must never surface a
# result, so filtering has to happen after the match, not instead of it.
# This cap bounds that overfetch regardless of how popular a query term
# is, consistent with this project's "bound remotely influenced
# resources" convention even though search queries are always local.
_OVERFETCH_LIMIT = 500


def _match_expression(query: str) -> str | None:
    """Turn free-typed `query` into a safe FTS5 MATCH expression, or
    `None` for a blank query (nothing to search). Every whitespace-
    separated token is individually double-quoted (FTS5 escapes an
    embedded `"` as `""`) and implicitly AND-ed together -- this treats
    the query as a literal phrase-per-token search, never letting a
    user's typed text be interpreted as FTS5 query syntax (AND/OR/NOT/
    NEAR/column filters/prefix `*`), which would otherwise let oddly
    formatted input raise a syntax error deep inside a MATCH clause
    instead of just searching for it literally."""
    tokens = query.split()
    if not tokens:
        return None
    return " ".join('"' + token.replace('"', '""') + '"' for token in tokens)


@dataclass(frozen=True)
class PostSearchHit:
    board: Board
    root_post_id: str
    subject: str
    body: str


@dataclass(frozen=True)
class FileSearchHit:
    area: FileArea
    file_id: str
    filename: str
    description: str | None


@dataclass(frozen=True)
class ChannelMessageSearchHit:
    channel: Channel
    message_id: int
    author_label: str
    body: str


def search_posts(db: Database, user: User, query: str, *, limit: int = 20) -> list[PostSearchHit]:
    """Approved board posts matching `query`, most relevant first,
    filtered to boards `user` can currently read (level, age, Community
    inheritance -- `netbbs.communities.get_effective_min_read_level`/
    `get_effective_min_age`, the same gate `_new_scan_screen` applies)."""
    from netbbs.boards.boards import list_boards  # deferred -- see module's TYPE_CHECKING note

    expr = _match_expression(query)
    if expr is None:
        return []

    rows = db.connection.execute(
        """
        SELECT board_id, root_post_id, subject, body FROM post_search
        WHERE post_search MATCH ? ORDER BY bm25(post_search) LIMIT ?
        """,
        (expr, _OVERFETCH_LIMIT),
    ).fetchall()

    boards_by_id = {board.id: board for board in list_boards(db)}
    hits: list[PostSearchHit] = []
    for row in rows:
        board = boards_by_id.get(row["board_id"])
        if board is None:
            continue
        if not (
            meets_level(user, get_effective_min_read_level(db, board))
            and meets_age(db, user, get_effective_min_age(db, board))
        ):
            continue
        hits.append(
            PostSearchHit(board=board, root_post_id=row["root_post_id"], subject=row["subject"], body=row["body"])
        )
        if len(hits) >= limit:
            break
    return hits


def search_files(db: Database, user: User, query: str, *, limit: int = 20) -> list[FileSearchHit]:
    """Approved files matching `query`, most relevant first, filtered to
    areas `user` can currently read -- same gate as `search_posts`."""
    from netbbs.files.areas import list_file_areas  # deferred -- see module's TYPE_CHECKING note

    expr = _match_expression(query)
    if expr is None:
        return []

    rows = db.connection.execute(
        """
        SELECT area_id, file_id, filename, description FROM file_search
        WHERE file_search MATCH ? ORDER BY bm25(file_search) LIMIT ?
        """,
        (expr, _OVERFETCH_LIMIT),
    ).fetchall()

    areas_by_id = {area.id: area for area in list_file_areas(db)}
    hits: list[FileSearchHit] = []
    for row in rows:
        area = areas_by_id.get(row["area_id"])
        if area is None:
            continue
        if not (
            meets_level(user, get_effective_min_read_level(db, area))
            and meets_age(db, user, get_effective_min_age(db, area))
        ):
            continue
        hits.append(
            FileSearchHit(area=area, file_id=row["file_id"], filename=row["filename"], description=row["description"])
        )
        if len(hits) >= limit:
            break
    return hits


def search_channel_messages(
    db: Database, user: User, query: str, *, visible_channels: list[Channel], limit: int = 20
) -> list[ChannelMessageSearchHit]:
    """Retained channel scrollback matching `query`, most relevant
    first, filtered to `visible_channels` -- the caller (`netbbs.net.
    login_flow`) supplies this via `netbbs.net.chat_flow.
    list_visible_channels_for(db, user)`, the same call `_new_scan_
    screen` already makes, rather than this module reaching into chat
    visibility rules itself and risking the two drifting apart."""
    expr = _match_expression(query)
    if expr is None:
        return []

    rows = db.connection.execute(
        """
        SELECT channel_id, message_id, body FROM channel_message_search
        WHERE channel_message_search MATCH ? ORDER BY bm25(channel_message_search) LIMIT ?
        """,
        (expr, _OVERFETCH_LIMIT),
    ).fetchall()

    channels_by_id = {channel.id: channel for channel in visible_channels}
    hits: list[ChannelMessageSearchHit] = []
    for row in rows:
        channel = channels_by_id.get(row["channel_id"])
        if channel is None:
            continue
        message_row = db.connection.execute(
            "SELECT author_label FROM channel_messages WHERE id = ?", (row["message_id"],)
        ).fetchone()
        if message_row is None:
            continue  # trimmed since the search index was last pruned
        hits.append(
            ChannelMessageSearchHit(
                channel=channel, message_id=row["message_id"], author_label=message_row["author_label"],
                body=row["body"],
            )
        )
        if len(hits) >= limit:
            break
    return hits


# -- jump-to-hit cursors ---------------------------------------------------
#
# Selecting a search hit should land a user on the matched post/file, not
# just somewhere in its board/area -- these compute the `after=` cursor
# netbbs.boards.posts.list_posts_page/netbbs.files.entries.list_files_page
# already accept (the same parameter netbbs.net.login_flow's [N]ew scan
# threads through as initial_cursor), set to the *immediately preceding*
# root/file so the hit itself becomes the first item shown, mirroring
# list_posts_page's own has_older boundary query exactly. `("", "")` is
# returned when the hit is the oldest item on its board/area -- an
# empty-string sentinel that compares less than any real (created_at,
# stable_id) tuple (neither is ever an empty string), so `after=("", "")`
# reliably starts from the very beginning without list_posts_page/
# list_files_page needing a fourth "from the start" pagination mode of
# their own just for this.


def post_jump_cursor(db: Database, board_id: int, root_post_id: str) -> tuple[str, str]:
    row = db.connection.execute(
        "SELECT created_at FROM posts WHERE post_id = ? AND board_id = ?", (root_post_id, board_id)
    ).fetchone()
    if row is None:
        return ("", "")
    predecessor = db.connection.execute(
        """
        SELECT created_at, post_id FROM posts root
        WHERE root.board_id = ? AND root.post_id = root.root_post_id
          AND (root.created_at, root.post_id) < (?, ?)
        ORDER BY root.created_at DESC, root.post_id DESC
        LIMIT 1
        """,
        (board_id, row["created_at"], root_post_id),
    ).fetchone()
    if predecessor is None:
        return ("", "")
    return (predecessor["created_at"], predecessor["post_id"])


def file_jump_cursor(db: Database, area_id: int, file_id: str) -> tuple[str, str]:
    row = db.connection.execute(
        "SELECT created_at FROM files WHERE file_id = ? AND area_id = ?", (file_id, area_id)
    ).fetchone()
    if row is None:
        return ("", "")
    predecessor = db.connection.execute(
        """
        SELECT created_at, file_id FROM files
        WHERE area_id = ? AND (created_at, file_id) < (?, ?)
        ORDER BY created_at DESC, file_id DESC
        LIMIT 1
        """,
        (area_id, row["created_at"], file_id),
    ).fetchone()
    if predecessor is None:
        return ("", "")
    return (predecessor["created_at"], predecessor["file_id"])


# -- index maintenance ---------------------------------------------------


def reindex_post(db: Database, board_id: int, root_post_id: str) -> None:
    """Recompute `post_search`'s entry for one edit chain: remove
    whatever revision (if any) is currently indexed for `root_post_id`,
    then index the current resolved version -- the newest row sharing
    `root_post_id` that is `status = 'approved'` -- if one still exists.
    Idempotent, and safe to call after any mutation that could change
    which revision (if any) that is: a new post/edit created, a pending
    edit approved, a post/edit deleted, or the expiry sweep flipping a
    revision's status. Mirrors `netbbs.boards.posts._resolve_current_
    version`'s own "newest approved row for this root" query exactly,
    tie-break included (`id`, not `post_id` -- GitHub issue #68), so the
    index can never disagree with what `list_posts_page` would actually
    show."""
    db.connection.execute("DELETE FROM post_search WHERE root_post_id = ?", (root_post_id,))
    current = db.connection.execute(
        """
        SELECT subject, body FROM posts
        WHERE root_post_id = ? AND board_id = ? AND status = 'approved'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (root_post_id, board_id),
    ).fetchone()
    if current is not None:
        db.connection.execute(
            "INSERT INTO post_search (subject, body, board_id, root_post_id) VALUES (?, ?, ?, ?)",
            (current["subject"], current["body"], board_id, root_post_id),
        )
    db.connection.commit()


def reindex_file(db: Database, area_id: int, file_id: str) -> None:
    """Recompute `file_search`'s entry for one file -- unlike posts,
    files have no edit chain, so this is a plain "is this file currently
    approved" check against the single `files` row for `file_id`, not a
    resolved-version query. Idempotent; safe after upload, approval,
    deletion, or the expiry sweep."""
    db.connection.execute("DELETE FROM file_search WHERE file_id = ?", (file_id,))
    current = db.connection.execute(
        "SELECT filename, description FROM files WHERE file_id = ? AND area_id = ? AND status = 'approved'",
        (file_id, area_id),
    ).fetchone()
    if current is not None:
        db.connection.execute(
            "INSERT INTO file_search (filename, description, area_id, file_id) VALUES (?, ?, ?, ?)",
            (current["filename"], current["description"], area_id, file_id),
        )
    db.connection.commit()


def index_channel_message(db: Database, channel_id: int, message_id: int, kind: str, body: str | None) -> None:
    """Index one freshly recorded channel message, if its `kind` counts
    as searchable content (`_CHANNEL_CONTENT_KINDS`, mirroring
    `netbbs.activity`'s identical unread-counting exclusion of system
    notices). Channel messages have no edit/approval concept, so this is
    a plain insert, never a resolve-and-replace -- `netbbs.chat.
    scrollback.record_message` calls this once per new message, right
    alongside its own trim step (`prune_channel_message_search`)."""
    if kind not in _CHANNEL_CONTENT_KINDS or body is None:
        return
    db.connection.execute(
        "INSERT INTO channel_message_search (body, channel_id, message_id) VALUES (?, ?, ?)",
        (body, channel_id, message_id),
    )


def prune_channel_message_search(db: Database, channel_id: int) -> None:
    """Remove every indexed message for `channel_id` no longer present
    in `channel_messages` -- called immediately after `netbbs.chat.
    scrollback.record_message`'s own ring-buffer trim `DELETE`, so the
    search index can never outlive what scrollback itself still
    retains."""
    db.connection.execute(
        """
        DELETE FROM channel_message_search
        WHERE channel_id = ? AND message_id NOT IN (
            SELECT id FROM channel_messages WHERE channel_id = ?
        )
        """,
        (channel_id, channel_id),
    )


# -- integrity checking and rebuild (issue #74) ---------------------------
#
# The three FTS tables above are maintained by explicit calls from every
# write path in netbbs.boards.posts/netbbs.files.entries/netbbs.chat.
# scrollback, not SQL triggers or one shared transaction with the
# authoritative write -- a crash, SQLite error, interrupted migration, or
# a future write path that forgets to call the right reindex function can
# leave a table stale with no supported way to detect or repair it. The
# three `_expected_*_index` functions below are the single source of
# truth for "what should currently be indexed," computed straight from
# `posts`/`files`/`channel_messages`; `check_index_integrity` compares
# that against what the FTS tables actually contain, and `rebuild_indexes`
# replaces their contents with it outright. Both therefore agree by
# construction -- a rebuild always converges to a clean check immediately
# after, and neither can drift from the other the way two independently
# written queries could.


def _expected_post_index(db: Database) -> dict[str, tuple[int, str, str]]:
    """`root_post_id -> (board_id, subject, body)` for the current
    resolved version of every post -- the newest `status = 'approved'`
    row sharing a `root_post_id`, tie-broken on `id` (GitHub issue #68),
    exactly matching `_resolve_current_version`/`reindex_post`. Computed
    with one query plus an ascending scan (each root's last-seen row in
    `created_at, id` order is its newest), rather than one query per
    root_post_id, since the table can hold many roots."""
    rows = db.connection.execute(
        """
        SELECT board_id, root_post_id, subject, body
        FROM posts WHERE status = 'approved'
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    resolved: dict[str, tuple[int, str, str]] = {}
    for row in rows:
        resolved[row["root_post_id"]] = (row["board_id"], row["subject"], row["body"])
    return resolved


def _expected_file_index(db: Database) -> dict[str, tuple[int, str, str | None]]:
    """`file_id -> (area_id, filename, description)` for every currently
    approved file. Files have no edit chain, so unlike posts this is a
    plain one-to-one mirror of `files`, matching `reindex_file`."""
    rows = db.connection.execute(
        "SELECT area_id, file_id, filename, description FROM files WHERE status = 'approved'"
    ).fetchall()
    return {row["file_id"]: (row["area_id"], row["filename"], row["description"]) for row in rows}


def _expected_channel_message_index(db: Database) -> dict[int, tuple[int, str]]:
    """`message_id -> (channel_id, body)` for every currently retained
    channel message whose `kind` counts as searchable content
    (`_CHANNEL_CONTENT_KINDS`), matching `index_channel_message`. Channel
    messages have no edit/approval concept -- retained in
    `channel_messages` at all is the only criterion."""
    placeholders = ",".join("?" * len(_CHANNEL_CONTENT_KINDS))
    rows = db.connection.execute(
        f"SELECT id, channel_id, body FROM channel_messages "
        f"WHERE kind IN ({placeholders}) AND body IS NOT NULL",
        _CHANNEL_CONTENT_KINDS,
    ).fetchall()
    return {row["id"]: (row["channel_id"], row["body"]) for row in rows}


@dataclass(frozen=True)
class IndexDrift:
    """One table's disagreement between what's currently indexed and
    what `_expected_*_index` says should be. Every field holds only ids
    (`root_post_id`/`file_id`/`message_id`), never indexed text -- an
    integrity report must not itself become a way to read otherwise-
    inaccessible content."""

    missing: tuple[str | int, ...]
    """Should be indexed (approved/retained content) but currently isn't."""
    stale: tuple[str | int, ...]
    """Indexed, but with different content than the authoritative row --
    e.g. the wrong edit-chain revision, or a filename changed since."""
    extra: tuple[str | int, ...]
    """Indexed but shouldn't be -- e.g. content since deleted, expired,
    or (for posts) no longer the resolved current revision."""

    @property
    def is_clean(self) -> bool:
        return not (self.missing or self.stale or self.extra)


@dataclass(frozen=True)
class SearchIndexIntegrityReport:
    posts: IndexDrift
    files: IndexDrift
    channel_messages: IndexDrift

    @property
    def is_clean(self) -> bool:
        return self.posts.is_clean and self.files.is_clean and self.channel_messages.is_clean


def _diff_index(expected: dict, actual: dict) -> IndexDrift:
    expected_keys = set(expected)
    actual_keys = set(actual)
    return IndexDrift(
        missing=tuple(sorted(expected_keys - actual_keys, key=str)),
        extra=tuple(sorted(actual_keys - expected_keys, key=str)),
        stale=tuple(sorted((k for k in expected_keys & actual_keys if expected[k] != actual[k]), key=str)),
    )


def check_index_integrity(db: Database) -> SearchIndexIntegrityReport:
    """Compare all three FTS tables against authoritative data without
    rebuilding anything -- a read-only diagnostic safe to run at startup
    or on demand. See `IndexDrift`/`SearchIndexIntegrityReport` for what
    a caller can learn from the result; `rebuild_indexes` is the repair
    action once drift is found."""
    posts_actual = {
        row["root_post_id"]: (row["board_id"], row["subject"], row["body"])
        for row in db.connection.execute("SELECT root_post_id, board_id, subject, body FROM post_search")
    }
    files_actual = {
        row["file_id"]: (row["area_id"], row["filename"], row["description"])
        for row in db.connection.execute("SELECT file_id, area_id, filename, description FROM file_search")
    }
    channel_actual = {
        row["message_id"]: (row["channel_id"], row["body"])
        for row in db.connection.execute("SELECT message_id, channel_id, body FROM channel_message_search")
    }
    return SearchIndexIntegrityReport(
        posts=_diff_index(_expected_post_index(db), posts_actual),
        files=_diff_index(_expected_file_index(db), files_actual),
        channel_messages=_diff_index(_expected_channel_message_index(db), channel_actual),
    )


def rebuild_indexes(db: Database) -> SearchIndexIntegrityReport:
    """
    Rebuild all three FTS tables from authoritative data, replacing their
    entire contents. Idempotent, and safe to run at any time -- a crash
    between an authoritative commit and its reindex call, an interrupted
    migration, or a restored older backup can all leave these tables
    inconsistent with no other supported repair path.

    Uses the exact same `_expected_*_index` computation `check_index_
    integrity` compares against, so the returned report (the state
    *before* this rebuild ran, for visibility into what was actually
    wrong) is immediately followed by a genuinely clean index -- calling
    `check_index_integrity` again right after always reports
    `is_clean == True`.
    """
    before = check_index_integrity(db)

    posts_expected = _expected_post_index(db)
    db.connection.execute("DELETE FROM post_search")
    db.connection.executemany(
        "INSERT INTO post_search (root_post_id, board_id, subject, body) VALUES (?, ?, ?, ?)",
        [(root_post_id, board_id, subject, body) for root_post_id, (board_id, subject, body) in posts_expected.items()],
    )

    files_expected = _expected_file_index(db)
    db.connection.execute("DELETE FROM file_search")
    db.connection.executemany(
        "INSERT INTO file_search (file_id, area_id, filename, description) VALUES (?, ?, ?, ?)",
        [(file_id, area_id, filename, description) for file_id, (area_id, filename, description) in files_expected.items()],
    )

    channel_expected = _expected_channel_message_index(db)
    db.connection.execute("DELETE FROM channel_message_search")
    db.connection.executemany(
        "INSERT INTO channel_message_search (message_id, channel_id, body) VALUES (?, ?, ?)",
        [(message_id, channel_id, body) for message_id, (channel_id, body) in channel_expected.items()],
    )

    db.connection.commit()
    return before


def _print_report(report: SearchIndexIntegrityReport) -> None:
    if report.is_clean:
        print("Search indexes are consistent with authoritative data.")
        return
    for name, drift in (
        ("post_search", report.posts),
        ("file_search", report.files),
        ("channel_message_search", report.channel_messages),
    ):
        if drift.is_clean:
            continue
        print(
            f"{name}: {len(drift.missing)} missing, {len(drift.stale)} stale, "
            f"{len(drift.extra)} extra"
        )


# -- CLI ---------------------------------------------------------------
#
# `python -m netbbs.search check|rebuild --db PATH` -- a standalone
# maintenance command (issue #74), mirroring `python -m netbbs.backup`'s
# own subcommand shape. Deliberately reports only counts, never the drifted
# ids/content themselves, matching `IndexDrift`'s own "never expose
# content" rule -- an operator who needs to see exactly what's wrong can
# still call `check_index_integrity`/`rebuild_indexes` directly from a
# Python shell against the same database.

_DEFAULT_DB_PATH = Path("netbbs.db")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m netbbs.search", description="Check or rebuild a NetBBS node's local search indexes."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="Report drift between search indexes and authoritative data.")
    check_parser.add_argument(
        "--db", type=Path, default=_DEFAULT_DB_PATH, help=f"path to the node's database file (default: {_DEFAULT_DB_PATH})"
    )

    rebuild_parser = subparsers.add_parser("rebuild", help="Rebuild all search indexes from authoritative data.")
    rebuild_parser.add_argument(
        "--db", type=Path, default=_DEFAULT_DB_PATH, help=f"path to the node's database file (default: {_DEFAULT_DB_PATH})"
    )

    args = parser.parse_args(argv)

    db = Database(args.db)
    try:
        if args.command == "check":
            _print_report(check_index_integrity(db))
        else:
            before = rebuild_indexes(db)
            if before.is_clean:
                print("Search indexes were already consistent; rebuilt anyway.")
            else:
                print("Drift found before rebuild:")
                _print_report(before)
            print("Rebuild complete.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
