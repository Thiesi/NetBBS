"""Pinned posts and files, and keeping them from expiring (issue #675).

Pinning used to have no effect anywhere: `list_pinned_posts` had no
caller, the toggles were reachable only on the pending-post screen, and
both flags lived on one revision, so an exempt post's edit expired and
the post fell back to its pre-edit text.
"""

from __future__ import annotations

import asyncio
import datetime
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import (
    create_post,
    edit_post,
    get_post,
    list_pinned_posts,
    list_posts_page,
    set_post_exempt,
    set_post_pinned,
)
from netbbs.files import entries as entries_module
from netbbs.files.areas import create_file_area
from netbbs.files.entries import list_files_page, set_file_pinned, upload_file
from netbbs.moderation import BoardPermission, grant_permissions
from netbbs.net import board_flow
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.file_flow import _show_area
from netbbs.net.session import Session
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def mod(db):
    return create_user(db, "mod", password="hunter2", user_level=10)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def _board(db, owner, **kwargs):
    board = create_board(db, "general", creator=owner, **kwargs)
    grant_permissions(
        db, owner, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=owner
    )
    return board


def _posts(db, board, author, count, monkeypatch):
    stamps = iter(f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}.000000Z" for i in range(count))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(stamps))
    made = [create_post(db, board, author, f"Subject {i}", f"Body {i}") for i in range(count)]
    monkeypatch.undo()
    return made


def _age(db, table: str, row_id: int, days: int) -> None:
    stamp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    db.connection.execute(f"UPDATE {table} SET created_at = ? WHERE id = ?", (stamp, row_id))
    db.connection.commit()


# -- the board page ------------------------------------------------------------


def _older_pages(db, board, user, page, limit):
    seen = []
    while page.has_older:
        page = list_posts_page(db, board, user, limit=limit, before=page.oldest_cursor, with_pinned=True)
        assert page.pinned_count == 0
        seen += [p.subject for p in page.posts]
    return seen


def test_a_pinned_post_is_listed_first_on_the_page_a_board_opens_on(db, mod, monkeypatch):
    board = _board(db, mod)
    made = _posts(db, board, mod, 12, monkeypatch)
    set_post_pinned(db, made[0], True, changed_by=mod)

    newest = list_posts_page(db, board, mod, limit=5, with_pinned=True)
    assert newest.pinned_count == 1
    assert [p.subject for p in newest.posts] == ["Subject 0", "Subject 8", "Subject 9", "Subject 10", "Subject 11"]
    # The cursors are the feed's, not the pinned post's.
    assert newest.oldest_cursor == (made[8].created_at, made[8].post_id)
    # It stays in the dated feed too, where it was posted.
    seen = _older_pages(db, board, mod, newest, 5)
    assert sorted(seen, key=lambda s: int(s.split()[1])) == [f"Subject {i}" for i in range(0, 8)]


def test_the_newest_page_does_not_list_a_shown_pin_twice(db, mod, monkeypatch):
    board = _board(db, mod)
    made = _posts(db, board, mod, 4, monkeypatch)
    set_post_pinned(db, made[3], True, changed_by=mod)
    page = list_posts_page(db, board, mod, limit=5, with_pinned=True)
    assert [p.subject for p in page.posts] == ["Subject 3", "Subject 0", "Subject 1", "Subject 2"]
    assert not page.has_newer and not page.has_older


def test_a_page_reached_by_a_cursor_has_no_pinned_block(db, mod, monkeypatch):
    """A [N]ew scan or [F]ind jump opens on its target, not on old pins
    (Codex review on #783)."""
    board = _board(db, mod)
    made = _posts(db, board, mod, 6, monkeypatch)
    set_post_pinned(db, made[0], True, changed_by=mod)
    jumped = list_posts_page(
        db, board, mod, limit=5, after=(made[3].created_at, made[3].post_id), with_pinned=True
    )
    assert jumped.pinned_count == 0
    assert [p.subject for p in jumped.posts] == ["Subject 4", "Subject 5"]
    caught_up = list_posts_page(
        db, board, mod, limit=5, after=(made[5].created_at, made[5].post_id), with_pinned=True
    )
    assert caught_up.posts == []


def test_pins_past_the_page_share_are_still_reached_by_paging(db, mod, monkeypatch):
    """More pins than half the page: the rest are not lost (Codex review
    on #783) -- they are in the dated feed where they were posted."""
    board = _board(db, mod)
    made = _posts(db, board, mod, 10, monkeypatch)
    for post in made[:6]:
        set_post_pinned(db, post, True, changed_by=mod)

    page = list_posts_page(db, board, mod, limit=6, with_pinned=True)
    assert page.pinned_count == 3
    assert [p.subject for p in page.posts] == [
        "Subject 0", "Subject 1", "Subject 2", "Subject 7", "Subject 8", "Subject 9",
    ]
    seen = _older_pages(db, board, mod, page, 6)
    for pin in ("Subject 3", "Subject 4", "Subject 5"):
        assert pin in seen


def test_a_board_of_only_pinned_posts_still_lists_them(db, mod):
    board = _board(db, mod)
    post = create_post(db, board, mod, "Rules", "Be nice.")
    set_post_pinned(db, post, True, changed_by=mod)
    page = list_posts_page(db, board, mod, limit=5, with_pinned=True)
    assert [p.subject for p in page.posts] == ["Rules"]
    assert page.pinned_count == 1
    assert not page.has_older and not page.has_newer


def test_without_with_pinned_the_feed_is_unchanged(db, mod, monkeypatch):
    board = _board(db, mod)
    made = _posts(db, board, mod, 3, monkeypatch)
    set_post_pinned(db, made[0], True, changed_by=mod)
    page = list_posts_page(db, board, mod, limit=5)
    assert [p.subject for p in page.posts] == ["Subject 0", "Subject 1", "Subject 2"]
    assert page.pinned_count == 0


# -- the flags belong to the post ------------------------------------------------


def test_an_edit_keeps_the_pin_and_the_exemption(db, mod):
    board = _board(db, mod)
    post = create_post(db, board, mod, "Rules", "v1")
    set_post_pinned(db, post, True, changed_by=mod)
    set_post_exempt(db, post, True, changed_by=mod)
    edited = edit_post(db, get_post(db, post.post_id), board, subject="Rules", body="v2", edited_by=mod)
    assert edited.pinned and edited.exempt_from_expiry

    set_post_pinned(db, edited, False, changed_by=mod)
    rows = db.connection.execute(
        "SELECT pinned FROM posts WHERE root_post_id = ?", (post.root_post_id,)
    ).fetchall()
    assert [r["pinned"] for r in rows] == [0, 0]


def test_an_exempt_post_keeps_its_edited_text_when_the_edit_would_expire(db, mod):
    """The bug in the issue: the edit's own row was not exempt, expired,
    and the post showed its pre-edit text."""
    board = _board(db, mod, max_post_age_days=30)
    post = create_post(db, board, mod, "Rules", "old text")
    set_post_exempt(db, post, True, changed_by=mod)
    edited = edit_post(db, get_post(db, post.post_id), board, subject="Rules", body="new text", edited_by=mod)
    _age(db, "posts", post.id, 60)
    _age(db, "posts", edited.id, 45)

    page = list_posts_page(db, board, mod, with_pinned=True)
    assert [p.body for p in page.posts] == ["new text"]


def test_removing_a_post_clears_its_pin_and_keep(db, mod):
    """A removed post's placeholder must neither stay at the top nor
    outlive expiry (Codex review on #783)."""
    from netbbs.boards.posts import tombstone_post

    board = _board(db, mod)
    grant_permissions(
        db, mod, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=mod
    )
    post = create_post(db, board, mod, "Rules", "v1")
    set_post_pinned(db, post, True, changed_by=mod)
    set_post_exempt(db, post, True, changed_by=mod)
    tombstone_post(db, get_post(db, post.post_id), board, tombstoned_by=mod)
    rows = db.connection.execute(
        "SELECT pinned, exempt_from_expiry FROM posts WHERE root_post_id = ?", (post.post_id,)
    ).fetchall()
    assert all((r["pinned"], r["exempt_from_expiry"]) == (0, 0) for r in rows)
    assert list_pinned_posts(db, board, requesting_user=mod) == []


def test_list_pinned_posts_skips_one_with_no_approved_revision(db, mod):
    board = _board(db, mod, moderated=True)
    grant_permissions(
        db, mod, object_type="board", object_id=board.id, permissions=BoardPermission.APPROVE, granted_by=mod
    )
    pending = create_post(db, board, mod, "Pending", "not yet")
    set_post_pinned(db, pending, True, changed_by=mod)
    assert list_pinned_posts(db, board, requesting_user=mod) == []


def test_the_migration_moves_revision_flags_onto_the_root(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS
    from tests.legacy_schema import insert_user_on_old_schema

    index = next(i for i, m in enumerate(MIGRATIONS) if "Issue #675" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    path = tmp_path / "node.db"
    old = Database(path)
    mod = insert_user_on_old_schema(old, "mod", user_level=10)
    board = create_board(old, "general", creator=mod)
    post = create_post(old, board, mod, "Rules", "v1")
    edited = edit_post(old, post, board, subject="Rules", body="v2", edited_by=mod)
    # What the pending-post screen used to do: set the flag on one revision.
    old.connection.execute("UPDATE posts SET exempt_from_expiry = 1 WHERE id = ?", (edited.id,))
    old.connection.execute("UPDATE posts SET pinned = 1 WHERE id = ?", (post.id,))
    old.connection.commit()
    old.close()
    monkeypatch.undo()

    upgraded = Database(path)
    try:
        rows = upgraded.connection.execute(
            "SELECT pinned, exempt_from_expiry FROM posts WHERE root_post_id = ? ORDER BY id", (post.post_id,)
        ).fetchall()
        assert [(r["pinned"], r["exempt_from_expiry"]) for r in rows] == [(1, 1), (1, 1)]
        # And a revision made afterwards takes the root's flags (the trigger).
        third = edit_post(upgraded, get_post(upgraded, post.post_id), board, subject="Rules", body="v3", edited_by=mod)
        assert third.pinned and third.exempt_from_expiry
    finally:
        upgraded.close()


# -- files ------------------------------------------------------------------------


def test_a_pinned_file_is_listed_first_on_the_newest_page(db, mod, monkeypatch):
    area = create_file_area(db, "downloads", creator=mod)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=mod
    )
    stamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(8))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(stamps))
    files = [upload_file(db, area, mod, f"f{i}.txt", f"payload {i}".encode()) for i in range(8)]
    monkeypatch.undo()
    set_file_pinned(db, files[0], True, changed_by=mod)

    page = list_files_page(db, area, mod, limit=4, with_pinned=True)
    assert page.pinned_count == 1
    assert [e.filename for e in page.entries] == ["f0.txt", "f5.txt", "f6.txt", "f7.txt"]
    seen = []
    while page.has_older:
        page = list_files_page(db, area, mod, limit=4, before=page.oldest_cursor, with_pinned=True)
        assert page.pinned_count == 0
        seen += [e.filename for e in page.entries]
    assert sorted(seen) == [f"f{i}.txt" for i in range(5)]
    # A jump lands on its target, with no pinned block.
    assert list_files_page(db, area, mod, limit=4, after=(files[1].created_at, files[1].file_id), with_pinned=True).pinned_count == 0


def test_a_file_flag_lands_on_the_file_named_not_a_reused_row(db, mod):
    """`files.id` is a rowid a later upload may reuse; the setters act on
    the content-addressed `file_id` (Codex review on #783)."""
    import dataclasses

    area = create_file_area(db, "downloads", creator=mod)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=mod
    )
    first = upload_file(db, area, mod, "a.txt", b"a")
    second = upload_file(db, area, mod, "b.txt", b"b")
    stale = dataclasses.replace(first, id=second.id)
    set_file_pinned(db, stale, True, changed_by=mod)
    pinned = {row["filename"]: row["pinned"] for row in db.connection.execute("SELECT filename, pinned FROM files")}
    assert pinned == {"a.txt": 1, "b.txt": 0}


# -- the screens ------------------------------------------------------------------


class BoardSession(Session):
    def __init__(self, inputs, *, width=80, height=24):
        self._inputs = list(inputs)
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "NetBBS"
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        return self._inputs.pop(0)

    async def read_editor_key(self, **kwargs) -> EditorKey:
        if not self._inputs:
            raise AssertionError("ran out of scripted input")
        return EditorKey(EditorKeyKind.CHAR, char=self._inputs.pop(0))

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    def visible(self) -> str:
        return _SGR.sub("", "".join(self.written))


def test_a_moderator_pins_from_the_reader_and_the_list_says_so(db, mod, monkeypatch):
    board = _board(db, mod)
    _posts(db, board, mod, 3, monkeypatch)
    # Open post 1, pin it, back to the list, leave.
    session = BoardSession(["1", "i", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, mod))
    text = session.visible()
    assert "P[i]n" in text
    assert "Post pinned: it is listed at the top of this board." in text
    assert list_pinned_posts(db, board, requesting_user=mod)[0].subject == "Subject 0"
    assert "pin" in text.rsplit("Choice:", 2)[-2]  # the list drawn after the pin marks it


def test_keep_is_offered_only_where_posts_expire(db, mod, monkeypatch):
    board = _board(db, mod)
    _posts(db, board, mod, 1, monkeypatch)
    session = BoardSession(["1", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, mod))
    assert "[K]eep" not in session.visible()

    expiring = create_board(db, "news", creator=mod, max_post_age_days=30)
    grant_permissions(
        db, mod, object_type="board", object_id=expiring.id, permissions=BoardPermission.EDIT, granted_by=mod
    )
    create_post(db, expiring, mod, "Headline", "Body")
    session = BoardSession(["1", "k", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, expiring, mod))
    assert "[K]eep" in session.visible()
    assert "Post kept: it will not expire." in session.visible()


def test_a_caller_without_edit_permission_cannot_pin(db, mod, alice, monkeypatch):
    board = _board(db, mod)
    _posts(db, board, mod, 1, monkeypatch)
    session = BoardSession(["1", "i", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    assert "P[i]n" not in session.visible()
    assert list_pinned_posts(db, board, requesting_user=alice) == []


class FileSession:
    def __init__(self, keys, width=80, height=24):
        self._keys = iter(keys)
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("ran out of scripted keys")
        return key

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return ""

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("ran out of scripted keys")
        if key == "DOWN":
            return EditorKey(EditorKeyKind.DOWN)
        return EditorKey(EditorKeyKind.CHAR, char=key)

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    async def read_byte(self):
        raise NotImplementedError

    def visible(self) -> str:
        return _SGR.sub("", "".join(self.written))


def test_keep_only_undoes_old_exemptions_where_files_no_longer_expire(tmp_path, monkeypatch):
    """With expiry off, [K]eep offers the kept files only, so it cannot
    make a new exemption (Codex review on #783)."""
    path = tmp_path / "node.db"
    db = Database(path)
    mod = create_user(db, "mod", password="hunter2", user_level=10)
    area = create_file_area(db, "downloads", creator=mod)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=mod
    )
    stamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(2))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(stamps))
    kept = upload_file(db, area, mod, "kept.txt", b"k")
    upload_file(db, area, mod, "plain.txt", b"p")
    monkeypatch.undo()
    db.connection.execute("UPDATE files SET exempt_from_expiry = 1 WHERE id = ?", (kept.id,))
    db.connection.commit()
    lane = DatabaseLane(path)
    try:
        # Highlight plain.txt (the second row), then [K]eep.
        session = FileSession(["DOWN", "DOWN", "k", "b"])
        asyncio.run(_show_area(session, lane, area, mod))
    finally:
        lane.close()
    flags = {r["filename"]: r["exempt_from_expiry"] for r in db.connection.execute("SELECT filename, exempt_from_expiry FROM files")}
    assert flags == {"kept.txt": 0, "plain.txt": 0}
    db.close()


def test_a_moderator_pins_the_highlighted_file(tmp_path, monkeypatch):
    path = tmp_path / "node.db"
    db = Database(path)
    mod = create_user(db, "mod", password="hunter2", user_level=10)
    area = create_file_area(db, "downloads", creator=mod)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=mod
    )
    stamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(3))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(stamps))
    for i in range(3):
        upload_file(db, area, mod, f"f{i}.txt", f"payload {i}".encode())
    monkeypatch.undo()
    lane = DatabaseLane(path)
    try:
        session = FileSession(["DOWN", "i", "b"])
        asyncio.run(_show_area(session, lane, area, mod))
    finally:
        lane.close()
    text = session.visible()
    assert "P[i]n" in text
    assert "f0.txt pinned: it is listed first in this area." in text
    assert "pin f0.txt" in text
    assert list_files_page(db, area, mod, with_pinned=True).entries[0].filename == "f0.txt"
    db.close()


# -- review round two (Codex on #783) ------------------------------------------------


def test_a_removed_post_refuses_a_pin_or_keep(db, mod):
    from netbbs.boards.posts import PostError, tombstone_post

    board = _board(db, mod)
    grant_permissions(
        db, mod, object_type="board", object_id=board.id, permissions=BoardPermission.DELETE, granted_by=mod
    )
    post = create_post(db, board, mod, "Rules", "v1")
    stale = get_post(db, post.post_id)  # a reader left open
    tombstone_post(db, stale, board, tombstoned_by=mod)
    with pytest.raises(PostError, match="removed"):
        set_post_pinned(db, stale, True, changed_by=mod)
    with pytest.raises(PostError, match="removed"):
        set_post_exempt(db, stale, True, changed_by=mod)


def test_unpinning_an_old_post_returns_to_the_list(db, mod, monkeypatch):
    """Its dated place is on an older page; the reader must not go on to
    show some other post as if it were this one."""
    board = _board(db, mod)
    made = _posts(db, board, mod, 12, monkeypatch)
    set_post_pinned(db, made[0], True, changed_by=mod)
    # Open the pinned row, unpin it, and one [B]ack leaves the board: the
    # unpin already went back to the list.
    session = BoardSession(["1", "i", "b"])
    asyncio.run(board_flow._show_board(session, db, board, mod))
    assert "Post unpinned" in session.visible()
    assert list_pinned_posts(db, board, requesting_user=mod) == []


def test_keep_on_an_older_file_page_stays_on_that_page(tmp_path, monkeypatch):
    path = tmp_path / "node.db"
    db = Database(path)
    mod = create_user(db, "mod", password="hunter2", user_level=10)
    area = create_file_area(db, "downloads", creator=mod, max_file_age_days=30)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=mod
    )
    stamps = iter(f"2026-01-01T00:00:{i:02d}.000000Z" for i in range(8))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(stamps))
    for i in range(8):
        upload_file(db, area, mod, f"f{i}.txt", f"payload {i}".encode())
    monkeypatch.undo()
    # Keep the files fresh for the expiry sweep.
    db.connection.execute("UPDATE files SET created_at = strftime('%Y-%m-%dT%H:%M:%f000Z', 'now', '-' || (10 - id) || ' minutes')")
    db.connection.commit()
    lane = DatabaseLane(path)
    try:
        # Older page (f0-f2), highlight its first row, keep it, leave.
        session = FileSession(["o", "DOWN", "k", "b"])
        asyncio.run(_show_area(session, lane, area, mod))
    finally:
        lane.close()
    # The redraw after the keystroke, up to the outcome written above its
    # prompt, is the older page again.
    redraw = session.visible().rsplit("k\n", 1)[1].split("f0.txt kept", 1)[0]
    assert "f0.txt" in redraw and "f7.txt" not in redraw
    db.close()


def test_a_hidden_area_refuses_a_file_pin(db, mod):
    from netbbs.files.entries import FileEntryError

    area = create_file_area(db, "downloads", creator=mod)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=mod
    )
    entry = upload_file(db, area, mod, "a.txt", b"a")
    db.connection.execute("UPDATE file_areas SET link_hidden_at = '2026-01-01T00:00:00.000000Z' WHERE id = ?", (area.id,))
    db.connection.commit()
    with pytest.raises(FileEntryError, match="no longer available"):
        set_file_pinned(db, entry, True, changed_by=mod)


# -- review round three (Codex on #783) ----------------------------------------------


def test_the_pending_post_screen_reports_a_refused_pin(db, mod):
    """Approved and removed by someone else while this moderator sat on
    the pending screen: the pin is refused and the screen goes on."""
    from netbbs.boards.posts import approve_post, tombstone_post
    from netbbs.net.admin_flow import _post_action_screen

    board = _board(db, mod, moderated=True)
    grant_permissions(
        db, mod, object_type="board", object_id=board.id,
        permissions=BoardPermission.APPROVE | BoardPermission.DELETE, granted_by=mod,
    )
    stale = create_post(db, board, mod, "Pending", "text")
    approve_post(db, stale, approved_by=mod)
    tombstone_post(db, get_post(db, stale.post_id), board, tombstoned_by=mod)
    lane = DatabaseLane(db.path)
    try:
        session = BoardSession(["p", "b"])
        asyncio.run(_post_action_screen(session, lane, mod, stale, board))
    finally:
        lane.close()
    assert "this post has been removed" in session.visible()


def test_the_pending_file_screen_reports_a_refused_pin(db, mod):
    from netbbs.net.admin_flow import _file_action_screen

    area = create_file_area(db, "downloads", creator=mod, moderated=True)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id,
        permissions=BoardPermission.EDIT | BoardPermission.APPROVE, granted_by=mod,
    )
    entry = upload_file(db, area, mod, "a.txt", b"a")
    db.connection.execute("UPDATE file_areas SET link_hidden_at = '2026-01-01T00:00:00.000000Z' WHERE id = ?", (area.id,))
    db.connection.commit()
    lane = DatabaseLane(db.path)
    try:
        session = FileSession(["p", "b"])
        asyncio.run(_file_action_screen(session, lane, mod, entry, area))
    finally:
        lane.close()
    assert "no longer available" in session.visible()


def test_keep_on_a_file_deleted_meanwhile_is_refused_not_raised(tmp_path, monkeypatch):
    path = tmp_path / "node.db"
    db = Database(path)
    mod = create_user(db, "mod", password="hunter2", user_level=10)
    area = create_file_area(db, "downloads", creator=mod, max_file_age_days=30)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=mod
    )
    upload_file(db, area, mod, "gone.txt", b"g")

    class DeletingSession(FileSession):
        async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
            key = await super().read_editor_key(distinguish_ctrl_h=distinguish_ctrl_h)
            if key.char == "k":
                db.connection.execute("DELETE FROM files")
                db.connection.commit()
            return key

    lane = DatabaseLane(path)
    try:
        session = DeletingSession(["k", "b"])
        asyncio.run(_show_area(session, lane, area, mod))
    finally:
        lane.close()
    assert "Not changed:" in session.visible()
    db.close()
