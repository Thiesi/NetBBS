"""Draft-editor fields shared across the SysOp console.

Integer, minimum-age, name-requirement and level fields, the seeded line
reader and the redraw-preference lookup that the board, file-area, channel,
door, trust and settings editors all use. Split out of
`netbbs.net.admin_flow` (issue #1311).
"""

from __future__ import annotations

from typing import Awaitable, Callable

from netbbs.access_map import LevelContext
from netbbs.age_requirement import VERIFIED as VERIFIED_AGE
from netbbs.auth.users import User
from netbbs.communities import get_community
from netbbs.digits import is_ascii_number
from netbbs.level_names import level_label
from netbbs.net.char_input import InputCancelled
from netbbs.net.resource_editor import (
    inline_field,
    read_field_line,
    write_field_prompt,
    write_field_message,
    choice_field,
    choice_step,
)
from netbbs.net.session import Session
from netbbs.net.redraw_preference import redraw_in_place_enabled, redraw_in_place_ever_set
from netbbs.rendering import MUTED_COLOR, colored, sanitize_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


def _optional_text_field(key: str) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """`text_field`'s own convention (`_CLEAR_HINT`, issue #557) for the
    INFO fields -- the only free-text settings on the node a SysOp may
    legitimately want empty.

    This used to be a third spelling of the same idea: blank kept the
    value and a typed `-` cleared it, on a screen whose other text
    fields clear by emptying the line. `-` is still accepted, for the
    muscle memory it already built and because it is the only way to
    clear a field whose current value is too long to seed inline.
    """

    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        current = draft.get(key) or ""
        await write_field_prompt(session, colored(f"({_CLEAR_HINT}):", fg_color=MUTED_COLOR), hint=_CLEAR_HINT)
        try:
            raw = (await _read_seeded_line(session, initial=current)).strip()
        except InputCancelled:
            await session.write_line("")
            return
        draft[key] = "" if raw == "-" else raw

    return prompt


async def _read_int(session: Session, *, default: int) -> int | None:
    """Reads a line opening on `default`: Enter saves what is shown, Esc
    leaves it alone, a valid integer replaces it, anything else shows a
    cancellation message and returns `None` -- callers should treat
    `None` as "abort the current screen". See `_EDIT_HINT` (issue #557)
    for why this no longer asks for a value against an empty line."""
    try:
        raw = (await _read_seeded_line(session, initial=str(default))).strip()
    except InputCancelled:
        await session.write_line("")
        return default
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        await write_field_message(session, colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
        return None


async def _prompt_optional_int(session: Session, label: str, *, current: int | None) -> tuple[int | None, bool]:
    """Generic nullable-int prompt -- the same open-on-the-current-value
    shape as `_prompt_min_age` below (see `_CLEAR_HINT`), factored out
    separately
    (rather than having that function delegate here) so its existing
    "no gate" wording -- already asserted on by
    tests/test_admin_flow.py and tests/test_board_pagination_ui.py --
    stays exactly as it is. Used for every nullable-int field
    introduced by design doc §16's Community inheritance model:
    boards'/file areas' own `min_read_level`/`min_write_level`
    (this is the *only* way to ever set one back to `None` -- i.e. opt
    a resource into inheriting its Community's default -- `_read_int`
    has no clearing mechanism at all), plus Community's own
    `default_min_read_level`/`default_min_write_level`. "Clear" is the
    accurate word in both cases, not "no gate" (a level isn't a gate
    the way age/name-requirement are)."""
    await write_field_prompt(session, colored(f"{label} ({_CLEAR_HINT}):", fg_color=MUTED_COLOR), hint=_CLEAR_HINT)
    try:
        raw = (await _read_seeded_line(
            session, initial="" if current is None else str(current)
        )).strip()
    except InputCancelled:
        await session.write_line("")
        return current, True
    if not raw or raw.lower() == "none":
        return None, True
    try:
        return int(raw), True
    except ValueError:
        await write_field_message(session, colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
        return None, False


#: Issue #557. Both prompts below open on the field's current value and
#: read an empty line as "clear", which is the convention issue #529
#: established for the text fields on these same Create/Edit screens.
#:
#: Before this they were the other half of a screen with two answers for
#: the same gesture: a text field's empty submit cleared the value, a
#: gate's kept it, and each said so in its own prompt text. A SysOp
#: learns the convention on whichever field they meet first. Applied to
#: an age gate, "clear the line to clear the value" silently left the
#: gate in place -- and the screen afterwards looks exactly like one
#: where it worked, because the value was never on the line to begin
#: with. That is the same shape of harm as #540 (a gate nobody intended,
#: in force, with nothing on screen connecting it to what the operator
#: did), from the other direction.
#:
#: Escape is what "blank" used to mean, now on a key of its own instead
#: of overloaded onto the empty string. `none` stays accepted as an
#: explicit spelling for the muscle memory it already built.
_CLEAR_HINT = "Enter saves, blank clears, Esc keeps"


#: The same convention for a field with no null state to clear *to* --
#: a channel's own `min_level`, an MRC hub port, a trust weight. There is
#: no third answer to invent here: the value is in the buffer, so Enter
#: saves what is shown and Escape leaves it alone, which is exactly what
#: `netbbs.net.resource_editor`'s text fields say. An emptied line has no
#: meaning for a field that must hold a number, and cannot be reached by
#: accident now that the line starts populated, so it is read as "no
#: change" rather than turned into an error a caller would only ever see
#: by deleting a value on purpose.
_EDIT_HINT = "Enter saves, Esc cancels"


def _seconds_text(value: float) -> str:
    """A delay as the SysOp would type it: `60`, not `60.0`, and never the
    6-significant-digit rounding `:g` would seed and save back."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


async def _read_seeded_line(session: Session, *, initial: str, echo: bool = True) -> str:
    """Read one line opening on `initial`, with a row to do it in.

    Every prompt below writes its label as a line of its own and then
    calls this, which is `netbbs.net.resource_editor.text_field`'s shape
    and for its reason (issue #546, Codex review): `read_line` moves its
    cursor with single-row `CSI D`/`CSI C`, so a value that soft-wraps
    onto a second row makes every Home, Left, Backspace and tail redraw
    clamp to the row it is on while the logical cursor walks into text
    above -- the display and the value that would be saved diverge,
    silently.

    The prompt occupying its own line is what makes `viewport` the whole
    terminal width rather than "whatever was left after the label", which
    on the supported 40-column floor is frequently nothing: these labels
    are longer than 40 columns on their own. A callable, not a number, so
    a caller who resizes mid-edit gets rows sized for the terminal they
    now have.

    Raises `InputCancelled` on Escape, which every caller reads as
    "leave the value alone". `echo=False` reads a password unseen.
    """
    return await read_field_line(session, initial=initial, echo=echo)


#: Bounds for an age gate (issue #540). `0` stays accepted and keeps its
#: existing meaning -- `netbbs.attestation.meets_age` opens with
#: `if not min_age: return True`, so zero is "no gate" rather than a
#: gate nobody passes. The ceiling is loose on purpose: it exists to
#: catch a typo, not to legislate how old a caller may be.
MIN_AGE_FLOOR = 0


MIN_AGE_CEILING = 120


async def _prompt_min_age(session: Session, *, current: int | None) -> tuple[int | None, bool]:
    """Shared min_age prompt for board/channel/area create+edit screens
    (design doc §18). Returns `(value, ok)` -- `ok=False`
    means the caller should cancel.

    Opens on the current gate, if any. Enter saves what is shown, an
    emptied line (or the explicit word `none`) clears the gate, and
    Escape leaves it alone -- see `_CLEAR_HINT` for why this is no longer
    "blank = keep"."""
    await write_field_prompt(
        session, colored(f"Minimum age ({_CLEAR_HINT}, {MIN_AGE_FLOOR}-{MIN_AGE_CEILING}):", fg_color=MUTED_COLOR),
        hint=f"Age {MIN_AGE_FLOOR}-{MIN_AGE_CEILING}; Enter saves; Esc keeps",
    )
    try:
        raw = (await _read_seeded_line(
            session, initial="" if current is None else str(current)
        )).strip()
    except InputCancelled:
        await session.write_line("")
        return current, True
    if not raw or raw.lower() == "none":
        return None, True
    try:
        value = int(raw)
    except ValueError:
        await write_field_message(session, colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
        return None, False
    if not (MIN_AGE_FLOOR <= value <= MIN_AGE_CEILING):
        # Issue #540. This used to accept any integer at all, and
        # `meets_age` compares `compute_age(birthdate) >= min_age`, so a
        # fat-fingered 188 or 1000 refused every caller on the node --
        # including callers who would otherwise have qualified -- with
        # nothing on screen explaining why. "Nobody can get into this
        # area" does not obviously point at a mistyped age.
        #
        # A negative value was worse than useless: truthy, so treated as
        # a real gate, and then passed by everyone with a birthdate
        # while still failing closed for anyone without one.
        #
        # Refused rather than clamped, and nothing is written, so the
        # SysOp sees that their value did not take.
        await write_field_message(session,
            colored(
                f"A minimum age must be between {MIN_AGE_FLOOR} and {MIN_AGE_CEILING}, "
                f"or 'none' for no gate -- cancelled.",
                fg_color=MUTED_COLOR,
            )
        )
        return None, False
    return value, True


async def _prompt_min_age_check(
    session: Session, *, current: int | None, current_requirement: str | None,
) -> tuple[int | None, str | None, bool]:
    """`_prompt_min_age` that also asks how the age is checked (issue
    #1082): a trailing `v` (`18v`) asks for a verified age. Returns
    `(value, requirement, ok)`. Opens on what is set, `18v` included, so
    Enter keeps both; an emptied line or `none` clears both."""
    verified = current_requirement == VERIFIED_AGE
    shown = "" if current is None else str(current)
    if verified:
        shown += "v"
    await write_field_prompt(
        session,
        colored(
            f"Minimum age ({_CLEAR_HINT}, {MIN_AGE_FLOOR}-{MIN_AGE_CEILING}, add v for verified only):",
            fg_color=MUTED_COLOR,
        ),
        hint=f"Age {MIN_AGE_FLOOR}-{MIN_AGE_CEILING}, 18v = verified only; Enter saves; Esc keeps",
    )
    try:
        raw = (await _read_seeded_line(session, initial=shown)).strip()
    except InputCancelled:
        await session.write_line("")
        return current, current_requirement, True
    if not raw or raw.lower() == "none":
        return None, None, True
    wants_verified = raw[-1:].lower() == "v"
    number = raw[:-1].strip() if wants_verified else raw
    if not is_ascii_number(number):
        await write_field_message(session, colored("Not a number -- cancelled.", fg_color=MUTED_COLOR))
        return current, current_requirement, False
    value = int(number)
    if not (MIN_AGE_FLOOR <= value <= MIN_AGE_CEILING):
        await write_field_message(session,
            colored(
                f"A minimum age must be between {MIN_AGE_FLOOR} and {MIN_AGE_CEILING}, "
                f"or 'none' for no gate -- cancelled.",
                fg_color=MUTED_COLOR,
            )
        )
        return current, current_requirement, False
    return value, VERIFIED_AGE if wants_verified else None, True


def _optional_int_label(value: int | None, *, none_word: str = "none") -> str:
    return str(value) if value is not None else none_word


def _users_phrase(count: int) -> str:
    return f"{count} user{'s' if count != 1 else ''}"


def _plain_level_label(levels: LevelContext | None, level: int | None) -> str:
    """A channel's, door's or setting's level, with how many enabled,
    approved accounts it lets in (design doc §5.7, issue #1008)."""
    if level is None:
        return "none"
    if levels is None:
        return str(level)
    return f"{level_label(level, levels.names)} \u00b7 {_users_phrase(levels.users_at_or_above(level))}"


def _setting_level_label(levels: LevelContext, level: int) -> str:
    return f"level {level_label(level, levels.names)} and up · {_users_phrase(levels.users_at_or_above(level))}"


def _effective_draft_level(levels: LevelContext, draft: dict, key: str) -> tuple[int, str | None]:
    """A board's or file area's read or write level as the draft stands:
    `(level, where)`, `where` saying where an inherited level comes from."""
    stored = draft.get(key)
    if stored is not None:
        return stored, None
    community = levels.communities.get(draft.get("community_id"))
    default = getattr(community, f"default_{key}", None) if community is not None else None
    if default is not None:
        return default, f"from Community {community.name}"
    return 0, "the default"


def _resource_level_label(levels: LevelContext | None, draft: dict, key: str) -> str:
    """`min_read_level`/`min_write_level` on a board or file area editor:
    the value, where an inherited one comes from, and how many accounts it
    lets in. Posting needs reading too, so a write level below the read
    level is counted at the read level and says so."""
    if levels is None:
        return _optional_int_label(draft.get(key))
    level, where = _effective_draft_level(levels, draft, key)
    label = level_label(level, levels.names)
    shown = f"none: {label} {where}" if where is not None else label
    counted, extra = level, ""
    if key == "min_write_level":
        read_level, _ = _effective_draft_level(levels, draft, "min_read_level")
        if read_level > level:
            counted, extra = read_level, f" (reading needs {read_level})"
    return f"{shown} \u00b7 {_users_phrase(levels.users_at_or_above(counted))}{extra}"


def _community_default_label(levels: LevelContext | None, draft: dict, key: str, community_id: int | None) -> str:
    """A Community's default read or write level, with how many accounts it
    lets in and how many boards and file areas inherit it."""
    value = draft.get(key)
    label = _optional_int_label(value)
    if levels is None:
        return label
    if value is not None:
        label = f"{level_label(value, levels.names)} \u00b7 {_users_phrase(levels.users_at_or_above(value))}"
    direction = "read" if "read" in key else "write"
    inheriting = levels.inheriting.get((community_id, direction), {}) if community_id is not None else {}
    parts = [f"{count} {what}{'s' if count != 1 else ''}" for what, count in sorted(inheriting.items())]
    return label + (f" \u00b7 inherited by {', '.join(parts)}" if parts else "")


# Tri-state "recommend this to Link peers" cycle -- `[L]ink this board`/
# `[L]ink this file area`'s own `default_moderated` field (dogfood
# report: converting these screens from their old fixed linear prompt
# chain). Unlike a plain `bool_field`, silence is a real third state
# here (send no recommendation at all, matching every other `default_*`
# field's own "blank = no recommendation" convention), not just a
# "current" value to keep -- so this cycles through it like
# `_name_requirement_field`'s own three-value set rather than confirming
# a toggle.
_LINK_RECOMMENDATION_VALUES: list[bool | None] = [None, True, False]


def _link_recommendation_label(value: bool | None) -> str:
    if value is None:
        return "no recommendation"
    return "yes" if value else "no"


def _optional_int_field(key: str, label: str) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        value, ok = await _prompt_optional_int(session, label, current=draft.get(key))
        if ok:
            draft[key] = value

    return prompt


def _int_field(key: str, label: str) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """A plain, non-nullable int field (channel's own `min_level` has
    no Community-inheritance concept the way boards'/areas' `min_read_
    level`/`min_write_level` do, so `_read_int`, not `_prompt_optional_
    int`, is the right underlying primitive here)."""

    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        # No `[current]` in the prompt any more: the value is in the
        # line the caller is editing, so showing it twice would read as
        # two different numbers.
        await write_field_prompt(session, colored(f"{label} ({_EDIT_HINT}):", fg_color=MUTED_COLOR))
        value = await _read_int(session, default=draft.get(key))
        if value is not None:
            draft[key] = value

    return prompt


def _min_age_field(
    key: str = "min_age", requirement_key: str | None = None,
) -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """The Min age field. With `requirement_key` (issue #1082) it also
    edits whether the age must be verified: typed as `18v`, shown by
    `_min_age_label` as "18, verified only"."""
    @inline_field
    async def prompt(session: Session, lane: DatabaseLane, draft: dict) -> None:
        if requirement_key is None:
            value, ok = await _prompt_min_age(session, current=draft.get(key))
            if ok:
                draft[key] = value
            return
        value, requirement, ok = await _prompt_min_age_check(
            session, current=draft.get(key), current_requirement=draft.get(requirement_key),
        )
        if ok:
            draft[key] = value
            draft[requirement_key] = requirement

    return prompt


def _min_age_label(value: int | None, requirement: str | None) -> str:
    """A Min age field's value, with how it is checked (issue #1082). A
    resource that inherits its age but asks for a verified one says so."""
    if requirement == VERIFIED_AGE:
        return f"{_optional_int_label(value)}, verified only"
    return _optional_int_label(value)


def _name_requirement_label(value: str | None) -> str:
    """Dogfood follow-up: since issue #153 turned this from typed text
    into a cycling toggle, showing the raw stored value verbatim
    ('verified_and_displayed') means every render still displays the
    field's internal string constant, underscores and all, rather than
    words a SysOp would actually write. 'none'/'verified' already read
    fine as-is; only the compound value needs it."""
    return (value or "none").replace("_", " ")


_NAME_REQUIREMENT_VALUES = [None, "verified", "verified_and_displayed"]


def _name_requirement_field(key: str = "name_requirement") -> Callable[[Session, DatabaseLane, dict], Awaitable[None]]:
    """Cycles none -> verified -> verified_and_displayed -> none on
    each hotkey press (dogfood feature request, issue #153) instead of
    requiring the SysOp to type the literal string
    `verified_and_displayed`."""
    return choice_field(key, _NAME_REQUIREMENT_VALUES)


def _name_requirement_step(key: str = "name_requirement") -> Callable[[dict, int], None]:
    """`FieldSpec.step` counterpart to `_name_requirement_field`
    (dogfood feature request, issue #160's cursor-navigation
    follow-up) -- Left/Right cycle the same three values Space/Enter/
    the hotkey letter already do, just in either direction."""
    return choice_step(key, _NAME_REQUIREMENT_VALUES)


def _resolve_redraw_preference(db: Database, actor: User) -> tuple[bool, bool]:
    """`(redraw_in_place, show_hint)` for `edit_resource_draft` -- one
    `lane.run` round trip instead of two, resolved once by the caller
    before entering the screen, the same "caller resolves, function
    just trusts it" shape `description_level` already uses. The hint
    is only ever worth showing when the preference is both off and
    never explicitly touched -- an account that already turned it on,
    or already turned it down, doesn't need to be told about it again."""
    enabled = redraw_in_place_enabled(db, actor)
    show_hint = not enabled and not redraw_in_place_ever_set(db, actor)
    return enabled, show_hint


# Dogfood feature request, issue #150's own concrete example ("new
# SysOps have absolutely no clue what name requirements do"): Ctrl-H
# help text for every FieldSpec below that exposes this field. One
# shared string so the four call sites (board/area/channel plus
# Community's own cascading default) can never drift apart.
#: Appended to a Min age field's help (issue #1082): how to ask for a
#: verified age, which the field itself shows as "18, verified only".
_VERIFIED_AGE_HELP = (
    " Type the age with a v (18v) to accept only an age you, or staff who verify "
    "identity, have verified: a caller old enough by the birthdate they entered sees "
    "it marked 'needs verification' and is told to ask. A plain number accepts that "
    "birthdate when there is no verified age. Callers set a birthdate in Your profile › "
    "Name & details; you verify it from the main menu's [V]erify, and staff can once "
    "you switch on Can verify identity for them (Users)."
)


_NAME_REQUIREMENT_HELP = (
    "Who may post, upload or join: 'verified' needs a verified real name but shows it "
    "nowhere; 'verified_and_displayed' also shows that name with each post, file or "
    "chat line, as it was when written. Reading a board or area is not gated. You verify a "
    "caller's name from the main menu's [V]erify; staff can once you switch on Can "
    "verify identity for them (Users)."
)


def _community_label(db: Database, community_id: int | None) -> str:
    """Detail-screen display helper -- `(none)` for `community_id is
    None`, else the Community's own name (sanitized, same as every
    other user-controlled string shown on these detail screens)."""
    community = get_community(db, community_id)
    return sanitize_text(community.name) if community is not None else "(none)"
