"""What a test *can* hold the SysOp console's presentation to.

A review by eye is what decides whether a screen reads well; it cannot be
trusted to keep deciding it. These walk the console on a seeded node with
redraw-in-place on -- the default for a new account, and the mode in which a
screen that is too tall simply loses its top -- and assert the things that were
wrong before the detail-panel pass and would be silently wrong again:

* no screen is taller or wider than the terminal it is drawn on;
* a fact's label and its value are different colours, and a panel has headings;
* no menu description is long enough to be cut mid-word.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path

import pytest

from netbbs.net.admin_flow import admin_menu
from netbbs.rendering import LABEL_COLOR, METADATA_COLOR, fg
from netbbs.rendering.width import display_width
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_detail_view import ScriptedSession, _Exhausted

_SGR = re.compile(r"\x1b\[[0-9;]*m")
_ROOT = Path(__file__).resolve().parent.parent


def _load_gallery():
    """`scripts/sysop_gallery.py` owns the list of console screens and the node
    they are photographed on, so the screens a reviewer looks at and the
    screens held to the terminal's size here are the same list."""
    spec = importlib.util.spec_from_file_location("sysop_gallery", _ROOT / "scripts" / "sysop_gallery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_gallery = _load_gallery()
_PANELS = _gallery.WALKS


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    root = tmp_path_factory.mktemp("console")
    database = Database(root / "node.db")
    sysop = _gallery.seed(database)
    database.close()
    lane = DatabaseLane(root / "node.db")
    yield lane, sysop, root
    lane.close()


def _screen(node, keys, *, width=80, height=24) -> tuple[list[str], str]:
    """The rows on the terminal once the named walk's keys have been typed,
    plain and styled."""
    lane, sysop, root = node
    keys, title = keys
    session = ScriptedSession(keys, width=width, height=height)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(
            session, lane, sysop,
            node_controls=_gallery.node_controls(root), link_context=_gallery.link_context(),
        ))
    styled = "".join(session.written)
    styled = styled[styled.rfind("\x1b[2J"):]
    rows = session.on_terminal()
    assert rows[0].endswith(title), f"walk landed on {rows[0]!r}, not {title!r}"
    return rows, styled


@pytest.mark.parametrize("name", sorted(_PANELS))
def test_no_console_screen_is_taller_or_wider_than_the_terminal(node, name):
    rows, _styled = _screen(node, _PANELS[name])
    assert len(rows) <= 24, f"{name}: {len(rows)} rows on a 24-row terminal\n" + "\n".join(rows)
    too_wide = [row for row in rows if display_width(row) > 80]
    assert not too_wide, f"{name}: wider than 80 columns: {too_wide!r}"


@pytest.mark.parametrize("name", [
    "link status", "backup", "node", "users > a user", "content > a board", "content > a door",
    "settings > join link", "settings > update", "trust > published identity",
])
def test_a_panel_has_headings_and_colours_a_label_apart_from_its_value(node, name):
    rows, styled = _screen(node, _PANELS[name])
    assert any(row and row == row.upper() and row.strip().isascii() and row[0].isalpha() for row in rows), (
        f"{name}: no uppercase section heading\n" + "\n".join(rows)
    )
    assert fg(LABEL_COLOR) in styled, f"{name}: no label-coloured text"
    assert fg(METADATA_COLOR) in styled, f"{name}: no heading-coloured text"
    # A labelled row is never one colour end to end.
    labelled = [line for line in styled.split("\r\n") if fg(LABEL_COLOR) in line and ":" in _SGR.sub("", line)]
    assert labelled
    assert all(len(set(re.findall(r"\x1b\[38;5;(\d+)m", line))) >= 2 for line in labelled), name


@pytest.mark.parametrize("name", ["link status", "backup", "settings > join link", "trust > history"])
def test_a_paged_panel_still_fits_a_narrow_terminal(node, name):
    rows, _styled = _screen(node, _PANELS[name], width=40, height=24)
    assert len(rows) <= 24, "\n".join(rows)
    assert all(display_width(row) <= 40 for row in rows), "\n".join(rows)


def test_no_menu_description_is_long_enough_to_be_cut_mid_word():
    """`menu_grid` cuts a description at its column -- 34 characters in two
    columns at 80 -- rather than wrapping it (`_entry_block_lines`). Forty-nine
    had outgrown that and were shown as "Managed netbbs.org subdomain statu"."""
    source = (_ROOT / "src/netbbs/net/admin_flow.py").read_text(encoding="utf-8")
    too_long = [brief for brief in re.findall(r'brief="([^"{}]*)"', source) if len(brief) > 34]
    assert not too_long, too_long
