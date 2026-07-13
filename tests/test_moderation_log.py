"""Tests for netbbs.moderation.log."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.moderation import list_actions_for_object, list_actions_for_target_user, record_action
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


# -- recording ----------------------------------------------------------


def test_record_action_returns_populated_entry(db, sysop, alice):
    entry = record_action(
        db,
        actor=sysop,
        action="grant",
        object_type="board",
        object_id=1,
        target_user_id=alice.id,
        detail="EDIT,APPROVE",
    )
    assert entry.actor_user_id == sysop.id
    assert entry.action == "grant"
    assert entry.object_type == "board"
    assert entry.object_id == 1
    assert entry.target_user_id == alice.id
    assert entry.detail == "EDIT,APPROVE"
    assert entry.created_at


def test_record_action_allows_all_optional_fields_absent(db, sysop):
    entry = record_action(db, actor=sysop, action="node-config-change")
    assert entry.object_type is None
    assert entry.object_id is None
    assert entry.target_user_id is None
    assert entry.detail is None


# -- querying by object ---------------------------------------------------


def test_list_actions_for_object_returns_matching_entries_in_order(db, sysop, alice):
    record_action(db, actor=sysop, action="grant", object_type="board", object_id=1, target_user_id=alice.id)
    record_action(db, actor=sysop, action="revoke", object_type="board", object_id=1, target_user_id=alice.id)
    entries = list_actions_for_object(db, "board", 1)
    assert [e.action for e in entries] == ["grant", "revoke"]


def test_list_actions_for_object_excludes_other_objects(db, sysop, alice):
    record_action(db, actor=sysop, action="grant", object_type="board", object_id=1, target_user_id=alice.id)
    record_action(db, actor=sysop, action="grant", object_type="board", object_id=2, target_user_id=alice.id)
    entries = list_actions_for_object(db, "board", 1)
    assert len(entries) == 1


def test_list_actions_for_object_empty_when_nothing_recorded(db):
    assert list_actions_for_object(db, "board", 999) == []


# -- querying by target user -----------------------------------------------


def test_list_actions_for_target_user_returns_matching_entries(db, sysop, alice):
    record_action(db, actor=sysop, action="grant", object_type="board", object_id=1, target_user_id=alice.id)
    record_action(db, actor=sysop, action="mute", object_type="channel", object_id=1, target_user_id=alice.id)
    entries = list_actions_for_target_user(db, alice.id)
    assert [e.action for e in entries] == ["grant", "mute"]


def test_list_actions_for_target_user_excludes_other_users(db, sysop, alice):
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    record_action(db, actor=sysop, action="grant", object_type="board", object_id=1, target_user_id=alice.id)
    entries = list_actions_for_target_user(db, bob.id)
    assert entries == []
