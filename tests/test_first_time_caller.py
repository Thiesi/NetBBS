"""A first-time caller in the browser (issue #840).

The field test's newcomer typed "3" and Enter where "03" was wanted, typed
whole words at one-key menus, found no help on the main menu, was asked a
Unicode question the browser makes pointless, and could not write to "sysop".
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import char_input
from netbbs.net.char_input import InputHistory
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.login_flow import _confirm_unicode_style
from netbbs.net.mail_flow import resolve_sysop_alias
from netbbs.net.main_menu import _main_menu
from netbbs.net.picker import pick_item
from netbbs.net.unicode_style_preference import unicode_style_enabled, unicode_style_ever_set
from netbbs.storage.database import Database
from tests.test_new_scan import FakeSession, _visible_text


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


# -- one digit and Enter (F115) -------------------------------------------------


@pytest.mark.parametrize("enter", ["\r", "\n"])
def test_one_digit_and_enter_picks_that_row(enter):
    session = FakeSession(["3", enter])
    picked = asyncio.run(
        pick_item(
            session, ["Pens", "Inks", "Paper", "Nibs"], name_of=str, stable_id_of=lambda item: len(item),
            title="Boards", empty_message="none",
        )
    )
    assert picked == "Paper"


def test_the_list_says_a_number_is_enough():
    session = FakeSession(["b"])
    asyncio.run(pick_item(session, ["Pens"], name_of=str, stable_id_of=len, title="Boards", empty_message="none"))
    assert "or type a number to select" in _visible_text(session)


# -- a word typed at a one-key prompt (F114, F112) ---------------------------------


class _Bytes:
    """A byte source fed from a script, as a Telnet or SSH session reads."""

    def __init__(self, data: bytes):
        self._data = list(data)

    async def read_byte(self):
        return self._data.pop(0) if self._data else None

    async def read_byte_with_timeout(self, timeout):
        return self._data.pop(0) if self._data else None


async def _no_echo(text: str) -> None:
    pass


def test_the_rest_of_a_guarded_word_and_its_enter_are_dropped():
    source = _Bytes(b"ommunities\r\nx")
    char_input.arm_word_guard(source)
    assert asyncio.run(char_input.read_key(source, _no_echo)) == "x"


def test_a_digit_ends_the_guard_and_is_read():
    source = _Bytes(b"01")
    char_input.arm_word_guard(source)
    assert asyncio.run(char_input.read_key(source, _no_echo)) == "0"


def test_a_pause_ends_the_guard(monkeypatch):
    source = _Bytes(b"ok")
    char_input.arm_word_guard(source)
    monkeypatch.setattr(char_input.time, "monotonic", lambda: 10**9)
    assert asyncio.run(char_input.read_key(source, _no_echo)) == "o"


def test_a_yes_no_answer_arms_the_guard():
    armed = []

    class _Session(FakeSession):
        def arm_word_guard(self):
            armed.append(True)

        async def read_editor_key(self, **kwargs):
            from netbbs.net.char_input import EditorKey, EditorKeyKind

            return EditorKey(EditorKeyKind.CHAR, char=await self.read_key())

    asyncio.run(prompt_yes_no(_Session(["n"]), "Switch?", default=False))
    assert armed


# -- the main menu ------------------------------------------------------------------


class _MenuSession(FakeSession):
    def __init__(self, inputs, transport_name="telnet"):
        super().__init__(inputs)
        self.transport_name = transport_name
        self.armed = 0

    def arm_word_guard(self):
        self.armed += 1

    async def discard_buffered_input(self):
        pass

    async def read_any_key(self, echo: bool = True) -> str:
        return await self.read_key()


def _menu(db, session, user):
    asyncio.run(_main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user))


def test_help_explains_the_board_and_names_the_sysop(db):
    create_user(db, "InkWell", password="hunter2", user_level=SYSOP_LEVEL)
    lena = create_user(db, "lena_h", password="hunter2", user_level=10)
    session = _MenuSession(["?", " ", "l", "y"])
    _menu(db, session, lena)
    text = _visible_text(session)

    assert "[?] Help" in text
    assert "How this board works" in text
    assert "run by InkWell" in text
    assert "NetBBS-User-Handbook" in text


def test_a_letter_at_the_main_menu_arms_the_word_guard(db):
    lena = create_user(db, "lena_h", password="hunter2", user_level=10)
    session = _MenuSession(["l", "y"])
    _menu(db, session, lena)
    assert session.armed >= 1


# -- the Unicode question (F090) and "sysop" (F087) -----------------------------------


def test_the_browser_is_not_asked_about_plain_ascii(db):
    lena = create_user(db, "lena_h", password="hunter2", user_level=10)
    session = _MenuSession([], transport_name="web")
    asyncio.run(_confirm_unicode_style(session, db, lena))

    assert "Switch to plain ASCII" not in _visible_text(session)
    # Not saved: the same account on an ASCII-only client is still asked.
    assert not unicode_style_ever_set(db, lena) and unicode_style_enabled(db, lena)


def test_sysop_as_an_address_names_the_first_sysop(db):
    create_user(db, "InkWell", password="hunter2", user_level=SYSOP_LEVEL)
    create_user(db, "Copperplate", password="hunter2", user_level=SYSOP_LEVEL)

    assert resolve_sysop_alias(db, "sysop") == "InkWell"
    assert resolve_sysop_alias(db, "SysOp") == "InkWell"
    assert resolve_sysop_alias(db, "Harold") == "Harold"


def test_an_enter_right_behind_the_answer_ends_the_guard():
    """"y", Enter, "n": two answers, not one word -- the second is read."""
    source = _Bytes(b"\rn")
    char_input.arm_word_guard(source)
    asyncio.run(char_input.discard_buffered_enter(source))
    assert asyncio.run(char_input.read_key(source, _no_echo)) == "n"


def test_a_fast_no_does_not_leak_its_o_through_the_enter_check():
    """"n" is the answer; the Enter check peeks "o", pushes it back, and the
    next screen must still not read it (review on #871)."""
    source = _Bytes(b"o\rx")
    char_input.arm_word_guard(source)
    asyncio.run(char_input.discard_buffered_enter(source))
    assert asyncio.run(char_input.read_key(source, _no_echo)) == "x"


def test_an_umlaut_in_a_guarded_word_is_dropped_too():
    source = _Bytes("\u00dcbersicht\rx".encode())  # Uebersicht, Enter, x
    char_input.arm_word_guard(source)
    assert asyncio.run(char_input.read_key(source, _no_echo)) == "x"


# -- the browser: clicks and Ctrl-H ------------------------------------------------


def _web_scenario(handler, events):
    import aiohttp

    from tests.test_web_line_editing import _run_server

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    for event in events:
                        await ws.send_json(event)
                    await asyncio.sleep(0.5)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_a_click_is_a_key_at_a_menu_but_types_nothing_into_a_line():
    results = []

    async def handler(session):
        results.append(await session.read_key())
        results.append(await session.read_line())

    _web_scenario(handler, [
        {"type": "click", "data": "m"},
        {"type": "click", "data": "b"},
        {"type": "key", "data": "hi\r"},
    ])
    assert results == ["m", "hi"]


def test_a_clicked_row_number_arrives_as_its_two_digits():
    results = []

    async def handler(session):
        results.append(await session.read_key())
        results.append(await session.read_key())

    _web_scenario(handler, [{"type": "click", "data": "03"}])
    assert results == ["0", "3"]


def test_ctrl_h_reaches_a_menu_in_the_browser():
    from netbbs.net.char_input import HELP_KEY

    results = []

    async def handler(session):
        results.append(await session.read_key())

    _web_scenario(handler, [{"type": "key", "data": "\x08"}])
    assert results == [HELP_KEY]


def test_sysop_typed_at_the_review_screens_to_reaches_the_sysop(tmp_path):
    """[T]o on the review screen resolves "sysop" as the first prompt does
    (review on #871)."""
    from netbbs.mail import list_inbox
    from netbbs.net.mail_flow import browse_mail
    from netbbs.storage.execution import DatabaseLane
    from tests.test_mail_flow import FakeSession as MailSession

    db = Database(tmp_path / "node.db")
    inkwell = create_user(db, "InkWell", password="hunter2pw", user_level=SYSOP_LEVEL)
    lena = create_user(db, "lena_h", password="hunter2pw", user_level=10)
    create_user(db, "harold", password="hunter2pw", user_level=10)
    session = MailSession(keys=["c", "t", "s", "b"], lines=["harold", "Hello", "Body", "/done", "sysop"])
    lane = DatabaseLane(db.path)
    try:
        asyncio.run(browse_mail(session, lane, lena))
        assert [message.subject for message in list_inbox(db, inkwell)] == ["Hello"]
    finally:
        lane.close()
        db.close()
