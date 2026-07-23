"""
Tests for netbbs.communities (design doc §16): the Communities core
module -- CRUD, deletion cascade, and scalar-default resolution.
Admin/main-menu UI wiring is not built yet (see the design doc's
status note); these drive the library layer directly.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board, get_board_by_name
from netbbs.chat.channels import create_channel, get_channel_by_name
from netbbs.communities import (
    Community,
    CommunityError,
    create_community,
    delete_community,
    get_community,
    get_community_by_name,
    get_effective_min_age,
    get_effective_min_read_level,
    get_effective_min_write_level,
    get_effective_name_requirement,
    list_communities,
    update_community,
)
from netbbs.files.areas import create_file_area, get_file_area_by_name
from netbbs.moderation import BoardPermission, get_grant, grant_permissions
from netbbs.moderation.log import list_actions_for_object
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=100)


# -- create/get/list ---------------------------------------------------------


def test_create_community_round_trips(db, sysop):
    community = create_community(db, "Vintage Computing", description="Old iron", creator=sysop)
    assert community.name == "Vintage Computing"
    assert community.description == "Old iron"
    assert community.hidden is False
    assert community.default_min_read_level is None
    assert community.default_min_write_level is None
    assert community.default_min_age is None
    assert community.default_name_requirement is None


def test_create_community_rejects_name_collision(db, sysop):
    create_community(db, "Politics", creator=sysop)
    with pytest.raises(CommunityError):
        create_community(db, "Politics", creator=sysop)


def test_create_community_rejects_invalid_name_requirement(db, sysop):
    with pytest.raises(CommunityError, match="name_requirement"):
        create_community(db, "Politics", default_name_requirement="bogus", creator=sysop)


def test_get_community_by_name_raises_for_unknown_name(db):
    with pytest.raises(CommunityError):
        get_community_by_name(db, "nonexistent")


def test_get_community_returns_none_for_none_id(db):
    assert get_community(db, None) is None


def test_get_community_returns_none_for_unknown_id(db):
    assert get_community(db, 99999) is None


def test_list_communities_alphabetical(db, sysop):
    create_community(db, "Zebras", creator=sysop)
    create_community(db, "Amiga", creator=sysop)
    names = [c.name for c in list_communities(db)]
    assert names == ["Amiga", "Zebras"]


def test_create_community_records_moderation_log_entry(db, sysop):
    community = create_community(db, "Politics", creator=sysop)
    entries = list_actions_for_object(db, "community", community.id)
    assert any(e.action == "create_community" for e in entries)


# -- update -------------------------------------------------------------------


def test_update_community_replaces_full_state(db, sysop):
    community = create_community(db, "Politics", creator=sysop)
    updated = update_community(
        db, community, name="Politics & Society", description="new desc", hidden=True,
        default_min_read_level=1, default_min_write_level=5, default_min_age=18,
        default_name_requirement="verified", changed_by=sysop,
    )
    assert updated.name == "Politics & Society"
    assert updated.description == "new desc"
    assert updated.hidden is True
    assert updated.default_min_read_level == 1
    assert updated.default_min_write_level == 5
    assert updated.default_min_age == 18
    assert updated.default_name_requirement == "verified"


def test_update_community_rejects_name_collision(db, sysop):
    create_community(db, "Taken", creator=sysop)
    community = create_community(db, "Politics", creator=sysop)
    with pytest.raises(CommunityError):
        update_community(
            db, community, name="Taken", description=None, hidden=False,
            default_min_read_level=None, default_min_write_level=None, default_min_age=None,
            default_name_requirement=None, changed_by=sysop,
        )


def test_update_community_rejects_invalid_name_requirement(db, sysop):
    community = create_community(db, "Politics", creator=sysop)
    with pytest.raises(CommunityError, match="name_requirement"):
        update_community(
            db, community, name="Politics", description=None, hidden=False,
            default_min_read_level=None, default_min_write_level=None, default_min_age=None,
            default_name_requirement="bogus", changed_by=sysop,
        )


def test_update_community_can_clear_a_previously_set_default(db, sysop):
    community = create_community(db, "Politics", default_min_age=18, creator=sysop)
    updated = update_community(
        db, community, name="Politics", description=None, hidden=False,
        default_min_read_level=None, default_min_write_level=None, default_min_age=None,
        default_name_requirement=None, changed_by=sysop,
    )
    assert updated.default_min_age is None


# -- deletion: reverts resources to Uncategorized, revokes scoped grants -----


def test_delete_community_reverts_referencing_board_to_uncategorized(db, sysop):
    community = create_community(db, "Politics", creator=sysop)
    create_board(db, "elections", community_id=community.id, creator=sysop)
    delete_community(db, community, deleted_by=sysop)
    assert get_board_by_name(db, "elections").community_id is None


def test_delete_community_reverts_referencing_channel_and_area(db, sysop):
    community = create_community(db, "Politics", creator=sysop)
    channel = create_channel(db, "debate", community_id=community.id, creator=sysop)
    area = create_file_area(db, "manifestos", community_id=community.id, creator=sysop)
    delete_community(db, community, deleted_by=sysop)

    assert get_channel_by_name(db, channel.name).community_id is None
    assert get_file_area_by_name(db, area.name).community_id is None


def test_delete_community_revokes_scoped_community_blanket_grant(db, sysop):
    community = create_community(db, "Politics", creator=sysop)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    grant_permissions(
        db, alice, object_type="board", object_id=None, community_id=community.id,
        permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    delete_community(db, community, deleted_by=sysop)
    assert get_grant(db, alice, object_type="board", object_id=None, community_id=community.id) is None


def test_delete_community_does_not_touch_an_unrelated_local_blanket_grant(db, sysop):
    community = create_community(db, "Politics", creator=sysop)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    grant_permissions(db, alice, object_type="board", object_id=None, permissions=BoardPermission.EDIT, granted_by=sysop)
    delete_community(db, community, deleted_by=sysop)
    assert get_grant(db, alice, object_type="board", object_id=None) is not None


def test_delete_community_removes_the_row(db, sysop):
    community = create_community(db, "Politics", creator=sysop)
    delete_community(db, community, deleted_by=sysop)
    assert get_community(db, community.id) is None


# -- scalar-default resolution -------------------------------------------------


def test_effective_min_read_level_uses_explicit_board_value_over_community_default(db, sysop):
    community = create_community(db, "Politics", default_min_read_level=5, creator=sysop)
    board = create_board(db, "elections", min_read_level=1, community_id=community.id, creator=sysop)
    assert get_effective_min_read_level(db, board) == 1


def test_effective_min_read_level_falls_back_to_community_default_when_board_is_null(db, sysop):
    community = create_community(db, "Politics", default_min_read_level=5, creator=sysop)
    board = create_board(db, "elections", min_read_level=None, community_id=community.id, creator=sysop)
    assert get_effective_min_read_level(db, board) == 5


def test_effective_min_read_level_falls_back_to_system_default_with_no_community(db, sysop):
    board = create_board(db, "general", min_read_level=None, creator=sysop)
    assert get_effective_min_read_level(db, board) == 0


def test_effective_min_read_level_falls_back_to_system_default_when_community_default_also_null(db, sysop):
    community = create_community(db, "Politics", creator=sysop)  # no default set
    board = create_board(db, "elections", min_read_level=None, community_id=community.id, creator=sysop)
    assert get_effective_min_read_level(db, board) == 0


def test_effective_min_write_level_same_shape_as_read_level(db, sysop):
    community = create_community(db, "Politics", default_min_write_level=3, creator=sysop)
    board = create_board(db, "elections", min_write_level=None, community_id=community.id, creator=sysop)
    assert get_effective_min_write_level(db, board) == 3


def test_effective_min_age_uses_explicit_board_value_over_community_default(db, sysop):
    community = create_community(db, "Politics", default_min_age=21, creator=sysop)
    board = create_board(db, "elections", min_age=18, community_id=community.id, creator=sysop)
    assert get_effective_min_age(db, board) == 18


def test_effective_min_age_falls_back_to_community_default(db, sysop):
    community = create_community(db, "Politics", default_min_age=21, creator=sysop)
    board = create_board(db, "elections", min_age=None, community_id=community.id, creator=sysop)
    assert get_effective_min_age(db, board) == 21


def test_effective_min_age_none_with_no_community(db, sysop):
    board = create_board(db, "general", creator=sysop)
    assert get_effective_min_age(db, board) is None


def test_effective_min_age_works_for_channels(db, sysop):
    community = create_community(db, "Politics", default_min_age=21, creator=sysop)
    channel = create_channel(db, "debate", community_id=community.id, creator=sysop)
    assert get_effective_min_age(db, channel) == 21


def test_effective_min_age_works_for_file_areas(db, sysop):
    community = create_community(db, "Politics", default_min_age=21, creator=sysop)
    area = create_file_area(db, "manifestos", community_id=community.id, creator=sysop)
    assert get_effective_min_age(db, area) == 21


def test_effective_name_requirement_uses_explicit_board_value_over_community_default(db, sysop):
    community = create_community(db, "Politics", default_name_requirement="verified_and_displayed", creator=sysop)
    board = create_board(db, "elections", name_requirement="verified", community_id=community.id, creator=sysop)
    assert get_effective_name_requirement(db, board) == "verified"


def test_effective_name_requirement_falls_back_to_community_default(db, sysop):
    community = create_community(db, "Politics", default_name_requirement="verified", creator=sysop)
    board = create_board(db, "elections", community_id=community.id, creator=sysop)
    assert get_effective_name_requirement(db, board) == "verified"


def test_effective_name_requirement_none_with_no_community(db, sysop):
    board = create_board(db, "general", creator=sysop)
    assert get_effective_name_requirement(db, board) is None
