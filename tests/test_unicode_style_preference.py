"""Tests for netbbs.net.unicode_style_preference: the per-user character
set preference (issue #929) and the decoration style derived from it."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.net.unicode_style_preference import (
    apply_charset_preference,
    charset_preference,
    charset_preference_ever_set,
    effective_charset,
    set_charset_preference,
    set_unicode_style_enabled,
    unicode_style_enabled,
)
from netbbs.rendering.charset import ASCII, CP437, UTF8
from netbbs.storage.database import Database
from netbbs.user_preferences import set_user_preference


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_defaults_to_auto_with_decoration(db, alice):
    assert charset_preference(db, alice) == "auto"
    assert unicode_style_enabled(db, alice) is True
    assert charset_preference_ever_set(db, alice) is False


@pytest.mark.parametrize("value", ["auto", "unicode", "cp437", "ascii"])
def test_every_choice_is_kept_per_user(db, alice, value):
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_charset_preference(db, alice, value)
    assert charset_preference(db, alice) == value
    assert charset_preference(db, bob) == "auto"
    assert unicode_style_enabled(db, alice) is (value != "ascii")
    assert charset_preference_ever_set(db, alice) is True


def test_an_unknown_value_is_refused(db, alice):
    with pytest.raises(ValueError):
        set_charset_preference(db, alice, "latin1")


def test_the_old_style_switched_off_reads_as_ascii(db, alice):
    set_user_preference(db, alice, "unicode_style", "off")
    assert charset_preference(db, alice) == "ascii"
    assert charset_preference_ever_set(db, alice) is True


def test_the_old_style_left_on_reads_as_auto_and_is_not_a_choice(db, alice):
    set_user_preference(db, alice, "unicode_style", "on")
    assert charset_preference(db, alice) == "auto"
    assert charset_preference_ever_set(db, alice) is False


def test_the_two_way_view(db, alice):
    set_unicode_style_enabled(db, alice, False)
    assert charset_preference(db, alice) == "ascii"
    set_unicode_style_enabled(db, alice, True)
    assert charset_preference(db, alice) == "auto"


class _Session:
    def __init__(self, transport_name="telnet", detected=CP437):
        self.transport_name = transport_name
        self.detected_charset = detected
        self.output_charset = detected


@pytest.mark.parametrize(
    ("preference", "expected"),
    [("auto", CP437), ("unicode", UTF8), ("cp437", CP437), ("ascii", ASCII)],
)
def test_an_explicit_choice_beats_detection(preference, expected):
    assert effective_charset(preference, _Session(detected=CP437)) == expected


@pytest.mark.parametrize(("preference", "expected"), [("auto", UTF8), ("cp437", UTF8), ("ascii", ASCII)])
def test_the_browser_reads_utf8_whatever_the_account_says(preference, expected):
    assert effective_charset(preference, _Session(transport_name="web", detected=UTF8)) == expected


def test_going_back_to_auto_restores_what_was_detected():
    session = _Session(detected=CP437)
    apply_charset_preference(session, "ascii")
    assert session.output_charset == ASCII
    apply_charset_preference(session, "auto")
    assert session.output_charset == CP437
