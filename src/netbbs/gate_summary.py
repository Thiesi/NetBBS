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

A gate the caller entering does not meet is drawn in the error colour
(issue #1115), so the one line also says why they can read a board but not
post to it -- the separate "Read only: posting needs ..." line it replaced
repeated the same level. An ASCII caller also gets ` (not met)` after it.
"""

from __future__ import annotations

from netbbs.age_requirement import describe_age_gate
from typing import NamedTuple

from netbbs.attestation import meets_name_requirement
from netbbs.communities import (
    get_effective_age_requirement,
    get_effective_min_age,
    get_effective_min_read_level,
    get_effective_min_write_level,
    get_effective_name_requirement,
    meets_read_gate,
    meets_resource_age,
    meets_write_gate,
)
from netbbs.rendering.ansi import colored
from netbbs.rendering.theme import ERROR_COLOR, GATE_COLOR
from netbbs.rendering.width import display_width, truncate_to_width
from netbbs.storage.database import Database

#: What the read and write levels let a caller do, per resource kind.
_LEVEL_VERBS = {"board": ("read", "post"), "file_area": ("browse", "upload")}

_LABEL = "Requires: "

#: Said after an unmet gate to an ASCII caller (issue #1115).
_UNMET_MARK = " (not met)"


class Gate(NamedTuple):
    """One gate: its words, and which check it stands for (`"age"`,
    `"name"`, `"read"`, `"write"`, `"level"`, `"members"`)."""

    text: str
    check: str


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


def resource_gate_items(db: Database, resource) -> tuple[Gate, ...]:
    """The gates on `resource` (a `Board`, `FileArea` or `Channel`) that
    restrict anyone, most personal first: age, name, then level. Empty when
    the resource is open to every caller."""
    kind = _kind(resource)
    gates: list[Gate] = []
    age = describe_age_gate(get_effective_min_age(db, resource), get_effective_age_requirement(db, resource))
    if age:
        gates.append(Gate(age, "age"))
    name = _name_gate(get_effective_name_requirement(db, resource))
    if name:
        gates.append(Gate(name, "name"))
    if kind == "channel":
        if resource.min_level > 0:
            gates.append(Gate(f"level {resource.min_level}+", "level"))
        if resource.members_only:
            gates.append(Gate("members only", "members"))
        return tuple(gates)
    read_verb, write_verb = _LEVEL_VERBS[kind]
    read = get_effective_min_read_level(db, resource)
    write = get_effective_min_write_level(db, resource)
    if read > 0:
        gates.append(Gate(f"level {read}+ to {read_verb}", "read"))
    # Writing needs the read level too, so a write level at or below it
    # restricts nobody further and would only repeat the number.
    if write > read:
        gates.append(Gate(f"level {write}+ to {write_verb}", "write"))
    return tuple(gates)


def resource_gates(db: Database, resource) -> tuple[str, ...]:
    """`resource_gate_items`' words alone."""
    return tuple(gate.text for gate in resource_gate_items(db, resource))


def unmet_gates(db: Database, user, resource) -> frozenset[str]:
    """The words of each gate on `resource` that `user` does not meet,
    asked with the same checks access uses (issue #1115), so a SysOp, who
    passes the age and name gates (#1096), sees none of those marked. A
    channel's own level and membership are left out: a caller reading its
    header is already inside."""
    checks = {
        "age": lambda: meets_resource_age(db, user, resource),
        "name": lambda: meets_name_requirement(db, user, get_effective_name_requirement(db, resource)),
        "read": lambda: meets_read_gate(db, user, resource),
        "write": lambda: meets_write_gate(db, user, resource),
    }
    return frozenset(
        gate.text for gate in resource_gate_items(db, resource)
        if gate.check in checks and not checks[gate.check]()
    )


def gates_line(
    gates: tuple[str, ...],
    *,
    width: int,
    unicode_style: bool = True,
    ellipsis: str = "...",
    unmet: frozenset[str] | set[str] = frozenset(),
) -> str | None:
    """`gates` as one header line no wider than `width`, in the gate
    colour, or `None` for an ungated resource. A gate in `unmet` is drawn
    in the error colour, and an ASCII caller (`unicode_style=False`) also
    gets ` (not met)` after it (issue #1115). `ellipsis` is the session's
    (`netbbs.rendering.charset.ellipsis_for`): CP437 and ASCII sessions get
    three dots."""
    if not gates:
        return None
    separator = " · " if unicode_style else " - "
    # Each piece with the colour it is drawn in; the label and separators
    # keep the gate colour.
    pieces: list[tuple[str, int]] = [(_LABEL, GATE_COLOR)]
    for index, gate in enumerate(gates):
        if index:
            pieces.append((separator, GATE_COLOR))
        if gate in unmet:
            pieces.append((gate + ("" if unicode_style else _UNMET_MARK), ERROR_COLOR))
        else:
            pieces.append((gate, GATE_COLOR))
    plain = "".join(text for text, _ in pieces)
    room = max(1, width)
    if display_width(plain) <= room:
        return "".join(colored(text, fg_color=color) for text, color in pieces)
    # Too wide: cut the whole line as one, then give each kept character
    # back the colour of the piece it came from. The ellipsis takes the
    # colour of the piece it cuts into.
    cut = truncate_to_width(plain, room, ellipsis=ellipsis)
    kept = cut[: len(cut) - len(ellipsis)] if cut != plain and cut.endswith(ellipsis) else cut
    out: list[str] = []
    position = 0
    last_color = GATE_COLOR
    for text, color in pieces:
        if position >= len(kept):
            break
        part = text[: len(kept) - position]
        out.append(colored(part, fg_color=color))
        position += len(part)
        last_color = color
    if kept != cut:
        out.append(colored(ellipsis, fg_color=last_color))
    return "".join(out)


def resource_gates_line(
    db: Database, resource, *, width: int, unicode_style: bool = True, ellipsis: str = "...", user=None
) -> str | None:
    """`resource_gates` and `gates_line` in one call, for a screen header;
    with `user`, the gates they do not meet are marked."""
    unmet = unmet_gates(db, user, resource) if user is not None else frozenset()
    return gates_line(
        resource_gates(db, resource), width=width, unicode_style=unicode_style, ellipsis=ellipsis, unmet=unmet
    )
