"""
Deleting a carried Link resource hides it; Restore un-hides it; Purge deletes
it for real (issue #683, decided 2026-09-26). Against a real SQLite database.
"""

from __future__ import annotations

import pytest

from netbbs.activity import unread_replies_to
from netbbs.auth.users import create_user
from netbbs.boards.boards import BoardError, create_board, get_board_by_name, list_boards
from netbbs.boards.posts import create_post, tombstone_post
from netbbs.chat.channels import ChannelError, get_channel_by_name, list_channels
from netbbs.files.areas import FileAreaError, get_file_area_by_area_id, get_file_area_by_name, list_file_areas
from netbbs.link.boards import link_board, materialize_carried_post
from netbbs.link.carry import (
    EXCLUDED,
    CarryDecisionError,
    accept_genesis,
    carried_from_elsewhere,
    carry_decision_counts,
    hide_carried_resource,
    list_carry_decisions,
    purge_excluded,
    restore_excluded,
)
from netbbs.link.channels import get_channel_by_channel_id
from netbbs.link.events import (
    build_board_genesis,
    build_board_post,
    build_channel_genesis,
    build_file_area_genesis,
)
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.link.store import carried_board_ids, uncarried_resource_ids
from netbbs.moderation.log import list_recent_actions
from netbbs.search import search_posts
from netbbs.storage.database import Database

BOARD_ID = "b" * 64
CHANNEL_ID = "c" * 64
AREA_ID = "a" * 64


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


@pytest.fixture(scope="module")
def remote():
    return bootstrap_node_identity("hide-remote")


@pytest.fixture(scope="module")
def own():
    return bootstrap_node_identity("hide-own")


def _carry(db, own, genesis, kind):
    assert accept_genesis(
        db, kind=kind, envelope=genesis.to_dict(), sender_fingerprint=genesis.payload["origin_fingerprint"],
        content_id=genesis.content_id, own_fingerprint=own.fingerprint, cap=None,
    ) == "carried"


def _carried_board(db, own, remote, name="Remote Discussion"):
    _carry(db, own, build_board_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        board_id=BOARD_ID, name=name, created_at="2026-01-01T00:00:00Z",
    ), "boards")
    return get_board_by_name(db, name)


def _remote_post(db, remote, subject, board_id=BOARD_ID):
    post = build_board_post(
        signing_identity=remote.signing_key, home_node_fingerprint=remote.fingerprint, local_user_id="wanderer",
        board_id=board_id, subject=subject, body="hello world", created_at="2026-01-02T00:00:00Z",
    )
    return materialize_carried_post(db, post, sender_fingerprint=remote.fingerprint)


def test_a_hidden_board_is_invisible_to_every_listing_lookup_and_search(db, own, remote, sysop):
    board = _carried_board(db, own, remote)
    own_post = create_post(db, board, sysop, "mine", "hello world")
    _remote_post(db, remote, "a reply to mine").__class__  # materialized
    db.connection.execute("UPDATE posts SET parent_post_id = ? WHERE subject = 'a reply to mine'", (own_post.post_id,))
    db.connection.commit()
    assert search_posts(db, sysop, "hello")
    assert unread_replies_to(db, sysop)

    hide_carried_resource(db, "boards", BOARD_ID, actor=sysop)

    assert [b.name for b in list_boards(db)] == []
    with pytest.raises(BoardError):
        get_board_by_name(db, "Remote Discussion")
    assert search_posts(db, sysop, "hello") == []
    assert unread_replies_to(db, sysop) == []


def test_a_hidden_channel_and_file_area_are_invisible(db, own, remote, sysop):
    _carry(db, own, build_channel_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        channel_id=CHANNEL_ID, name="lobby", created_at="2026-01-01T00:00:00Z",
    ), "channels")
    _carry(db, own, build_file_area_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        area_id=AREA_ID, name="files", created_at="2026-01-01T00:00:00Z",
    ), "file_areas")

    hide_carried_resource(db, "channels", CHANNEL_ID, actor=sysop)
    hide_carried_resource(db, "file_areas", AREA_ID, actor=sysop)

    assert list_channels(db) == [] and list_file_areas(db) == []
    with pytest.raises(ChannelError):
        get_channel_by_name(db, "lobby")
    with pytest.raises(FileAreaError):
        get_file_area_by_name(db, "files")
    # No peer may subscribe to it; no transfer grant resolves into it.
    assert get_channel_by_channel_id(db, CHANNEL_ID) is None
    assert get_file_area_by_area_id(db, AREA_ID) is None


def test_hide_then_restore_brings_back_local_posts_authorship_and_moderation(db, own, remote, sysop):
    """What a replay of signed events could not reproduce: this node's own
    users' posts (keyed by a local hash) and its own moderation."""
    board = _carried_board(db, own, remote)
    mine = create_post(db, board, sysop, "mine", "hello")
    removed = _remote_post(db, remote, "moderated away")
    tombstone_post(db, removed, board, tombstoned_by=sysop)
    before = db.connection.execute(
        "SELECT post_id, author_user_id, subject, tombstoned_at FROM posts ORDER BY id"
    ).fetchall()

    hide_carried_resource(db, "boards", BOARD_ID, actor=sysop)
    [decision] = list_carry_decisions(db, EXCLUDED)
    assert decision.hidden and decision.reason == "deleted"
    restore_excluded(db, "boards", BOARD_ID, actor=sysop)

    after = db.connection.execute(
        "SELECT post_id, author_user_id, subject, tombstoned_at FROM posts ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert get_board_by_name(db, "Remote Discussion").id == board.id
    assert carry_decision_counts(db) == {}
    assert mine.author_user_id == sysop.id
    actions = [entry.action for entry in list_recent_actions(db)]
    assert "hide_link_resource" in actions and "restore_link_resource" in actions


def test_a_hidden_board_is_not_carried_takes_nothing_new_and_is_declared_not_carried(db, own, remote, sysop):
    from netbbs.link.store import _all_board_events

    _carried_board(db, own, remote)
    hide_carried_resource(db, "boards", BOARD_ID, actor=sysop)

    assert carried_board_ids(db) == []
    assert uncarried_resource_ids(db) == {"boards": (BOARD_ID,)}
    assert _all_board_events(db, BOARD_ID) == {}
    assert _remote_post(db, remote, "arrived while hidden") is None


def test_purge_deletes_for_real_stays_excluded_and_restore_takes_it_on_again(db, own, remote, sysop):
    board = _carried_board(db, own, remote)
    create_post(db, board, sysop, "mine", "hello")
    hide_carried_resource(db, "boards", BOARD_ID, actor=sysop)

    purge_excluded(db, "boards", BOARD_ID, actor=sysop)

    assert db.connection.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    [decision] = list_carry_decisions(db, EXCLUDED)
    assert (decision.reason, decision.hidden) == ("purged", False)
    with pytest.raises(CarryDecisionError):
        purge_excluded(db, "boards", BOARD_ID, actor=sysop)

    restore_excluded(db, "boards", BOARD_ID, actor=sysop)
    assert get_board_by_name(db, "Remote Discussion").board_id == BOARD_ID
    assert carry_decision_counts(db) == {}


def test_purging_a_hidden_file_area_takes_its_remote_catalogue(db, own, remote, sysop):
    from netbbs.link.events import build_file_descriptor
    from netbbs.link.files import materialize_carried_file_descriptor

    _carry(db, own, build_file_area_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        area_id=AREA_ID, name="files", created_at="2026-01-01T00:00:00Z",
    ), "file_areas")
    materialize_carried_file_descriptor(db, build_file_descriptor(
        signing_identity=remote.signing_key, area_id=AREA_ID, file_id="f1", filename="f1.zip",
        size_bytes=10, sha256="0" * 64, created_at="2026-01-02T00:00:00Z",
    ), sender_fingerprint=remote.fingerprint)
    hide_carried_resource(db, "file_areas", AREA_ID, actor=sysop)

    purge_excluded(db, "file_areas", AREA_ID, actor=sysop)

    assert db.connection.execute("SELECT COUNT(*) FROM remote_files").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM file_areas").fetchone()[0] == 0


def test_a_hidden_resource_keeps_its_name_and_says_so(db, own, remote, sysop):
    _carried_board(db, own, remote, name="general")
    hide_carried_resource(db, "boards", BOARD_ID, actor=sysop)

    with pytest.raises(BoardError, match="excluded"):
        create_board(db, "general", creator=sysop)


def test_a_hide_that_fails_leaves_nothing_behind(db, own, remote, sysop, monkeypatch):
    from netbbs.link import carry as carry_module

    _carried_board(db, own, remote)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(carry_module, "record_carry_decision", boom)
    with pytest.raises(RuntimeError):
        hide_carried_resource(db, "boards", BOARD_ID, actor=sysop)

    assert db.connection.execute("SELECT link_hidden_at FROM boards").fetchone()[0] is None
    assert carry_decision_counts(db) == {}
    assert list_recent_actions(db) == []


def test_a_genesis_arriving_for_a_hidden_resource_is_not_taken_as_carried(db, own, remote, sysop):
    _carried_board(db, own, remote)
    hide_carried_resource(db, "boards", BOARD_ID, actor=sysop)
    genesis = build_board_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        board_id=BOARD_ID, name="Remote Discussion", created_at="2026-01-01T00:00:00Z",
    )
    assert accept_genesis(
        db, kind="boards", envelope=genesis.to_dict(), sender_fingerprint=remote.fingerprint,
        content_id=genesis.content_id, own_fingerprint=own.fingerprint, cap=None,
    ) == "hidden"
    assert list_carry_decisions(db, EXCLUDED)[0].reason == "deleted"


def test_only_a_resource_originated_elsewhere_is_hidden(db, own, remote, sysop):
    _carried_board(db, own, remote)
    mine = create_board(db, "mine", creator=sysop)
    link_board(db, mine, node_identity=own)

    assert carried_from_elsewhere(db, "boards", BOARD_ID, own.fingerprint) is True
    assert carried_from_elsewhere(db, "boards", mine.board_id, own.fingerprint) is False
    # A board transferred to this node is this node's to close, not to hide.
    db.connection.execute("UPDATE boards SET link_origin_fingerprint = ? WHERE board_id = ?", (own.fingerprint, BOARD_ID))
    db.connection.commit()
    assert carried_from_elsewhere(db, "boards", BOARD_ID, own.fingerprint) is False
    # Without a loaded identity: a genesis this node received counts.
    assert carried_from_elsewhere(db, "boards", BOARD_ID, None) is True
    assert carried_from_elsewhere(db, "boards", mine.board_id, None) is False


def test_a_chat_session_in_a_channel_that_gets_hidden_degrades(db, own, remote, sysop):
    from netbbs.net.chat_flow import _fresh_channel, _meets_live_participation_requirements

    _carry(db, own, build_channel_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        channel_id=CHANNEL_ID, name="lobby", created_at="2026-01-01T00:00:00Z",
    ), "channels")
    channel = get_channel_by_name(db, "lobby")
    hide_carried_resource(db, "channels", CHANNEL_ID, actor=sysop)

    assert _fresh_channel(db, channel) is None
    assert _meets_live_participation_requirements(db, channel, sysop) is False


def test_a_hidden_channel_stops_offering_its_pending_invitations(db, own, remote, sysop):
    from netbbs.chat.membership import create_invitation, list_pending_invitations_for_user

    _carry(db, own, build_channel_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        channel_id=CHANNEL_ID, name="lobby", created_at="2026-01-01T00:00:00Z",
    ), "channels")
    guest = create_user(db, "guest", password="hunter2", user_level=10)
    create_invitation(db, get_channel_by_name(db, "lobby"), guest, invited_by=sysop)
    assert [view.channel_name for view in list_pending_invitations_for_user(db, guest)] == ["lobby"]

    hide_carried_resource(db, "channels", CHANNEL_ID, actor=sysop)

    assert list_pending_invitations_for_user(db, guest) == []


def test_a_caller_already_inside_a_hidden_board_cannot_post_to_it(db, own, remote, sysop):
    """They still hold the `Board` from before it was hidden; the write path
    refuses it."""
    from netbbs.boards.posts import PostError

    board = _carried_board(db, own, remote)
    hide_carried_resource(db, "boards", BOARD_ID, actor=sysop)

    with pytest.raises(PostError, match="no longer available"):
        create_post(db, board, sysop, "late", "still typing")


def test_a_caller_already_inside_a_hidden_file_area_cannot_upload_to_it(db, own, remote, sysop):
    from netbbs.files.entries import FileEntryError, upload_file

    _carry(db, own, build_file_area_genesis(
        signing_identity=remote.signing_key, origin_fingerprint=remote.fingerprint,
        area_id=AREA_ID, name="files", created_at="2026-01-01T00:00:00Z",
    ), "file_areas")
    area = get_file_area_by_name(db, "files")
    hide_carried_resource(db, "file_areas", AREA_ID, actor=sysop)

    with pytest.raises(FileEntryError, match="no longer available"):
        upload_file(db, area, sysop, "late.txt", b"still uploading")


def test_excluded_lists_a_hidden_resource_under_its_local_name(db, own, remote, sysop):
    """Renamed here (or given a collision suffix): that is the name the SysOp
    knows, and the one Purge asks them to type."""
    _carried_board(db, own, remote)
    db.connection.execute(
        "UPDATE boards SET name = 'Renamed Here', description = 'local words' WHERE board_id = ?", (BOARD_ID,)
    )
    db.connection.commit()
    hide_carried_resource(db, "boards", BOARD_ID, actor=sysop)

    [decision] = list_carry_decisions(db, EXCLUDED)
    assert (decision.name, decision.description) == ("Renamed Here", "local words")


def test_hiding_is_refused_once_this_node_has_become_the_boards_origin(db, own, remote, sysop):
    """An origin transfer to this node accepted while the SysOp was confirming
    makes this node the board's authority; the hide re-checks under the lock."""
    _carried_board(db, own, remote)
    db.connection.execute("UPDATE boards SET link_origin_fingerprint = ? WHERE board_id = ?", (own.fingerprint, BOARD_ID))
    db.connection.commit()

    with pytest.raises(CarryDecisionError, match="origin"):
        hide_carried_resource(db, "boards", BOARD_ID, actor=sysop, own_fingerprint=own.fingerprint)
