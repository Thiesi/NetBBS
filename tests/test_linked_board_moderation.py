"""Moderation and trust on Linked boards (issue #677, from the #674 audit).

Each test pins one way a carried or Linked post used to escape the
moderation or trust decision that was supposed to govern it:

- approving a held carried post re-signed it as this node's own post;
- approving a pending edit signed a duplicate *new* post instead of the edit;
- the carrying node's own "Moderated" flag never held a remote post;
- a remote edit published a post nobody here had approved;
- a remote edit brought a locally tombstoned post back;
- an author trust holds for approval had their *edits* published unreviewed;
- trust-hidden posts emptied pages, and still showed in counts and search;
- a closed board still offered [P]ost;
- a carrying node's moderator was not told their change stays local.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from netbbs.activity import board_read_cursor, ensure_board_baseline, record_post_opened, unread_post_count
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board, get_board_by_name
from netbbs.boards.posts import (
    approve_post,
    count_visible_posts,
    create_post,
    edit_post,
    get_post,
    list_posts_page,
    tombstone_post,
)
from netbbs.link.boards import (
    LinkContext,
    link_board,
    materialize_carried_board,
    materialize_carried_board_post_moderator_edit,
    materialize_carried_post,
    materialize_carried_post_edit,
    queue_board_post_if_linked,
)
from netbbs.link.events import (
    build_board_genesis,
    build_board_post,
    build_board_post_edit,
    build_board_post_moderator_edit,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.protocol import LinkNode
from netbbs.link.transport import persist_accepted_events
from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, register_subject, set_trust_override
from netbbs.moderation.roles import BoardPermission, grant_permissions
from netbbs.net import board_flow
from netbbs.search import search_posts
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

NOW = "2026-08-14T12:00:00+00:00"
BOARD_ID = "remote-board-id"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def node_identity():
    return bootstrap_node_identity("roanoke")


@pytest.fixture
def remote():
    return bootstrap_node_identity("elsewhere")


def _carried_board(db, remote, *, moderated=False):
    genesis = build_board_genesis(
        signing_identity=remote.signing_key,
        origin_fingerprint=remote.fingerprint,
        board_id=BOARD_ID,
        name="Remote Discussion",
        created_at="2026-01-01T00:00:00Z",
    )
    materialize_carried_board(db, genesis)
    if moderated:
        db.connection.execute("UPDATE boards SET moderated = 1 WHERE board_id = ?", (BOARD_ID,))
        db.connection.commit()
    return get_board_by_name(db, "Remote Discussion")


def _remote_post(remote, *, user="wanderer", subject="hello", body="first post", minute=0):
    return build_board_post(
        signing_identity=remote.signing_key,
        home_node_fingerprint=remote.fingerprint,
        local_user_id=user,
        board_id=BOARD_ID,
        subject=subject,
        body=body,
        created_at=f"2026-01-01T{minute // 60:02d}:{minute % 60:02d}:00Z",
    )


def _carry(db, remote, **kwargs):
    initial_status = kwargs.pop("initial_status", "approved")
    return materialize_carried_post(
        db, _remote_post(remote, **kwargs), sender_fingerprint=remote.fingerprint, initial_status=initial_status
    )


def _remote_edit(remote, root, *, previous, body="edited body", user="wanderer"):
    return build_board_post_edit(
        signing_identity=remote.signing_key,
        author={"home_node_fingerprint": remote.fingerprint, "local_user_id": user},
        board_id=BOARD_ID,
        root_post_id=root.post_id,
        previous_event_id=previous,
        subject="hello",
        body=body,
        created_at="2026-01-02T00:00:00Z",
    )


def _set_trust(db, remote, user, state):
    subject = TrustSubject.user(remote.fingerprint, user)
    register_subject(db, subject, first_accepted_at=NOW, now_iso=NOW)
    set_trust_override(db, subject, TrustDimension.CONTENT_CONDUCT, state, reason="test", now_iso=NOW)


def _quarantine(db, remote, user):
    _set_trust(db, remote, user, TrustState.QUARANTINED)


def _link_events_count(db):
    return db.connection.execute("SELECT COUNT(*) FROM link_events").fetchone()[0]


# -- approving out of the queue --------------------------------------------


def test_approving_a_held_carried_post_does_not_sign_it_as_this_nodes_own(db, sysop, remote, node_identity):
    from netbbs.link.boards import is_carried_post, queue_approved_board_post_if_linked

    board = _carried_board(db, remote)
    held = _carry(db, remote, initial_status="pending")
    assert is_carried_post(db, held)
    events_before = _link_events_count(db)

    approved = approve_post(db, held, approved_by=sysop)
    queue_approved_board_post_if_linked(db, approved, board, node_identity=node_identity)

    assert queue_board_post_if_linked(db, approved, board, node_identity=node_identity) is None
    row = db.connection.execute("SELECT link_event_json FROM posts WHERE post_id = ?", (held.post_id,)).fetchone()
    assert row["link_event_json"] is None
    assert _link_events_count(db) == events_before


def _moderated_origin_board(db, owner, node_identity):
    board = create_board(db, "general", creator=owner, moderated=True)
    link_board(db, board, node_identity=node_identity)
    return board


def _queued_object_type(db, post_id):
    row = db.connection.execute("SELECT link_event_json FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    if row["link_event_json"] is None:
        return None
    return json.loads(row["link_event_json"])["envelope"]["object_type"]


def test_approving_an_authors_pending_edit_queues_the_edit_not_a_new_post(db, sysop, alice, node_identity):
    from netbbs.link.boards import queue_approved_board_post_if_linked

    board = _moderated_origin_board(db, sysop, node_identity)
    root = approve_post(db, create_post(db, board, alice, "Subject", "Body"), approved_by=sysop)
    queue_approved_board_post_if_linked(db, root, board, node_identity=node_identity)
    assert _queued_object_type(db, root.post_id) == "board_post"

    pending_edit = edit_post(db, root, board, subject="Subject", body="Body, revised", edited_by=alice)
    assert pending_edit.status == "pending"
    approved_edit = approve_post(db, pending_edit, approved_by=sysop)
    queue_approved_board_post_if_linked(db, approved_edit, board, node_identity=node_identity)

    assert _queued_object_type(db, approved_edit.post_id) == "board_post_edit"


def test_approving_a_moderators_pending_edit_queues_a_moderator_edit(db, sysop, alice, node_identity):
    from netbbs.link.boards import queue_approved_board_post_if_linked

    board = _moderated_origin_board(db, sysop, node_identity)
    moderator = create_user(db, "mod", password="hunter2", user_level=10)
    grant_permissions(
        db, moderator, object_type="board", object_id=board.id, permissions=BoardPermission.EDIT, granted_by=sysop
    )
    root = approve_post(db, create_post(db, board, alice, "Subject", "Body"), approved_by=sysop)
    queue_approved_board_post_if_linked(db, root, board, node_identity=node_identity)

    pending_edit = edit_post(db, root, board, subject="Subject", body="[edited by a moderator]", edited_by=moderator)
    approved_edit = approve_post(db, pending_edit, approved_by=sysop)
    queue_approved_board_post_if_linked(db, approved_edit, board, node_identity=node_identity)

    assert _queued_object_type(db, approved_edit.post_id) == "board_post_moderator_edit"


# -- what a received post or revision is stored as ---------------------------


def test_a_moderated_carrying_node_holds_remote_posts_for_review(db, remote, alice):
    board = _carried_board(db, remote, moderated=True)

    post = _carry(db, remote)

    assert post.status == "pending"
    assert list_posts_page(db, board, alice).posts == []


def test_an_unmoderated_carrying_node_publishes_remote_posts(db, remote, alice):
    board = _carried_board(db, remote)
    assert _carry(db, remote).status == "approved"
    assert len(list_posts_page(db, board, alice).posts) == 1


def test_a_remote_edit_does_not_publish_a_post_still_awaiting_approval(db, remote, alice):
    board = _carried_board(db, remote)
    held = _carry(db, remote, initial_status="pending")

    edit = materialize_carried_post_edit(
        db, _remote_edit(remote, held, previous=held.post_id), sender_fingerprint=remote.fingerprint
    )

    assert edit.status == "pending"
    assert list_posts_page(db, board, alice).posts == []


def test_a_remote_edit_on_a_moderated_carrying_node_awaits_approval(db, remote, alice, sysop):
    board = _carried_board(db, remote)
    root = _carry(db, remote)
    db.connection.execute("UPDATE boards SET moderated = 1 WHERE id = ?", (board.id,))
    db.connection.commit()

    edit = materialize_carried_post_edit(
        db, _remote_edit(remote, root, previous=root.post_id), sender_fingerprint=remote.fingerprint
    )

    assert edit.status == "pending"
    assert list_posts_page(db, board, alice).posts[0].body == "first post"


def test_a_remote_edit_does_not_undo_a_local_tombstone(db, remote, alice, sysop):
    board = _carried_board(db, remote)
    root = _carry(db, remote)
    tombstone_post(db, root, board, tombstoned_by=sysop)

    edit = _remote_edit(remote, root, previous=root.post_id, body="it is back")
    result = materialize_carried_post_edit(db, edit, sender_fingerprint=remote.fingerprint)

    assert result is None
    shown = list_posts_page(db, board, alice).posts[0]
    assert shown.subject == "[removed by moderator]"
    # The signed event itself is retained for relay; only the projection is refused.
    assert db.connection.execute(
        "SELECT 1 FROM link_events WHERE content_id = ?", (edit.content_id,)
    ).fetchone() is not None


def test_an_origin_moderator_edit_does_not_undo_a_local_tombstone(db, remote, alice, sysop):
    board = _carried_board(db, remote)
    root = _carry(db, remote)
    tombstone_post(db, root, board, tombstoned_by=sysop)
    moderator_edit = build_board_post_moderator_edit(
        signing_identity=remote.signing_key,
        board_id=BOARD_ID,
        root_post_id=root.post_id,
        previous_event_id=root.post_id,
        subject="hello",
        body="restored by the origin",
        created_at="2026-01-03T00:00:00Z",
    )

    result = materialize_carried_board_post_moderator_edit(
        db, moderator_edit, sender_fingerprint=remote.fingerprint
    )

    assert result is None
    assert list_posts_page(db, board, alice).posts[0].subject == "[removed by moderator]"


def test_an_edit_by_an_author_trust_holds_for_approval_is_held_too(tmp_path, remote):
    """Transport level: `persist_accepted_events` asked trust about new
    posts but never about edits, so a probationary author's approved post
    could be rewritten with unreviewed text."""
    db = Database(tmp_path / "node.db")
    try:
        board = _carried_board(db, remote)
        root = _carry(db, remote)
        # An established home node, so the decision reaches the author,
        # who is on probation: allowed, but held for approval.
        home = TrustSubject.node(remote.fingerprint)
        register_subject(db, home, first_accepted_at=NOW, now_iso=NOW)
        for dimension in TrustDimension:
            set_trust_override(db, home, dimension, TrustState.ESTABLISHED, reason="test", now_iso=NOW)
        _set_trust(db, remote, "wanderer", TrustState.PROBATIONARY)
        edit = _remote_edit(remote, root, previous=root.post_id, body="unreviewed text")
        node = LinkNode(identity=bootstrap_node_identity("roanoke"))
        node.events[edit.content_id] = edit.to_dict()
        lane = DatabaseLane(db.path)
        try:
            asyncio.run(
                persist_accepted_events(
                    lane, node, [edit.content_id], sender_fingerprint=remote.fingerprint,
                    max_carried_boards=None, enforce_trust_policy=True,
                )
            )
        finally:
            lane.close()
        assert get_post(db, edit.content_id).status == "pending"
        reader = create_user(db, "reader", password="hunter2", user_level=10)
        assert list_posts_page(db, board, reader).posts[0].body == "first post"
    finally:
        db.close()


# -- trust-hidden posts: pages, counts, search -------------------------------


def test_hidden_newest_posts_do_not_empty_the_page(db, remote, alice):
    board = _carried_board(db, remote)
    for minute in range(3):
        _carry(db, remote, subject=f"visible {minute}", minute=minute)
    for minute in range(10, 16):
        _carry(db, remote, user="troll", subject=f"hidden {minute}", minute=minute)
    _quarantine(db, remote, "troll")

    page = list_posts_page(db, board, alice)

    assert [p.subject for p in page.posts] == ["visible 0", "visible 1", "visible 2"]
    assert page.has_older is False and page.has_newer is False


def test_paging_skips_a_run_of_hidden_posts_longer_than_one_batch(db, remote, alice):
    board = _carried_board(db, remote)
    for minute in range(6):
        _carry(db, remote, subject=f"visible {minute}", minute=minute)
    for minute in range(100, 170):  # more hidden roots than one query batch
        _carry(db, remote, user="troll", subject=f"hidden {minute}", minute=minute)
    _carry(db, remote, subject="visible newest", minute=500)
    _quarantine(db, remote, "troll")

    newest = list_posts_page(db, board, alice)
    assert [p.subject for p in newest.posts] == ["visible 2", "visible 3", "visible 4", "visible 5", "visible newest"]
    assert newest.has_older is True and newest.has_newer is False

    oldest = newest.posts[0]
    older = list_posts_page(db, board, alice, before=(oldest.created_at, oldest.post_id))
    assert [p.subject for p in older.posts] == ["visible 0", "visible 1"]
    assert older.has_older is False and older.has_newer is True


def test_hidden_posts_are_not_counted(db, remote, alice):
    board = _carried_board(db, remote)
    _carry(db, remote, subject="visible", minute=0)
    _carry(db, remote, user="troll", subject="hidden", minute=1)
    _quarantine(db, remote, "troll")

    assert count_visible_posts(db, board)[0] == 1


def test_hidden_posts_are_not_reported_as_unread(db, remote, alice):
    board = _carried_board(db, remote)
    first = _carry(db, remote, subject="seen", minute=0)
    record_post_opened(db, alice, board, first)
    _carry(db, remote, user="troll", subject="hidden", minute=1)
    _carry(db, remote, subject="new and visible", minute=2)
    _quarantine(db, remote, "troll")

    assert unread_post_count(db, alice, board) == 1


def test_a_hidden_post_is_neither_unread_nor_a_gap_in_what_was_opened(db, remote, alice):
    """Issue #710: the floor folds over opened posts in an unbroken run of
    what a reader may see; a trust-hidden post between them is not a gap."""
    board = _carried_board(db, remote)
    ensure_board_baseline(db, alice, board)
    first = _carry(db, remote, subject="first", minute=0)
    _carry(db, remote, user="troll", subject="hidden", minute=1)
    third = _carry(db, remote, subject="third", minute=2)
    _quarantine(db, remote, "troll")

    record_post_opened(db, alice, board, third)
    record_post_opened(db, alice, board, first)

    assert unread_post_count(db, alice, board) == 0
    assert db.connection.execute(
        "SELECT COUNT(*) FROM user_board_opened_posts WHERE user_id = ?", (alice.id,)
    ).fetchone()[0] == 0


def test_a_late_carried_post_is_unread_until_opened_and_leaves_the_jump_alone(db, remote, alice):
    """A carried post arriving out of order -- an old authored date, a new
    arrival id -- is unread until opened, and opening it does not pull the
    jump position back into history already read."""
    board = _carried_board(db, remote)
    ensure_board_baseline(db, alice, board)
    on_time = _carry(db, remote, subject="on time", minute=500)
    record_post_opened(db, alice, board, on_time)
    late = _carry(db, remote, subject="late", minute=1)
    assert late.id > on_time.id and late.created_at < on_time.created_at

    assert unread_post_count(db, alice, board) == 1
    record_post_opened(db, alice, board, late)

    assert unread_post_count(db, alice, board) == 0
    assert board_read_cursor(db, alice, board) == (on_time.created_at, on_time.post_id)


def test_hidden_posts_do_not_appear_in_search(db, remote, alice):
    _carried_board(db, remote)
    _carry(db, remote, subject="shared keyword visible", minute=0)
    _carry(db, remote, user="troll", subject="shared keyword hidden", minute=1)
    _quarantine(db, remote, "troll")

    hits = search_posts(db, alice, "keyword")

    assert [hit.subject for hit in hits] == ["shared keyword visible"]


# -- caller-facing: closed boards and local-only moderation ------------------


class _Session:
    """Scripted input; one queue for keys and lines."""

    def __init__(self, inputs):
        self._inputs = list(inputs)
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None

    async def write(self, text):
        self.written.append(text)

    async def write_line(self, text=""):
        self.written.append(text + "\r\n")

    async def read_key(self, **kwargs):
        if not self._inputs:
            raise AssertionError("ran out of scripted input")
        return self._inputs.pop(0)

    async def read_line(self, **kwargs):
        return await self.read_key()

    def visible(self):
        """Printed text without SGR codes, whitespace runs (prompt wrapping) collapsed."""
        return re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", "".join(self.written)))


def test_a_closed_board_offers_no_post_action_and_says_why(db, remote, alice):
    board = _carried_board(db, remote)
    _carry(db, remote)
    db.connection.execute("UPDATE boards SET link_closed_at = ? WHERE id = ?", (NOW, board.id))
    db.connection.commit()

    session = _Session(["p", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))

    text = session.visible()
    assert "This message board is closed. It can be read, but it takes no new posts." in text
    assert "[P]ost" not in text
    assert "Subject: " not in text  # "p" was refused, not taken as [P]ost


def test_removing_a_carried_post_on_a_non_origin_node_says_it_stays_local(db, remote, sysop):
    board = _carried_board(db, remote)
    _carry(db, remote, subject="Carried subject")

    session = _Session(["1", "t", "y", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, sysop))

    text = session.visible()
    assert 'Remove "Carried subject" on this node only?' in text
    assert "cannot be undone" not in text
    assert "Post removed on this node." in text
    assert "Other nodes carrying this board keep the original" in text


def test_removing_a_post_on_the_origin_node_is_not_called_local(db, sysop, alice, node_identity):
    board = create_board(db, "general", creator=sysop)
    link_board(db, board, node_identity=node_identity)
    create_post(db, board, alice, "Local subject", "Body")
    link_context = LinkContext(link_node=LinkNode(identity=node_identity))

    session = _Session(["1", "t", "y", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, sysop, link_context=link_context))

    text = session.visible()
    assert 'Remove "Local subject"? This cannot be undone.' in text
    assert "Post removed." in text
    assert "keep the original" not in text


# -- Codex review round 1 ----------------------------------------------------


def _queued_envelope(db, post_id):
    row = db.connection.execute("SELECT link_event_json FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    return None if row["link_event_json"] is None else json.loads(row["link_event_json"])["envelope"]


def _two_pending_edits(db, sysop, alice, node_identity):
    from netbbs.link.boards import queue_approved_board_post_if_linked

    board = _moderated_origin_board(db, sysop, node_identity)
    root = approve_post(db, create_post(db, board, alice, "Subject", "Body"), approved_by=sysop)
    queue_approved_board_post_if_linked(db, root, board, node_identity=node_identity)
    first = edit_post(db, root, board, subject="Subject", body="first revision", edited_by=alice)
    db.connection.execute("UPDATE posts SET created_at = ? WHERE post_id = ?", ("2099-01-01T00:00:01Z", first.post_id))
    second = edit_post(db, root, board, subject="Subject", body="second revision", edited_by=alice)
    db.connection.execute("UPDATE posts SET created_at = ? WHERE post_id = ?", ("2099-01-01T00:00:02Z", second.post_id))
    db.connection.commit()
    return board, get_post(db, first.post_id), get_post(db, second.post_id)


def test_the_second_of_two_pending_edits_is_signed_against_the_first(db, sysop, alice, node_identity):
    """Both pending edits were written against the same head. Once the
    first is approved and sent, the network's head is the first edit; the
    second must name it, or every peer refuses the event."""
    from netbbs.link.boards import queue_approved_board_post_if_linked

    board, first, second = _two_pending_edits(db, sysop, alice, node_identity)
    for pending in (first, second):
        queue_approved_board_post_if_linked(
            db, approve_post(db, pending, approved_by=sysop), board, node_identity=node_identity
        )

    first_event = _queued_envelope(db, first.post_id)
    second_event = _queued_envelope(db, second.post_id)
    from netbbs.link.events import event_content_id

    assert second_event["payload"]["previous_event_id"] == event_content_id(first_event)


def test_an_edit_approved_after_a_newer_one_is_not_sent(db, sysop, alice, node_identity):
    """Approving in the other order: this node shows the newer text, so
    sending the older edit afterwards would leave peers on older text."""
    from netbbs.link.boards import queue_approved_board_post_if_linked

    board, first, second = _two_pending_edits(db, sysop, alice, node_identity)
    for pending in (second, first):
        queue_approved_board_post_if_linked(
            db, approve_post(db, pending, approved_by=sysop), board, node_identity=node_identity
        )

    assert _queued_envelope(db, second.post_id) is not None
    assert _queued_envelope(db, first.post_id) is None
    assert list_posts_page(db, board, alice).posts[0].body == "second revision"


def test_an_expired_local_tombstone_still_refuses_a_later_remote_edit(db, remote, alice, sysop):
    """The expiry sweep ages a tombstone revision like any other; it is no
    less a removal once expired."""
    board = _carried_board(db, remote)
    root = _carry(db, remote)
    tombstone = tombstone_post(db, root, board, tombstoned_by=sysop)
    db.connection.execute("UPDATE posts SET status = 'expired' WHERE post_id = ?", (tombstone.post_id,))
    db.connection.commit()

    edit = _remote_edit(remote, root, previous=root.post_id, body="it is back")

    assert materialize_carried_post_edit(db, edit, sender_fingerprint=remote.fingerprint) is None


def test_counting_a_board_asks_trust_once_per_author(db, remote, alice, monkeypatch):
    """A board with a long carried history must not cost one trust lookup
    per post on every [N]ew scan or admin view."""
    from netbbs.link import enforcement

    board = _carried_board(db, remote)
    for minute in range(20):
        _carry(db, remote, user="wanderer" if minute % 2 else "rover", subject=f"post {minute}", minute=minute)
    calls = []
    real = enforcement.content_visible_for_subject
    monkeypatch.setattr(
        enforcement, "content_visible_for_subject",
        lambda db_, subject: calls.append(subject) or real(db_, subject),
    )

    assert count_visible_posts(db, board)[0] == 20
    assert len(calls) == 2


# -- Codex review round 2 ----------------------------------------------------


def test_every_remote_edit_after_a_local_tombstone_is_retained(db, remote, alice, sysop):
    """The first edit after a local tombstone is retained but not
    projected. The second names it as its predecessor, which is therefore
    not in `posts`; it must be retained all the same, or the node loses it
    from durable storage and cannot relay it after a restart."""
    board = _carried_board(db, remote)
    root = _carry(db, remote)
    tombstone_post(db, root, board, tombstoned_by=sysop)
    first = _remote_edit(remote, root, previous=root.post_id, body="first edit")
    second = build_board_post_edit(
        signing_identity=remote.signing_key,
        author={"home_node_fingerprint": remote.fingerprint, "local_user_id": "wanderer"},
        board_id=BOARD_ID,
        root_post_id=root.post_id,
        previous_event_id=first.content_id,
        subject="hello",
        body="second edit",
        created_at="2026-01-03T00:00:00Z",
    )

    for edit in (first, second):
        assert materialize_carried_post_edit(db, edit, sender_fingerprint=remote.fingerprint) is None

    retained = {
        row[0] for row in db.connection.execute(
            "SELECT content_id FROM link_events WHERE content_id IN (?, ?)", (first.content_id, second.content_id)
        )
    }
    assert retained == {first.content_id, second.content_id}
    assert list_posts_page(db, board, alice).posts[0].subject == "[removed by moderator]"


# -- Codex review round 3 ----------------------------------------------------


def _remote_post_on_our_board(db, board, remote, *, subject="remote subject"):
    post = build_board_post(
        signing_identity=remote.signing_key,
        home_node_fingerprint=remote.fingerprint,
        local_user_id="wanderer",
        board_id=board.board_id,
        subject=subject,
        body="remote body",
        created_at="2026-01-01T00:00:00Z",
    )
    return materialize_carried_post(db, post, sender_fingerprint=remote.fingerprint)


def test_the_origin_signs_its_moderator_edit_of_a_remote_authors_post(db, sysop, remote, node_identity):
    """Carried rows keep their event only in `link_events`; the chain
    lookup read only `link_event_json`, so an origin could never send a
    moderator edit of a post a remote author wrote."""
    from netbbs.link.boards import queue_board_post_moderator_edit_if_linked

    board = create_board(db, "general", creator=sysop)
    link_board(db, board, node_identity=node_identity)
    carried = _remote_post_on_our_board(db, board, remote)

    edited = edit_post(db, carried, board, subject=carried.subject, body="[moderated]", edited_by=sysop)
    event = queue_board_post_moderator_edit_if_linked(db, edited, board, node_identity=node_identity, edited_by=sysop)

    assert event is not None
    assert event.payload["root_post_id"] == carried.post_id
    assert event.payload["previous_event_id"] == carried.post_id


def test_the_origin_signs_its_tombstone_of_a_remote_authors_post(db, sysop, remote, node_identity):
    from netbbs.link.boards import queue_board_post_tombstone_if_linked

    board = create_board(db, "general", creator=sysop)
    link_board(db, board, node_identity=node_identity)
    carried = _remote_post_on_our_board(db, board, remote)

    tombstoned = tombstone_post(db, carried, board, tombstoned_by=sysop)
    event = queue_board_post_tombstone_if_linked(db, tombstoned, board, node_identity=node_identity)

    assert event is not None
    assert event.payload["previous_event_id"] == carried.post_id


def test_replies_to_you_asks_trust_once_per_author(db, remote, alice, monkeypatch):
    from netbbs.activity import unread_replies_to
    from netbbs.link import enforcement

    board = _carried_board(db, remote)
    mine = create_post(db, board, alice, "question", "anyone?")
    for minute in range(10):
        reply = build_board_post(
            signing_identity=remote.signing_key,
            home_node_fingerprint=remote.fingerprint,
            local_user_id="wanderer" if minute % 2 else "rover",
            board_id=BOARD_ID,
            subject=f"re {minute}",
            body="answer",
            created_at=f"2026-01-01T00:{minute:02d}:00Z",
            parent_post_id=mine.post_id,
        )
        materialize_carried_post(db, reply, sender_fingerprint=remote.fingerprint)
    calls = []
    real = enforcement.content_visible_for_subject
    monkeypatch.setattr(
        enforcement, "content_visible_for_subject",
        lambda db_, subject: calls.append(subject) or real(db_, subject),
    )

    assert len(unread_replies_to(db, alice)) == 10
    assert len(calls) == 2



# -- issue #692: a rejection survives [R]epair carried posts --------------------------


def _rebuild(db):
    from netbbs.link.boards import rebuild_carried_post_materialization

    return rebuild_carried_post_materialization(db)


def _post_row(db, post_id):
    return db.connection.execute("SELECT status FROM posts WHERE post_id = ?", (post_id,)).fetchone()


def test_a_rejected_carried_post_stays_gone_after_repair(db, sysop, remote):
    from netbbs.boards.posts import delete_post

    _carried_board(db, remote, moderated=True)
    held = _carry(db, remote, subject="refused")
    assert held.status == "pending"

    delete_post(db, held, deleted_by=sysop, reason="off topic")
    _rebuild(db)

    assert _post_row(db, held.post_id) is None
    row = db.connection.execute("SELECT * FROM post_rejections WHERE post_id = ?", (held.post_id,)).fetchone()
    assert row["rejected_by_user_id"] == sysop.id and row["reason"] == "off topic"
    # The signed event is kept (design doc §9.3).
    assert db.connection.execute(
        "SELECT 1 FROM link_events WHERE content_id = ?", (held.post_id,)
    ).fetchone() is not None


def test_a_rejected_carried_edit_stays_gone_after_repair(db, sysop, remote):
    from netbbs.boards.posts import delete_post

    _carried_board(db, remote, moderated=True)
    root = _carry(db, remote)
    root = approve_post(db, root, approved_by=sysop)
    edit = materialize_carried_post_edit(
        db, _remote_edit(remote, root, previous=root.post_id, body="refused text"),
        sender_fingerprint=remote.fingerprint,
    )
    assert edit.status == "pending"

    delete_post(db, edit, deleted_by=sysop)
    _rebuild(db)

    assert _post_row(db, edit.post_id) is None
    assert get_post(db, root.post_id).body == "first post"


def test_a_rejected_carried_post_is_not_projected_by_any_path(db, sysop, remote):
    from netbbs.boards.posts import delete_post

    _carried_board(db, remote, moderated=True)
    event = _remote_post(remote, subject="refused")
    held = materialize_carried_post(db, event, sender_fingerprint=remote.fingerprint)
    delete_post(db, held, deleted_by=sysop)

    assert materialize_carried_post(db, event, sender_fingerprint=remote.fingerprint) is None


def test_repair_holds_a_post_whose_author_trust_holds_for_approval(db, remote):
    """A post repair does bring back gets the status sync would give it."""
    _carried_board(db, remote)
    missing = _carry(db, remote, subject="restored")
    db.connection.execute("DELETE FROM posts WHERE post_id = ?", (missing.post_id,))
    db.connection.commit()
    home = TrustSubject.node(remote.fingerprint)
    register_subject(db, home, first_accepted_at=NOW, now_iso=NOW)
    for dimension in TrustDimension:
        set_trust_override(db, home, dimension, TrustState.ESTABLISHED, reason="test", now_iso=NOW)
    _set_trust(db, remote, "wanderer", TrustState.PROBATIONARY)

    assert _rebuild(db) == 1
    assert _post_row(db, missing.post_id)["status"] == "pending"


def test_repair_still_restores_a_post_that_was_never_rejected(db, remote):
    _carried_board(db, remote)
    missing = _carry(db, remote, subject="restored")
    db.connection.execute("DELETE FROM posts WHERE post_id = ?", (missing.post_id,))
    db.connection.commit()

    assert _rebuild(db) == 1
    assert _post_row(db, missing.post_id)["status"] == "approved"


def test_rejecting_a_local_post_records_it_too(db, sysop, alice):
    from netbbs.boards.posts import delete_post

    board = create_board(db, "general", creator=sysop, moderated=True)
    held = create_post(db, board, alice, "refused", "text")

    delete_post(db, held, deleted_by=sysop)

    assert db.connection.execute(
        "SELECT board_id FROM post_rejections WHERE post_id = ?", (held.post_id,)
    ).fetchone()[0] == board.id



def test_the_migration_keeps_rejections_made_before_it(tmp_path, monkeypatch, remote):
    """The moderation log already names every rejected post; a repair right
    after upgrading must not republish them (Codex review on #780)."""
    from netbbs.boards.posts import delete_post
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "post_rejections" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    # Today's materialization asks the table this schema does not have yet.
    import netbbs.link.boards as link_boards

    monkeypatch.setattr(link_boards, "_rejected_here", lambda db, content_id: False)
    path = tmp_path / "node.db"
    old = Database(path)
    sysop = create_user(old, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    _carried_board(old, remote, moderated=True)
    held = _carry(old, remote, subject="refused")
    # What delete_post did before #692: log, delete, nothing else.
    old.connection.execute("DELETE FROM posts WHERE post_id = ?", (held.post_id,))
    old.connection.execute(
        "INSERT INTO moderation_log (actor_user_id, action, object_type, object_id, detail, created_at) "
        "VALUES (?, 'reject', 'board', ?, ?, ?)",
        (sysop.id, held.board_id, held.post_id, NOW),
    )
    old.connection.commit()
    old.close()
    monkeypatch.undo()

    db = Database(path)
    try:
        _rebuild(db)
        assert _post_row(db, held.post_id) is None
    finally:
        db.close()
