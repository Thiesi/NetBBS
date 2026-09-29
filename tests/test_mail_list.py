"""The mailbox (issue #810).

Mail opens on the Inbox: a From / Subject / Date table with a cursor, the
board post list's shape (issue #679), with Sent, Compose and a kept
letter's Draft on its action bar. Before #810 it opened a four-option menu,
and the Inbox and Sent were the generic picker: each row numbered twice
(`01. (#5) [NEW] ...`), no columns, and a `[S]earch` that said "by name"
and matched the subject with its `[NEW] ` prefix.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs import mail as mail_module
from netbbs.auth.users import create_user
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.mail import get_mail, list_inbox, mark_read, send_mail
from netbbs.net import mail_flow
from netbbs.net import main_menu as main_menu_module
from netbbs.net.char_input import EditorKey, EditorKeyKind, InputCancelled, InputHistory
from netbbs.net.mail_flow import browse_mail
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.rendering.width import display_width
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CLEAR = "\x1b[2J"
_KINDS = {
    "UP": EditorKeyKind.UP,
    "DOWN": EditorKeyKind.DOWN,
    "ENTER": EditorKeyKind.ENTER,
    "PGDN": EditorKeyKind.PAGE_DOWN,
    "PGUP": EditorKeyKind.PAGE_UP,
}
ESC = object()


class FakeSession:
    """Keys and lines from one script, in order; Up/Down/Enter arrive as
    structured keys, the way a real terminal's do."""

    def __init__(self, inputs, *, width=80, height=24):
        self._inputs = list(inputs)
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\r\n")

    def _next(self, what):
        if not self._inputs:
            raise AssertionError(f"ran out of scripted input ({what})")
        return self._inputs.pop(0)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        line = self._next("read_line")
        if line is ESC:
            assert kwargs.get("cancellable")
            raise InputCancelled()
        return line

    async def read_key(self, echo: bool = True) -> str:
        return self._next("read_key")

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        raw = self._next("read_editor_key")
        if raw in _KINDS:
            return EditorKey(_KINDS[raw])
        if raw.startswith("CTRL+"):
            return EditorKey(EditorKeyKind.CTRL, char=raw[len("CTRL+"):].lower())
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    def text(self) -> str:
        return "".join(self.written)

    def visible(self) -> str:
        return _SGR.sub("", self.text())

    def screens(self) -> list[str]:
        """Each redraw-in-place screen, as visible text."""
        return [_SGR.sub("", part) for part in self.text().split(_CLEAR) if part.strip()]


def _rows(screen: str) -> list[str]:
    return screen.replace("\r\n", "\n").rstrip("\n").split("\n")


@pytest.fixture
def node(tmp_path, monkeypatch):
    db_path = tmp_path / "node.db"
    db = Database(db_path)
    lane = DatabaseLane(db_path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    set_redraw_in_place_enabled(db, bob, True)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    carol = create_user(db, "carol", password="hunter2pw", user_level=10)
    # Mail is listed newest first; the Windows clock gives many sends the
    # same stamp, so each send gets its own.
    stamps = iter(f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}.000000Z" for i in range(3600))
    monkeypatch.setattr(mail_module, "utc_now_iso", lambda: next(stamps))
    yield db, lane, bob, alice, carol
    lane.close()
    db.close()


def _run(session, lane, user, **kwargs):
    asyncio.run(browse_mail(session, lane, user, **kwargs))


# -- the screen ----------------------------------------------------------------


def test_mail_opens_on_the_inbox_with_its_counts(node):
    db, lane, bob, alice, _ = node
    send_mail(db, alice, bob, "Hello", "body")
    mark_read(db, bob, send_mail(db, alice, bob, "Earlier", "body"))
    session = FakeSession(["b"])

    _run(session, lane, bob)

    screen = session.screens()[0]
    assert "NetBBS › Mail › Inbox" in screen
    assert "1 unread message · 2 of 500" in screen
    assert re.search(r"#\s+From\s+Subject\s+Date", screen)
    # No menu to pass through first.
    assert "[I]nbox" not in screen
    for label in ("[C]ompose", "[S]ent", "[B]ack"):
        assert label in screen


def test_each_row_is_numbered_once_with_new_as_a_column(node):
    db, lane, bob, alice, _ = node
    send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(["b"])

    _run(session, lane, bob)

    screen = session.screens()[0]
    assert re.search(r"> 1  new  alice +Hello +\S", screen)
    assert not re.search(r"^\W*01\. ", screen, re.MULTILINE)
    assert "(#" not in screen
    assert "[NEW]" not in screen


@pytest.mark.parametrize(("width", "height"), [(80, 24), (40, 12), (100, 40)])
def test_the_list_fits_the_terminal(node, monkeypatch, width, height):
    """As many rows as fit, never running past the screen, even with a
    kept letter's notice and the identity note above the list."""
    db, lane, bob, alice, _ = node
    for i in range(60):
        send_mail(db, alice, bob, f"Subject {i}", "body")
    path = mail_flow._letter_draft_path(lane, bob)
    path.write_text("A letter", encoding="utf-8")

    async def warning(lane, technical_address):
        return "Caution: changed"

    monkeypatch.setattr(mail_flow, "_link_mail_identity_warning", warning)
    session = FakeSession(["b"], width=width, height=height)

    _run(session, lane, bob)

    screen = session.screens()[0]
    rows = _rows(screen)
    assert len(rows) <= height
    assert all(display_width(row) <= width for row in rows)
    listed = [int(n) for n in re.findall(r"Subject (\d+)\b", screen)]
    assert listed == list(range(59, 59 - len(listed), -1))
    assert len(listed) >= 3
    assert "You have an unfinished letter" in screen


def test_a_narrow_terminal_lists_prose_rows(node):
    db, lane, bob, alice, _ = node
    send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(["b"], width=40, height=12)

    _run(session, lane, bob)

    screen = session.screens()[0]
    assert "1 new alice: Hello" in screen
    assert "Subject" not in screen  # no column heading


def test_columns_stay_aligned_with_wide_characters(node):
    """Display width, not characters: a subject in CJK, emoji and accents
    is cut to its column, and the dates line up."""
    db, lane, bob, alice, carol = node
    send_mail(db, alice, bob, "東京の天気 Ça et Cœur 🙂 " * 4, "body")
    send_mail(db, carol, bob, "Plain", "body")
    send_mail(db, alice, bob, "Short", "body")
    session = FakeSession(["b"])

    _run(session, lane, bob)

    rows = [row for row in _rows(session.screens()[0]) if re.search(r"\d{2}\.\d{2}\.\d{4}", row)]
    assert len(rows) == 3
    date_columns = {display_width(row[: re.search(r"\d{2}\.\d{2}\.\d{4}", row).start()]) for row in rows}
    assert len(date_columns) == 1
    assert any("…" in row or "..." in row for row in rows)


def test_a_long_link_address_is_cut_with_an_ellipsis():
    row = mail_flow._MailRow(
        message=_message(), name="x" * 60, subject="Hello", when="01.01.2026 00:00",
    )
    widths = mail_flow._mail_column_widths([row], width=80, number_width=1, sent=False, show_status=False)
    assert widths is not None
    lines = mail_flow._mail_list_rows(
        [row], width=80, first_number=1, number_width=1, widths=widths, highlighted=None,
        sent=False, show_status=False, accent=51,
    )
    visible = _SGR.sub("", lines[0])
    assert "x" * widths[0] not in visible
    assert "..." in visible
    assert "Hello" in visible
    assert display_width(visible) < 80


def _message(**overrides):
    fields = dict(
        id=1, sender_user_id=2, sender_label="alice", recipient_user_id=1, subject="Hello", body="body",
        created_at="2026-01-01T00:00:00Z", read_at=None, sender_deleted_at=None, recipient_deleted_at=None,
    )
    fields.update(overrides)
    return mail_module.MailMessage(**fields)


def test_untrusted_subject_text_cannot_break_the_row(node):
    db, lane, bob, alice, _ = node
    send_mail(db, alice, bob, "Evil\x1b[2J\ttab\x07bell", "body")
    session = FakeSession(["b"])

    _run(session, lane, bob)

    text = session.text()
    assert text.count(_CLEAR) == 1  # only the screen's own clear
    assert "\x07" not in text
    assert re.search(r"alice +Evil\[2J tabbell", session.visible())


def test_redraw_in_place_off_scrolls_without_clearing(node):
    db, lane, bob, alice, _ = node
    set_redraw_in_place_enabled(db, bob, False)
    send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(["b"])

    _run(session, lane, bob)

    assert _CLEAR not in session.text()
    assert "NetBBS › Mail › Inbox" in session.visible()
    assert "Choice: " in session.visible()


def test_the_prompt_is_the_one_the_main_menu_passes(node):
    db, lane, bob, alice, _ = node
    session = FakeSession(["b"])

    _run(session, lane, bob, choice_prompt=lambda: "12:34:56 UTC Choice: ")

    assert "12:34:56 UTC Choice: " in session.visible()


def test_the_main_menu_hands_mail_its_clock_prompt(node, monkeypatch):
    db, lane, bob, _, _ = node
    seen = {}

    async def fake_browse_mail(session, lane, user, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(main_menu_module, "browse_mail", fake_browse_mail)
    session = FakeSession(["e", "l", "y"])
    session.read_line = lambda *args, **kwargs: _async("y")

    asyncio.run(main_menu_module._main_menu(
        session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob, lane=lane,
    ))

    assert seen["choice_prompt"]() == main_menu_module._main_menu_prompt(db, bob, None)


async def _async(value):
    return value


# -- reading ---------------------------------------------------------------------


def test_down_and_enter_open_the_highlighted_message(node):
    db, lane, bob, alice, carol = node
    send_mail(db, alice, bob, "Older", "The older one")
    send_mail(db, carol, bob, "Newer", "The newer one")
    session = FakeSession(["DOWN", "ENTER", "b", "b"])

    _run(session, lane, bob)

    assert "The older one" in session.visible()
    assert "The newer one" not in session.visible()
    # Back on the list with the cursor on the message just read.
    assert re.search(r"> 2 +alice +Older", session.screens()[-1])


def test_a_digit_opens_that_row(node):
    db, lane, bob, alice, carol = node
    send_mail(db, alice, bob, "Older", "The older one")
    send_mail(db, carol, bob, "Newer", "The newer one")
    session = FakeSession(["2", "b", "b"])

    _run(session, lane, bob)

    assert "The older one" in session.visible()


def test_received_mail_shows_whom_it_is_to(node):
    db, lane, bob, alice, _ = node
    send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(["1", "b", "b"])

    _run(session, lane, bob)

    view = next(screen for screen in session.screens() if "Mail › Inbox › Hello" in screen)
    assert re.search(r"From: alice\s+To: bob\s+Date: ", view)


def test_pages_turn_and_number_from_one(node):
    db, lane, bob, alice, _ = node
    for i in range(40):
        send_mail(db, alice, bob, f"Subject {i}", "body")
    session = FakeSession(["n", "PGDN", "p", "b"])

    _run(session, lane, bob)

    first, second, third, back = session.screens()
    first_listed = re.findall(r"Subject (\d+)\b", first)
    second_listed = re.findall(r"Subject (\d+)\b", second)
    assert first_listed[0] == "39"
    assert int(second_listed[0]) == 39 - len(first_listed)
    assert re.search(r"> +1 +new +alice +Subject " + second_listed[0], second)
    assert "[P]rev page" in second
    assert re.findall(r"Subject (\d+)\b", back) == second_listed


# -- read state ------------------------------------------------------------------


def test_unread_in_the_message_marks_it_unread_again(node):
    db, lane, bob, alice, _ = node
    message = send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(["1", "u", "b"])

    _run(session, lane, bob)

    assert get_mail(db, bob, message.id).is_read is False
    last = session.screens()[-1]
    assert "Marked unread." in last
    assert re.search(r"> 1  new  alice +Hello", last)


def test_u_on_the_list_toggles_the_highlighted_message(node):
    db, lane, bob, alice, _ = node
    message = send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(["u", "u", "b"])

    _run(session, lane, bob)

    screens = session.screens()
    assert "[U] Read" in screens[0]
    assert "Marked read." in screens[1] and "[U]nread" in screens[1]
    assert "Marked unread." in screens[2]
    assert get_mail(db, bob, message.id).is_read is False


def test_order_puts_unread_mail_first_and_is_remembered(node):
    db, lane, bob, alice, carol = node
    send_mail(db, alice, bob, "Unread old", "body")
    mark_read(db, bob, send_mail(db, carol, bob, "Read new", "body"))
    session = FakeSession(["o", "b"])

    _run(session, lane, bob)

    newest, unread_first = session.screens()
    assert newest.index("Read new") < newest.index("Unread old")
    assert unread_first.index("Unread old") < unread_first.index("Read new")
    assert "unread first" in unread_first

    again = FakeSession(["b"])
    _run(again, lane, bob)
    screen = again.screens()[0]
    assert screen.index("Unread old") < screen.index("Read new")


# -- find --------------------------------------------------------------------------


def test_find_matches_the_name_or_the_subject(node):
    db, lane, bob, alice, carol = node
    send_mail(db, alice, bob, "Lunch", "body")
    send_mail(db, carol, bob, "Dinner", "body")
    send_mail(db, carol, bob, "Breakfast with alice", "body")
    session = FakeSession(["f", "ALICE", "f", "", "b"])

    _run(session, lane, bob)

    found, everything = session.screens()[1:]
    assert "Lunch" in found and "Breakfast with alice" in found
    assert "Dinner" not in found
    assert 'matching "ALICE"' in found
    assert "Dinner" in everything and "matching" not in everything


def test_find_does_not_match_the_new_marker(node):
    """The old picker searched `[NEW] subject`, so "new" matched every
    unread message."""
    db, lane, bob, alice, _ = node
    send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(["f", "new", "b"])

    _run(session, lane, bob)

    last = session.screens()[-1]
    assert "Hello" not in last
    assert 'Nothing here matches "new"' in last


def test_esc_at_find_keeps_the_list(node):
    db, lane, bob, alice, _ = node
    send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(["f", ESC, "b"])

    _run(session, lane, bob)

    assert "Hello" in session.screens()[-1]
    assert "matching" not in session.visible()


# -- sent --------------------------------------------------------------------------


def test_sent_is_a_table_and_back_returns_to_the_inbox(node):
    db, lane, bob, alice, _ = node
    send_mail(db, bob, alice, "Outgoing", "body")
    session = FakeSession(["s", "b", "b"])

    _run(session, lane, bob)

    inbox, sent, inbox_again = session.screens()
    assert "NetBBS › Mail › Sent" in sent
    assert re.search(r"#\s+To\s+Subject\s+Date", sent)
    assert re.search(r"> 1  alice +Outgoing", sent)
    assert "1 sent message" in sent
    assert "NetBBS › Mail › Inbox" in inbox_again


# -- the identity warning -----------------------------------------------------------


def test_a_changed_node_identity_is_flagged_on_the_row(node, monkeypatch):
    db, lane, bob, alice, _ = node
    send_mail(db, alice, bob, "Hello", "body")
    send_mail(db, alice, bob, "Again", "body")

    async def warning(lane, technical_address):
        return "Caution: changed" if technical_address == "alice" else None

    monkeypatch.setattr(mail_flow, "_link_mail_identity_warning", warning)
    session = FakeSession(["DOWN", "b"])

    _run(session, lane, bob)

    screen = session.screens()[-1]
    assert re.search(r"! alice +Hello", screen)
    assert "! Identity changed" in screen


def test_unread_mail_stays_unread_until_opened(node):
    db, lane, bob, alice, _ = node
    send_mail(db, alice, bob, "Hello", "body")
    session = FakeSession(["DOWN", "UP", "b"])

    _run(session, lane, bob)

    assert list_inbox(db, bob)[0].is_read is False


def test_an_outcome_notice_does_not_shift_the_page(node):
    """Review on #877: a notice takes a row from one render's budget; the
    page must stay where it was, and [N]ext page must go on from the rows
    on screen rather than land on the same page again."""
    db, lane, bob, alice, _ = node
    for i in range(60):
        send_mail(db, alice, bob, f"Subject {i}", "body")
    session = FakeSession(["n", "u", "n", "b"])

    _run(session, lane, bob)

    first, second, marked, third = (re.findall(r"Subject (\d+)\b", s) for s in session.screens())
    page = len(first)
    assert second[0] == str(59 - page)
    assert "Marked read." in session.screens()[2]
    assert marked[0] == second[0]
    # The next page starts right after the last row that was on screen.
    assert third[0] == str(int(marked[-1]) - 1)


def test_a_draft_that_cannot_be_read_is_not_offered(node):
    db, lane, bob, alice, _ = node
    mail_flow._letter_draft_path(lane, bob).write_bytes(b"\xff\xfe\xfa not utf-8")
    session = FakeSession(["b"])

    _run(session, lane, bob)

    assert "[D]raft" not in session.screens()[0]
