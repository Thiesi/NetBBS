"""Tests for services.managed_dns.names (issue #201)."""

from __future__ import annotations

import pytest

from services.managed_dns.names import InvalidNameError, normalize_name


def test_normalize_name_lowercases():
    assert normalize_name("MyBoard") == "myboard"


def test_normalize_name_strips_surrounding_whitespace():
    assert normalize_name("  myboard  ") == "myboard"


@pytest.mark.parametrize("name", ["myboard", "my-board", "board2", "a", "a" * 63])
def test_normalize_name_accepts_valid_labels(name):
    assert normalize_name(name) == name.lower()


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "-myboard",
        "myboard-",
        "my board",
        "my_board",
        "my.board",
        "a" * 64,
    ],
)
def test_normalize_name_rejects_invalid_labels(name):
    with pytest.raises(InvalidNameError):
        normalize_name(name)
