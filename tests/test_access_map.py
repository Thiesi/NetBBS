"""
The access map (design doc §5.7, issue #1005) is only worth showing a SysOp
if it says what the checks do. Two kinds of test hold it to that:

- for accounts at every interesting level, each gate's answer matches the
  check the caller's screens actually make;
- every level comparison in the source belongs to a gate kind the map
  covers, or is listed here as not a gate, so a new gate can't be added
  without the map hearing of it.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from netbbs.access_map import (
    Gate,
    GateKind,
    LevelSource,
    gates_open_at,
    level_change,
    levels_in_use,
    list_gates,
)
from netbbs.attestation import attest_age
from netbbs.auth.users import SYSOP_LEVEL, create_user, is_usable_sysop
from netbbs.boards.boards import create_board
from netbbs.chat.channels import create_channel
from netbbs.communities import create_community, meets_write_gate
from netbbs.config import set_mail_min_level, set_node_map_min_level
from netbbs.doors.registry import create_door
from netbbs.files.areas import create_file_area
from netbbs.link.onboarding import set_configured_link_enabled
from netbbs.mail import mail_access_refusal
from netbbs.mrc.settings import (
    MrcSettings,
    OpenRoomSettings,
    load_open_room_settings,
    save_mrc_settings,
    save_open_room_settings,
)
from netbbs.net.board_flow import _read_only_reason, visible_boards
from netbbs.net.chat_flow import _authorize_channel_entry, _open_room_gate_denial, _visible_channels_for
from netbbs.net.directory_flow import playable_registrations
from netbbs.net.door_flow import _visible_doors
from netbbs.net.file_flow import visible_areas
from netbbs.net.node_map_flow import may_open_node_map
from netbbs.storage.database import Database

LEVELS = (0, 4, 5, 9, 10, 49, 50, 99, 100, 254, SYSOP_LEVEL)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _node(db: Database):
    """A node with a gate of every kind, levels spread so that every one
    of `LEVELS` sits just below or at some gate."""
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    members = create_community(
        db, "Members", default_min_read_level=10, default_min_write_level=50, creator=sysop
    )
    create_board(db, "lobby", min_read_level=0, min_write_level=5, creator=sysop)
    create_board(db, "lounge", min_read_level=None, min_write_level=None, community_id=members.id, creator=sysop)
    create_board(db, "orphan", min_read_level=None, min_write_level=None, creator=sysop)
    create_board(db, "staff", min_read_level=100, min_write_level=100, creator=sysop)
    # Writing below reading: nobody posts who cannot open the board.
    create_board(db, "upside", min_read_level=50, min_write_level=10, creator=sysop)
    create_file_area(db, "uploads", min_read_level=None, min_write_level=None, community_id=members.id, creator=sysop)
    create_file_area(db, "public", min_read_level=0, min_write_level=0, creator=sysop)
    create_channel(db, "chat", creator=sysop)
    create_channel(db, "back room", min_level=50, community_id=members.id, creator=sysop)
    create_door(db, "trivia", "trivia.py", creator=sysop)
    create_door(db, "blacksite", "blacksite.py", min_play_level=50, creator=sysop)
    set_node_map_min_level(db, 10)
    set_mail_min_level(db, 5)
    set_configured_link_enabled(db, True)
    save_mrc_settings(db, MrcSettings(enabled=True, host="127.0.0.1", port=5000, tls=False, site_name="Board"))
    save_open_room_settings(db, OpenRoomSettings(enabled=True, min_level=99))
    return sysop


def _gate(gates: list[Gate], kind: GateKind, name: str) -> Gate:
    return next(gate for gate in gates if gate.kind is kind and gate.name == name)


# -- what the map says


def test_a_gate_names_where_its_level_comes_from(db):
    _node(db)
    gates = list_gates(db)

    lobby = _gate(gates, GateKind.BOARD_READ, "lobby")
    assert (lobby.level, lobby.source, lobby.community_name) == (0, LevelSource.RESOURCE, None)
    lounge = _gate(gates, GateKind.BOARD_WRITE, "lounge")
    assert (lounge.level, lounge.source, lounge.community_name) == (50, LevelSource.COMMUNITY, "Members")
    orphan = _gate(gates, GateKind.BOARD_READ, "orphan")
    assert (orphan.level, orphan.source) == (0, LevelSource.DEFAULT)
    uploads = _gate(gates, GateKind.AREA_READ, "uploads")
    assert (uploads.level, uploads.source) == (10, LevelSource.COMMUNITY)
    assert _gate(gates, GateKind.MAIL, "Mail").source is LevelSource.SETTING
    assert _gate(gates, GateKind.SYSOP, "SysOp console").level == SYSOP_LEVEL


def test_writing_opens_no_lower_than_reading(db):
    _node(db)
    upside = _gate(list_gates(db), GateKind.BOARD_WRITE, "upside")

    assert (upside.level, upside.opens_at) == (10, 50)


def test_other_gates_are_named_not_counted(db):
    sysop = _node(db)
    create_board(db, "adults", min_age=18, name_requirement="verified", creator=sysop)
    create_channel(db, "inner circle", members_only=True, hidden=True, creator=sysop)
    gates = list_gates(db)

    assert _gate(gates, GateKind.BOARD_READ, "adults").conditions == ("age 18+",)
    assert _gate(gates, GateKind.BOARD_WRITE, "adults").conditions == ("age 18+", "verified name")
    inner = _gate(gates, GateKind.CHANNEL, "inner circle")
    assert inner.conditions == ("members only",) and inner.hidden
    # The level alone still opens it; the conditions are for the screen.
    assert _gate(gates, GateKind.BOARD_READ, "adults").opens_for(0)


def test_a_switched_off_gate_opens_for_nobody(db):
    _node(db)
    save_open_room_settings(db, OpenRoomSettings(enabled=False, min_level=0))
    set_configured_link_enabled(db, False)
    gates = list_gates(db)

    rooms = _gate(gates, GateKind.MRC_OPEN_ROOM, "Open MRC rooms")
    assert rooms.off == "switched off" and not rooms.opens_for(SYSOP_LEVEL)
    assert _gate(gates, GateKind.NODE_MAP, "Node map").off == "Link is off"


def test_level_change_lists_what_is_gained_and_lost(db):
    _node(db)
    gates = list_gates(db)

    up = level_change(gates, 9, 50)
    gained = {(gate.kind, gate.name) for gate in up.gained}
    assert (GateKind.BOARD_WRITE, "lounge") in gained
    assert (GateKind.BOARD_READ, "upside") in gained and (GateKind.BOARD_WRITE, "upside") in gained
    assert (GateKind.DOOR, "blacksite") in gained
    assert (GateKind.NODE_MAP, "Node map") in gained
    assert (GateKind.BOARD_READ, "staff") not in gained
    assert up.lost == ()

    down = level_change(gates, 50, 9)
    assert {(g.kind, g.name) for g in down.lost} == gained and down.gained == ()
    assert level_change(gates, 10, 10).gained == ()


def test_levels_in_use_are_the_thresholds_and_the_levels_held(db):
    _node(db)
    create_user(db, "member", password="hunter2pw", user_level=7)

    assert levels_in_use(db, list_gates(db)) == [0, 5, 7, 10, 50, 99, 100, SYSOP_LEVEL]


# -- the map agrees with the checks


def _real_check(db: Database, user, gate: Gate) -> bool:
    """What the caller's screens decide for `gate`, asked the way they ask."""
    kind = gate.kind
    if kind in (GateKind.BOARD_READ, GateKind.BOARD_WRITE):
        board = next(
            (b for b in visible_boards(db, user, community_id=None, community_scoped=False) if b.id == gate.object_id),
            None,
        )
        if kind is GateKind.BOARD_READ or board is None:
            return board is not None
        return _read_only_reason(db, user, board, closed=False) is None
    if kind in (GateKind.AREA_READ, GateKind.AREA_WRITE):
        area = next((a for a in visible_areas(db, user) if a.id == gate.object_id), None)
        if kind is GateKind.AREA_READ or area is None:
            return area is not None
        return meets_write_gate(db, user, area)
    if kind is GateKind.CHANNEL:
        channel = next((c for c in _visible_channels_for(db, user) if c.id == gate.object_id), None)
        return channel is not None and _authorize_channel_entry(db, channel, user)[0]
    if kind is GateKind.DOOR:
        listed = any(d.id == gate.object_id for d in _visible_doors(db, user, community_id=None, community_scoped=False))
        playable = any(door_id == gate.object_id for door_id, _, _ in playable_registrations(db, user))
        assert listed == playable, "the door picker and Who disagree"
        return listed
    if kind is GateKind.NODE_MAP:
        return may_open_node_map(db, user)
    if kind is GateKind.MAIL:
        return mail_access_refusal(db, user) is None
    if kind is GateKind.MRC_OPEN_ROOM:
        return _open_room_gate_denial(db, user, load_open_room_settings(db)) is None
    if kind is GateKind.SYSOP:
        return is_usable_sysop(user)
    raise AssertionError(f"no real check for {kind}: add one here")


def test_every_gate_agrees_with_the_real_check_at_every_level(db):
    _node(db)
    users = [create_user(db, f"caller{level}", password="hunter2pw", user_level=level) for level in LEVELS]
    gates = list_gates(db)

    assert {gate.kind for gate in gates} == set(GateKind), "the fixture node lacks a gate kind"
    disagreements = [
        f"{gate.kind.value} {gate.name!r} at level {user.user_level}: map says {gate.opens_for(user.user_level)}"
        for gate in gates
        for user in users
        if gate.opens_for(user.user_level) != _real_check(db, user, gate)
    ]
    assert disagreements == []


def test_a_condition_the_map_names_is_one_the_check_applies(db):
    """The map leaves age out of `opens_for` and names it instead; the real
    check does turn away an account too young, at a level that opens it."""
    sysop = _node(db)
    create_board(db, "adults", min_age=18, creator=sysop)
    minor = create_user(db, "minor", password="hunter2pw", user_level=10)
    attest_age(db, minor, date(2015, 1, 1), verifier=sysop)
    gate = _gate(list_gates(db), GateKind.BOARD_READ, "adults")

    assert gate.opens_for(minor.user_level) and gate.conditions == ("age 18+",)
    assert _real_check(db, minor, gate) is False


def test_gates_open_at_is_opens_for(db):
    _node(db)
    gates = list_gates(db)

    for level in LEVELS:
        assert gates_open_at(gates, level) == [g for g in gates if g.opens_for(level)]


# -- every level check in the source is on the map

# Each function in src/netbbs that compares an account's level with anything
# but the SysOp level. A gate kind means the access map covers it; a string
# says why it is not a gate. A new entry is a new gate, or a new rule about
# levels: put a gate kind on the map for it (netbbs.access_map) or say here
# why it isn't one.
LEVEL_CHECK_SITES: dict[tuple[str, str], GateKind | str] = {
    ("netbbs/permissions/levels.py", "require_level"): "the primitive itself",
    ("netbbs/permissions/levels.py", "meets_level"): "the primitive itself",
    ("netbbs/permissions/levels.py", "requires_level.decorator.async_wrapper"): "the primitive itself",
    ("netbbs/permissions/levels.py", "requires_level.decorator.sync_wrapper"): "the primitive itself",
    ("netbbs/communities.py", "passes_level_gate"): GateKind.BOARD_READ,
    ("netbbs/communities.py", "_require_level_gate"): GateKind.BOARD_READ,
    ("netbbs/communities.py", "require_read_gate"): GateKind.BOARD_READ,
    ("netbbs/communities.py", "require_write_gate"): GateKind.BOARD_WRITE,
    ("netbbs/communities.py", "meets_read_gate"): GateKind.BOARD_READ,
    ("netbbs/communities.py", "meets_write_gate"): GateKind.BOARD_WRITE,
    ("netbbs/net/chat_flow.py", "_visible_channels_for"): GateKind.CHANNEL,
    ("netbbs/net/chat_flow.py", "_may_enter_quietly"): GateKind.CHANNEL,
    ("netbbs/net/chat_flow.py", "_authorize_channel_entry"): GateKind.CHANNEL,
    ("netbbs/net/chat_flow.py", "_open_room_gate_denial"): GateKind.MRC_OPEN_ROOM,
    ("netbbs/net/door_flow.py", "_visible_doors"): GateKind.DOOR,
    ("netbbs/net/door_flow.py", "browse_doors"): GateKind.DOOR,
    ("netbbs/net/directory_flow.py", "playable_registrations"): GateKind.DOOR,
    ("netbbs/net/node_map_flow.py", "may_open_node_map"): GateKind.NODE_MAP,
    ("netbbs/mail.py", "mail_access_refusal"): GateKind.MAIL,
    ("netbbs/auth/users.py", "set_user_level"): "who may raise whom (§5.6)",
    ("netbbs/chat/moderation.py", "_ensure_target_rank_allows_moderation"): "moderator rank, not access",
    ("netbbs/net/login_flow.py", "_apply_access_change"): "tells a live session its level changed",
    ("netbbs/net/main_menu.py", "_access_change_notice"): "tells a live session its level changed",
}

_LEVEL_CALLS = {"meets_level", "require_level", "passes_level_gate", "_require_level_gate"}
_SRC = Path(__file__).resolve().parents[1] / "src"


def _is_sysop_level(node: ast.AST | None) -> bool:
    return (isinstance(node, ast.Name) and node.id == "SYSOP_LEVEL") or (
        isinstance(node, ast.Attribute) and node.attr == "SYSOP_LEVEL"
    )


def _level_check_sites() -> set[tuple[str, str]]:
    sites = set()
    for path in sorted((_SRC / "netbbs").rglob("*.py")):
        module = path.relative_to(_SRC).as_posix()
        scope: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                scope.append(node.name)
                self.generic_visit(node)
                scope.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name in _LEVEL_CALLS:
                    position = 2 if name in {"passes_level_gate", "_require_level_gate"} else 1
                    level = node.args[position] if len(node.args) > position else None
                    if not _is_sysop_level(level):
                        sites.add((module, ".".join(scope)))
                self.generic_visit(node)

            def visit_Compare(self, node):
                operands = [node.left, *node.comparators]
                if (
                    any(isinstance(o, ast.Attribute) and o.attr == "user_level" for o in operands)
                    and not any(_is_sysop_level(o) for o in operands)
                    and any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in node.ops)
                ):
                    sites.add((module, ".".join(scope)))
                self.generic_visit(node)

        Visitor().visit(ast.parse(path.read_text(encoding="utf-8")))
    return sites


def test_every_level_check_in_the_source_is_accounted_for():
    found = _level_check_sites()

    unknown = sorted(found - LEVEL_CHECK_SITES.keys())
    assert unknown == [], (
        "a level check the access map does not know about: give it a gate kind in "
        "netbbs.access_map, or list it in LEVEL_CHECK_SITES with why it is not a gate"
    )
    assert sorted(LEVEL_CHECK_SITES.keys() - found) == [], "stale LEVEL_CHECK_SITES entries"


def test_every_gate_kind_is_reached_by_some_check():
    """A gate kind no check uses would be a map entry nothing enforces. The
    SysOp console is checked against SYSOP_LEVEL, which the scan skips."""
    kinds = {kind for kind in LEVEL_CHECK_SITES.values() if isinstance(kind, GateKind)}
    # Areas share the board helpers in netbbs.communities.
    assert kinds | {GateKind.AREA_READ, GateKind.AREA_WRITE, GateKind.SYSOP} == set(GateKind)
