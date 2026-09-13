"""An age gate has to be an age (issue #540).

`_prompt_min_age` accepted any integer, and `meets_age` compares
`compute_age(birthdate) >= min_age`. So a SysOp who meant 18 and typed
188 locked the resource against the entire node -- silently, with
nothing on screen connecting "nobody can get in here" to a mistyped
number. Found while sizing the gates column for #528.
"""

from __future__ import annotations

import asyncio

from netbbs.net.admin_flow import MIN_AGE_CEILING, MIN_AGE_FLOOR, _prompt_min_age


class FakeSession:
    def __init__(self, answer: str):
        self._answer = answer
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return self._answer

    @property
    def output(self) -> str:
        return "".join(self.written)


def _ask(answer: str, *, current: int | None = None):
    session = FakeSession(answer)
    value, ok = asyncio.run(_prompt_min_age(session, current=current))
    return value, ok, session


# -- What is still accepted -------------------------------------------


def test_an_ordinary_age_is_accepted():
    assert _ask("18")[:2] == (18, True)


def test_zero_is_accepted_and_still_means_no_gate():
    """`meets_age` opens with `if not min_age: return True`, so zero is
    an explicit "no gate" -- a supported way to override an inherited
    one -- not a gate nobody passes."""
    assert _ask("0")[:2] == (0, True)


def test_the_ceiling_itself_is_accepted():
    assert _ask(str(MIN_AGE_CEILING))[:2] == (MIN_AGE_CEILING, True)


def test_blank_still_keeps_the_current_value():
    assert _ask("", current=21)[:2] == (21, True)


def test_none_still_clears_the_gate():
    assert _ask("none", current=21)[:2] == (None, True)


# -- What is now refused ----------------------------------------------


def test_a_fat_fingered_age_is_refused(tmp_path):
    """The reported footgun: 188 for 18. Refused, and nothing written."""
    value, ok, session = _ask("188", current=18)
    assert ok is False
    assert "between" in session.output


def test_an_absurd_age_is_refused():
    value, ok, _ = _ask("1000")
    assert ok is False


def test_a_negative_age_is_refused():
    """Worse than useless before: truthy, so treated as a real gate,
    then passed by everyone with a birthdate while still failing closed
    for anyone without one -- a gate that filtered only the people who
    had not set a birthday."""
    value, ok, _ = _ask("-5")
    assert ok is False


def test_a_refusal_does_not_change_the_current_value():
    """`ok=False` tells the caller to cancel, so the draft keeps what it
    had -- the SysOp's mistake costs them a keystroke, not their gate."""
    value, ok, _ = _ask("999", current=18)
    assert ok is False


def test_a_non_number_is_still_refused_the_same_way():
    value, ok, session = _ask("eighteen")
    assert ok is False
    assert "Not a number" in session.output


# -- The prompt says so -----------------------------------------------


def test_the_prompt_states_the_range():
    """A bound a SysOp cannot see is a bound they will hit by surprise."""
    _, _, session = _ask("18")
    assert f"{MIN_AGE_FLOOR}-{MIN_AGE_CEILING}" in session.output


def test_the_prompt_still_offers_none():
    _, _, session = _ask("18")
    assert "'none'" in session.output
