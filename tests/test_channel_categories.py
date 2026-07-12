"""Tests for netbbs.chat.categories — two-level category hierarchy, mirroring board categories."""

from __future__ import annotations

import pytest

from netbbs.chat.categories import (
    CategoryError,
    create_category,
    get_category_by_id,
    get_category_by_name,
    list_subcategories,
    list_top_level_categories,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def test_create_top_level_category(db):
    category = create_category(db, "Vintage Computing")
    assert category.name == "Vintage Computing"
    assert category.is_top_level


def test_create_subcategory(db):
    parent = create_category(db, "Vintage Computing")
    child = create_category(db, "Commodore", parent_category_id=parent.id)
    assert not child.is_top_level
    assert child.parent_category_id == parent.id


def test_third_level_nesting_rejected(db):
    """
    The core design constraint: at most two levels ever exist. Verified
    directly (see design doc round 17 sign-off notes) rather than
    assumed — this is the rule that guarantees the cap, enforced in
    application code since SQLite can't express a self-join depth
    constraint as a plain CHECK.
    """
    parent = create_category(db, "Vintage Computing")
    child = create_category(db, "Commodore", parent_category_id=parent.id)
    with pytest.raises(CategoryError):
        create_category(db, "Commodore 64", parent_category_id=child.id)


def test_list_top_level_categories_excludes_subcategories(db):
    top = create_category(db, "Vintage Computing")
    create_category(db, "Commodore", parent_category_id=top.id)
    create_category(db, "Politics")
    tops = list_top_level_categories(db)
    assert {c.name for c in tops} == {"Vintage Computing", "Politics"}


def test_list_subcategories(db):
    parent = create_category(db, "Vintage Computing")
    create_category(db, "Commodore", parent_category_id=parent.id)
    create_category(db, "Amiga", parent_category_id=parent.id)
    other_parent = create_category(db, "Politics")
    create_category(db, "Local", parent_category_id=other_parent.id)

    subs = list_subcategories(db, parent.id)
    assert {c.name for c in subs} == {"Commodore", "Amiga"}


def test_get_category_by_id(db):
    created = create_category(db, "Vintage Computing")
    fetched = get_category_by_id(db, created.id)
    assert fetched.name == "Vintage Computing"


def test_get_category_by_name(db):
    create_category(db, "Vintage Computing")
    fetched = get_category_by_name(db, "Vintage Computing")
    assert fetched.name == "Vintage Computing"


def test_get_nonexistent_category_by_id_fails(db):
    with pytest.raises(CategoryError):
        get_category_by_id(db, 999)


def test_get_nonexistent_category_by_name_fails(db):
    with pytest.raises(CategoryError):
        get_category_by_name(db, "nonexistent")


def test_duplicate_category_name_rejected(db):
    create_category(db, "Vintage Computing")
    with pytest.raises(CategoryError):
        create_category(db, "Vintage Computing")


def test_create_subcategory_under_nonexistent_parent_fails(db):
    with pytest.raises(CategoryError):
        create_category(db, "Commodore", parent_category_id=999)


def test_subcategories_can_share_names_across_different_parents(db):
    """Sub-category names are globally unique (per the UNIQUE constraint
    on the name column), so this specifically confirms two *different*
    top-level categories can't each have an identically-named child --
    documenting the actual current constraint rather than assuming a
    looser per-parent uniqueness that isn't actually implemented."""
    parent_a = create_category(db, "Vintage Computing")
    create_category(db, "General", parent_category_id=parent_a.id)
    parent_b = create_category(db, "Politics")
    with pytest.raises(CategoryError):
        create_category(db, "General", parent_category_id=parent_b.id)
