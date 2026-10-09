"""Paced list art (issue #929, step 6 for the list screens): a list's art
plays at its speed the first time a caller arrives at that list in a
session, and is drawn at once on every page, cursor move, search and
redraw after that."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net import art_pacing
from netbbs.net.art_pacing import ART_SPEEDS, art_speed, set_art_speed
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.list_art import BOARD_LIST, CHAT_CHANNEL_PICKER, FILE_AREA, LIST_KINDS
from netbbs.net.picker import pick_item
from netbbs.rendering.art_slots import parse_slot_art
from netbbs.storage.database import Database
from tests.test_list_slot_art import BOARDS, LIST_ART, SlotSession

DOWN = EditorKey(EditorKeyKind.DOWN)


class PacedSession(SlotSession):
    """A slot-art session with a live terminal, so art may be paced."""

    paces_art = True
    in_break_in = False

    async def take_waiting_key(self, timeout: float) -> bool:
        return False


class _Calls(list):
    """The paced draws' texts, and the time limit each was given."""

    def __init__(self) -> None:
        super().__init__()
        self.limits: list[float] = []


@pytest.fixture
def paced(monkeypatch):
    """Record each paced draw instead of sending it at a line speed."""
    calls = _Calls()

    async def fake_pace(session, text, *, speed, write, limit=0, clock=None):
        calls.append(text)
        calls.limits.append(limit)
        await write(text)

    monkeypatch.setattr(art_pacing, "pace", fake_pace)
    return calls


def _pick(session, *, speed=2400, once=BOARD_LIST, art_text=LIST_ART, masthead="", limit=0):
    art = parse_slot_art(art_text, require_menu=False, require_list=True) if art_text else None
    return asyncio.run(pick_item(
        session, BOARDS, name_of=lambda item: item, stable_id_of=BOARDS.index,
        title="Message boards", empty_message="No boards.",
        slot_art=art, slot_column_of=lambda item: f"{len(item)} new",
        masthead=masthead, art_speed=speed, art_limit=limit, art_once=once,
    ))


def test_list_art_plays_on_the_first_arrival(paced):
    _pick(PacedSession(["b"]))
    assert len(paced) == 1
    assert "Message boards" in paced[0]


def test_paging_and_the_cursor_draw_the_art_at_once(paced):
    _pick(PacedSession(["n", DOWN, "p", DOWN, "b"]))
    assert len(paced) == 1


def test_coming_back_to_the_list_in_the_same_session_draws_it_at_once(paced):
    session = PacedSession(["b", "b"])
    _pick(session)
    _pick(session)
    assert len(paced) == 1


def test_each_list_plays_once_on_its_own(paced):
    session = PacedSession(["b", "b"])
    _pick(session, once=BOARD_LIST)
    _pick(session, once=FILE_AREA)
    assert len(paced) == 2


@pytest.mark.parametrize("change", [
    {"speed": 0},
    {"once": ""},
])
def test_no_speed_draws_the_art_at_once(paced, change):
    _pick(PacedSession(["b"]), **change)
    assert paced == []


def test_no_live_terminal_or_a_break_in_draws_the_art_at_once(paced):
    quiet = PacedSession(["b"])
    quiet.paces_art = False
    _pick(quiet)
    broken_in = PacedSession(["b"])
    broken_in.in_break_in = True
    _pick(broken_in)
    quick = PacedSession(["b"])
    quick.animations_enabled = False
    _pick(quick)
    assert paced == []


def test_an_ascii_caller_gets_the_generated_list_unpaced(paced):
    _pick(PacedSession(["b"], charset="ascii"))
    assert paced == []


def test_a_masthead_above_the_list_plays_once_too(paced):
    session = PacedSession(["n", "p", "b"])
    _pick(session, art_text="", masthead="== THE NIB & QUILL BOARDS ==")
    assert len(paced) == 1
    assert "THE NIB & QUILL" in paced[0]


@pytest.mark.parametrize("art", [{}, {"art_text": "", "masthead": "== THE NIB & QUILL BOARDS =="}])
def test_the_lists_time_limit_reaches_the_pacing(paced, art):
    _pick(PacedSession(["b"]), limit=30, **art)
    assert paced.limits == [30]


def test_paced_list_art_still_arrives_whole(paced):
    paced_session = PacedSession(["b"])
    _pick(paced_session)
    plain_session = PacedSession(["b"])
    _pick(plain_session, speed=0)
    assert "".join(paced_session.written) == "".join(plain_session.written)


def test_each_list_has_its_own_speed(tmp_path):
    db = Database(tmp_path / "speed.db")
    try:
        for kind in LIST_KINDS:
            assert art_speed(db, kind) == 0
        set_art_speed(db, BOARD_LIST, 9600)
        set_art_speed(db, CHAT_CHANNEL_PICKER, 38400)
        assert art_speed(db, BOARD_LIST) == 9600
        assert art_speed(db, FILE_AREA) == 0
        assert art_speed(db, CHAT_CHANNEL_PICKER) == 38400
        assert set(ART_SPEEDS) >= {art_speed(db, kind) for kind in LIST_KINDS}
    finally:
        db.close()
