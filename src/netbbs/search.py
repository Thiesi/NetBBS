"""
Local full-text search over this node's own carried content (design doc
§6.6, issue #56's last piece) -- board posts, files, and recent channel
scrollback -- and over the searching caller's own mailbox (issue #824).
Never Link-wide: a search only ever queries this node's own SQLite FTS5
tables, and a query string is never transmitted to any peer or broadcast
over Link, by design and without exception.

Four FTS5 virtual tables (`netbbs.storage.migrations`) are kept in sync
with `posts`/`files`/`channel_messages`/`mail_messages` by explicit calls
from `netbbs.boards.posts`, `netbbs.files.entries`,
`netbbs.chat.scrollback`, `netbbs.mail` and `netbbs.link.mail` at every
write path -- never SQL triggers,
matching this codebase's existing convention. `post_search` only ever
holds the *resolved current* approved revision of a post's edit chain
(mirroring `netbbs.boards.posts._resolve_current_version`): a superseded
revision, a still-pending edit, or a root with no approved revision left
is never indexed. `file_search` mirrors `files` one-to-one (files have
no edit chain). `channel_message_search` is pruned in lockstep with
`netbbs.chat.scrollback`'s own bounded ring-buffer trim, so a search can
never surface a message already gone from scrollback. `mail_search`
mirrors `mail_messages` one-to-one, keyed by the letter's id (see
`search_mail`'s section).

Query-time authorization reuses the exact same visibility gates normal
browsing already enforces (`netbbs.net.scan_and_find._new_scan_screen`'s own
pattern) -- a level/age/community gate for boards and file areas,
`netbbs.net.chat_flow.list_visible_channels_for` for channels -- so
search can never be a side-channel revealing a restricted resource's
existence or content. Mail is searched only in the caller's own Inbox and
Sent folder.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from netbbs.attestation import meets_age
from netbbs.auth.users import User
from netbbs.communities import get_effective_min_age, meets_read_gate
from netbbs.rendering.pipe_codes import strip_pipe_codes
from netbbs.rendering.post_body import indexed_post_body, plain_post_body
from netbbs.rendering.reflow import print_wrapped
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
    from netbbs.mail import MailMessage

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
    separated token is individually double-quoted and implicitly AND-ed
    together -- this treats the query as a literal phrase-per-token
    search, never letting a user's typed text be interpreted as FTS5
    query syntax (AND/OR/NOT/NEAR/column filters/prefix `*`), which
    would otherwise let oddly formatted input raise a syntax error deep
    inside a MATCH clause instead of just searching for it literally.

    A caller's own `"` characters are stripped before tokenizing, not
    individually re-escaped and quoted along with the token they
    surround -- this was investigated as a dogfood report claiming a
    quoted phrase (e.g. `"quokkatown"`) silently failed to match, but
    turned out to be a misdiagnosis: verified directly against FTS5
    that the un-stripped double-double-quoted form
    (`""""quokkatown""""`) already matches identically to the stripped
    form. Kept anyway as a harmless simplification -- cleaner emitted
    syntax, one fewer moving part -- but it fixes no real defect; still
    no phrase/adjacency semantics either way (`"a b"` is the same
    AND-of-tokens as `a b`).

    The report's actual reproducible symptom (`"quokkatown" OR
    aardvark` returning zero matches) turned out to be unrelated to
    quoting at all: the *unquoted* `quokkatown OR aardvark` fails
    identically, because "OR" becomes a third required literal word --
    exactly the "never interpreted as FTS5 syntax" behavior this
    function's docstring already documents as intentional, not a bug.
    The caller-facing UI hint for that specific case lives in
    `netbbs.net.scan_and_find._looks_like_attempted_boolean_syntax`, not
    here -- this function's own contract (never special-case any word)
    is correct and shouldn't grow an exception for "OR"/"AND"/"NOT".
    """
    tokens = query.replace('"', " ").split()
    if not tokens:
        return None
    return " ".join('"' + token + '"' for token in tokens)


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
    `get_effective_min_age`, the same gate `_new_scan_screen` applies),
    and to posts the board page itself would show: a carried post whose
    author trust suppresses is hidden from search as it is from the feed
    (design doc §12.8, issue #677)."""
    from netbbs.boards.boards import list_boards  # deferred -- see module's TYPE_CHECKING note
    from netbbs.link.enforcement import link_content_visible  # deferred, same cycle

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
            meets_read_gate(db, user, board)
            and meets_age(db, user, get_effective_min_age(db, board))
        ):
            continue
        if not link_content_visible(db, row["root_post_id"]):
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
            meets_read_gate(db, user, area)
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
    visibility rules itself and risking the two drifting apart. A carried
    message trust suppresses is skipped, as scrollback skips it (issue
    #677)."""
    from netbbs.link.enforcement import link_content_visible  # deferred -- see module's TYPE_CHECKING note

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
            "SELECT author_label, link_content_id FROM channel_messages WHERE id = ?", (row["message_id"],)
        ).fetchone()
        if message_row is None:
            continue  # trimmed since the search index was last pruned
        if message_row["link_content_id"] is not None and not link_content_visible(
            db, message_row["link_content_id"]
        ):
            continue
        hits.append(
            ChannelMessageSearchHit(
                channel=channel, message_id=row["message_id"], author_label=message_row["author_label"],
                body=row["body"],
            )
        )
        if len(hits) >= limit:
            break
    return hits


# -- the caller's own mail (issue #824) --------------------------------------
#
# `mail_search` holds every `mail_messages` row's subject and plain-text body
# under the letter's own id as its rowid, one entry per row for as long as
# the row exists: `index_mail_without_commit` on every insert (`netbbs.mail`
# and `netbbs.link.mail`), `unindex_mail_without_commit` on every delete
# (`netbbs.mail`). Which side of a letter is still in whose mailbox is not
# indexed -- a letter deleted by one party is still the other's -- and is
# decided at query time from the row itself, so marking a side deleted
# changes nothing here.
#
# A letter is also found by the From/To name the mailbox shows for it. Those
# names are resolved when shown (a Link node can rename; a local recipient's
# name is looked up by id), so they cannot be indexed; they are matched here
# the way FTS5's default `unicode61` tokenizer would match them: every typed
# word as a whole word, ignoring case and accents. A query matches a letter
# when each of its words is in the subject, the body or that name.

_WORD_RE = re.compile(r"[^\W_]+")


def _search_words(text: str) -> str:
    """`text` as a space-separated run of folded words, padded with a space
    at each end so a phrase can be looked up with word boundaries."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return " " + " ".join(_WORD_RE.findall(folded)) + " "


@dataclass(frozen=True)
class MailSearchHit:
    message: MailMessage
    sent: bool
    """Found in the caller's Sent folder, not their Inbox. A letter to
    oneself is in both, and is a hit in each."""
    label: str
    """Who the letter is from (Inbox) or to (Sent), as the mailbox shows it."""


def search_mail(db: Database, user: User, query: str, *, limit: int = 20) -> list[MailSearchHit]:
    """`user`'s own letters matching `query`, newest first: their Inbox and
    their Sent folder, never anyone else's, and never a letter they deleted
    from their side (the other party's copy is theirs). Mail from the BBS
    itself is in the Inbox, so it is searched too. Nothing for a caller
    `netbbs.mail.mail_access_refusal` turns away (issue #816); the screen
    also refuses a session that came in as the guest, which only it can
    tell."""
    from netbbs.mail import (  # deferred -- see module's TYPE_CHECKING note
        MailMessage, mail_access_refusal, recipient_display_label, sender_display_label,
    )

    tokens = [token for token in query.replace('"', " ").split() if _search_words(token).strip()]
    if not tokens or mail_access_refusal(db, user) is not None:
        return []

    # Every side of a letter still in the caller's mailbox, with the name it
    # shows -- never the bodies, which the index answers for.
    sides = db.connection.execute(
        """
        SELECT id, sender_user_id, sender_label, recipient_user_id, recipient_remote_address,
               recipient_label, from_system, recipient_user_id = ? AND recipient_deleted_at IS NULL AS in_inbox,
               sender_user_id = ? AND sender_deleted_at IS NULL AS in_sent
          FROM mail_messages
         WHERE (recipient_user_id = ? AND recipient_deleted_at IS NULL)
            OR (sender_user_id = ? AND sender_deleted_at IS NULL)
         ORDER BY id DESC
        """,
        (user.id, user.id, user.id, user.id),
    ).fetchall()
    if not sides:
        return []

    matched: list[set[int]] = []
    for token in tokens:
        rows = db.connection.execute(
            """
            SELECT m.id FROM mail_search s JOIN mail_messages m ON m.id = s.rowid
             WHERE mail_search MATCH ?
               AND ((m.recipient_user_id = ? AND m.recipient_deleted_at IS NULL)
                 OR (m.sender_user_id = ? AND m.sender_deleted_at IS NULL))
            """,
            (_match_expression(token), user.id, user.id),
        ).fetchall()
        matched.append({row["id"] for row in rows})
    phrases = [_search_words(token) for token in tokens]

    labels: dict[tuple, str] = {}
    found: list[tuple[int, bool, str]] = []
    for row in sides:
        for sent in (False, True):
            if not row["in_sent" if sent else "in_inbox"]:
                continue
            envelope = MailMessage(
                id=row["id"], sender_user_id=row["sender_user_id"], sender_label=row["sender_label"],
                recipient_user_id=row["recipient_user_id"], subject="", body="", created_at="",
                read_at=None, sender_deleted_at=None, recipient_deleted_at=None,
                recipient_remote_address=row["recipient_remote_address"],
                from_system=bool(row["from_system"]), recipient_label=row["recipient_label"],
            )
            if sent:
                key = (True, envelope.recipient_remote_address, envelope.recipient_user_id, envelope.recipient_label)
            else:
                key = (False, envelope.sender_label, envelope.from_system)
            if key not in labels:
                labels[key] = recipient_display_label(db, envelope) if sent else sender_display_label(db, envelope)
            label = labels[key]
            name_words = _search_words(label)
            if all(
                row["id"] in ids or phrase in name_words for ids, phrase in zip(matched, phrases)
            ):
                found.append((row["id"], sent, label))
        if len(found) >= limit:
            break
    from netbbs.mail import get_mail  # deferred, as above

    return [
        MailSearchHit(message=get_mail(db, user, mail_id), sent=sent, label=label)
        for mail_id, sent, label in found[:limit]
    ]


def index_mail_without_commit(db: Database, mail_id: int) -> None:
    """Index letter `mail_id`, just inserted: its subject, and its body as
    plain text -- color codes and escape sequences (issue #809) are neither
    words anyone searches for nor anything a result may print. Called in
    the inserting transaction, before its commit."""
    row = db.connection.execute("SELECT subject, body FROM mail_messages WHERE id = ?", (mail_id,)).fetchone()
    if row is None:
        return
    db.connection.execute("DELETE FROM mail_search WHERE rowid = ?", (mail_id,))
    db.connection.execute(
        "INSERT INTO mail_search (rowid, subject, body) VALUES (?, ?, ?)",
        (mail_id, row["subject"], plain_post_body(row["body"])),
    )


def unindex_mail_without_commit(db: Database, mail_ids) -> None:
    """Forget letters `mail_ids`, being deleted for good: a letter nobody
    has any more leaves no words of it behind. Called in the deleting
    transaction."""
    db.connection.executemany("DELETE FROM mail_search WHERE rowid = ?", [(mail_id,) for mail_id in mail_ids])


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
        SELECT * FROM posts
        WHERE root_post_id = ? AND board_id = ? AND status = 'approved'
        ORDER BY id DESC
        LIMIT 1
        """,
        (root_post_id, board_id),
    ).fetchone()
    if current is not None:
        # Plain text only: color codes and escape sequences (issue #711)
        # are neither searchable words nor anything a result snippet may
        # print.
        layout = current["layout"] if "layout" in current.keys() else "prose"
        current = {"subject": current["subject"], "body": indexed_post_body(current["body"], layout)}
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
# The four FTS tables above are maintained by explicit calls from every
# write path in netbbs.boards.posts/netbbs.files.entries/netbbs.chat.
# scrollback/netbbs.mail/netbbs.link.mail, not SQL triggers or one shared transaction with the
# authoritative write -- a crash, SQLite error, interrupted migration, or
# a future write path that forgets to call the right reindex function can
# leave a table stale with no supported way to detect or repair it. The
# four `_expected_*_index` functions below are the single source of
# truth for "what should currently be indexed," computed straight from
# `posts`/`files`/`channel_messages`/`mail_messages`; `check_index_integrity` compares
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
    `id` order -- local receipt order, as `_resolve_current_version` uses
    since issue #675 -- is its newest), rather than one query per
    root_post_id, since the table can hold many roots."""
    rows = db.connection.execute(
        """
        SELECT * FROM posts WHERE status = 'approved'
        ORDER BY id ASC
        """
    ).fetchall()
    resolved: dict[str, tuple[int, str, str]] = {}
    layouts: dict[str, str] = {}
    for row in rows:
        resolved[row["root_post_id"]] = (row["board_id"], row["subject"], row["body"])
        layouts[row["root_post_id"]] = row["layout"] if "layout" in row.keys() else "prose"
    # What `reindex_post` indexes (issue #711), or every colored post reads
    # as stale (Codex review on #750).
    return {
        root: (board, subject, indexed_post_body(body, layouts.get(root, "prose")))
        for root, (board, subject, body) in resolved.items()
    }


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
    try:
        rows = db.connection.execute(
            f"SELECT id, channel_id, body, external_source FROM channel_messages "
            f"WHERE kind IN ({placeholders}) AND body IS NOT NULL",
            _CHANNEL_CONTENT_KINDS,
        ).fetchall()
    except sqlite3.OperationalError:
        # Migration tests exercise schemas from before `external_source`
        # existed -- the same tolerance `record_message` has.
        rows = db.connection.execute(
            f"SELECT id, channel_id, body, NULL AS external_source FROM channel_messages "
            f"WHERE kind IN ({placeholders}) AND body IS NOT NULL",
            _CHANNEL_CONTENT_KINDS,
        ).fetchall()
    return {row["id"]: (row["channel_id"], _indexed_channel_body(row["body"], row["external_source"])) for row in rows}


def _expected_mail_index(db: Database) -> dict[int, tuple[str, str]]:
    """`mail id -> (subject, plain body)` for every letter still stored,
    whoever's mailbox it is in, matching `index_mail_without_commit`."""
    rows = db.connection.execute("SELECT id, subject, body FROM mail_messages").fetchall()
    return {row["id"]: (row["subject"], plain_post_body(row["body"])) for row in rows}


def _indexed_channel_body(body: str, external_source: str | None) -> str:
    """What the index holds for a channel message: the stored body, or
    for an MRC row (issue #298) the body with its `|NN` color codes
    stripped -- the same normalization `record_message(index_body=...)`
    applies on insert, so an integrity check never reports a colored
    line as drift and a rebuild never puts the codes back."""
    if external_source == "mrc":
        return strip_pipe_codes(body).strip()
    return body


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
    # Issue #824. Ids only, like the others: never a letter's words.
    mail: IndexDrift

    @property
    def is_clean(self) -> bool:
        return self.posts.is_clean and self.files.is_clean and self.channel_messages.is_clean and self.mail.is_clean


def _diff_index(expected: dict, actual: dict) -> IndexDrift:
    expected_keys = set(expected)
    actual_keys = set(actual)
    return IndexDrift(
        missing=tuple(sorted(expected_keys - actual_keys, key=str)),
        extra=tuple(sorted(actual_keys - expected_keys, key=str)),
        stale=tuple(sorted((k for k in expected_keys & actual_keys if expected[k] != actual[k]), key=str)),
    )


def check_index_integrity(db: Database) -> SearchIndexIntegrityReport:
    """Compare all four FTS tables against authoritative data without
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
    mail_actual = {
        row["rowid"]: (row["subject"], row["body"])
        for row in db.connection.execute("SELECT rowid, subject, body FROM mail_search")
    }
    return SearchIndexIntegrityReport(
        posts=_diff_index(_expected_post_index(db), posts_actual),
        files=_diff_index(_expected_file_index(db), files_actual),
        channel_messages=_diff_index(_expected_channel_message_index(db), channel_actual),
        mail=_diff_index(_expected_mail_index(db), mail_actual),
    )


def rebuild_indexes(db: Database) -> SearchIndexIntegrityReport:
    """
    Rebuild all four FTS tables from authoritative data, replacing their
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

    mail_expected = _expected_mail_index(db)
    db.connection.execute("DELETE FROM mail_search")
    db.connection.executemany(
        "INSERT INTO mail_search (rowid, subject, body) VALUES (?, ?, ?)",
        [(mail_id, subject, body) for mail_id, (subject, body) in mail_expected.items()],
    )

    db.connection.commit()
    return before


def _print_report(report: SearchIndexIntegrityReport) -> None:
    if report.is_clean:
        print_wrapped("Search indexes are consistent with authoritative data.")
        return
    for name, drift in (
        ("post_search", report.posts),
        ("file_search", report.files),
        ("channel_message_search", report.channel_messages),
        ("mail_search", report.mail),
    ):
        if drift.is_clean:
            continue
        print_wrapped(
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
                print_wrapped("Search indexes were already consistent; rebuilt anyway.")
            else:
                print_wrapped("Drift found before rebuild:")
                _print_report(before)
            print_wrapped("Rebuild complete.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
