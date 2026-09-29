"""
Tests for `netbbs.sort_preferences`: the three-level (global/Community/
category) cascading sort-mode preference behind the channel/board/
file-area pickers' `[O]rder` command (design doc, dogfood feature
request).
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.communities import create_community
from netbbs.sort_preferences import (
    DEFAULT_SORT_MODE_BY_KIND,
    clear_sort_preference,
    get_effective_sort_mode,
    list_sort_preferences,
    set_sort_preference,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def retro(db, alice):
    return create_community(db, "Retro Computing", creator=alice)


@pytest.fixture
def other_community(db, alice):
    return create_community(db, "Other", creator=alice)


def test_default_is_the_sysop_order_for_boards_and_file_areas_when_nothing_is_ever_set(db, alice):
    assert get_effective_sort_mode(db, alice, "board") == DEFAULT_SORT_MODE_BY_KIND["board"] == "sysop"
    assert get_effective_sort_mode(db, alice, "file_area") == DEFAULT_SORT_MODE_BY_KIND["file_area"] == "sysop"


def test_default_is_alphabetical_for_channels_when_nothing_is_ever_set(db, alice):
    # Deliberately *not* "activity" here, unlike boards/file areas --
    # see DEFAULT_SORT_MODE_BY_KIND's own comment: a channel's activity
    # is only ever node-local ChatHub state, so defaulting every user to
    # it would silently reintroduce the exact cross-node-disagreement
    # dogfood bug this whole feature exists to let users opt into
    # knowingly, not be defaulted into silently.
    assert get_effective_sort_mode(db, alice, "channel") == DEFAULT_SORT_MODE_BY_KIND["channel"] == "alphabetical"


def test_global_preference_applies_once_set(db, alice):
    set_sort_preference(db, alice, "channel", "alphabetical")
    assert get_effective_sort_mode(db, alice, "channel") == "alphabetical"


def test_global_preference_is_per_resource_kind(db, alice):
    """Landed per the user's own explicit design ask: someone who wants
    channels sorted by activity might not want boards sorted the same
    way -- each resource_kind's global default is independent."""
    set_sort_preference(db, alice, "channel", "alphabetical")
    assert get_effective_sort_mode(db, alice, "board") == "sysop"


def test_global_preference_upserts_rather_than_erroring_on_a_second_set(db, alice):
    set_sort_preference(db, alice, "channel", "alphabetical")
    set_sort_preference(db, alice, "channel", "recent")
    assert get_effective_sort_mode(db, alice, "channel") == "recent"


def test_community_override_beats_the_global_default(db, alice, retro):
    set_sort_preference(db, alice, "channel", "alphabetical")
    set_sort_preference(db, alice, "channel", "volume", community_id=retro.id)
    assert get_effective_sort_mode(db, alice, "channel", community_id=retro.id) == "volume"
    assert get_effective_sort_mode(db, alice, "channel") == "alphabetical"


def test_community_override_does_not_leak_into_a_different_community(db, alice, retro, other_community):
    set_sort_preference(db, alice, "channel", "volume", community_id=retro.id)
    assert get_effective_sort_mode(db, alice, "channel", community_id=other_community.id) == "alphabetical"


def test_category_override_beats_both_community_and_global(db, alice, retro):
    set_sort_preference(db, alice, "channel", "alphabetical")
    set_sort_preference(db, alice, "channel", "volume", community_id=retro.id)
    set_sort_preference(db, alice, "channel", "recent", category_id=5)
    assert get_effective_sort_mode(db, alice, "channel", community_id=retro.id, category_id=5) == "recent"


def test_community_fallback_still_applies_to_a_different_category_in_the_same_community(db, alice, retro):
    """The 'Amiga' example (design doc dogfood conversation): a
    category override for one specific category must not affect a
    sibling category in the same Community, which should keep falling
    back to that Community's own override."""
    set_sort_preference(db, alice, "channel", "volume", community_id=retro.id)
    set_sort_preference(db, alice, "channel", "recent", category_id=5)
    assert get_effective_sort_mode(db, alice, "channel", community_id=retro.id, category_id=999) == "volume"


def test_clearing_a_scope_falls_back_to_the_next_one_up(db, alice, retro):
    set_sort_preference(db, alice, "channel", "alphabetical")
    set_sort_preference(db, alice, "channel", "volume", community_id=retro.id)
    clear_sort_preference(db, alice, "channel", community_id=retro.id)
    assert get_effective_sort_mode(db, alice, "channel", community_id=retro.id) == "alphabetical"


def test_clearing_a_scope_that_was_never_set_is_a_harmless_no_op(db, alice, retro):
    clear_sort_preference(db, alice, "channel", community_id=retro.id)
    assert get_effective_sort_mode(db, alice, "channel", community_id=retro.id) == "alphabetical"


def test_preferences_are_isolated_per_user(db, alice, bob):
    set_sort_preference(db, alice, "channel", "recent")
    assert get_effective_sort_mode(db, bob, "channel") == "alphabetical"


def test_set_sort_preference_rejects_both_community_and_category_at_once(db, alice, retro):
    with pytest.raises(ValueError):
        set_sort_preference(db, alice, "channel", "alphabetical", community_id=retro.id, category_id=5)


def test_set_sort_preference_rejects_an_unknown_resource_kind(db, alice):
    with pytest.raises(ValueError):
        set_sort_preference(db, alice, "bogus", "alphabetical")


def test_set_sort_preference_rejects_an_unknown_sort_mode(db, alice):
    with pytest.raises(ValueError):
        set_sort_preference(db, alice, "channel", "bogus")


def test_list_sort_preferences_orders_global_then_community_then_category(db, alice, retro):
    set_sort_preference(db, alice, "channel", "recent", category_id=5)
    set_sort_preference(db, alice, "channel", "volume", community_id=retro.id)
    set_sort_preference(db, alice, "channel", "alphabetical")
    prefs = list_sort_preferences(db, alice)
    assert [(p.community_id, p.category_id) for p in prefs] == [
        (None, None),
        (retro.id, None),
        (None, 5),
    ]


def test_list_sort_preferences_only_returns_this_users_own_rows(db, alice, bob):
    set_sort_preference(db, alice, "channel", "alphabetical")
    assert list_sort_preferences(db, bob) == []
