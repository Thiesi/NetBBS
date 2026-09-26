"""
The local name a carried resource is stored under (issue #671).

A Link resource is identified by its content-addressed id, never by its name,
and independently run nodes reuse the same names -- `general`, `files`,
`chat`. `boards.name`, `channels.name` and `file_areas.name` are unique here,
so a genesis whose name is already taken locally cannot be inserted as it
stands. It used to raise `sqlite3.IntegrityError` out of the sync pass, after
`save_event` had already kept the genesis, so the resource was never carried
and nothing said so.

Carry is opt-out (design doc §9.3), so a collision is resolved without asking
the SysOp: the resource is carried under its name suffixed with a prefix of
its own id, which the SysOp can rename like any carried resource. Peers keep
seeing the genesis name. Names are compared case-insensitively, as issue
#300's channel rename already did, so a carried `General` does not sit beside
a local `general` looking like the same thing.
"""

from __future__ import annotations

from netbbs.storage.database import Database

_TABLES = frozenset({"boards", "channels", "file_areas"})

# Increasing prefixes of the resource id. Resource ids are unique, so the last
# candidate can only be taken by a local resource deliberately named after
# another resource's id; `None` then tells the caller to refuse, which the
# transport tolerates like a carry-cap refusal.
_SUFFIX_LENGTHS = (8, 16, None)


def free_local_name(db: Database, table: str, name: str, resource_id: str) -> str | None:
    """`name` if no row of `table` already uses it (ignoring case), else the
    first of `name-<id prefix>` that is free, else `None`."""
    if table not in _TABLES:
        raise ValueError(f"not a carried-resource table: {table!r}")
    if not isinstance(resource_id, str) or not resource_id:
        # Nothing validates a genesis id's type before this runs, and this is
        # after `save_event`: refuse, which the transport tolerates, rather
        # than raise out of the sync pass.
        return None
    candidates = [name] + [
        f"{name}-{resource_id if length is None else resource_id[:length]}" for length in _SUFFIX_LENGTHS
    ]
    for candidate in dict.fromkeys(candidates):
        taken = db.connection.execute(
            f"SELECT 1 FROM {table} WHERE lower(name) = lower(?)", (candidate,)
        ).fetchone()
        if taken is None:
            return candidate
    return None
