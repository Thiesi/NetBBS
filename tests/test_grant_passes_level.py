"""
A board or file area's read or write grant lets its holder past that
resource's minimum level (design doc §5.2, issue #836) -- and past nothing
else: not another resource's level, not the age gate.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards import create_board
from netbbs.boards.posts import create_post, list_posts_page
from netbbs.communities import create_community, meets_read_gate, meets_write_gate
from netbbs.files.areas import create_file_area
from netbbs.files.entries import list_files_page, upload_file
from netbbs.moderation.roles import BoardPermission, grant_permissions
from netbbs.net.board_flow import _read_only_reason, visible_boards
from netbbs.net.file_flow import visible_areas
from netbbs.permissions import InsufficientLevelError
from netbbs.search import search_posts
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


@pytest.fixture
def helper(db):
    return create_user(db, "helper", password="hunter2", user_level=10)


def _grant(db, sysop, user, object_type, object_id, permission, **kwargs):
    grant_permissions(
        db, user, object_type=object_type, object_id=object_id, permissions=permission, granted_by=sysop, **kwargs
    )


def test_a_write_grant_opens_a_sysop_only_announcements_board(db, sysop, helper):
    news = create_board(db, "News", creator=sysop, min_write_level=SYSOP_LEVEL)
    with pytest.raises(InsufficientLevelError):
        create_post(db, news, helper, "Meeting", "Thursday")
    _grant(db, sysop, helper, "board", news.id, BoardPermission.WRITE)
    create_post(db, news, helper, "Meeting", "Thursday")
    assert meets_write_gate(db, helper, news)
    assert _read_only_reason(db, helper, news, closed=False) is None


def test_a_grant_on_one_board_opens_no_other(db, sysop, helper):
    news = create_board(db, "News", creator=sysop, min_write_level=SYSOP_LEVEL)
    other = create_board(db, "Staff room", creator=sysop, min_write_level=SYSOP_LEVEL, min_read_level=200)
    _grant(db, sysop, helper, "board", news.id, BoardPermission.WRITE | BoardPermission.READ)
    with pytest.raises(InsufficientLevelError):
        create_post(db, other, helper, "Hello", "text")
    assert other not in visible_boards(db, helper, community_id=None, community_scoped=False)
    assert "posting needs level 255" in _read_only_reason(db, helper, other, closed=False)


def test_a_read_grant_opens_reading_listing_and_search(db, sysop, helper):
    hidden = create_board(db, "Inner circle", creator=sysop, min_read_level=200)
    create_post(db, hidden, sysop, "Secret handshake", "fountain pens only")
    assert not meets_read_gate(db, helper, hidden)
    with pytest.raises(InsufficientLevelError):
        list_posts_page(db, hidden, helper)
    assert search_posts(db, helper, "handshake") == []

    _grant(db, sysop, helper, "board", hidden.id, BoardPermission.READ)
    assert [post.subject for post in list_posts_page(db, hidden, helper).posts] == ["Secret handshake"]
    assert hidden.id in {board.id for board in visible_boards(db, helper, community_id=None, community_scoped=False)}
    assert [hit.subject for hit in search_posts(db, helper, "handshake")] == ["Secret handshake"]


def test_a_read_grant_does_not_pass_the_age_gate(db, sysop, helper):
    adults = create_board(db, "Adults", creator=sysop, min_read_level=200, min_age=18)
    _grant(db, sysop, helper, "board", adults.id, BoardPermission.READ)
    assert meets_read_gate(db, helper, adults)
    assert adults.id not in {b.id for b in visible_boards(db, helper, community_id=None, community_scoped=False)}


def test_an_approve_grant_alone_opens_no_level_gate(db, sysop, helper):
    news = create_board(db, "News", creator=sysop, min_write_level=SYSOP_LEVEL)
    _grant(db, sysop, helper, "board", news.id, BoardPermission.APPROVE)
    with pytest.raises(InsufficientLevelError):
        create_post(db, news, helper, "Meeting", "Thursday")


def test_file_area_grants_open_upload_and_listing(db, sysop, helper):
    vault = create_file_area(db, "Vault", creator=sysop, min_read_level=200, min_write_level=SYSOP_LEVEL)
    with pytest.raises(InsufficientLevelError):
        upload_file(db, vault, helper, "ink.txt", b"blue-black")
    _grant(db, sysop, helper, "file_area", vault.id, BoardPermission.READ | BoardPermission.WRITE)
    upload_file(db, vault, helper, "ink.txt", b"blue-black")
    assert [entry.filename for entry in list_files_page(db, vault, helper).entries] == ["ink.txt"]
    assert vault.id in {area.id for area in visible_areas(db, helper, community_id=None, community_scoped=False)}


def test_a_community_blanket_write_grant_opens_its_boards(db, sysop, helper):
    pens = create_community(db, "Pens", creator=sysop)
    inside = create_board(db, "Pen news", creator=sysop, min_write_level=SYSOP_LEVEL, community_id=pens.id)
    outside = create_board(db, "Club news", creator=sysop, min_write_level=SYSOP_LEVEL)
    _grant(db, sysop, helper, "board", None, BoardPermission.WRITE, community_id=pens.id)
    create_post(db, inside, helper, "New nibs", "arrived")
    with pytest.raises(InsufficientLevelError):
        create_post(db, outside, helper, "Hello", "text")


def test_the_grant_screen_offers_read_and_post_as_access_presets():
    from netbbs.moderation.roles import ModeratorGrantError
    from netbbs.net.admin_flow import _moderator_preset_label, _moderator_preset_permissions

    assert _moderator_preset_permissions("board", "post") == BoardPermission.READ | BoardPermission.WRITE
    assert _moderator_preset_permissions("file_area", "read") == BoardPermission.READ
    assert "past the level gates" in _moderator_preset_label("board", "post")
    with pytest.raises(ModeratorGrantError):
        _moderator_preset_permissions("channel", "post")


def test_a_node_wide_blanket_read_grant_opens_every_board_it_covers(db, sysop, helper):
    # The scope decides what a grant opens (review on #868): a blanket grant
    # covers every board, and file areas are a kind of their own.
    one = create_board(db, "Inner circle", creator=sysop, min_read_level=200)
    two = create_board(db, "Staff room", creator=sysop, min_read_level=SYSOP_LEVEL)
    vault = create_file_area(db, "Vault", creator=sysop, min_read_level=200)
    _grant(db, sysop, helper, "board", None, BoardPermission.READ)
    assert meets_read_gate(db, helper, one) and meets_read_gate(db, helper, two)
    assert not meets_read_gate(db, helper, vault)
