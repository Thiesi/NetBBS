"""A post's earlier versions, and an author withdrawing a post (issue #675).

Every edit is kept as a revision, but a caller only ever saw an "edited"
badge. Decided with the maintainer: [H]istory is for moderators only (the
board's edit permission), and it shows every version. An author withdraws a
post with an ordinary author edit to "[withdrawn by author]", which is not
final and which a moderator still sees behind in the history.
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
    MAX_LISTED_REVISIONS,
    WITHDRAWN_PLACEHOLDER,
    PostError,
    create_post,
    edit_post,
    get_post,
    list_post_revisions,
    tombstone_post,
    visible_post,
    withdraw_post,
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


class Clock:
    """Stamps for `posts.utc_now_iso`: increasing by default, settable to
    any value to model another node's clock."""

    def __init__(self):
        self.tick = 0
        self.fixed: str | None = None

    def __call__(self) -> str:
        if self.fixed is not None:
            return self.fixed
        self.tick += 1
        return f"2026-01-01T00:{self.tick // 60:02d}:{self.tick % 60:02d}.000000Z"


@pytest.fixture
def clock(monkeypatch):
    stamps = Clock()
    monkeypatch.setattr(posts_module, "utc_now_iso", stamps)
    return stamps


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def mod(db):
    return create_user(db, "mod", password="hunter2", user_level=10)


@pytest.fixture
def reader(db):
    return create_user(db, "reader", password="hunter2", user_level=10)


def _board(db, alice, mod, **kwargs):
    made = create_board(db, "general", creator=alice, **kwargs)
    grant_permissions(
        db, mod, object_type="board", object_id=made.id,
        permissions=BoardPermission.EDIT | BoardPermission.DELETE | BoardPermission.APPROVE, granted_by=alice,
    )
    return made


@pytest.fixture
def board(db, alice, mod):
    return _board(db, alice, mod)


def _edit(db, board, post, body, by):
    return edit_post(db, get_post(db, post.post_id), board, subject=post.subject, body=body, edited_by=by)


def _bodies(revisions):
    return [revision.post.body for revision in revisions]


# -- who sees history ---------------------------------------------------------------


def test_a_moderator_sees_every_version(db, clock, board, alice, mod):
    post = create_post(db, board, alice, "Plans", "something abusive")
    _edit(db, board, post, "[edited by a moderator]", mod)
    _edit(db, board, post, "the author's fix", alice)
    revisions = list_post_revisions(db, post, board, requesting_user=mod)
    assert _bodies(revisions) == ["something abusive", "[edited by a moderator]", "the author's fix"]
    assert [revision.by_moderator for revision in revisions] == [False, True, False]


def test_history_is_for_moderators_only(db, clock, board, alice, reader):
    post = create_post(db, board, alice, "Plans", "v1")
    _edit(db, board, post, "v2", alice)
    with pytest.raises(PostError):
        list_post_revisions(db, post, board, requesting_user=reader)
    # Not even the author, without the board's edit permission.
    with pytest.raises(PostError):
        list_post_revisions(db, post, board, requesting_user=alice)


def test_a_removed_post_shows_what_it_said_to_a_moderator(db, clock, board, alice, mod):
    post = create_post(db, board, alice, "Plans", "v1")
    _edit(db, board, post, "v2", alice)
    tombstone_post(db, get_post(db, post.post_id), board, tombstoned_by=mod)
    # The placeholder is not a version of what was written.
    assert _bodies(list_post_revisions(db, post, board, requesting_user=mod)) == ["v1", "v2"]


def test_versions_follow_the_chain_not_the_clock(db, clock, board, alice, mod):
    """A carried revision's time is its author's clock and may run
    backwards; the chain's own links decide the order (Codex review on
    #789)."""
    post = create_post(db, board, alice, "Plans", "v1")
    _edit(db, board, post, "v2", alice)
    clock.fixed = "2025-06-01T00:00:00.000000Z"  # a clock behind the others
    _edit(db, board, post, "v3", mod)
    assert _bodies(list_post_revisions(db, post, board, requesting_user=mod)) == ["v1", "v2", "v3"]


def test_expired_versions_are_left_out_without_a_listing_first(db, clock, alice, mod):
    board = _board(db, alice, mod, max_post_age_days=30)
    clock.fixed = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    post = create_post(db, board, alice, "Plans", "old")
    clock.fixed = None
    clock.tick = 0
    clock.fixed = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _edit(db, board, post, "fresh", alice)
    assert _bodies(list_post_revisions(db, post, board, requesting_user=mod)) == ["fresh"]


def test_a_carried_moderator_edit_is_marked_as_one(db, clock, board, alice, mod):
    post = create_post(db, board, alice, "Plans", "v1")
    edited = _edit(db, board, post, "v2", alice)
    db.connection.execute(
        "INSERT INTO link_events (content_id, sender_fingerprint, object_type, envelope_json, received_at) "
        "VALUES (?, 'origin', 'board_post_moderator_edit', '{}', '2026-01-01T00:00:00Z')",
        (edited.post_id,),
    )
    db.connection.commit()
    assert [r.by_moderator for r in list_post_revisions(db, post, board, requesting_user=mod)] == [False, True]


def test_a_long_history_lists_the_newest_versions(db, clock, board, alice, mod):
    post = create_post(db, board, alice, "Plans", "v0")
    for number in range(1, MAX_LISTED_REVISIONS + 10):
        _edit(db, board, post, f"v{number}", alice)
    revisions = list_post_revisions(db, post, board, requesting_user=mod)
    assert len(revisions) == MAX_LISTED_REVISIONS
    assert revisions[-1].post.body == f"v{MAX_LISTED_REVISIONS + 9}"


# -- withdrawal -------------------------------------------------------------------------


def test_the_author_withdraws_a_post_with_an_ordinary_edit(db, clock, board, alice, mod):
    post = create_post(db, board, alice, "Plans", "what I regret")
    withdrawn = withdraw_post(db, post, board, withdrawn_by=alice)
    assert withdrawn.body == WITHDRAWN_PLACEHOLDER
    assert withdrawn.subject == "Plans"
    assert withdrawn.tombstoned_at is None
    shown = visible_post(db, post.post_id)
    assert shown.body == WITHDRAWN_PLACEHOLDER
    # Not final: the author can edit it again.
    _edit(db, board, post, "on second thought", alice)
    assert visible_post(db, post.post_id).body == "on second thought"
    # And a moderator still sees what was withdrawn.
    assert "what I regret" in _bodies(list_post_revisions(db, post, board, requesting_user=mod))


def test_only_the_author_can_withdraw(db, clock, board, alice, mod):
    post = create_post(db, board, alice, "Plans", "v1")
    with pytest.raises(PostError, match="author"):
        withdraw_post(db, post, board, withdrawn_by=mod)


def test_a_withdrawal_is_not_held_for_approval(db, clock, alice, mod):
    """Held, the post would show what its author withdrew until a moderator
    got to it."""
    from netbbs.boards.posts import approve_post

    board = _board(db, alice, mod, moderated=True)
    post = create_post(db, board, alice, "Plans", "v1")
    approve_post(db, post, approved_by=mod)
    withdraw_post(db, get_post(db, post.post_id), board, withdrawn_by=alice)
    assert visible_post(db, post.post_id).body == WITHDRAWN_PLACEHOLDER


def test_a_withdrawal_clears_the_pin_and_the_keep(db, clock, board, alice, mod):
    """A withdrawn post neither stays at the top of the board nor outlives
    its expiry. (The Link side is in tests/test_link_boards.py.)"""
    from netbbs.boards.posts import set_post_exempt, set_post_pinned

    post = create_post(db, board, alice, "Plans", "v1")
    set_post_pinned(db, post, True, changed_by=mod)
    set_post_exempt(db, post, True, changed_by=mod)
    withdraw_post(db, get_post(db, post.post_id), board, withdrawn_by=alice)
    shown = visible_post(db, post.post_id)
    assert shown.withdrawn and not shown.pinned and not shown.exempt_from_expiry
    with pytest.raises(PostError, match="already withdrawn"):
        withdraw_post(db, shown, board, withdrawn_by=alice)


# -- the reader -------------------------------------------------------------------------


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


def test_a_moderator_opens_an_earlier_version_from_the_reader(db, clock, board, alice, mod):
    post = create_post(db, board, alice, "Plans", "the first draft")
    _edit(db, board, post, "the final text", alice)
    # Open the post, [H]istory, pick #02 (the original: newest is #01),
    # [B]ack from it, [B]ack out of the versions, the reader, the list.
    session = FakeSession(["1", "h", "0", "2", "b", "b", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, mod))
    text = session.visible()
    assert "[H]istory" in text
    assert "Versions of Plans" in text
    assert "the first draft" in text
    assert "original" in text and "current" in text


def test_readers_and_authors_get_no_history(db, clock, board, alice, reader):
    post = create_post(db, board, alice, "Plans", "v1")
    _edit(db, board, post, "v2", alice)
    for caller in (reader, alice):
        session = FakeSession(["1", "b", "b"])
        asyncio.run(board_flow._show_board(session, db, board, caller))
        assert "[H]istory" not in session.visible()


def test_a_moderator_sees_a_removed_never_edited_post(db, clock, board, alice, mod):
    """Its one version is what the moderator opens History for (claude
    review on #789)."""
    post = create_post(db, board, alice, "Plans", "what was removed")
    tombstone_post(db, get_post(db, post.post_id), board, tombstoned_by=mod)
    session = FakeSession(["1", "h", "0", "1", "b", "b", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, mod))
    text = session.visible()
    assert "no earlier versions" not in text
    assert "what was removed" in text
    assert "current" not in text.split("Versions of")[1]


def test_the_author_withdraws_from_the_reader(db, clock, board, alice, reader):
    create_post(db, board, alice, "Plans", "what I regret")
    # Open, [W]ithdraw, confirm, back to the list, leave.
    session = FakeSession(["1", "w", "y", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    text = session.visible()
    assert "Post withdrawn." in text
    assert WITHDRAWN_PLACEHOLDER in text
    # Nobody else is offered it.
    session = FakeSession(["1", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, reader))
    assert "[W]ithdraw" not in session.visible()


# -- review (Codex on #789) -------------------------------------------------------------


def test_text_that_already_reads_as_the_placeholder_is_still_withdrawn(db, clock, board, alice):
    post = create_post(db, board, alice, "Plans", "v1")
    _edit(db, board, post, WITHDRAWN_PLACEHOLDER, alice)  # typed by hand: not a withdrawal
    assert not visible_post(db, post.post_id).withdrawn
    withdraw_post(db, visible_post(db, post.post_id), board, withdrawn_by=alice)
    assert visible_post(db, post.post_id).withdrawn


def test_a_withdrawn_post_cannot_be_pinned_or_kept_again(db, clock, board, alice, mod):
    from netbbs.boards.posts import set_post_exempt, set_post_pinned

    post = create_post(db, board, alice, "Plans", "v1")
    withdraw_post(db, post, board, withdrawn_by=alice)
    shown = visible_post(db, post.post_id)
    with pytest.raises(PostError, match="withdrawn"):
        set_post_pinned(db, shown, True, changed_by=mod)
    with pytest.raises(PostError, match="withdrawn"):
        set_post_exempt(db, shown, True, changed_by=mod)
    set_post_pinned(db, shown, False, changed_by=mod)  # unpinning stays allowed


def test_a_withdrawal_the_link_cannot_carry_says_so(db, clock, alice, mod):
    """A post written before its board was Linked has no chain to extend;
    the author is told other nodes keep the old text (Codex review on
    #789)."""
    from netbbs.link.boards import link_board
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.net.notices import take_notices

    board = create_board(db, "linked", creator=alice)
    post = create_post(db, board, alice, "Plans", "v1")  # before linking
    link_board(db, board, node_identity=bootstrap_node_identity("here"))
    session = FakeSession(["y"])
    assert asyncio.run(board_flow._withdraw_existing_post(session, db, board, post, alice, link_context=None))
    notices = _SGR.sub("", "".join(take_notices(session)))
    assert "could not be sent to other nodes" in notices
