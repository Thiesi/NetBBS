"""Tests for netbbs.chat.categories — two-level category hierarchy, mirroring board categories."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.categories import (
    CategoryError,
    create_category,
    delete_category,
    get_category_by_id,
    get_category_by_name,
    list_subcategories,
    list_top_level_categories,
)
from netbbs.chat.channels import create_channel, get_channel_by_name
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def creator(db):
    return create_user(db, "creator", password="hunter2", user_level=10)


def test_create_top_level_category(db, creator):
    category = create_category(db, "Vintage Computing", created_by=creator)
    assert category.name == "Vintage Computing"
    assert category.is_top_level


def test_create_subcategory(db, creator):
    parent = create_category(db, "Vintage Computing", created_by=creator)
    child = create_category(db, "Commodore", parent_category_id=parent.id, created_by=creator)
    assert not child.is_top_level
    assert child.parent_category_id == parent.id


def test_third_level_nesting_rejected(db, creator):
    """
    The core design constraint: at most two levels ever exist. Verified
    directly (see design doc round 17 sign-off notes) rather than
    assumed — this is the rule that guarantees the cap, enforced in
    application code since SQLite can't express a self-join depth
    constraint as a plain CHECK.
    """
    parent = create_category(db, "Vintage Computing", created_by=creator)
    child = create_category(db, "Commodore", parent_category_id=parent.id, created_by=creator)
    with pytest.raises(CategoryError):
        create_category(db, "Commodore 64", parent_category_id=child.id, created_by=creator)


def test_list_top_level_categories_excludes_subcategories(db, creator):
    top = create_category(db, "Vintage Computing", created_by=creator)
    create_category(db, "Commodore", parent_category_id=top.id, created_by=creator)
    create_category(db, "Politics", created_by=creator)
    tops = list_top_level_categories(db)
    assert {c.name for c in tops} == {"Vintage Computing", "Politics"}


def test_list_subcategories(db, creator):
    parent = create_category(db, "Vintage Computing", created_by=creator)
    create_category(db, "Commodore", parent_category_id=parent.id, created_by=creator)
    create_category(db, "Amiga", parent_category_id=parent.id, created_by=creator)
    other_parent = create_category(db, "Politics", created_by=creator)
    create_category(db, "Local", parent_category_id=other_parent.id, created_by=creator)

    subs = list_subcategories(db, parent.id)
    assert {c.name for c in subs} == {"Commodore", "Amiga"}


def test_get_category_by_id(db, creator):
    created = create_category(db, "Vintage Computing", created_by=creator)
    fetched = get_category_by_id(db, created.id)
    assert fetched.name == "Vintage Computing"


def test_get_category_by_name(db, creator):
    create_category(db, "Vintage Computing", created_by=creator)
    fetched = get_category_by_name(db, "Vintage Computing")
    assert fetched.name == "Vintage Computing"


def test_get_nonexistent_category_by_id_fails(db):
    with pytest.raises(CategoryError):
        get_category_by_id(db, 999)


def test_get_nonexistent_category_by_name_fails(db):
    with pytest.raises(CategoryError):
        get_category_by_name(db, "nonexistent")


def test_duplicate_category_name_rejected(db, creator):
    create_category(db, "Vintage Computing", created_by=creator)
    with pytest.raises(CategoryError):
        create_category(db, "Vintage Computing", created_by=creator)


def test_create_subcategory_under_nonexistent_parent_fails(db, creator):
    with pytest.raises(CategoryError):
        create_category(db, "Commodore", parent_category_id=999, created_by=creator)


def test_subcategories_can_share_names_across_different_parents(db, creator):
    """Sub-category names are globally unique (per the UNIQUE constraint
    on the name column), so this specifically confirms two *different*
    top-level categories can't each have an identically-named child --
    documenting the actual current constraint rather than assuming a
    looser per-parent uniqueness that isn't actually implemented."""
    parent_a = create_category(db, "Vintage Computing", created_by=creator)
    create_category(db, "General", parent_category_id=parent_a.id, created_by=creator)
    parent_b = create_category(db, "Politics", created_by=creator)
    with pytest.raises(CategoryError):
        create_category(db, "General", parent_category_id=parent_b.id, created_by=creator)


# -- delete_category (design doc -- channel management round) ------------


def test_delete_category_sets_channels_using_it_to_uncategorized(db, creator):
    category = create_category(db, "Vintage Computing", created_by=creator)
    create_channel(db, "lobby", category_id=category.id, creator=creator)
    delete_category(db, category, deleted_by=creator)
    updated = get_channel_by_name(db, "lobby")
    assert updated.category_id is None


def test_delete_category_sets_child_categories_to_top_level(db, creator):
    parent = create_category(db, "Vintage Computing", created_by=creator)
    child = create_category(db, "Commodore", parent_category_id=parent.id, created_by=creator)
    delete_category(db, parent, deleted_by=creator)
    updated = get_category_by_id(db, child.id)
    assert updated.parent_category_id is None
    assert updated.is_top_level


def test_delete_category_actually_removes_the_row(db, creator):
    category = create_category(db, "Vintage Computing", created_by=creator)
    delete_category(db, category, deleted_by=creator)
    with pytest.raises(CategoryError):
        get_category_by_id(db, category.id)
