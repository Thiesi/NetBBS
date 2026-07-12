"""Tests for netbbs.files.categories — two-level category hierarchy,
mirroring test_board_categories.py."""

from __future__ import annotations

import pytest

from netbbs.files.categories import (
    FileAreaCategoryError,
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
    category = create_category(db, "Documents")
    assert category.name == "Documents"
    assert category.is_top_level


def test_create_subcategory(db):
    parent = create_category(db, "Documents")
    child = create_category(db, "Manuals", parent_category_id=parent.id)
    assert not child.is_top_level
    assert child.parent_category_id == parent.id


def test_third_level_nesting_rejected(db):
    parent = create_category(db, "Documents")
    child = create_category(db, "Manuals", parent_category_id=parent.id)
    with pytest.raises(FileAreaCategoryError):
        create_category(db, "Printers", parent_category_id=child.id)


def test_list_top_level_categories_excludes_subcategories(db):
    top = create_category(db, "Documents")
    create_category(db, "Manuals", parent_category_id=top.id)
    create_category(db, "Utilities")
    tops = list_top_level_categories(db)
    assert {c.name for c in tops} == {"Documents", "Utilities"}


def test_list_subcategories(db):
    parent = create_category(db, "Documents")
    create_category(db, "Manuals", parent_category_id=parent.id)
    create_category(db, "Forms", parent_category_id=parent.id)
    other_parent = create_category(db, "Utilities")
    create_category(db, "Compression", parent_category_id=other_parent.id)

    subs = list_subcategories(db, parent.id)
    assert {c.name for c in subs} == {"Manuals", "Forms"}


def test_get_category_by_id(db):
    created = create_category(db, "Documents")
    fetched = get_category_by_id(db, created.id)
    assert fetched.name == "Documents"


def test_get_category_by_name(db):
    create_category(db, "Documents")
    fetched = get_category_by_name(db, "Documents")
    assert fetched.name == "Documents"


def test_get_nonexistent_category_by_id_fails(db):
    with pytest.raises(FileAreaCategoryError):
        get_category_by_id(db, 999)


def test_get_nonexistent_category_by_name_fails(db):
    with pytest.raises(FileAreaCategoryError):
        get_category_by_name(db, "nonexistent")


def test_duplicate_category_name_rejected(db):
    create_category(db, "Documents")
    with pytest.raises(FileAreaCategoryError):
        create_category(db, "Documents")


def test_create_subcategory_under_nonexistent_parent_fails(db):
    with pytest.raises(FileAreaCategoryError):
        create_category(db, "Manuals", parent_category_id=999)
