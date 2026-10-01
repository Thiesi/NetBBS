"""
Level names (design doc §5.7, issue #1009): an optional label per level, such
as "Member" for 10, shown beside the number wherever the console shows or asks
for a level. Labels are for reading only. Gates and accounts store numbers, so
naming, renaming or clearing a level changes nobody's access. 255 is always
"SysOp" and cannot be renamed.

Kept as one JSON object in the node's config table: a few dozen short strings
at most, travelling in backups with the rest of the node's settings.
"""

from __future__ import annotations

import json

from netbbs.auth.users import SYSOP_LEVEL, User
from netbbs.config import get_config, set_config
from netbbs.digits import is_ascii_number
from netbbs.rendering.sanitize import sanitize_text
from netbbs.storage.database import Database

LEVEL_NAMES_CONFIG_KEY = "level_names"
SYSOP_LEVEL_NAME = "SysOp"
MAX_LEVEL_NAME_LENGTH = 12


class LevelNameError(ValueError):
    """A level name that cannot be used, with why."""


def get_level_names(db: Database) -> dict[int, str]:
    """Every named level, 255 included. A stored value that cannot be read
    is ignored rather than breaking every screen that shows a level."""
    names: dict[int, str] = {}
    try:
        stored = json.loads(get_config(db, LEVEL_NAMES_CONFIG_KEY) or "{}")
    except ValueError:
        stored = {}
    if isinstance(stored, dict):
        for key, value in stored.items():
            if isinstance(key, str) and is_ascii_number(key) and 0 <= int(key) < SYSOP_LEVEL and isinstance(value, str):
                names[int(key)] = value
    names[SYSOP_LEVEL] = SYSOP_LEVEL_NAME
    return names


def set_level_name(db: Database, level: int, name: str | None, *, changed_by: User) -> dict[int, str]:
    """Name `level`, or clear its name with `None` or a blank name. Returns
    the names as they now stand. Raises `LevelNameError` for 255, a level
    outside 0-254, a name too long or made only of digits (a level prompt
    takes either, so a name must not read as a number), or a name another
    level already has."""
    from netbbs.moderation.log import record_action

    if level == SYSOP_LEVEL:
        raise LevelNameError(f"{SYSOP_LEVEL} is always called {SYSOP_LEVEL_NAME}.")
    if not 0 <= level < SYSOP_LEVEL:
        raise LevelNameError(f"Levels run from 0 to {SYSOP_LEVEL}.")
    cleaned = " ".join(sanitize_text(name or "").split())
    names = get_level_names(db)
    if cleaned:
        if len(cleaned) > MAX_LEVEL_NAME_LENGTH:
            raise LevelNameError(f"A level name is at most {MAX_LEVEL_NAME_LENGTH} characters.")
        if not any(char.isalpha() for char in cleaned):
            raise LevelNameError("A level name needs a letter, so it can't be mistaken for a level.")
        taken = next((other for other, other_name in names.items()
                      if other != level and other_name.casefold() == cleaned.casefold()), None)
        if taken is not None:
            raise LevelNameError(f"Level {taken} is already called {names[taken]}.")
        names[level] = cleaned
    else:
        names.pop(level, None)
    stored = {str(key): value for key, value in sorted(names.items()) if key != SYSOP_LEVEL}
    set_config(db, LEVEL_NAMES_CONFIG_KEY, json.dumps(stored))
    record_action(
        db, actor=changed_by, action="name_level",
        detail=f"level {level} named {cleaned!r}" if cleaned else f"level {level} name cleared",
    )
    return get_level_names(db)


def level_label(level: int, names: dict[int, str]) -> str:
    """`10 (Member)`, or `10` for a level with no name."""
    name = names.get(level)
    return f"{level} ({name})" if name else str(level)


def parse_level(raw: str, names: dict[int, str]) -> int | None:
    """A level typed as a number or as a level's name (any case), or `None`
    when it is neither. The range is the caller's to check."""
    text = raw.strip()
    if is_ascii_number(text):
        return int(text)
    folded = text.casefold()
    return next((level for level, name in names.items() if name.casefold() == folded), None)
