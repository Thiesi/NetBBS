"""
`netbbs.net.location`: every breadcrumb comes from where the session is,
never from a path typed at the call site (issue #1246).
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import pathlib

import pytest

from netbbs.net.admin_flow import admin_menu
from netbbs.net.location import crumbs, entered, located
from netbbs.net.session import Session
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_detail_view import ScriptedSession, _Exhausted

_NET = pathlib.Path(__file__).resolve().parent.parent / "src" / "netbbs" / "net"


class _Bare(Session):
    """Only what `netbbs.net.location` touches; the I/O is never called."""

    async def write(self, text):  # pragma: no cover
        raise NotImplementedError

    async def read_line(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def read_key(self, echo=True):  # pragma: no cover
        raise NotImplementedError

    async def read_editor_key(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def close(self):  # pragma: no cover
        raise NotImplementedError


def _session() -> Session:
    return _Bare()


def test_crumbs_start_with_the_node_and_follow_the_trail():
    session = _session()
    assert crumbs(session) == ("NetBBS",)
    with entered(session, "SysOp"), entered(session, "Operations"):
        assert crumbs(session) == ("NetBBS", "SysOp", "Operations")
        assert crumbs(session, own=True) == ("NetBBS", "SysOp")
    assert crumbs(session) == ("NetBBS",)


def test_the_trail_is_restored_on_an_exception():
    session = _session()
    with pytest.raises(RuntimeError):
        with entered(session, "Settings"):
            raise RuntimeError("boom")
    assert session.location == ()


def test_located_names_the_screen_while_it_runs():
    session = _session()
    seen = []

    @located("Outbox")
    async def outbox(session):
        seen.append(crumbs(session, own=True))
        seen.append(crumbs(session))

    with entered(session, "SysOp"):
        asyncio.run(outbox(session))
        assert session.location == ("SysOp",)
    assert seen == [("NetBBS", "SysOp"), ("NetBBS", "SysOp", "Outbox")]


def test_the_class_default_trail_is_never_shared():
    first, second = _session(), _session()
    with entered(first, "Mail"):
        assert second.location == ()


def _typed_paths() -> list[str]:
    """Every call in `netbbs.net` that still spells a breadcrumb itself: a
    `breadcrumb=` keyword whose value is not a `crumbs(...)` call."""
    found = []
    for path in sorted(_NET.rglob("*.py")):
        if path.name == "location.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "breadcrumb":
                    continue
                value = keyword.value
                if isinstance(value, ast.Call) and getattr(value.func, "id", None) == "crumbs":
                    continue
                if isinstance(value, ast.Tuple) and not value.elts:
                    continue  # before sign-in: no node to be in yet
                found.append(f"{path.relative_to(_NET).as_posix()}:{node.lineno}")
    return found


def test_no_screen_types_its_own_path():
    # The literal paths drifted apart (issue #1246); a new screen must take
    # its breadcrumb from `crumbs(session)` too.
    assert _typed_paths() == []


# -- the SysOp gallery: each step is beside or under the screen before it -----

_ROOT = _NET.parent.parent.parent
_spec = importlib.util.spec_from_file_location("sysop_gallery", _ROOT / "scripts" / "sysop_gallery.py")
_gallery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gallery)

#: A screen whose title is a sentence goes by its menu entry's word on the trail.
_LABELS = {
    "SysOp operations console": "SysOp",
    "Manage message boards/file areas/chat channels": "Content",
    "Node management": "Node",
    "Level Admin SysOp Tool (LAST)": "Levels",
}


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    root = tmp_path_factory.mktemp("trail")
    database = Database(root / "node.db")
    sysop = _gallery.seed(database)
    database.close()
    lane = DatabaseLane(root / "node.db")
    yield lane, sysop, root
    lane.close()


def _path(node, keys) -> list[str]:
    """The breadcrumb line's segments once `keys` are typed, on a terminal
    wide enough that no path collapses to its title."""
    lane, sysop, root = node
    session = ScriptedSession(list(keys), width=200)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(
            session, lane, sysop,
            node_controls=_gallery.node_controls(root), link_context=_gallery.link_context(),
        ))
    return [segment.strip() for segment in session.on_terminal()[0].split("\u203a")]


@pytest.mark.parametrize("name", sorted(_gallery.WALKS))
def test_a_console_screen_is_beside_or_under_the_one_that_opened_it(node, name):
    # The path a walk shows only ever grows by the screen just left (it
    # opened this one) or stays (a picker's pick, a page turn). A screen
    # that drops an ancestor -- "Settings › Timestamp display" with no
    # "SysOp" -- or names one it never passed through fails here.
    keys, title = _gallery.WALKS[name]
    steps = [_path(node, keys[:typed]) for typed in range(len(keys) + 1)]
    assert steps[-1][-1] == title
    assert steps[0] == ["NetBBS", "SysOp operations console"]
    for before, after in zip(steps, steps[1:]):
        if after == before:
            continue
        *ancestors, _title = after
        opened_by = [*before[:-1], _LABELS.get(before[-1], before[-1])]
        assert ancestors in (before[:-1], opened_by), f"{' › '.join(before)}  ->  {' › '.join(after)}"
        assert len(set(after)) == len(after), f"a name shown twice: {after}"
