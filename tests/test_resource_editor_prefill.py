"""What a pre-filled field prompt does with Enter, an emptied line, and
Escape (issue #529).

The convention this replaces mattered enough to be written down: every
edit screen used to treat an empty submit as "keep the current value",
because the prompt opened empty and Enter on an untouched prompt was the
natural way to skip a field. With the value already in the buffer, an
empty result can only mean the caller deleted it on purpose -- so
clearing gets the empty string, and "leave it alone" moves onto Escape,
which is its own key rather than an overload of the empty string.
"""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import InputCancelled
from netbbs.net.resource_editor import text_field


class FakeSession:
    """Records what `read_line` was offered, and answers with a script."""

    def __init__(self, answer=None, *, cancel: bool = False):
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.offered_initial: str | None = None
        self.offered_cancellable: bool | None = None
        self._answer = answer
        self._cancel = cancel

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, *, initial: str = "", cancellable: bool = False, **kw) -> str:
        self.offered_initial = initial
        self.offered_cancellable = cancellable
        if self._cancel:
            raise InputCancelled
        return self._answer

    @property
    def output(self) -> str:
        return "".join(self.written)


def _edit(draft, answer=None, *, cancel: bool = False, key: str = "description"):
    session = FakeSession(answer, cancel=cancel)
    asyncio.run(text_field(key)(session, None, draft))
    return session


def test_the_prompt_opens_on_the_current_value():
    """The reported complaint: a long description had to be retyped in
    its entirety to change one word."""
    draft = {"description": "Weekly release builds"}
    session = _edit(draft, "Weekly release builds, signed")
    assert session.offered_initial == "Weekly release builds"


def test_enter_saves_whatever_is_shown():
    draft = {"description": "Weekly release builds"}
    _edit(draft, "Weekly release builds, signed")
    assert draft["description"] == "Weekly release builds, signed"


def test_an_unchanged_value_submitted_as_is_stays_put():
    draft = {"description": "Weekly release builds"}
    _edit(draft, "Weekly release builds")
    assert draft["description"] == "Weekly release builds"


def test_an_emptied_line_clears_the_value():
    """The convention change. This used to mean "keep", which would now
    mean a caller who selected all and deleted watched the old value
    reappear."""
    draft = {"description": "Weekly release builds"}
    _edit(draft, "")
    assert draft["description"] == ""


def test_escape_leaves_the_draft_untouched():
    """What "keep" actually meant, now on its own key."""
    draft = {"description": "Weekly release builds"}
    session = _edit(draft, cancel=True)
    assert draft["description"] == "Weekly release builds"
    assert session.offered_cancellable is True


def test_a_field_with_no_value_yet_opens_empty():
    """A fresh create draft has nothing to pre-fill, and must not offer
    the string "None"."""
    draft = {"description": None}
    session = _edit(draft, "A new area")
    assert session.offered_initial == ""
    assert draft["description"] == "A new area"


def test_surrounding_whitespace_is_still_stripped():
    draft = {"description": "old"}
    _edit(draft, "   padded   ")
    assert draft["description"] == "padded"


def test_the_prompt_says_which_keys_do_what():
    """The rule changed, so the prompt has to teach it rather than
    leave a caller guessing whether Enter keeps or clears."""
    draft = {"description": "old"}
    session = _edit(draft, "new")
    assert "Enter" in session.output and "Esc" in session.output
