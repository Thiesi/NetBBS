"""Linked resources marked by colour in their lists (issue #1104): a
board, file area or channel shared over NetBBS Link has its name drawn in
`LINKED_COLOR` instead of the accent, with no extra column, and gets
`LINKED_MARKER` after its name where colour can't say it (a plain-ASCII
caller, a row inside SysOp art)."""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards import create_board
from netbbs.chat.channels import create_channel
from netbbs.files.areas import create_file_area
from netbbs.link.boards import linked_board_ids
from netbbs.link.channels import linked_channel_ids
from netbbs.link.files import linked_area_ids
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.picker import ListColumn, pick_item
from netbbs.rendering import ACCENT_COLOR, LINKED_COLOR, LINKED_MARKER, MUTED_COLOR, fg
from netbbs.rendering.art_slots import ListSlotRow, parse_slot_art, render_list_slot_art
from netbbs.storage import Database
from tests.test_list_slot_art import LIST_ART, SlotSession, _screen

ITEMS = ["Local", "Shared"]
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _pick(keys, *, charset="utf-8", columns=False, slot=False, linked=True):
    session = SlotSession(keys, charset=charset)
    kwargs = {}
    if columns:
        kwargs.update(
            columns=[ListColumn("new", 6, MUTED_COLOR)],
            column_values_of=lambda item: ["3 new"],
        )
    if slot:
        kwargs.update(
            slot_art=parse_slot_art(LIST_ART, require_menu=False, require_list=True),
            slot_column_of=lambda item: "",
        )
    asyncio.run(pick_item(
        session, ITEMS, name_of=lambda item: item, stable_id_of=ITEMS.index,
        title="Message boards", empty_message="No boards.",
        linked_of=(lambda item: item == "Shared") if linked else None,
        **kwargs,
    ))
    return "".join(session.written)


def _colour_of(raw: str, word: str) -> str:
    """The last SGR sequence written before the first `word`."""
    index = raw.index(word)
    return _SGR.findall(raw[:index])[-1]


def test_a_linked_name_takes_the_linked_colour_and_the_others_keep_the_accent():
    raw = _pick(["b"])
    assert fg(LINKED_COLOR) in _colour_of(raw, "Shared")
    assert fg(ACCENT_COLOR) in _colour_of(raw, "Local")


def test_a_list_without_linked_of_draws_every_name_in_the_accent():
    raw = _pick(["b"], linked=False)
    assert fg(ACCENT_COLOR) in _colour_of(raw, "Shared")
    assert fg(LINKED_COLOR) not in raw


def test_the_colour_needs_no_marker_and_no_extra_column():
    visible = _SGR.sub("", _pick(["b"]))
    assert "Shared" + LINKED_MARKER not in visible
    assert "LINK" not in visible


def test_a_plain_ascii_caller_gets_the_marker_after_the_name():
    visible = _SGR.sub("", _pick(["b"], charset="ascii"))
    assert "Shared" + LINKED_MARKER in visible
    assert "Local" + LINKED_MARKER not in visible


def test_the_table_form_colours_the_name_cell_and_keeps_the_columns_aligned():
    raw = _pick(["b"], columns=True, charset="ascii")
    assert fg(LINKED_COLOR) in _colour_of(raw, "Shared")
    rows = [line for line in _SGR.sub("", raw).splitlines() if "3 new" in line]
    assert len(rows) == 2
    assert rows[0].index("3 new") == rows[1].index("3 new")
    assert "Shared" + LINKED_MARKER in rows[1]


def test_a_row_inside_list_art_shows_the_marker_whatever_the_charset():
    screen = _screen(_pick(["b"], slot=True, charset="cp437"))
    assert any("Shared" + LINKED_MARKER in row for row in screen)
    assert not any("Local" + LINKED_MARKER in row for row in screen)


def test_a_name_cut_to_fit_list_art_keeps_its_marker_whole():
    art = parse_slot_art(LIST_ART, require_menu=False, require_list=True)
    long_name = "A very long board name that cannot possibly fit the region"
    drawn = render_list_slot_art(art, fields={}, rows=[ListSlotRow("01.", long_name, "", suffix=LINKED_MARKER)])
    row = _screen(drawn)[2]
    assert row.rstrip(" |").endswith(LINKED_MARKER.strip())
    assert len(row) == len(_screen(drawn)[0])


def test_help_explains_the_colour_only_on_a_list_with_a_linked_entry():
    help_key = EditorKey(EditorKeyKind.CTRL, char="h")
    with_linked = _SGR.sub("", _pick([help_key, "b"]))
    without = _SGR.sub("", _pick([help_key, "b"], linked=False))
    assert "is Linked: shared with other nodes over NetBBS Link" in with_linked
    assert "Linked" not in without
    ascii_help = _SGR.sub("", _pick([help_key, "b"], charset="ascii"))
    assert f"A name ending in{LINKED_MARKER} is Linked" in ascii_help


def test_the_linked_id_helpers_read_every_genesis_in_one_query(tmp_path):
    db = Database(tmp_path / "linked.db")
    try:
        sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
        local_board = create_board(db, "Local", creator=sysop)
        shared_board = create_board(db, "Shared", creator=sysop)
        local_area = create_file_area(db, "Local files", creator=sysop)
        shared_area = create_file_area(db, "Shared files", creator=sysop)
        local_channel = create_channel(db, "local", creator=sysop)
        shared_channel = create_channel(db, "shared", creator=sysop)
        for table, row_id in (("boards", shared_board.id), ("file_areas", shared_area.id), ("channels", shared_channel.id)):
            db.connection.execute(f"UPDATE {table} SET link_genesis_json = '{{}}' WHERE id = ?", (row_id,))
        db.connection.commit()
        assert linked_board_ids(db) == {shared_board.id}
        assert linked_area_ids(db) == {shared_area.id}
        assert linked_channel_ids(db) == {shared_channel.id}
        assert local_board.id not in linked_board_ids(db)
        assert local_area.id not in linked_area_ids(db) and local_channel.id not in linked_channel_ids(db)
    finally:
        db.close()
