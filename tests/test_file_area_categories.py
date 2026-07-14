"""Tests for netbbs.files.categories — two-level category hierarchy,
mirroring test_board_categories.py."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.files.areas import create_file_area, get_file_area_by_name
from netbbs.files.categories import (
    FileAreaCategoryError,
    create_category,
    delete_category,
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


@pytest.fixture
def creator(db):
    return create_user(db, "creator", password="hunter2", user_level=10)


def test_create_top_level_category(db, creator):
    category = create_category(db, "Documents", created_by=creator)
    assert category.name == "Documents"
    assert category.is_top_level


def test_create_subcategory(db, creator):
    parent = create_category(db, "Documents", created_by=creator)
    child = create_category(db, "Manuals", parent_category_id=parent.id, created_by=creator)
    assert not child.is_top_level
    assert child.parent_category_id == parent.id


def test_third_level_nesting_rejected(db, creator):
    parent = create_category(db, "Documents", created_by=creator)
    child = create_category(db, "Manuals", parent_category_id=parent.id, created_by=creator)
    with pytest.raises(FileAreaCategoryError):
        create_category(db, "Printers", parent_category_id=child.id, created_by=creator)


def test_list_top_level_categories_excludes_subcategories(db, creator):
    top = create_category(db, "Documents", created_by=creator)
    create_category(db, "Manuals", parent_category_id=top.id, created_by=creator)
    create_category(db, "Utilities", created_by=creator)
    tops = list_top_level_categories(db)
    assert {c.name for c in tops} == {"Documents", "Utilities"}


def test_list_subcategories(db, creator):
    parent = create_category(db, "Documents", created_by=creator)
    create_category(db, "Manuals", parent_category_id=parent.id, created_by=creator)
    create_category(db, "Forms", parent_category_id=parent.id, created_by=creator)
    other_parent = create_category(db, "Utilities", created_by=creator)
    create_category(db, "Compression", parent_category_id=other_parent.id, created_by=creator)

    subs = list_subcategories(db, parent.id)
    assert {c.name for c in subs} == {"Manuals", "Forms"}


def test_get_category_by_id(db, creator):
    created = create_category(db, "Documents", created_by=creator)
    fetched = get_category_by_id(db, created.id)
    assert fetched.name == "Documents"


def test_get_category_by_name(db, creator):
    create_category(db, "Documents", created_by=creator)
    fetched = get_category_by_name(db, "Documents")
    assert fetched.name == "Documents"


def test_get_nonexistent_category_by_id_fails(db):
    with pytest.raises(FileAreaCategoryError):
        get_category_by_id(db, 999)


def test_get_nonexistent_category_by_name_fails(db):
    with pytest.raises(FileAreaCategoryError):
        get_category_by_name(db, "nonexistent")


def test_duplicate_category_name_rejected(db, creator):
    create_category(db, "Documents", created_by=creator)
    with pytest.raises(FileAreaCategoryError):
        create_category(db, "Documents", created_by=creator)


def test_create_subcategory_under_nonexistent_parent_fails(db, creator):
    with pytest.raises(FileAreaCategoryError):
        create_category(db, "Manuals", parent_category_id=999, created_by=creator)


# -- delete_category (design doc -- board/area management round) ----------


def test_delete_category_sets_areas_using_it_to_uncategorized(db, creator):
    category = create_category(db, "Documents", created_by=creator)
    create_file_area(db, "docs", category_id=category.id, creator=creator)
    delete_category(db, category, deleted_by=creator)
    updated = get_file_area_by_name(db, "docs")
    assert updated.category_id is None


def test_delete_category_sets_child_categories_to_top_level(db, creator):
    parent = create_category(db, "Documents", created_by=creator)
    child = create_category(db, "Manuals", parent_category_id=parent.id, created_by=creator)
    delete_category(db, parent, deleted_by=creator)
    updated = get_category_by_id(db, child.id)
    assert updated.parent_category_id is None
    assert updated.is_top_level


def test_delete_category_actually_removes_the_row(db, creator):
    category = create_category(db, "Documents", created_by=creator)
    delete_category(db, category, deleted_by=creator)
    with pytest.raises(FileAreaCategoryError):
        get_category_by_id(db, category.id)
