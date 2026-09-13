"""Tests for netbbs.net.node_theme -- the node-wide accent/header/clock
identity-color override (issue #162). Mirrors
tests/test_color_depth_preference.py's shape for the analogous
per-user truecolor override, except this one is node-wide, not
per-user, and stores a caller-chosen RGB triple rather than a fixed
enum of values."""

from __future__ import annotations

import pytest

from netbbs.net.node_theme import (
    accent_color_override,
    clock_color_override,
    effective_accent_color,
    effective_clock_color,
    effective_header_color,
    effective_node_name_gradient,
    header_color_override,
    node_name_gradient_override,
    set_accent_color_override,
    set_clock_color_override,
    set_header_color_override,
    set_node_name_gradient_override,
)
from netbbs.net.session import Session
from netbbs.rendering import ACCENT_COLOR, CLOCK_COLOR, HEADER_COLOR
from netbbs.storage.database import Database


class _FakeSession(Session):
    def __init__(self, supports_truecolor: bool):
        self.supports_truecolor = supports_truecolor
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = None

    async def write(self, text: str) -> None:
        raise NotImplementedError

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        raise NotImplementedError

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self):
        raise NotImplementedError

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


# -- accent --------------------------------------------------------------


def test_accent_defaults_to_no_override(db):
    assert accent_color_override(db) is None


def test_accent_can_be_set_and_read_back(db):
    set_accent_color_override(db, (10, 20, 30))
    assert accent_color_override(db) == (10, 20, 30)


def test_accent_can_be_cleared(db):
    set_accent_color_override(db, (10, 20, 30))
    set_accent_color_override(db, None)
    assert accent_color_override(db) is None


def test_accent_rejects_out_of_range_components(db):
    with pytest.raises(ValueError):
        set_accent_color_override(db, (256, 0, 0))
    with pytest.raises(ValueError):
        set_accent_color_override(db, (0, -1, 0))


def test_effective_accent_color_falls_back_to_theme_default_when_unset(db):
    assert effective_accent_color(_FakeSession(True), db) == ACCENT_COLOR
    assert effective_accent_color(_FakeSession(False), db) == ACCENT_COLOR


def test_effective_accent_color_returns_raw_rgb_for_a_truecolor_session(db):
    set_accent_color_override(db, (10, 20, 30))
    assert effective_accent_color(_FakeSession(True), db) == (10, 20, 30)


def test_effective_accent_color_downgrades_to_256_for_a_non_truecolor_session(db):
    set_accent_color_override(db, (10, 20, 30))
    result = effective_accent_color(_FakeSession(False), db)
    assert isinstance(result, int)
    assert result != ACCENT_COLOR  # a real downgraded index, not the untouched default


# -- header ---------------------------------------------------------------


def test_header_defaults_to_no_override(db):
    assert header_color_override(db) is None


def test_header_can_be_set_cleared_and_resolved(db):
    set_header_color_override(db, (1, 2, 3))
    assert header_color_override(db) == (1, 2, 3)
    assert effective_header_color(_FakeSession(True), db) == (1, 2, 3)

    set_header_color_override(db, None)
    assert effective_header_color(_FakeSession(True), db) == HEADER_COLOR


# -- clock ------------------------------------------------------------------


def test_clock_defaults_to_no_override(db):
    assert clock_color_override(db) is None


def test_clock_can_be_set_cleared_and_resolved(db):
    set_clock_color_override(db, (4, 5, 6))
    assert clock_color_override(db) == (4, 5, 6)
    assert effective_clock_color(_FakeSession(True), db) == (4, 5, 6)

    set_clock_color_override(db, None)
    assert effective_clock_color(_FakeSession(True), db) == CLOCK_COLOR


# -- node-name gradient (issue #175) -----------------------------------


def test_node_name_gradient_defaults_to_no_override(db):
    assert node_name_gradient_override(db) is None


def test_node_name_gradient_can_be_set_and_read_back(db):
    set_node_name_gradient_override(db, "rainbow")
    assert node_name_gradient_override(db) == "rainbow"


def test_node_name_gradient_can_be_cleared(db):
    set_node_name_gradient_override(db, "rainbow")
    set_node_name_gradient_override(db, None)
    assert node_name_gradient_override(db) is None


def test_node_name_gradient_rejects_an_unknown_gradient_name(db):
    with pytest.raises(ValueError):
        set_node_name_gradient_override(db, "not-a-real-gradient")


def test_effective_node_name_gradient_falls_back_to_none_when_unset(db):
    assert effective_node_name_gradient(db) is None


def test_effective_node_name_gradient_returns_the_override_once_set(db):
    set_node_name_gradient_override(db, "gold")
    assert effective_node_name_gradient(db) == "gold"


# -- independence ------------------------------------------------------------


def test_the_three_slots_are_independent(db):
    set_accent_color_override(db, (1, 1, 1))
    assert header_color_override(db) is None
    assert clock_color_override(db) is None


def test_node_name_gradient_is_independent_of_the_three_rgb_slots(db):
    set_accent_color_override(db, (1, 1, 1))
    set_header_color_override(db, (2, 2, 2))
    set_clock_color_override(db, (3, 3, 3))
    assert node_name_gradient_override(db) is None

    set_node_name_gradient_override(db, "red")
    assert accent_color_override(db) == (1, 1, 1)
    assert header_color_override(db) == (2, 2, 2)
    assert clock_color_override(db) == (3, 3, 3)
