"""A post's earlier versions, from the reader's [H]istory (issue #675).

Every edit is kept as a revision, but a caller only ever saw an "edited"
badge. Who sees what was decided with the maintainer: a reader sees the
versions back to the most recent moderator edit and nothing of a removed
post; a moderator sees them all.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards import posts as posts_module
from netbbs.boards.boards import create_board
from netbbs.boards.posts import (
    MAX_LISTED_REVISIONS,
    create_post,
    edit_post,
    get_post,
    list_post_revisions,
    tombstone_post,
)
from netbbs.moderation import BoardPermission, grant_permissions
from netbbs.net import board_flow
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.session import Session
from netbbs.storage.database import Database

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def clock(monkeypatch):
    """Distinct, increasing timestamps: two revisions made in one Windows
    clock tick would otherwise share one."""
    ticks = iter(f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}.000000Z" for i in range(3600))
    monkeypatch.setattr(posts_module, "utc_now_iso", lambda: next(ticks))


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def mod(db):
    return create_user(db, "mod", password="hunter2", user_level=10)


@pytest.fixture
def reader(db):
    return create_user(db, "reader", password="hunter2", user_level=10)


@pytest.fixture
def board(db, alice, mod):
    made = create_board(db, "general", creator=alice)
    grant_permissions(
        db, mod, object_type="board", object_id=made.id,
        permissions=BoardPermission.EDIT | BoardPermission.DELETE, granted_by=alice,
    )
    return made


def _edit(db, board, post, body, by):
    return edit_post(db, get_post(db, post.post_id), board, subject=post.subject, body=body, edited_by=by)


def _bodies(revisions):
    return [revision.post.body for revision in revisions]


# -- who sees which versions ---------------------------------------------------------


def test_a_reader_sees_every_version_of_an_authors_edits(db, clock, board, alice, reader):
    post = create_post(db, board, alice, "Plans", "v1")
    _edit(db, board, post, "v2", alice)
    _edit(db, board, post, "v3", alice)
    revisions = list_post_revisions(db, post, board, requesting_user=reader)
    assert _bodies(revisions) == ["v1", "v2", "v3"]
    assert not any(revision.by_moderator for revision in revisions)


def test_a_reader_sees_nothing_before_a_moderator_edit(db, clock, board, alice, mod, reader):
    post = create_post(db, board, alice, "Plans", "something abusive")
    _edit(db, board, post, "[edited by a moderator]", mod)
    _edit(db, board, post, "the author's fix", alice)

    as_reader = list_post_revisions(db, post, board, requesting_user=reader)
    assert _bodies(as_reader) == ["[edited by a moderator]", "the author's fix"]
    assert [revision.by_moderator for revision in as_reader] == [True, False]

    as_moderator = list_post_revisions(db, post, board, requesting_user=mod)
    assert _bodies(as_moderator) == ["something abusive", "[edited by a moderator]", "the author's fix"]


def test_a_removed_post_shows_its_versions_to_a_moderator_only(db, clock, board, alice, mod, reader):
    post = create_post(db, board, alice, "Plans", "v1")
    _edit(db, board, post, "v2", alice)
    tombstone_post(db, get_post(db, post.post_id), board, tombstoned_by=mod)
    assert list_post_revisions(db, post, board, requesting_user=reader) == []
    # The placeholder is not a version of what was written.
    assert _bodies(list_post_revisions(db, post, board, requesting_user=mod)) == ["v1", "v2"]


def test_a_carried_moderator_edit_is_marked_as_one(db, clock, board, alice, reader):
    """An origin's moderator edit arrives as its own event type."""
    post = create_post(db, board, alice, "Plans", "v1")
    edited = _edit(db, board, post, "v2", alice)
    db.connection.execute(
        "INSERT INTO link_events (content_id, sender_fingerprint, object_type, envelope_json, received_at) "
        "VALUES (?, 'origin', 'board_post_moderator_edit', '{}', '2026-01-01T00:00:00Z')",
        (edited.post_id,),
    )
    db.connection.commit()
    revisions = list_post_revisions(db, post, board, requesting_user=reader)
    assert _bodies(revisions) == ["v2"]
    assert revisions[0].by_moderator


def test_a_long_history_lists_the_newest_versions(db, clock, board, alice, reader):
    post = create_post(db, board, alice, "Plans", "v0")
    for number in range(1, MAX_LISTED_REVISIONS + 10):
        _edit(db, board, post, f"v{number}", alice)
    revisions = list_post_revisions(db, post, board, requesting_user=reader)
    assert len(revisions) == MAX_LISTED_REVISIONS
    assert revisions[-1].post.body == f"v{MAX_LISTED_REVISIONS + 9}"


# -- the reader ------------------------------------------------------------------------


class FakeSession(Session):
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
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_line)")
        return self._inputs.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_key)")
        return self._inputs.pop(0)

    async def read_editor_key(self, **kwargs) -> EditorKey:
        if not self._inputs:
            raise AssertionError("ran out of scripted input (read_editor_key)")
        return EditorKey(EditorKeyKind.CHAR, char=self._inputs.pop(0))

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    def visible(self) -> str:
        return _SGR.sub("", "".join(self.written))


def test_history_opens_an_earlier_version_from_the_reader(db, clock, board, alice, reader):
    post = create_post(db, board, alice, "Plans", "the first draft")
    _edit(db, board, post, "the final text", alice)
    # Open the post, [H]istory, pick #02 (the original: newest is #01),
    # [B]ack from it, [B]ack out of the versions, the reader, the list.
    session = FakeSession(["1", "h", "0", "2", "b", "b", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, reader))
    text = session.visible()
    assert "[H]istory" in text
    assert "Versions of Plans" in text
    assert "the first draft" in text
    assert "original" in text and "current" in text


def test_an_unedited_post_offers_no_history(db, clock, board, alice, reader):
    create_post(db, board, alice, "Plans", "v1")
    session = FakeSession(["1", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, reader))
    assert "[H]istory" not in session.visible()


def test_history_cut_to_one_version_says_so(db, clock, board, alice, mod, reader):
    """A post a moderator edited last has nothing earlier for a reader."""
    post = create_post(db, board, alice, "Plans", "something abusive")
    _edit(db, board, post, "[edited by a moderator]", mod)
    session = FakeSession(["1", "h", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, reader))
    text = session.visible()
    assert "There are no earlier versions of this post to show." in text
    assert "something abusive" not in text
