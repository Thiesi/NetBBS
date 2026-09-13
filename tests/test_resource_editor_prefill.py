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
        self.offered_viewport: int | None = None
        self._answer = answer
        self._cancel = cancel

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(
        self, echo: bool = True, *, initial: str = "", cancellable: bool = False,
        viewport: int | None = None, **kw,
    ) -> str:
        self.offered_initial = initial
        self.offered_cancellable = cancellable
        self.offered_viewport = viewport
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


# -- Codex review -----------------------------------------------------


def test_a_remote_value_is_sanitized_before_it_is_seeded():
    """P1. A carried Link board/channel/area stores its name and
    description verbatim from a remote genesis payload, sanitized only
    where the screen renders them. Seeding the line editor with the raw
    value would echo a hostile peer's escape sequences straight at the
    SysOp's terminal the moment they opened the field."""
    hostile = "Releases\x1b[2J\x1b]0;pwned\x07"
    draft = {"description": hostile}
    session = _edit(draft, "whatever")
    assert "\x1b" not in session.offered_initial
    assert "Releases" in session.offered_initial


def test_a_value_too_wide_for_one_row_is_edited_like_any_other():
    """Width used to disqualify a value from inline editing (issue
    #529): `read_line` moved with single-row CSI D/C, so a buffer that
    soft-wrapped made the display diverge from what would be saved, and
    the longest descriptions -- the case this feature was asked for --
    kept the old "blank = keep" prompt and had to be retyped.

    Issue #546 fixed that where it was actually broken, in the line
    editor, which now keeps a scrolling one-row window over the buffer.
    So no width gate is left: the value is seeded, and erasing it clears
    the field exactly as it does anywhere else."""
    draft = {"description": "x" * 200}
    session = _edit(draft, "")
    assert session.offered_initial == "x" * 200
    assert draft["description"] == ""


def test_the_window_is_sized_to_the_room_the_prompt_left():
    """The prompt is written on its own line precisely so the whole
    terminal width is available -- `viewport` is columns from the cursor
    to the right edge, not the terminal width in general.

    Handed over as a callable, not a number: a caller who shrinks their
    terminal mid-edit would otherwise keep getting rows sized for the
    terminal they had."""
    draft = {"description": "x" * 200}
    session = _edit(draft, "")
    assert callable(session.offered_viewport)
    assert session.offered_viewport() == session.terminal_width

    session.terminal_width = 40
    assert session.offered_viewport() == 40, "the width is read again, not remembered"


def test_a_value_that_fits_still_gets_the_editable_prompt():
    draft = {"description": "Weekly release builds"}
    session = _edit(draft, "Weekly release builds, signed")
    assert session.offered_initial == "Weekly release builds"
    assert draft["description"] == "Weekly release builds, signed"


def test_a_value_longer_than_the_line_editor_can_hold_is_not_prefilled():
    """`read_line` caps its buffer, and the transports disagreed about
    what that meant: Telnet/SSH submitted a silently shortened value
    while the web session seeded the whole thing. A carried Link
    resource's description is persisted from a remote payload with no
    per-field limit, so this is reachable rather than theoretical."""
    from netbbs.net.char_input import MAX_LINE_LENGTH

    draft = {"description": "x" * (MAX_LINE_LENGTH + 1)}
    session = _edit(draft, "")
    assert session.offered_initial == ""
    assert draft["description"] == "x" * (MAX_LINE_LENGTH + 1)


def test_a_tab_is_normalized_before_the_width_is_judged():
    """`sanitize_text` preserves a tab and `display_width` scores it
    zero, while the terminal advances to a tab stop -- so the raw string
    could be approved as fitting and then edit the wrong columns."""
    draft = {"description": "a\tb"}
    session = _edit(draft, "a b")
    assert "\t" not in session.offered_initial
    assert session.offered_initial == "a b"


def test_the_fallback_prompt_scrolls_too():
    """It is reached for a value too *long* to seed, not too wide -- and
    whoever is replacing such a value is about to type something long
    themselves, straight into the soft-wrap this change exists to
    remove. Only the inline branch opted in at first (Codex review)."""
    from netbbs.net.char_input import MAX_LINE_LENGTH

    draft = {"description": "x" * (MAX_LINE_LENGTH + 1)}
    session = _edit(draft, "a replacement")
    assert session.offered_initial == ""
    assert callable(session.offered_viewport)
    # The prompt shares this row, so the window gets what it leaves.
    assert 8 <= session.offered_viewport() < session.terminal_width
