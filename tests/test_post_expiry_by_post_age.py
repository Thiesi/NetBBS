"""Every revision of a post ages from the post's original revision (issue #793).

A carried revision's `created_at` is its author's clock, display metadata that
may run far behind (design doc §7.2). Aged by its own timestamp, an edit from a
node whose clock was weeks behind expired on arrival, and the post fell back to
the revision before it -- for a withdrawal, the very text the author took back.
Aging every revision by its post's first revision means an edit can never
expire before the post it belongs to. Ageing by when this node received content
was considered and rejected: it would keep old history carried late to a newly
subscribing node alive for a full maximum age."""

from __future__ import annotations

import datetime

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board, get_board_by_name, list_boards
from netbbs.boards.posts import (
    WITHDRAWN_PLACEHOLDER,
    _sweep_expired_posts,
    count_listed_posts,
    create_post,
    visible_post,
)
from netbbs.link.boards import materialize_carried_post, materialize_carried_post_edit
from tests.test_link_boards import _carried_board, _remote_edit, _remote_post, db, remote_node_identity  # noqa: F401


def _iso(when: datetime.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


NOW = datetime.datetime.now(datetime.timezone.utc)
#: 60 days ago, past the 30-day limit every board here has.
OLD = _iso(NOW - datetime.timedelta(days=60))


def _carried_board_with_age_limit(db, remote_node_identity):
    board_id = _carried_board(db, remote_node_identity)
    db.connection.execute("UPDATE boards SET max_post_age_days = 30 WHERE board_id = ?", (board_id,))
    db.connection.commit()
    name = db.connection.execute("SELECT name FROM boards WHERE board_id = ?", (board_id,)).fetchone()["name"]
    return board_id, get_board_by_name(db, name)


def _status(db, post_id):
    return db.connection.execute("SELECT status FROM posts WHERE post_id = ?", (post_id,)).fetchone()["status"]


def test_a_withdrawal_from_a_clock_far_behind_does_not_bring_the_text_back(db, remote_node_identity):
    board_id, board = _carried_board_with_age_limit(db, remote_node_identity)
    root = _remote_post(remote_node_identity, board_id=board_id, subject="Plans", body="what I regret",
                        created_at=_iso(NOW))
    materialize_carried_post(db, root, sender_fingerprint=remote_node_identity.fingerprint)

    # The author's node clock runs 60 days behind when they withdraw.
    withdrawal = _remote_edit(remote_node_identity, root, withdrawn=True, created_at=OLD)
    materialize_carried_post_edit(db, withdrawal, sender_fingerprint=remote_node_identity.fingerprint)
    _sweep_expired_posts(db, board)

    assert _status(db, withdrawal.content_id) == "approved", "aged by its own stamp, it expired on arrival"
    shown = visible_post(db, root.content_id)
    assert shown.body == WITHDRAWN_PLACEHOLDER and shown.withdrawn
    # The read-only count and the board ranking judge it as the sweep does.
    assert count_listed_posts(db, board)[0] == 1
    assert [b.name for b in list_boards(db, order_by="volume")] == [board.name]


def test_old_history_carried_late_still_expires_on_schedule(db, remote_node_identity):
    """The case receipt time got wrong: a post written 60 days ago that reaches
    a newly subscribing node today is past a 30-day limit already, and an edit
    made since does not keep it alive."""
    board_id, board = _carried_board_with_age_limit(db, remote_node_identity)
    root = _remote_post(remote_node_identity, board_id=board_id, body="old news", created_at=OLD)
    materialize_carried_post(db, root, sender_fingerprint=remote_node_identity.fingerprint)
    edit = _remote_edit(remote_node_identity, root, body="old news, corrected", created_at=_iso(NOW))
    materialize_carried_post_edit(db, edit, sender_fingerprint=remote_node_identity.fingerprint)

    assert count_listed_posts(db, board)[0] == 0
    _sweep_expired_posts(db, board)

    assert visible_post(db, root.content_id) is None
    # Past the grace period as well, so the unreferenced edit is already gone.
    statuses = db.connection.execute(
        "SELECT status FROM posts WHERE root_post_id = ?", (root.content_id,)
    ).fetchall()
    assert {row["status"] for row in statuses} == {"expired"}


def test_a_local_post_still_expires_by_its_own_age(db, remote_node_identity):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "local", creator=alice, max_post_age_days=30)
    old = create_post(db, board, alice, "Old", "news")
    fresh = create_post(db, board, alice, "Fresh", "news")
    db.connection.execute("UPDATE posts SET created_at = ? WHERE post_id = ?", (OLD, old.post_id))
    db.connection.commit()

    _sweep_expired_posts(db, board)

    assert visible_post(db, old.post_id) is None
    assert visible_post(db, fresh.post_id) is not None
