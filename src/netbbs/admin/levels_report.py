"""
`python -m netbbs.admin levels` (design doc §5.7, issue #1009): the access map
from the shell, for scripts and SysOps who would rather not open the console.
Read-only. Three reports, each as text or `--json`:

- the ladder: every level in use, its name, its accounts, what it first opens;
- one level: what it opens, what stays closed, and the conditions;
- one account moved to another level: what it gains, loses and still has
  blocked, the preview the console shows before a level change.
"""

from __future__ import annotations

import json
from typing import Any

from netbbs.access_map import (
    GATE_WORDS,
    AccountChange,
    Gate,
    account_level_change,
    gate_source,
    ladder_summary,
    level_ladder,
    list_gates,
)
from netbbs.auth.users import SYSOP_LEVEL, AuthError, get_user_by_username
from netbbs.level_names import get_level_names, level_label, parse_level
from netbbs.storage.database import Database


class LevelsReportError(Exception):
    """A level or account the report cannot find."""


def _gate_record(gate: Gate) -> dict[str, Any]:
    access, what = GATE_WORDS[gate.kind]
    return {
        "kind": gate.kind.value, "access": access, "what": what, "name": gate.name, "id": gate.object_id,
        "level": gate.level, "opens_at": gate.opens_at, "source": gate.source.value,
        "community": gate.community_name, "conditions": list(gate.conditions), "hidden": gate.hidden,
        "off": gate.off, "note": gate.note,
    }


def _gate_line(gate: Gate, extra: str | None = None) -> str:
    access, what = GATE_WORDS[gate.kind]
    remarks = [f"off: {gate.off}"] if gate.off else []
    remarks += [*gate.conditions, *([gate.note] if gate.note else []), *([extra] if extra else [])]
    name = gate.name + (" (hidden)" if gate.hidden else "")
    tail = f"  [{', '.join(remarks)}]" if remarks else ""
    return f"  {access:<8} {what:<9} {name:<24} {gate.opens_at:>3}  {gate_source(gate)}{tail}"


def resolve_level(db: Database, raw: str) -> int:
    level = parse_level(raw, get_level_names(db))
    if level is None or not 0 <= level <= SYSOP_LEVEL:
        raise LevelsReportError(f"{raw!r} is not a level from 0 to {SYSOP_LEVEL} or a level's name.")
    return level


def ladder_report(db: Database) -> tuple[list[str], Any]:
    names = get_level_names(db)
    ladder = level_ladder(db)
    lines = [f"{'Level':<24} {'Users':>5}  Opens here"]
    lines += [f"{level_label(step.level, names):<24} {step.users:>5}  {ladder_summary(step)}" for step in ladder]
    data = [
        {"level": step.level, "name": names.get(step.level), "users": step.users,
         "opens": [_gate_record(gate) for gate in step.opens]}
        for step in ladder
    ]
    return lines, data


def level_report(db: Database, level: int) -> tuple[list[str], Any]:
    names = get_level_names(db)
    gates = list_gates(db)
    open_gates = [gate for gate in gates if gate.opens_for(level)]
    closed = [gate for gate in gates if not gate.opens_for(level)]
    users = db.connection.execute(
        "SELECT COUNT(*) FROM users WHERE user_level = ? AND disabled_at IS NULL AND pending_approval = 0", (level,)
    ).fetchone()[0]
    lines = [f"Level {level_label(level, names)}: {users} account{'s' if users != 1 else ''}", "", "Opens:"]
    lines += [_gate_line(gate) for gate in open_gates] or ["  nothing"]
    lines += ["", "Still closed:"]
    lines += [_gate_line(gate) for gate in closed] or ["  nothing"]
    data = {
        "level": level, "name": names.get(level), "users": users,
        "opens": [_gate_record(gate) for gate in open_gates],
        "closed": [_gate_record(gate) for gate in closed],
    }
    return lines, data


def account_report(db: Database, username: str, new_level: int) -> tuple[list[str], Any]:
    try:
        user = get_user_by_username(db, username)
    except AuthError as exc:
        raise LevelsReportError(f"No account called {username!r}.") from exc
    names = get_level_names(db)
    change: AccountChange = account_level_change(db, user, new_level)
    lines = [f"{user.username}: level {level_label(change.old_level, names)} -> {level_label(change.new_level, names)}"]
    for title, gates in (("Gains", change.gained), ("Loses", change.lost)):
        lines += ["", f"{title}:"]
        lines += [_gate_line(gate) for gate in gates] or ["  nothing"]
    if change.blocked:
        lines += ["", "Still blocked (another gate keeps this account out):"]
        lines += [_gate_line(gate, "needs " + ", ".join(fails)) for gate, fails in change.blocked]
    data = {
        "username": user.username, "old_level": change.old_level, "new_level": change.new_level,
        "gained": [_gate_record(gate) for gate in change.gained],
        "lost": [_gate_record(gate) for gate in change.lost],
        "blocked": [{**_gate_record(gate), "needs": list(fails)} for gate, fails in change.blocked],
    }
    return lines, data


def run_levels_report(db: Database, *, level: str | None, user: str | None, to: str | None) -> tuple[list[str], Any]:
    """The report the arguments ask for. Raises `LevelsReportError`."""
    if user is not None or to is not None:
        if user is None or to is None:
            raise LevelsReportError("--user and --to go together: whose level, and to what.")
        if level is not None:
            raise LevelsReportError("Give a level, or --user with --to, not both.")
        return account_report(db, user, resolve_level(db, to))
    if level is not None:
        return level_report(db, resolve_level(db, level))
    return ladder_report(db)


def render_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)
