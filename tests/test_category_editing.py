"""
Categories can be renamed, described, reparented and ordered, and the
console lists each sub-category under its parent (issue #681) -- for
message boards, file areas and chat channels alike.
"""

from __future__ import annotations

import pytest

from netbbs.boards import categories as board_categories
from netbbs.chat import categories as channel_categories
from netbbs.files import categories as file_categories
from tests.test_admin_flow import FakeSession, _run, _visible, _written_text, db, lane, sysop  # noqa: F401 -- fixtures

MODULES = [
    pytest.param((board_categories, board_categories.CategoryError), id="boards"),
    pytest.param((file_categories, file_categories.FileAreaCategoryError), id="file areas"),
    pytest.param((channel_categories, channel_categories.CategoryError), id="channels"),
]


@pytest.mark.parametrize("kind", MODULES)
def test_a_category_is_renamed_described_and_reparented(db, sysop, kind):
    module, _error = kind
    retro = module.create_category(db, "Retro", created_by=sysop)
    news = module.create_category(db, "News", created_by=sysop)

    updated = module.update_category(
        db, news, name="Headlines", description="What happened", parent_category_id=retro.id, changed_by=sysop,
    )

    assert (updated.name, updated.description, updated.parent_category_id) == ("Headlines", "What happened", retro.id)
    assert [c.name for c in module.list_subcategories(db, retro.id)] == ["Headlines"]


@pytest.mark.parametrize("kind", MODULES)
def test_the_two_level_rule_holds_on_edit(db, sysop, kind):
    module, error = kind
    top = module.create_category(db, "Top", created_by=sysop)
    sub = module.create_category(db, "Sub", parent_category_id=top.id, created_by=sysop)
    other = module.create_category(db, "Other", created_by=sysop)

    with pytest.raises(error, match="only two levels"):
        module.update_category(db, other, name="Other", description=None, parent_category_id=sub.id, changed_by=sysop)
    with pytest.raises(error, match="sub-categories of its own"):
        module.update_category(db, top, name="Top", description=None, parent_category_id=other.id, changed_by=sysop)
    with pytest.raises(error, match="its own parent"):
        module.update_category(db, other, name="Other", description=None, parent_category_id=other.id, changed_by=sysop)
    with pytest.raises(error, match="already in use"):
        module.update_category(db, other, name="Top", description=None, parent_category_id=None, changed_by=sysop)


@pytest.mark.parametrize("kind", MODULES)
def test_categories_are_listed_in_the_sysops_order(db, sysop, kind):
    module, _error = kind
    for name in ("Alpha", "Beta", "Gamma"):
        module.create_category(db, name, created_by=sysop)
    gamma = module.get_category_by_name(db, "Gamma")

    assert module.move_category(db, gamma, -1, moved_by=sysop)
    assert module.move_category(db, module.get_category_by_name(db, "Gamma"), -1, moved_by=sysop)
    assert not module.move_category(db, module.get_category_by_name(db, "Gamma"), -1, moved_by=sysop)

    assert [c.name for c in module.list_top_level_categories(db)] == ["Gamma", "Alpha", "Beta"]


@pytest.mark.parametrize("kind", MODULES)
def test_a_moved_category_goes_last_under_its_new_parent(db, sysop, kind):
    module, _error = kind
    top = module.create_category(db, "Top", created_by=sysop)
    module.create_category(db, "Zeta", parent_category_id=top.id, created_by=sysop)
    loose = module.create_category(db, "Alpha", created_by=sysop)

    module.update_category(db, loose, name="Alpha", description=None, parent_category_id=top.id, changed_by=sysop)

    assert [c.name for c in module.list_subcategories(db, top.id)] == ["Zeta", "Alpha"]


def test_the_console_lists_sub_categories_under_their_parent(db, lane, sysop):
    retro = board_categories.create_category(db, "Retro", created_by=sysop)
    board_categories.create_category(db, "Amiga", parent_category_id=retro.id, created_by=sysop)
    board_categories.create_category(db, "News", created_by=sysop)

    session = FakeSession(["m", "c", "m", "l", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))

    listing = text[text.index("page 1/1"):]
    assert listing.index("Retro") < listing.index("- Amiga") < listing.index("News")
    assert "in Retro" in listing


def test_the_console_renames_and_moves_a_category(db, lane, sysop):
    board_categories.create_category(db, "Alpha", created_by=sysop)
    board_categories.create_category(db, "Beta", created_by=sysop)

    # Categories, Message board categories, List, 02 (Beta), Up, then its Name
    # (the cursor starts there, issue #1081), save, back out.
    session = FakeSession([
        "m", "c", "m", "l", "0", "2", "u", "ENTER", "Bravo", "s", "b", "b", "b", "b", "b", "b",
    ])
    _run(session, lane, sysop)

    assert [c.name for c in board_categories.list_top_level_categories(db)] == ["Bravo", "Alpha"]


def test_the_migration_places_existing_categories_in_name_order(db, sysop):
    """Categories made before issue #681 all sat at position 0; the
    migration's backfill keeps the alphabetical order they were shown in."""
    from netbbs.storage.migrations import MIGRATIONS

    top = board_categories.create_category(db, "Top", created_by=sysop)
    for name in ("Zulu", "Alpha", "Mike"):
        board_categories.create_category(db, name, parent_category_id=top.id, created_by=sysop)
    db.connection.execute("UPDATE board_categories SET position = 0")
    # Found by what it is, not where it sits: later migrations follow it.
    [migration] = [m for m in MIGRATIONS if "Issue #681: `position`" in m.description]
    backfill = [statement for statement in migration.sql.split(";") if "UPDATE board_categories" in statement]
    db.connection.executescript(backfill[0] + ";")

    assert [c.name for c in board_categories.list_subcategories(db, top.id)] == ["Alpha", "Mike", "Zulu"]


# -- Codex review on #799 ----------------------------------------------


@pytest.mark.parametrize("kind", MODULES)
def test_deleting_a_parent_puts_its_children_last_in_their_order(db, sysop, kind):
    module, _error = kind
    first = module.create_category(db, "First", created_by=sysop)
    parent = module.create_category(db, "Parent", created_by=sysop)
    module.create_category(db, "Zulu", parent_category_id=parent.id, created_by=sysop)
    module.create_category(db, "Alpha", parent_category_id=parent.id, created_by=sysop)
    module.create_category(db, "Last", created_by=sysop)
    assert first.position == 0

    module.delete_category(db, module.get_category_by_name(db, "Parent"), deleted_by=sysop)

    assert [c.name for c in module.list_top_level_categories(db)] == ["First", "Last", "Zulu", "Alpha"]


@pytest.mark.parametrize("kind", MODULES)
def test_an_edit_in_place_keeps_the_stored_position(db, sysop, kind):
    module, _error = kind
    module.create_category(db, "Alpha", created_by=sysop)
    beta = module.create_category(db, "Beta", created_by=sysop)
    module.move_category(db, beta, -1, moved_by=sysop)

    # `beta` is a copy from before the move.
    module.update_category(db, beta, name="Bravo", description=None, parent_category_id=None, changed_by=sysop)

    assert [c.name for c in module.list_top_level_categories(db)] == ["Bravo", "Alpha"]


def test_the_category_screen_follows_a_category_moved_under_a_parent(db, lane, sysop):
    board_categories.create_category(db, "Retro", created_by=sysop)
    board_categories.create_category(db, "Amiga", created_by=sysop)

    # List, 02 (Amiga, made second), its Parent field, pick Retro, Save: still Amiga's screen.
    session = FakeSession([
        "m", "c", "m", "l", "0", "2", "DOWN", "DOWN", "ENTER", "0", "2", "s", "b", "b", "b", "b", "b", "b",
    ])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))

    # The screen drawn with the save's outcome is still Amiga's, now under Retro.
    screen = text.split("Saved category 'Amiga'.", 1)[0]
    assert "Retro" in screen.rsplit("Parent:", 1)[1].splitlines()[0]



@pytest.mark.parametrize("kind", MODULES)
def test_an_edit_from_a_stale_copy_still_moves_the_category(db, sysop, kind):
    """Codex review on #799: the requested parent is compared with the
    stored one, not with the caller's copy."""
    module, _error = kind
    parent = module.create_category(db, "Parent", created_by=sysop)
    stale = module.create_category(db, "Loose", created_by=sysop)
    module.update_category(db, stale, name="Loose", description=None, parent_category_id=parent.id, changed_by=sysop)

    # `stale` still says top-level; the SysOp asks for top-level again.
    module.update_category(db, stale, name="Loose", description=None, parent_category_id=None, changed_by=sysop)

    assert module.get_category_by_name(db, "Loose").parent_category_id is None


@pytest.mark.parametrize("kind", MODULES)
def test_a_move_from_a_stale_copy_moves_it_among_its_current_siblings(db, sysop, kind):
    module, _error = kind
    parent = module.create_category(db, "Parent", created_by=sysop)
    module.create_category(db, "First", parent_category_id=parent.id, created_by=sysop)
    stale = module.create_category(db, "Second", created_by=sysop)
    module.update_category(db, stale, name="Second", description=None, parent_category_id=parent.id, changed_by=sysop)

    assert module.move_category(db, stale, -1, moved_by=sysop)

    assert [c.name for c in module.list_subcategories(db, parent.id)] == ["Second", "First"]


def test_ctrl_h_on_the_categories_screen_tells_categories_from_communities(db, lane, sysop):
    """Issue #838 (F030): both words sat on one menu with nothing saying
    how they differ, and this screen answered Ctrl-H with nothing."""
    session = FakeSession(["m", "c", "\x08", " ", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))

    assert "Ctrl-H: categories vs. Communities" in text
    assert "Categories help" in text
    assert "A topic that holds every kind at once" in text


def test_the_content_screen_says_what_categories_and_communities_are(db, lane, sysop):
    session = FakeSession(["m", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))

    assert "Group lists of one kind" in text
    assert "Topics holding every kind" in text
