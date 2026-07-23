"""Tests for netbbs.directory — vCard bio/visibility and finger-style
lookup (design doc §13)."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user, list_users
from netbbs.directory import MAX_BIO_BYTES, BioError, get_bio, get_vcard, is_bio_visible, set_bio, set_bio_visible
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


# -- bio ------------------------------------------------------------------


def test_get_bio_none_when_unset(db, alice):
    assert get_bio(db, alice) is None


def test_set_then_get_bio(db, alice):
    set_bio(db, alice, "Hi, I'm Alice.\nI like vintage computing.")
    assert get_bio(db, alice) == "Hi, I'm Alice.\nI like vintage computing."


def test_set_bio_rejects_more_than_six_lines(db, alice):
    seven_lines = "\n".join(f"line {i}" for i in range(7))
    with pytest.raises(BioError):
        set_bio(db, alice, seven_lines)


def test_set_bio_allows_exactly_six_lines(db, alice):
    six_lines = "\n".join(f"line {i}" for i in range(6))
    set_bio(db, alice, six_lines)  # must not raise
    assert get_bio(db, alice) == six_lines


def test_set_bio_rejects_a_single_huge_line(db, alice):
    """Regression test for GitHub issue #32: the 6-line cap alone
    doesn't bound a bio's size -- a single line can still be
    arbitrarily long."""
    huge_line = "x" * (MAX_BIO_BYTES + 1)
    with pytest.raises(BioError):
        set_bio(db, alice, huge_line)


def test_set_bio_allows_exactly_max_bytes(db, alice):
    exactly_at_limit = "x" * MAX_BIO_BYTES
    set_bio(db, alice, exactly_at_limit)  # must not raise
    assert get_bio(db, alice) == exactly_at_limit


# -- visibility -------------------------------------------------------------


def test_bio_visibility_defaults_to_hidden(db, alice):
    assert is_bio_visible(db, alice) is False


def test_set_bio_visible_true(db, alice):
    set_bio_visible(db, alice, True)
    assert is_bio_visible(db, alice) is True


def test_set_bio_visible_false_after_true(db, alice):
    set_bio_visible(db, alice, True)
    set_bio_visible(db, alice, False)
    assert is_bio_visible(db, alice) is False


# -- get_vcard / finger -----------------------------------------------------


def test_vcard_hides_bio_from_others_by_default(db, alice, bob):
    set_bio(db, alice, "Secret bio")
    vcard = get_vcard(db, alice, requesting_user=bob)
    assert vcard.bio is None
    assert vcard.bio_visible is False


def test_vcard_shows_bio_to_others_when_visible(db, alice, bob):
    set_bio(db, alice, "Public bio")
    set_bio_visible(db, alice, True)
    vcard = get_vcard(db, alice, requesting_user=bob)
    assert vcard.bio == "Public bio"
    assert vcard.bio_visible is True


def test_vcard_always_shows_bio_to_self_even_when_hidden(db, alice):
    set_bio(db, alice, "Secret bio")
    vcard = get_vcard(db, alice, requesting_user=alice)
    assert vcard.bio == "Secret bio"


def test_vcard_bio_none_when_never_set_even_if_visible(db, alice, bob):
    set_bio_visible(db, alice, True)
    vcard = get_vcard(db, alice, requesting_user=bob)
    assert vcard.bio is None


def test_vcard_includes_username_and_created_at(db, alice, bob):
    vcard = get_vcard(db, alice, requesting_user=bob)
    assert vcard.username == "alice"
    assert vcard.created_at == alice.created_at


# -- list_users (directory's underlying listing) ---------------------------


def test_list_users_returns_all_ordered_by_username(db):
    create_user(db, "zed", password="hunter2", user_level=10)
    create_user(db, "amy", password="hunter2", user_level=10)
    create_user(db, "mike", password="hunter2", user_level=10)
    usernames = [u.username for u in list_users(db)]
    assert usernames == ["amy", "mike", "zed"]


def test_list_users_case_insensitive_order(db):
    create_user(db, "Zebra", password="hunter2", user_level=10)
    create_user(db, "apple", password="hunter2", user_level=10)
    usernames = [u.username for u in list_users(db)]
    assert usernames == ["apple", "Zebra"]


def test_list_users_empty_when_no_users(db):
    assert list_users(db) == []
