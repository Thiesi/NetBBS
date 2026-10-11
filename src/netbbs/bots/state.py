"""Bounded, atomic JSON state, separate from any conversation context."""

import json
import os
from pathlib import Path

from netbbs.bots.registry import BotError

MAX_STATE_BYTES = 16384


def state_path(db, storage_name: str) -> Path:
    root = db.path.with_name(db.path.name + ".bots")
    directory = root / storage_name
    # The name was validated on creation. Do not follow a replacement directory
    # outside the store; same-OS-user programs are still not a security sandbox.
    if root.is_symlink() or directory.is_symlink():
        raise BotError("Bot state directories must not be symbolic links.")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "state.json"


def read_state(db, storage_name: str):
    path = state_path(db, storage_name)
    if path.is_symlink():
        raise BotError("Bot state must not be a symbolic link.")
    if not path.exists():
        return {}
    with path.open("rb") as stream:
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise BotError("Bot state exceeds 16 KiB.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise BotError("Bot state is invalid JSON; repair state.json before restarting it.") from exc


def write_state(db, storage_name: str, value) -> None:
    try:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, TypeError, RecursionError) as exc:
        raise BotError("State must be finite JSON.") from exc
    if len(raw) > MAX_STATE_BYTES:
        raise BotError("Bot state exceeds 16 KiB.")
    path = state_path(db, storage_name)
    temporary = path.with_suffix(".tmp")
    if temporary.is_symlink():
        raise BotError("Bot state temporary file must not be a symbolic link.")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
