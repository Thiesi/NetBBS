"""
The gates a board, file area or chat channel applies, in a caller's words
(issue #1105, design doc §3.6).

A caller entering a gated resource used to see nothing about why it is
gated: an adult area read exactly like an open one until someone was turned
away from it. The resource's header now carries one line naming the gates
that restrict anyone, for example::

    Requires: age 18+ verified · verified name · level 20+ to post

The values are the effective ones, after the Community cascade
(`netbbs.communities.get_effective_*`), so they are the values the checks
themselves use. A gate that restricts nobody is left out -- a level of 0, an
explicit minimum age of 0 -- and an ungated resource gets no line at all, so
most screens stay as they were. The age wording is the access map's
(`describe_age_gate`), the same "18+ verified" the console shows. A Linked
resource shows the gates this node applies, which are the ones here.
"""

from __future__ import annotations

from netbbs.age_requirement import describe_age_gate
from netbbs.communities import (
    get_effective_age_requirement,
    get_effective_min_age,
    get_effective_min_read_level,
    get_effective_min_write_level,
    get_effective_name_requirement,
)
from netbbs.rendering.ansi import colored
from netbbs.rendering.theme import GATE_COLOR
from netbbs.rendering.width import truncate_to_width
from netbbs.storage.database import Database

#: What the read and write levels let a caller do, per resource kind.
_LEVEL_VERBS = {"board": ("read", "post"), "file_area": ("browse", "upload")}

_LABEL = "Requires: "


def _kind(resource) -> str:
    name = type(resource).__name__
    if name == "Board":
        return "board"
    if name == "FileArea":
        return "file_area"
    if name == "Channel":
        return "channel"
    raise TypeError(f"no gates to describe for {name}")


def _name_gate(requirement: str | None) -> str | None:
    if requirement == "verified":
        return "verified name"
    if requirement == "verified_and_displayed":
        return "verified name, shown"
    return None


def resource_gates(db: Database, resource) -> tuple[str, ...]:
    """The gates on `resource` (a `Board`, `FileArea` or `Channel`) that
    restrict anyone, each in a few words, most personal first: age, name,
    then level. Empty when the resource is open to every caller."""
    kind = _kind(resource)
    gates: list[str] = []
    age = describe_age_gate(get_effective_min_age(db, resource), get_effective_age_requirement(db, resource))
    if age:
        gates.append(age)
    name = _name_gate(get_effective_name_requirement(db, resource))
    if name:
        gates.append(name)
    if kind == "channel":
        if resource.min_level > 0:
            gates.append(f"level {resource.min_level}+")
        if resource.members_only:
            gates.append("members only")
        return tuple(gates)
    read_verb, write_verb = _LEVEL_VERBS[kind]
    read = get_effective_min_read_level(db, resource)
    write = get_effective_min_write_level(db, resource)
    if read > 0:
        gates.append(f"level {read}+ to {read_verb}")
    # Writing needs the read level too, so a write level at or below it
    # restricts nobody further and would only repeat the number.
    if write > read:
        gates.append(f"level {write}+ to {write_verb}")
    return tuple(gates)


def gates_line(
    gates: tuple[str, ...], *, width: int, unicode_style: bool = True, ellipsis: str = "..."
) -> str | None:
    """`gates` as one header line no wider than `width`, in the gate
    colour, or `None` for an ungated resource. `ellipsis` is the session's
    (`netbbs.rendering.charset.ellipsis_for`): CP437 and ASCII sessions get
    three dots."""
    if not gates:
        return None
    separator = " · " if unicode_style else " - "
    text = truncate_to_width(_LABEL + separator.join(gates), max(1, width), ellipsis=ellipsis)
    return colored(text, fg_color=GATE_COLOR)


def resource_gates_line(
    db: Database, resource, *, width: int, unicode_style: bool = True, ellipsis: str = "..."
) -> str | None:
    """`resource_gates` and `gates_line` in one call, for a screen header."""
    return gates_line(resource_gates(db, resource), width=width, unicode_style=unicode_style, ellipsis=ellipsis)
