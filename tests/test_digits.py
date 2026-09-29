"""`is_ascii_number`: the one guard in front of `int()` on typed or received
text (issue #928)."""

from __future__ import annotations

import pytest

from netbbs.digits import is_ascii_number


@pytest.mark.parametrize("text", ["0", "7", "42", "0123456789"])
def test_ascii_digits_are_a_number(text):
    assert is_ascii_number(text)
    int(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " 1",
        "1 ",
        "-1",
        "+1",
        "1.5",
        "1a",
        "²",  # superscript two: isdigit() is True, int() raises
        "1³",
        "①",  # circled one: isdigit() is True, int() raises
        "٣",  # Arabic-Indic three: int() takes it, no prompt here means it
        "４２",  # fullwidth 42
    ],
)
def test_anything_else_is_not(text):
    assert not is_ascii_number(text)
