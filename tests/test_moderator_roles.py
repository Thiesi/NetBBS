"""Tests for netbbs.moderation.roles."""

from __future__ import annotations

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.communities import create_community
from netbbs.moderation import (
    BoardPermission,
    ChannelPermission,
    ModeratorGrantError,
    get_grant,
    grant_permissions,
    has_permission,
    list_actions_for_target_user,
    list_grants_for_object,
    list_grants_for_user,
    revoke_permissions,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=100)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


# -- granting: per-object ---------------------------------------------------


def test_grant_permissions_creates_per_object_grant(db, sysop, alice):
    grant = grant_permissions(
        db, alice, object_type="board", object_id=1,
        permissions=BoardPermission.EDIT, granted_by=sysop,
    )
    assert grant.user_id == alice.id
    assert grant.object_type == "board"
    assert grant.object_id == 1
    assert grant.has(BoardPermission.EDIT)
    assert not grant.has(BoardPermission.DELETE)


def test_grant_permissions_is_additive(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)
    grant = grant_permissions(
        db, alice, object_type="board", object_id=1,
        permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    assert grant.has(BoardPermission.EDIT)
    assert grant.has(BoardPermission.APPROVE)


def test_grant_permissions_supports_combined_flags_in_one_call(db, sysop, alice):
    grant = grant_permissions(
        db, alice, object_type="board", object_id=1,
        permissions=BoardPermission.EDIT | BoardPermission.DELETE,
        granted_by=sysop,
    )
    assert grant.has(BoardPermission.EDIT)
    assert grant.has(BoardPermission.DELETE)
    assert not grant.has(BoardPermission.APPROVE)


# -- granting: local-blanket --------------------------------------------


def test_grant_permissions_local_blanket_uses_none_object_id(db, sysop, alice):
    grant = grant_permissions(
        db, alice, object_type="board", object_id=None,
        permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    assert grant.object_id is None
    assert grant.has(BoardPermission.APPROVE)


def test_per_object_and_blanket_grants_for_same_user_are_independent(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)
    grant_permissions(db, alice, object_type="board", object_id=None, permissions=BoardPermission.APPROVE, granted_by=sysop)

    per_object = get_grant(db, alice, object_type="board", object_id=1)
    blanket = get_grant(db, alice, object_type="board", object_id=None)
    assert per_object.has(BoardPermission.EDIT) and not per_object.has(BoardPermission.APPROVE)
    assert blanket.has(BoardPermission.APPROVE) and not blanket.has(BoardPermission.EDIT)


# -- validation -----------------------------------------------------------


def test_grant_permissions_rejects_unknown_object_type(db, sysop, alice):
    with pytest.raises(ModeratorGrantError):
        grant_permissions(db, alice, object_type="nonsense", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)


def test_grant_permissions_rejects_mismatched_permission_enum(db, sysop, alice):
    with pytest.raises(ModeratorGrantError):
        grant_permissions(
            db, alice, object_type="board", object_id=1,
            permissions=ChannelPermission.MODERATE, granted_by=sysop,
        )


# -- has_permission: per-object + blanket fold-in --------------------------


def test_has_permission_true_for_direct_per_object_grant(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.WRITE, granted_by=sysop)
    assert has_permission(db, alice, object_type="board", object_id=1, permission=BoardPermission.WRITE)


def test_has_permission_true_via_local_blanket_grant(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=None, permissions=BoardPermission.DELETE, granted_by=sysop)
    # No per-object grant on board 42 at all — only the blanket should apply.
    assert has_permission(db, alice, object_type="board", object_id=42, permission=BoardPermission.DELETE)


def test_has_permission_false_when_neither_grant_exists(db, alice):
    assert not has_permission(db, alice, object_type="board", object_id=1, permission=BoardPermission.WRITE)


def test_has_permission_false_when_bit_not_granted(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.READ, granted_by=sysop)
    assert not has_permission(db, alice, object_type="board", object_id=1, permission=BoardPermission.DELETE)


def test_has_permission_does_not_leak_across_users(db, sysop, alice, bob):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)
    assert not has_permission(db, bob, object_type="board", object_id=1, permission=BoardPermission.EDIT)


def test_has_permission_does_not_leak_across_object_types(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)
    # file_area id 1 is a distinct object from board id 1.
    assert not has_permission(db, alice, object_type="file_area", object_id=1, permission=BoardPermission.EDIT)


# -- channel permission set -------------------------------------------------


def test_channel_grant_and_has_permission_for_manage_members(db, sysop, alice):
    grant_permissions(
        db, alice, object_type="channel", object_id=1,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=sysop,
    )
    assert has_permission(db, alice, object_type="channel", object_id=1, permission=ChannelPermission.MANAGE_MEMBERS)
    assert not has_permission(db, alice, object_type="channel", object_id=1, permission=ChannelPermission.MODERATE)


# -- revoking ---------------------------------------------------------------


def test_revoke_permissions_removes_only_specified_bits(db, sysop, alice):
    grant_permissions(
        db, alice, object_type="board", object_id=1,
        permissions=BoardPermission.EDIT | BoardPermission.DELETE, granted_by=sysop,
    )
    grant = revoke_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.DELETE, revoked_by=sysop)
    assert grant.has(BoardPermission.EDIT)
    assert not grant.has(BoardPermission.DELETE)


def test_revoke_permissions_deletes_row_when_mask_becomes_zero(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)
    grant = revoke_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, revoked_by=sysop)
    assert grant is None
    assert get_grant(db, alice, object_type="board", object_id=1) is None


def test_revoke_permissions_is_a_noop_when_nothing_was_granted(db, sysop, alice):
    grant = revoke_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, revoked_by=sysop)
    assert grant is None


def test_revoke_then_regrant_succeeds(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)
    revoke_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, revoked_by=sysop)
    grant = grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.WRITE, granted_by=sysop)
    assert grant.has(BoardPermission.WRITE)
    assert not grant.has(BoardPermission.EDIT)


# -- listing ------------------------------------------------------------


def test_list_grants_for_user_returns_all_of_their_grants(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)
    grant_permissions(db, alice, object_type="channel", object_id=2, permissions=ChannelPermission.MODERATE, granted_by=sysop)
    grants = list_grants_for_user(db, alice)
    assert len(grants) == 2


def test_list_grants_for_object_includes_blanket_grants(db, sysop, alice, bob):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)
    grant_permissions(db, bob, object_type="board", object_id=None, permissions=BoardPermission.APPROVE, granted_by=sysop)
    grants = list_grants_for_object(db, object_type="board", object_id=1)
    user_ids = {g.user_id for g in grants}
    assert user_ids == {alice.id, bob.id}


# -- Community-blanket tier (design doc §16, round 83) ----------------------


def test_grant_permissions_community_blanket_uses_none_object_id_and_community_id(db, sysop, alice):
    community = create_community(db, "Vintage Computing", creator=sysop)
    grant = grant_permissions(
        db, alice, object_type="board", object_id=None, community_id=community.id,
        permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    assert grant.object_id is None
    assert grant.community_id == community.id
    assert grant.has(BoardPermission.APPROVE)


def test_has_permission_true_via_community_blanket_for_a_board_in_that_community(db, sysop, alice):
    community = create_community(db, "Vintage Computing", creator=sysop)
    board = create_board(db, "amiga", community_id=community.id, creator=sysop)
    grant_permissions(
        db, alice, object_type="board", object_id=None, community_id=community.id,
        permissions=BoardPermission.DELETE, granted_by=sysop,
    )
    assert has_permission(db, alice, object_type="board", object_id=board.id, permission=BoardPermission.DELETE)


def test_community_blanket_does_not_leak_to_a_board_in_a_different_community(db, sysop, alice):
    vintage = create_community(db, "Vintage Computing", creator=sysop)
    politics = create_community(db, "Politics", creator=sysop)
    other_board = create_board(db, "elections", community_id=politics.id, creator=sysop)
    grant_permissions(
        db, alice, object_type="board", object_id=None, community_id=vintage.id,
        permissions=BoardPermission.DELETE, granted_by=sysop,
    )
    assert not has_permission(db, alice, object_type="board", object_id=other_board.id, permission=BoardPermission.DELETE)


def test_community_blanket_does_not_leak_to_an_uncategorized_board(db, sysop, alice):
    community = create_community(db, "Vintage Computing", creator=sysop)
    uncategorized_board = create_board(db, "general", creator=sysop)  # no community_id
    grant_permissions(
        db, alice, object_type="board", object_id=None, community_id=community.id,
        permissions=BoardPermission.DELETE, granted_by=sysop,
    )
    assert not has_permission(
        db, alice, object_type="board", object_id=uncategorized_board.id, permission=BoardPermission.DELETE
    )


def test_community_blanket_and_local_blanket_grants_are_independent(db, sysop, alice):
    """Community-blanket and local-blanket grants for the same
    (user, object_type) must coexist as two distinct rows -- confirms
    the migration's replaced partial unique indexes actually disambiguate
    them (a regression here would raise IntegrityError on the second
    grant_permissions call, or silently fold the two grants together)."""
    community = create_community(db, "Vintage Computing", creator=sysop)
    grant_permissions(
        db, alice, object_type="board", object_id=None, community_id=community.id,
        permissions=BoardPermission.APPROVE, granted_by=sysop,
    )
    grant_permissions(
        db, alice, object_type="board", object_id=None,
        permissions=BoardPermission.EDIT, granted_by=sysop,
    )
    community_blanket = get_grant(db, alice, object_type="board", object_id=None, community_id=community.id)
    local_blanket = get_grant(db, alice, object_type="board", object_id=None)
    assert community_blanket.has(BoardPermission.APPROVE) and not community_blanket.has(BoardPermission.EDIT)
    assert local_blanket.has(BoardPermission.EDIT) and not local_blanket.has(BoardPermission.APPROVE)


def test_has_permission_true_via_local_blanket_even_with_an_unrelated_community_blanket(db, sysop, alice):
    community = create_community(db, "Vintage Computing", creator=sysop)
    other_board = create_board(db, "general", creator=sysop)  # not in `community`
    grant_permissions(
        db, alice, object_type="board", object_id=None, community_id=community.id,
        permissions=BoardPermission.DELETE, granted_by=sysop,
    )
    grant_permissions(db, alice, object_type="board", object_id=None, permissions=BoardPermission.EDIT, granted_by=sysop)
    assert has_permission(db, alice, object_type="board", object_id=other_board.id, permission=BoardPermission.EDIT)


# -- audit logging ------------------------------------------------------


def test_grant_permissions_records_moderation_log_entry(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)
    entries = list_actions_for_target_user(db, alice.id)
    assert len(entries) == 1
    assert entries[0].action == "grant"
    assert entries[0].actor_user_id == sysop.id
    assert entries[0].detail == "EDIT"


def test_revoke_permissions_records_moderation_log_entry(db, sysop, alice):
    grant_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, granted_by=sysop)
    revoke_permissions(db, alice, object_type="board", object_id=1, permissions=BoardPermission.EDIT, revoked_by=sysop)
    entries = list_actions_for_target_user(db, alice.id)
    assert [e.action for e in entries] == ["grant", "revoke"]


# -- SysOp bypass (design doc -- board/area management round) --------------
#
# The `sysop` fixture above is level 100 -- a pre-existing test-suite
# convention, not the real SYSOP_LEVEL (255) constant -- so it does NOT
# trigger the bypass below; `real_sysop` is a separate fixture for
# exactly that.


@pytest.fixture
def real_sysop(db):
    return create_user(db, "real_sysop", password="hunter2", user_level=SYSOP_LEVEL)


def test_sysop_satisfies_board_permission_with_zero_grants(db, real_sysop):
    assert has_permission(
        db, real_sysop, object_type="board", object_id=1, permission=BoardPermission.APPROVE
    )


def test_sysop_satisfies_file_area_permission_with_zero_grants(db, real_sysop):
    assert has_permission(
        db, real_sysop, object_type="file_area", object_id=1, permission=BoardPermission.DELETE
    )


def test_sysop_satisfies_channel_permission_with_zero_grants(db, real_sysop):
    assert has_permission(
        db, real_sysop, object_type="channel", object_id=1, permission=ChannelPermission.MODERATE
    )


def test_a_level_100_user_does_not_get_the_bypass(db, sysop):
    """Confirms the bypass is keyed on the real SYSOP_LEVEL (255), not
    just "some elevated level" -- the pre-existing `sysop` fixture
    (level 100) must still need a real grant, same as anyone else."""
    assert not has_permission(
        db, sysop, object_type="board", object_id=1, permission=BoardPermission.APPROVE
    )


def test_sysop_bypass_does_not_affect_get_grant(db, real_sysop):
    """get_grant/list_grants_for_object answer "what grants actually
    exist", used for admin displays -- these must stay literal, not
    synthesize a fake grant for a SysOp."""
    assert get_grant(db, real_sysop, object_type="board", object_id=1) is None


def test_sysop_bypass_does_not_affect_list_grants_for_object(db, real_sysop):
    assert list_grants_for_object(db, object_type="board", object_id=1) == []


def test_sysop_bypass_still_validates_the_permission_type(db, real_sysop):
    """Input validation runs before the bypass check -- a SysOp passing
    a nonsensical object_type/permission combination is still caught,
    not silently waved through."""
    with pytest.raises(ModeratorGrantError):
        has_permission(
            db, real_sysop, object_type="board", object_id=1, permission=ChannelPermission.MODERATE
        )
