"""New mail announced at login, in [N]ew scan and mid-session (issue #823).

Before #823 nothing said that mail had arrived: the main menu's unread count
changed on its next redraw, and New scan left mail out.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs import mail as mail_module
from netbbs.auth.users import create_user
from netbbs.boards import list_boards
from netbbs.boards.boards import create_board
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.config import set_mail_min_level
from netbbs.mail import list_inbox, mark_read, mark_unread, send_mail, send_system_mail
from netbbs.net import mail_arrivals, scan_and_find
from netbbs.net.char_input import EditorKey, EditorKeyKind, InputHistory
from netbbs.net.mail_flow import browse_mail
from netbbs.net.main_menu import _main_menu
from netbbs.net.notices import pending_notices, take_notices
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CLEAR = "\x1b[2J"


class Session:
    """Keys come from a queue, so a test can let a screen sit idle while
    mail arrives, then press the next key."""

    def __init__(self, keys=()):
        self.keys: asyncio.Queue[str] | None = None
        self._initial = list(keys)
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = None
        self.pinned_notice_hook = None
        self.door_active = False

    def _queue(self) -> asyncio.Queue[str]:
        if self.keys is None:
            self.keys = asyncio.Queue()
            for key in self._initial:
                self.keys.put_nowait(key)
        return self.keys

    def press(self, *keys: str) -> None:
        for key in keys:
            self._queue().put_nowait(key)

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\r\n")

    async def read_key(self, echo: bool = True) -> str:
        return await self._queue().get()

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return await self._queue().get()

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        return EditorKey(EditorKeyKind.CHAR, char=await self._queue().get())

    def visible(self) -> str:
        return _SGR.sub("", "".join(self.written))

    def last_screen(self) -> str:
        text = "".join(self.written)
        return _SGR.sub("", text[text.rindex(_CLEAR):])


@pytest.fixture
def node(tmp_path, monkeypatch):
    db = Database(tmp_path / "node.db")
    lane = DatabaseLane(db.path)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    set_redraw_in_place_enabled(db, alice, True)
    stamps = iter(f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}.000000Z" for i in range(3600))
    monkeypatch.setattr(mail_module, "utc_now_iso", lambda: next(stamps))
    yield db, lane, alice, bob
    lane.close()
    db.close()


async def _until(condition, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def _watching(session, db, user):
    """Start a watcher and wait until it has taken its first look."""
    task = asyncio.create_task(mail_arrivals.watch_for_mail(session, db, user, poll_seconds=60))
    await _until(lambda: mail_arrivals.arrival_event(session) is not None)
    await asyncio.sleep(0)
    return task


async def _stop(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# -- the watcher --------------------------------------------------------------


def test_a_letter_arriving_is_announced_once_and_what_was_there_is_not(node):
    db, _lane, alice, bob = node
    send_mail(db, bob, alice, "Old news", "already here")

    async def scenario():
        session = Session()
        task = await _watching(session, db, alice)
        send_mail(db, bob, alice, "Lunch?", "noon")
        mail_arrivals.nudge("alice")
        await _until(lambda: pending_notices(session))
        event = mail_arrivals.arrival_event(session)
        lines = [_SGR.sub("", line) for line in take_notices(session)]
        # A second look finds nothing new.
        mail_arrivals.nudge("alice")
        await asyncio.sleep(0.05)
        again = pending_notices(session)
        await _stop(task)
        return lines, event.is_set(), again

    lines, event_set, again = asyncio.run(scenario())
    assert lines == ["New mail from bob: Lunch?"]
    assert event_set
    assert again == []


def test_marking_a_read_letter_unread_is_not_new_mail(node):
    db, _lane, alice, bob = node
    message = send_mail(db, bob, alice, "Seen", "before")
    mark_read(db, alice, message)

    async def scenario():
        session = Session()
        task = await _watching(session, db, alice)
        mark_unread(db, alice, list_inbox(db, alice)[0])
        mail_arrivals.nudge("alice")
        await asyncio.sleep(0.05)
        await _stop(task)
        return pending_notices(session)

    assert asyncio.run(scenario()) == []


def test_many_letters_at_once_are_counted_in_one_line(node):
    db, _lane, alice, bob = node

    async def scenario():
        session = Session()
        task = await _watching(session, db, alice)
        for n in range(mail_arrivals.MAX_NAMED + 1):
            send_mail(db, bob, alice, f"Letter {n}", "text")
        mail_arrivals.nudge("alice")
        await _until(lambda: pending_notices(session))
        await _stop(task)
        return [_SGR.sub("", line) for line in take_notices(session)]

    assert asyncio.run(scenario()) == [f"{mail_arrivals.MAX_NAMED + 1} new messages in your mailbox."]


def test_system_mail_is_announced_from_the_system(node):
    db, _lane, alice, _bob = node

    async def scenario():
        session = Session()
        task = await _watching(session, db, alice)
        send_system_mail(db, alice, "Your post was declined", "why")
        mail_arrivals.nudge("alice")
        await _until(lambda: pending_notices(session))
        await _stop(task)
        return [_SGR.sub("", line) for line in take_notices(session)]

    assert asyncio.run(scenario()) == [f"New mail from {mail_module.SYSTEM_SENDER_LABEL}: Your post was declined"]


def test_a_letter_is_found_without_a_nudge_at_the_next_poll(node):
    """Link delivery and other processes do not nudge: the poll finds it."""
    db, _lane, alice, bob = node

    async def scenario():
        session = Session()
        task = asyncio.create_task(mail_arrivals.watch_for_mail(session, db, alice, poll_seconds=0.05))
        await _until(lambda: mail_arrivals.arrival_event(session) is not None)
        await asyncio.sleep(0.01)
        send_mail(db, bob, alice, "Polled", "text")
        await _until(lambda: pending_notices(session))
        await _stop(task)
        return [_SGR.sub("", line) for line in take_notices(session)]

    assert asyncio.run(scenario()) == ["New mail from bob: Polled"]


def test_nothing_is_announced_to_a_caller_below_the_mail_level(node):
    db, _lane, alice, bob = node

    async def scenario():
        session = Session()
        task = await _watching(session, db, alice)
        # The SysOp raises the mail level after this letter was delivered.
        send_mail(db, bob, alice, "Before the change", "text")
        set_mail_min_level(db, 50)
        mail_arrivals.nudge("alice")
        await asyncio.sleep(0.05)
        await _stop(task)
        return pending_notices(session), mail_arrivals.arrival_event(session)

    notices, _event = asyncio.run(scenario())
    assert notices == []


def test_nothing_is_announced_to_a_guest_session(node):
    db, _lane, alice, bob = node

    async def scenario():
        session = Session()
        session.authenticated_without_credential = True
        task = await _watching(session, db, alice)
        send_mail(db, bob, alice, "Hello", "text")
        mail_arrivals.nudge("alice")
        await asyncio.sleep(0.05)
        await _stop(task)
        return pending_notices(session)

    assert asyncio.run(scenario()) == []


def test_in_chat_the_notice_goes_through_the_pinned_hook_at_once(node):
    db, _lane, alice, bob = node

    async def scenario():
        session = Session()
        shown: list[str] = []

        async def hook(text):
            shown.append(_SGR.sub("", text))

        session.pinned_notice_hook = hook
        task = await _watching(session, db, alice)
        send_mail(db, bob, alice, "Ping", "text")
        mail_arrivals.nudge("alice")
        await _until(lambda: shown)
        await _stop(task)
        return shown, pending_notices(session), mail_arrivals.arrival_event(session)

    shown, queued, _event = asyncio.run(scenario())
    assert shown == ["New mail from bob: Ping"]
    assert queued == []


def test_a_door_is_never_written_into_the_notice_waits(node):
    db, _lane, alice, bob = node

    async def scenario():
        session = Session()
        shown: list[str] = []

        async def hook(text):
            shown.append(text)

        session.pinned_notice_hook = hook
        session.door_active = True
        task = await _watching(session, db, alice)
        send_mail(db, bob, alice, "Ping", "text")
        mail_arrivals.nudge("alice")
        await _until(lambda: pending_notices(session))
        await _stop(task)
        return shown

    assert asyncio.run(scenario()) == []


# -- the main menu ------------------------------------------------------------


def _menu(session, db, lane, user):
    return _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user, lane=lane)


def test_the_first_menu_after_login_says_what_mail_waits(node):
    db, lane, alice, bob = node
    send_mail(db, bob, alice, "One", "text")
    send_mail(db, bob, alice, "Two", "text")

    async def scenario():
        # Ctrl-L redraws the menu once more before logging off.
        session = Session(["\x0c", "l", "y"])
        await _menu(session, db, lane, alice)
        return session

    session = asyncio.run(scenario())
    screens = [_SGR.sub("", part) for part in "".join(session.written).split(_CLEAR) if part.strip()]
    first = screens[0]
    assert "You have 2 unread messages -- [E]-mail to read them." in first
    assert first.index("You have 2 unread") < first.index("Choice")
    # Told once, not on every redraw.
    assert "You have 2 unread" not in screens[1]


def test_no_login_mail_notice_when_nothing_is_unread_or_mail_is_closed(node):
    db, lane, alice, bob = node

    async def scenario():
        session = Session(["l", "y"])
        await _menu(session, db, lane, alice)
        return session

    assert "You have" not in asyncio.run(scenario()).visible()
    send_mail(db, bob, alice, "One", "text")
    set_mail_min_level(db, 50)
    assert "You have 1 unread" not in asyncio.run(scenario()).visible()


def test_the_mail_notices_are_grouped_inbox_first(node, monkeypatch):
    db, lane, alice, bob = node
    send_mail(db, bob, alice, "One", "text")
    from netbbs.net import main_menu

    monkeypatch.setattr(main_menu, "pending_eviction_notice", lambda db, user: ("EVICTED LINE", 1))
    monkeypatch.setattr(main_menu, "acknowledge_eviction_notice", lambda db, user, n: None)
    monkeypatch.setattr(main_menu, "pending_delivery_notices", lambda db, user: (["BOUNCED LINE"], []))

    async def scenario():
        session = Session(["l", "y"])
        await _menu(session, db, lane, alice)
        return session

    text = asyncio.run(scenario()).visible()
    assert text.index("You have 1 unread") < text.index("EVICTED LINE") < text.index("BOUNCED LINE")


def test_an_idle_menu_redraws_when_mail_arrives(node):
    db, lane, alice, bob = node

    async def scenario():
        session = Session()
        watcher = await _watching(session, db, alice)
        menu = asyncio.create_task(_menu(session, db, lane, alice))
        await _until(lambda: "Choice" in session.visible())
        send_mail(db, bob, alice, "Are you there?", "text")
        mail_arrivals.nudge("alice")
        await _until(lambda: "New mail from bob" in session.visible())
        screen = session.last_screen()
        session.press("l", "y")
        await menu
        await _stop(watcher)
        return screen

    screen = asyncio.run(scenario())
    assert "New mail from bob: Are you there?" in screen
    assert "1 unread message" in screen
    assert screen.index("New mail from bob") < screen.index("Choice")


# -- the mailbox --------------------------------------------------------------


def test_the_open_mailbox_shows_a_letter_that_arrives(node):
    db, lane, alice, bob = node
    send_mail(db, bob, alice, "First", "text")

    async def scenario():
        session = Session()
        watcher = await _watching(session, db, alice)
        mailbox = asyncio.create_task(browse_mail(session, lane, alice))
        await _until(lambda: "First" in session.visible())
        send_mail(db, bob, alice, "Second", "text")
        mail_arrivals.nudge("alice")
        await _until(lambda: "Second" in session.last_screen())
        screen = session.last_screen()
        session.press("b")
        await mailbox
        await _stop(watcher)
        return screen

    screen = asyncio.run(scenario())
    assert "New mail from bob: Second" in screen
    assert "2 unread messages" in screen


# -- New scan -----------------------------------------------------------------


def _scan(session, db, lane, user):
    # The scan needs something to list, or it returns before any key.
    if not list_boards(db):
        create_board(db, "General", creator=user)
    return scan_and_find._new_scan_screen(
        session, db, lane, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user,
    )


def test_new_scan_says_what_mail_is_unread_and_e_opens_the_mailbox(node, monkeypatch):
    db, lane, alice, bob = node
    send_mail(db, bob, alice, "One", "text")
    send_mail(db, bob, alice, "Two", "text")
    opened: list[str] = []

    async def fake_browse_mail(session, lane, user, **kwargs):
        opened.append(user.username)
        for message in list_inbox(db, user):
            mark_read(db, user, message)

    monkeypatch.setattr(scan_and_find, "browse_mail", fake_browse_mail)

    async def scenario():
        session = Session(["e", "b"])
        await _scan(session, db, lane, alice)
        return session

    session = asyncio.run(scenario())
    text = session.visible()
    assert "Mail: 2 unread -- [E]-mail to read them" in text
    assert "[E]-mail" in text
    assert opened == ["alice"]
    # Back from the mailbox, the count is brought up to date.
    assert "Mail: nothing unread." in session.last_screen()


def test_new_scan_says_nothing_about_mail_to_a_caller_mail_is_closed_to(node):
    db, lane, alice, bob = node
    send_mail(db, bob, alice, "One", "text")
    set_mail_min_level(db, 50)

    async def scenario():
        session = Session(["e", "b"])
        await _scan(session, db, lane, alice)
        return session

    text = asyncio.run(scenario()).visible()
    assert "Mail:" not in text
    assert "-mail" not in text
