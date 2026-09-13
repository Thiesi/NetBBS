"""Tests for netbbs.net.color_depth_preference, the per-user manual
truecolor override -- mirrors tests/test_editor_preference.py's shape
for the analogous fullscreen-editor preference."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.net.color_depth_preference import (
    color_depth_override,
    effective_truecolor,
    set_color_depth_override,
)
from netbbs.net.session import Session
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


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_defaults_to_no_override(db, alice):
    assert color_depth_override(db, alice) is None


def test_can_be_set_to_truecolor(db, alice):
    set_color_depth_override(db, alice, "truecolor")
    assert color_depth_override(db, alice) == "truecolor"


def test_can_be_set_to_256(db, alice):
    set_color_depth_override(db, alice, "256")
    assert color_depth_override(db, alice) == "256"


def test_can_be_reset_to_auto(db, alice):
    set_color_depth_override(db, alice, "truecolor")
    set_color_depth_override(db, alice, "auto")
    assert color_depth_override(db, alice) is None


def test_is_per_user(db, alice):
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_color_depth_override(db, alice, "truecolor")
    assert color_depth_override(db, alice) == "truecolor"
    assert color_depth_override(db, bob) is None


def test_invalid_value_raises(db, alice):
    with pytest.raises(ValueError):
        set_color_depth_override(db, alice, "not-a-real-value")


# -- effective_truecolor -----------------------------------------------------


def test_effective_truecolor_follows_negotiated_value_without_override(db, alice):
    assert effective_truecolor(_FakeSession(True), db, alice) is True
    assert effective_truecolor(_FakeSession(False), db, alice) is False


def test_effective_truecolor_override_wins_over_negotiated_value(db, alice):
    set_color_depth_override(db, alice, "truecolor")
    assert effective_truecolor(_FakeSession(False), db, alice) is True

    set_color_depth_override(db, alice, "256")
    assert effective_truecolor(_FakeSession(True), db, alice) is False
