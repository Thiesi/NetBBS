"""The Boards, file areas and Chat channels lists drawn as the SysOp's art
(issue #929): each list masthead's mode, the lists filling the art's
`{list}` slot with their own compact value, and the console's [M]ode,
[C]heck and [P]review for them."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.files import create_file_area
from netbbs.net import admin_flow, board_flow, chat_flow, file_flow
from netbbs.net.board_list_banner import (
    board_list_banner_path,
    load_board_list_banner,
    load_board_list_slot_art,
    set_board_list_banner_enabled,
)
from netbbs.net.char_input import InputHistory
from netbbs.net.chat_channel_picker_banner import (
    chat_channel_picker_banner_path,
    set_chat_channel_picker_banner_enabled,
)
from netbbs.net.file_area_banner import file_area_banner_path, set_file_area_banner_enabled
from netbbs.net.list_art import (
    BOARD_LIST,
    CHAT_CHANNEL_PICKER,
    FILE_AREA,
    MASTHEAD_MODE,
    SLOTS_MODE,
    list_art_mode,
    set_list_art_mode,
)
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.ansi_parse import parse_ansi_into_buffer
from netbbs.rendering.screen_buffer import ScreenBuffer
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_board_list_masthead_integration import FakeSession as BoardSession
from tests.test_chat_channel_picker_masthead_integration import FakeSession as ChatSession
from tests.test_file_area_masthead_integration import FakeSession as FileSession

ESC = "\x1b"
ART = "\r\n".join([
    "+----------------------------------------------+",
    "| {title 30}                                   |",
    "| {list 44x4}                                  |",
    "|                                              |",
    "|                                              |",
    "|                                              |",
    "+---------------------------- {page 6} --------+",
]).encode("ascii")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def sysop(db):
    return create_user(db, "inkwell", password="hunter2", user_level=255)


def _text(session) -> str:
    return strip_ansi("".join(session.written))


def _art_screen(session) -> str:
    """The last full-screen draw, as the rows a terminal shows: art is
    positioned cell by cell, so stripping its escapes loses the spaces."""
    text = "".join(session.written)
    buffer = ScreenBuffer(80, 24)
    parse_ansi_into_buffer(text[text.rfind(ESC + "[2J"):], buffer)
    return "\n".join("".join(buffer.get_cell(r, c).char for c in range(80)).rstrip() for r in range(24))


# -- the mode -------------------------------------------------------------------


def test_the_mode_starts_above_the_list_and_switches_per_list(db):
    assert list_art_mode(db, BOARD_LIST) == MASTHEAD_MODE
    set_list_art_mode(db, BOARD_LIST, SLOTS_MODE)
    assert list_art_mode(db, BOARD_LIST) == SLOTS_MODE
    assert list_art_mode(db, FILE_AREA) == MASTHEAD_MODE
    with pytest.raises(ValueError):
        set_list_art_mode(db, BOARD_LIST, "sideways")


def test_in_slots_mode_the_art_is_the_list_not_a_masthead(db):
    board_list_banner_path(db).write_bytes(ART)
    set_board_list_banner_enabled(db, True)
    assert load_board_list_banner(db) != "" and load_board_list_slot_art(db) is None
    set_list_art_mode(db, BOARD_LIST, SLOTS_MODE)
    assert load_board_list_banner(db) == ""
    art = load_board_list_slot_art(db)
    assert art is not None and art.list is not None and art.problems == ()
    set_board_list_banner_enabled(db, False)
    assert load_board_list_slot_art(db) is None


# -- the three lists --------------------------------------------------------------


def test_the_board_list_fills_the_art_with_what_is_new(db, alice):
    create_board(db, "General", creator=alice)
    create_board(db, "Inks", creator=alice)
    board_list_banner_path(db).write_bytes(ART)
    set_board_list_banner_enabled(db, True)
    set_list_art_mode(db, BOARD_LIST, SLOTS_MODE)

    session = BoardSession(["b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    text = _art_screen(session)
    assert "| Available message boards" in text  # the {title} slot
    assert "01. General" in text and "not visited yet" in text
    assert "02. Inks" in text
    assert "+-----" in text and "1/1" in text


def test_a_board_the_caller_cannot_post_to_yet_says_so_in_the_art(db, alice):
    board = create_board(db, "Verified only", creator=alice)
    db.connection.execute("UPDATE boards SET name_requirement = 'verified' WHERE id = ?", (board.id,))
    db.connection.commit()
    board_list_banner_path(db).write_bytes(ART)
    set_board_list_banner_enabled(db, True)
    set_list_art_mode(db, BOARD_LIST, SLOTS_MODE)

    session = BoardSession(["b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    assert "needs verification" in _art_screen(session)


def test_the_file_area_list_fills_the_art_with_file_counts(db, lane, alice):
    create_file_area(db, "Utilities", creator=alice)
    file_area_banner_path(db).write_bytes(ART)
    set_file_area_banner_enabled(db, True)
    set_list_art_mode(db, FILE_AREA, SLOTS_MODE)

    session = FileSession(["b"])
    asyncio.run(file_flow.browse_file_areas(session, lane, alice))
    text = _art_screen(session)
    assert "01. Utilities" in text and "0 files" in text and "+-----" in text


def test_the_channel_list_fills_the_art_with_who_is_online(db, lane, alice):
    create_channel(db, "lobby", creator=alice)
    chat_channel_picker_banner_path(db).write_bytes(ART)
    set_chat_channel_picker_banner_enabled(db, True)
    set_list_art_mode(db, CHAT_CHANNEL_PICKER, SLOTS_MODE)

    session = ChatSession(["b"])
    asyncio.run(asyncio.wait_for(chat_flow.browse_channels(
        session, lane, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice,
    ), timeout=2))
    text = _art_screen(session)
    assert "01. lobby" in text and "0 online" in text and "+-----" in text


def test_masthead_mode_draws_the_list_as_before(db, alice):
    create_board(db, "General", creator=alice)
    board_list_banner_path(db).write_bytes(ART)
    set_board_list_banner_enabled(db, True)
    session = BoardSession(["b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    text = _text(session)
    # The art above, unfilled, and the generated list below it.
    assert "{list 44x4}" in text and "01. General" in text


# -- the console ------------------------------------------------------------------


class ConsoleSession(BoardSession):
    async def read_any_key(self) -> str:
        return " "


def test_mode_switches_and_is_audited(db, lane, sysop):
    session = ConsoleSession()
    asyncio.run(admin_flow._toggle_list_art_mode(session, lane, sysop, FILE_AREA))
    assert list_art_mode(db, FILE_AREA) == SLOTS_MODE
    logged = db.connection.execute(
        "SELECT detail FROM moderation_log WHERE action = 'set_file_area_art_mode'"
    ).fetchall()
    assert [row[0] for row in logged] == [SLOTS_MODE]


def test_check_reports_the_slot_and_whether_a_list_fits(db, lane, sysop):
    create_board(db, "General", creator=sysop)
    board_list_banner_path(db).write_bytes(ART)
    set_board_list_banner_enabled(db, True)
    session = ConsoleSession()
    asyncio.run(admin_flow._check_list_slot_art_screen(session, lane, sysop, BOARD_LIST))
    text = _text(session)
    assert "{list 44x4} at row 3, column 3" in text
    assert "[M]ode changes that" in text  # still in masthead mode
    assert "Your list: fits, 4 entries a page" in text
    assert "needs verification: fits" in text


def test_check_warns_when_the_gate_note_leaves_names_too_little_room(db, lane, sysop):
    narrow = "\r\n".join(["{list 34x4}", "", "", ""]).encode("ascii")
    board_list_banner_path(db).write_bytes(narrow)
    session = ConsoleSession()
    asyncio.run(admin_flow._check_list_slot_art_screen(session, lane, sysop, BOARD_LIST))
    text = _text(session)
    assert "needs verification: generated list instead" in text and "they need 12" in text


def test_preview_draws_your_list_into_the_art(db, lane, sysop):
    create_board(db, "General", creator=sysop)
    board_list_banner_path(db).write_bytes(ART)
    session = ConsoleSession()
    asyncio.run(admin_flow._preview_list_slot_art(session, lane, sysop, BOARD_LIST))
    text = _art_screen(session)
    assert "Board list" in text and "01. General" in text and "1/1" in text


def test_each_list_has_a_slot_sample_that_can_be_used():
    from netbbs.net.banner_presets import (
        BOARD_LIST_MASTHEAD_PRESETS,
        CHAT_CHANNEL_PICKER_MASTHEAD_PRESETS,
        FILE_AREA_MASTHEAD_PRESETS,
        load_board_list_masthead_preset,
        load_chat_channel_picker_masthead_preset,
        load_file_area_masthead_preset,
    )
    from netbbs.rendering import decode_banner_bytes
    from netbbs.rendering.art_slots import parse_slot_art

    for presets, load in (
        (BOARD_LIST_MASTHEAD_PRESETS, load_board_list_masthead_preset),
        (FILE_AREA_MASTHEAD_PRESETS, load_file_area_masthead_preset),
        (CHAT_CHANNEL_PICKER_MASTHEAD_PRESETS, load_chat_channel_picker_masthead_preset),
    ):
        samples = [preset for preset in presets if preset.mode == "slots"]
        assert len(samples) == 1
        art = parse_slot_art(decode_banner_bytes(load(samples[0])), require_menu=False, require_list=True)
        assert art.problems == () and art.list.height >= 10
        # Fits a 24-row terminal with the nav, the trailer and the prompt.
        assert art.height + 3 < 24


def test_preview_shows_channels_as_your_list_does(db, lane, sysop, alice):
    create_channel(db, "lobby", creator=sysop)
    create_channel(db, "back-room", creator=sysop, hidden=True)
    chat_channel_picker_banner_path(db).write_bytes(ART)
    session = ConsoleSession()
    asyncio.run(admin_flow._preview_list_slot_art(session, lane, alice, CHAT_CHANNEL_PICKER))
    text = _art_screen(session)
    assert "01. lobby" in text and "back-room" not in text


def test_check_says_an_ascii_reader_gets_the_generated_list(db, lane, sysop):
    create_board(db, "General", creator=sysop)
    board_list_banner_path(db).write_bytes(ART)
    session = ConsoleSession()
    session.output_charset = "ascii"
    asyncio.run(admin_flow._check_list_slot_art_screen(session, lane, sysop, BOARD_LIST))
    assert "Your list: generated list instead -- your terminal reads plain ASCII" in _text(session)


def test_preview_sends_the_art_as_art(db, lane, sysop):
    create_board(db, "General", creator=sysop)
    board_list_banner_path(db).write_bytes(ART.replace(b"+---", "+\u2665--".encode("utf-8"), 1))
    session = ConsoleSession()
    session.output_charset = "cp437"
    asyncio.run(admin_flow._preview_list_slot_art(session, lane, sysop, BOARD_LIST))
    assert "\x03" in "".join(session.written)


def test_board_values_are_read_once_per_list_not_per_row(db, alice, monkeypatch):
    for name in ("General", "Inks", "Nibs"):
        create_board(db, name, creator=alice)
    board_list_banner_path(db).write_bytes(ART)
    set_board_list_banner_enabled(db, True)
    set_list_art_mode(db, BOARD_LIST, SLOTS_MODE)
    calls = []
    real = board_flow.unread_post_count
    monkeypatch.setattr(board_flow, "unread_post_count", lambda *a, **k: calls.append(1) or real(*a, **k))
    session = BoardSession(["b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    # Once per board for the art's value; the generated table's own column
    # isn't drawn. The picker asks for every row's value several times per
    # render, so a per-call read would be a multiple of three.
    assert len(calls) == 3
