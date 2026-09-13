"""One answer for an empty submit, across a screen (issue #557).

Issue #529 changed what an empty submit means on the resource Create/Edit
screens: a field opens on its current value, Enter saves what is shown,
Escape leaves it alone, and an emptied line clears the field. The text
fields did that. The gate and level fields on the *same screen* kept the
old "blank = keep", and said so in their own prompt text.

Nothing was broken -- typing `none` cleared a gate, and the prompt said
so. The problem was the direction the mismatch failed in. A SysOp learns
the convention on whichever field they meet first; applied to an age
gate, "clear the line to clear the value" silently left the gate in
place, and the screen afterwards looked exactly like one where it had
worked, because the value was never on the line to begin with.

These tests hold the one convention across every value-field prompt in
`netbbs.net.admin_flow`, so a future field cannot quietly reintroduce a
second one.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.admin_flow import _int_field, _prompt_min_age, _prompt_optional_int, _read_int
from netbbs.net.char_input import InputCancelled


class FakeSession:
    """Records what was seeded into the line, which is the half of this
    convention a caller actually sees."""

    def __init__(self, answer: str = "", *, cancel: bool = False):
        self._answer = answer
        self._cancel = cancel
        self.written: list[str] = []
        self.seeded: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, *, initial: str = "", cancellable: bool = False, **kwargs) -> str:
        self.seeded.append(initial)
        if self._cancel:
            assert cancellable, "Escape can only be pressed at a prompt that accepts it"
            raise InputCancelled()
        return self._answer

    @property
    def output(self) -> str:
        return "".join(self.written)


# -- the prompt opens on the value it is editing -----------------------


def test_every_prompt_seeds_the_current_value():
    """Without this the rest of the convention cannot hold: an emptied
    line only means "clear" if the line started with something in it."""
    age = FakeSession("18")
    asyncio.run(_prompt_min_age(age, current=21))
    assert age.seeded == ["21"]

    level = FakeSession("5")
    asyncio.run(_prompt_optional_int(level, "Minimum read level", current=100))
    assert level.seeded == ["100"]

    plain = FakeSession("7")
    asyncio.run(_read_int(plain, default=3))
    assert plain.seeded == ["3"]


def test_an_unset_optional_value_seeds_an_empty_line():
    """"none" is a word the prompt accepts, not something to put in the
    buffer -- seeding it would make Enter-on-an-untouched-prompt submit
    the literal string."""
    age = FakeSession("")
    asyncio.run(_prompt_min_age(age, current=None))
    assert age.seeded == [""]

    level = FakeSession("")
    asyncio.run(_prompt_optional_int(level, "Minimum read level", current=None))
    assert level.seeded == [""]


# -- an emptied line clears ---------------------------------------------


@pytest.mark.parametrize("answer", ["", "none", "NONE"])
def test_an_emptied_line_clears_an_optional_value(answer):
    assert asyncio.run(_prompt_min_age(FakeSession(answer), current=21)) == (None, True)
    assert asyncio.run(
        _prompt_optional_int(FakeSession(answer), "Minimum read level", current=100)
    ) == (None, True)


# -- Escape keeps -------------------------------------------------------


def test_escape_leaves_every_kind_of_field_alone():
    """What "blank" used to mean, on a key of its own instead of
    overloaded onto the empty string."""
    assert asyncio.run(_prompt_min_age(FakeSession(cancel=True), current=21)) == (21, True)
    assert asyncio.run(
        _prompt_optional_int(FakeSession(cancel=True), "Minimum read level", current=100)
    ) == (100, True)
    assert asyncio.run(_read_int(FakeSession(cancel=True), default=3)) == 3


# -- no prompt advertises the old convention ----------------------------


def test_no_prompt_still_says_blank_equals_keep():
    """The wording is the part a SysOp reads before deciding what to
    press, so it is worth holding directly rather than inferring from
    behaviour."""
    prompts = [
        FakeSession("18"),
        FakeSession("5"),
        FakeSession("7"),
    ]
    asyncio.run(_prompt_min_age(prompts[0], current=21))
    asyncio.run(_prompt_optional_int(prompts[1], "Minimum read level", current=100))
    # `_read_int` is the reader; `_int_field` is what writes the prompt
    # a caller reads, so the wording is held where it is actually said.
    asyncio.run(_int_field("min_level", "Minimum level")(prompts[2], None, {"min_level": 3}))
    for session in prompts:
        assert "blank = keep" not in session.output, session.output
        assert "Esc" in session.output, session.output
