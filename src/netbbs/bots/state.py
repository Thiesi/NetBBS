"""Bounded, atomic JSON state, separate from any conversation context."""

import json
import os
import re
import shutil
from pathlib import Path

from netbbs.bots.registry import BotError

MAX_STATE_BYTES = 16384


def state_root(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".bots")


def state_path(db, storage_name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,23}", storage_name):
        raise BotError("Invalid bot state directory name.")
    root = state_root(db.path)
    directory = root / storage_name
    # The name was validated on creation. Do not follow a replacement directory
    # outside the store; same-OS-user programs are still not a security sandbox.
    if root.is_symlink() or directory.is_symlink():
        raise BotError("Bot state directories must not be symbolic links.")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "state.json"


def remove_state(db, storage_name: str) -> None:
    directory = state_path(db, storage_name).parent
    root = state_root(db.path)
    expected_root = root.parent.resolve() / root.name
    if root.resolve() != expected_root or directory.resolve() != expected_root / storage_name:
        raise BotError("Bot state directory is outside this node's store.")
    shutil.rmtree(directory)


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
