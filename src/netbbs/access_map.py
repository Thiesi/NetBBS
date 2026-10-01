"""
The access map (design doc §5.7, issue #1005): every level gate on this node
in one list, so a SysOp can see what a level opens without opening every
board, file area, channel, door and setting.

The map is built from the same effective-level functions the checks use
(`netbbs.communities.get_effective_*`, the node settings' own getters), and
`tests/test_access_map.py` holds every gate's answer to the real check for
sample accounts. It also fails when a level check appears in the code that
no gate kind here accounts for, so a new gate cannot be left off the map.

Levels only. A read or write grant (§5.2) lets one account past one board's
or area's level, and age, verified-name, members-only and hidden are facts
about each account; the map names those as a gate's `conditions` rather than
pretending the level is the whole answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from netbbs.auth.users import SYSOP_LEVEL
from netbbs.boards.boards import Board, list_boards
from netbbs.chat.channels import list_channels
from netbbs.communities import (
    get_community,
    get_effective_min_age,
    get_effective_min_read_level,
    get_effective_min_write_level,
    get_effective_name_requirement,
)
from netbbs.config import get_mail_min_level, get_node_map_min_level
from netbbs.doors.registry import list_doors
from netbbs.files.areas import FileArea, list_file_areas
from netbbs.storage.database import Database


class GateKind(str, Enum):
    BOARD_READ = "board_read"
    BOARD_WRITE = "board_write"
    AREA_READ = "area_read"
    AREA_WRITE = "area_write"
    CHANNEL = "channel"
    DOOR = "door"
    NODE_MAP = "node_map"
    MAIL = "mail"
    MRC_OPEN_ROOM = "mrc_open_room"
    SYSOP = "sysop"


class LevelSource(str, Enum):
    """Where a gate's level comes from."""

    RESOURCE = "resource"  # set on the board, area, channel or door itself
    COMMUNITY = "community"  # left to inherit, and its Community sets one
    DEFAULT = "default"  # left to inherit, and nothing sets one: 0
    SETTING = "setting"  # a node-wide setting
    FIXED = "fixed"  # the SysOp level, which nothing changes


@dataclass(frozen=True)
class Gate:
    """One thing a level opens.

    `level` is the gate's own effective level and `source` where it comes
    from. `opens_at` is the lowest level that actually gets through: posting
    on a board or uploading to an area needs its read level as well, so a
    write level below the read level opens nothing until the read level.

    `conditions` are the other gates on the same thing, in a few words each
    ("age 18+", "verified name"). `off` says why nobody gets through at any
    level right now (a closed board, a switched-off setting), or is `None`.
    `note` is anything else the level does not cover.
    """

    kind: GateKind
    object_id: int | None
    name: str
    level: int
    source: LevelSource
    opens_at: int
    community_name: str | None = None
    conditions: tuple[str, ...] = ()
    hidden: bool = False
    off: str | None = None
    note: str | None = None

    def opens_for(self, level: int) -> bool:
        """Whether an account at `level` gets past this gate by its level
        alone, the conditions aside."""
        return self.off is None and level >= self.opens_at


@dataclass(frozen=True)
class LevelChange:
    """What moving an account from one level to another changes."""

    old_level: int
    new_level: int
    gained: tuple[Gate, ...]
    lost: tuple[Gate, ...]


def _age_condition(min_age: int | None) -> tuple[str, ...]:
    return (f"age {min_age}+",) if min_age else ()


def _name_condition(requirement: str | None) -> tuple[str, ...]:
    if requirement == "verified":
        return ("verified name",)
    if requirement == "verified_and_displayed":
        return ("verified name, shown",)
    return ()


def _level_source(db: Database, resource: Board | FileArea, stored: int | None, default_field: str):
    """`(source, community_name)` for a board's or area's read or write
    level, mirroring `get_effective_min_read_level`'s cascade."""
    community = get_community(db, resource.community_id)
    if stored is not None:
        return LevelSource.RESOURCE, community.name if community else None
    if community is not None and getattr(community, default_field) is not None:
        return LevelSource.COMMUNITY, community.name
    return LevelSource.DEFAULT, community.name if community else None


def _board_gates(db: Database) -> list[Gate]:
    # Deferred: netbbs.link.boards imports the board flows' own modules.
    from netbbs.link.boards import is_board_closed, is_board_linked

    gates = []
    for board in list_boards(db):
        read_level = get_effective_min_read_level(db, board)
        write_level = get_effective_min_write_level(db, board)
        age = _age_condition(get_effective_min_age(db, board))
        read_source, community = _level_source(db, board, board.min_read_level, "default_min_read_level")
        write_source, _ = _level_source(db, board, board.min_write_level, "default_min_write_level")
        linked = is_board_linked(db, board)
        gates.append(Gate(
            GateKind.BOARD_READ, board.id, board.name, read_level, read_source, read_level,
            community_name=community, conditions=age,
        ))
        gates.append(Gate(
            GateKind.BOARD_WRITE, board.id, board.name, write_level, write_source, max(read_level, write_level),
            community_name=community,
            conditions=age + _name_condition(get_effective_name_requirement(db, board)),
            off="closed" if linked and is_board_closed(db, board) else None,
            # Issue #993: posts carried in from other nodes are not held to it.
            note="local callers only" if linked else None,
        ))
    return gates


def _area_gates(db: Database) -> list[Gate]:
    gates = []
    for area in list_file_areas(db):
        read_level = get_effective_min_read_level(db, area)
        write_level = get_effective_min_write_level(db, area)
        age = _age_condition(get_effective_min_age(db, area))
        read_source, community = _level_source(db, area, area.min_read_level, "default_min_read_level")
        write_source, _ = _level_source(db, area, area.min_write_level, "default_min_write_level")
        gates.append(Gate(
            GateKind.AREA_READ, area.id, area.name, read_level, read_source, read_level,
            community_name=community, conditions=age,
        ))
        gates.append(Gate(
            GateKind.AREA_WRITE, area.id, area.name, write_level, write_source, max(read_level, write_level),
            community_name=community,
            conditions=age + _name_condition(get_effective_name_requirement(db, area)),
        ))
    return gates


def _channel_gates(db: Database) -> list[Gate]:
    gates = []
    for channel in list_channels(db):
        community = get_community(db, channel.community_id)
        conditions = (
            _age_condition(get_effective_min_age(db, channel))
            + _name_condition(get_effective_name_requirement(db, channel))
            + (("members only",) if channel.members_only else ())
        )
        gates.append(Gate(
            GateKind.CHANNEL, channel.id, channel.name, channel.min_level, LevelSource.RESOURCE,
            channel.min_level, community_name=community.name if community else None,
            conditions=conditions, hidden=channel.hidden,
        ))
    return gates


def _door_gates(db: Database) -> list[Gate]:
    gates = []
    for door in list_doors(db):
        community = get_community(db, door.community_id)
        gates.append(Gate(
            GateKind.DOOR, door.id, door.name, door.min_play_level, LevelSource.RESOURCE, door.min_play_level,
            community_name=community.name if community else None,
        ))
    return gates


def _node_gates(db: Database) -> list[Gate]:
    from netbbs.link.onboarding import get_configured_link_enabled, resolve_link_enabled
    from netbbs.mrc.settings import load_mrc_settings, load_open_room_settings

    # The node map exists only while Link runs (§8.12). "unknown" is a node
    # that hasn't started since the setting was recorded: say nothing then.
    configured = get_configured_link_enabled(db)
    link_off = configured != "unknown" and not resolve_link_enabled(configured, db)
    map_level = get_node_map_min_level(db)
    mail_level = get_mail_min_level(db)
    rooms = load_open_room_settings(db)
    if not load_mrc_settings(db).enabled:
        rooms_off = "MRC is off"
    elif not rooms.enabled:
        rooms_off = "switched off"
    else:
        rooms_off = None
    return [
        Gate(
            GateKind.NODE_MAP, None, "Node map", map_level, LevelSource.SETTING, map_level,
            off="Link is off" if link_off else None,
        ),
        Gate(
            GateKind.MAIL, None, "Mail", mail_level, LevelSource.SETTING, mail_level,
            note="never the guest account",
        ),
        Gate(
            GateKind.MRC_OPEN_ROOM, None, "Open MRC rooms", rooms.min_level, LevelSource.SETTING, rooms.min_level,
            conditions=_age_condition(rooms.min_age) + _name_condition(rooms.name_requirement), off=rooms_off,
        ),
        Gate(GateKind.SYSOP, None, "SysOp console", SYSOP_LEVEL, LevelSource.FIXED, SYSOP_LEVEL),
    ]


def list_gates(db: Database) -> list[Gate]:
    """Every level gate on this node: boards, file areas, channels, doors,
    then the node-wide ones. Resources excluded from this node (§9.5) are
    left out, as nobody reaches them."""
    return _board_gates(db) + _area_gates(db) + _channel_gates(db) + _door_gates(db) + _node_gates(db)


def gates_open_at(gates: list[Gate], level: int) -> list[Gate]:
    """The gates an account at `level` gets past by its level alone."""
    return [gate for gate in gates if gate.opens_for(level)]


def level_change(gates: list[Gate], old_level: int, new_level: int) -> LevelChange:
    """What moving an account from `old_level` to `new_level` gains and
    loses, by level alone."""
    gained = tuple(g for g in gates if g.opens_for(new_level) and not g.opens_for(old_level))
    lost = tuple(g for g in gates if g.opens_for(old_level) and not g.opens_for(new_level))
    return LevelChange(old_level, new_level, gained, lost)


def levels_in_use(db: Database, gates: list[Gate]) -> list[int]:
    """The levels that matter on this node, ascending: every level a gate
    opens at, every level an account holds, and 0."""
    held = {row[0] for row in db.connection.execute("SELECT DISTINCT user_level FROM users")}
    return sorted({0} | held | {gate.opens_at for gate in gates})
