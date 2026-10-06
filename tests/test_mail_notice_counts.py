"""Mail notices count what is new since the last call, in the good-news
colour (issues #917, #944).

#823 told a caller at login how many letters were unread, and drew that --
like every other line about waiting mail -- in the warning colour. The
operator decided both: the login notice (and New scan's Mail line) also says
how many arrived since the caller's last call, and news of mail is drawn in
the node's accent, since new mail is good news rather than a problem.
The accent's gold turned out to read as the old amber, so #944 gave good news
a colour of its own: the palette's green, which no SysOp branding changes.
"""

from __future__ import annotations

import asyncio
import re

from netbbs import mail as mail_module
from netbbs.boards.boards import create_board
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.mail import mark_read, send_mail, set_kept, unread_count_since
from netbbs.net import mail_arrivals, main_menu, scan_and_find
from netbbs.net.char_input import InputHistory
from netbbs.net.mail_flow import browse_mail
from netbbs.net.main_menu import _main_menu
from netbbs.net.node_theme import set_accent_color_override
from netbbs.net.notices import pending_notices
from netbbs.rendering import ACCENT_COLOR, GOOD_NEWS_COLOR, MENU_KEY_COLOR, WARNING_COLOR, nearest_256, status_mark
from netbbs.session_history import previous_call_started_at, record_session_start
from tests.test_mail_arrivals import Session, _scan, _stop, _until, _watching, node  # noqa: F401

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CLEAR = "\x1b[2J"

EARLY = "2026-01-01T00:00:10.000000Z"
LAST_CALL = "2026-01-01T00:01:00.000000Z"
LATE = "2026-01-01T00:02:00.000000Z"
NOW = "2026-01-01T00:03:00.000000Z"


def _call(db, user, at: str) -> int:
    """A session of `user`'s, recorded as having begun at `at`."""
    history_id = record_session_start(db, user)
    db.connection.execute("UPDATE session_history SET connected_at = ? WHERE id = ?", (at, history_id))
    db.connection.commit()
    return history_id


def _letter(db, sender, recipient, subject: str, at: str, *, read: bool = False):
    message = send_mail(db, sender, recipient, subject, "text")
    db.connection.execute("UPDATE mail_messages SET created_at = ? WHERE id = ?", (at, message.id))
    db.connection.commit()
    if read:
        mark_read(db, recipient, message)
    return message


def _menu(session, db, lane, user, current_history_id):
    return _main_menu(
        session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user,
        lane=lane, current_history_id=current_history_id,
    )


def _raw(session) -> str:
    return "".join(session.written)


def _color_of(raw: str, text: str) -> str:
    """The SGR parameters in force where `text` starts in `raw`."""
    at = raw.index(text)
    codes = [match.group(1) for match in re.finditer(r"\x1b\[([0-9;]*)m", raw[:at])]
    return codes[-1] if codes else ""


def _first_menu(db, lane, user, current_history_id) -> Session:
    async def scenario():
        session = Session(["l", "y"])
        session.supports_truecolor = False
        await _menu(session, db, lane, user, current_history_id)
        return session

    return asyncio.run(scenario())


# -- the previous call --------------------------------------------------------


def test_the_previous_call_is_the_callers_newest_row_before_this_one(node):
    db, _lane, alice, bob = node
    assert previous_call_started_at(db, alice, current_history_id=None) is None
    _call(db, alice, EARLY)
    _call(db, bob, LATE)  # someone else's call is not alice's
    earlier = _call(db, alice, LAST_CALL)
    current = _call(db, alice, NOW)

    assert previous_call_started_at(db, alice, current_history_id=current) == LAST_CALL
    assert previous_call_started_at(db, alice, current_history_id=earlier) == EARLY
    # The first row of all has nothing before it.
    first = db.connection.execute(
        "SELECT MIN(id) AS id FROM session_history WHERE user_id = ?", (alice.id,)
    ).fetchone()["id"]
    assert previous_call_started_at(db, alice, current_history_id=first) is None


def test_link_mail_counts_as_new_by_when_it_arrived_not_when_it_was_written(node):
    """A letter a relay held for a day is dated by its sender (#808), long
    before the last call, and is still new to its reader."""
    db, _lane, alice, _bob = node
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_user_id, subject, body, created_at,
             link_source_event_id, sender_deleted_at)
        VALUES (NULL, 'carol@abcdef123456', ?, 'Held by a relay', 'text', ?, 'event-1', ?)
        """,
        (alice.id, EARLY, LATE),
    )
    db.connection.commit()
    assert unread_count_since(db, alice, LAST_CALL) == 1
    assert unread_count_since(db, alice, NOW) == 0


# -- the login notice ---------------------------------------------------------


def test_the_login_notice_counts_new_since_the_last_call_and_all_unread(node):
    db, lane, alice, bob = node
    _letter(db, bob, alice, "Old, unread", EARLY)
    _letter(db, bob, alice, "Old, read", EARLY, read=True)
    _letter(db, bob, alice, "New one", LATE)
    _letter(db, bob, alice, "New two", LATE)
    _letter(db, bob, alice, "New, read", LATE, read=True)
    _call(db, alice, LAST_CALL)
    current = _call(db, alice, NOW)

    session = _first_menu(db, lane, alice, current)
    text = session.visible()
    line = "2 new since your last call, 3 unread in all -- [E]-mail to read them."
    assert line in text
    assert text.index(line) < text.index("Choice")


def test_the_login_notice_says_nothing_new_when_only_older_mail_waits(node):
    db, lane, alice, bob = node
    _letter(db, bob, alice, "Old", EARLY)
    _call(db, alice, LAST_CALL)
    current = _call(db, alice, NOW)

    text = _first_menu(db, lane, alice, current).visible()
    assert "Nothing new since your last call, 1 unread in all -- [E]-mail to read it." in text


def test_the_first_call_gets_the_unread_count_alone(node):
    """This session's own row is not a previous call."""
    db, lane, alice, bob = node
    _letter(db, bob, alice, "Welcome", EARLY)
    _letter(db, bob, alice, "Hello", LATE)
    current = _call(db, alice, NOW)

    text = _first_menu(db, lane, alice, current).visible()
    assert "You have 2 unread messages -- [E]-mail to read them." in text
    assert "since your last call" not in text


def test_nothing_unread_no_line_even_after_a_previous_call(node):
    db, lane, alice, bob = node
    _letter(db, bob, alice, "Read already", LATE, read=True)
    _call(db, alice, LAST_CALL)
    current = _call(db, alice, NOW)

    text = _first_menu(db, lane, alice, current).visible()
    assert "since your last call" not in text
    assert "You have" not in text


def test_the_login_notice_and_the_menus_unread_count_are_in_the_good_news_colour(node):
    db, lane, alice, bob = node
    _letter(db, bob, alice, "New", LATE)
    _call(db, alice, LAST_CALL)
    current = _call(db, alice, NOW)

    raw = _raw(_first_menu(db, lane, alice, current))
    assert _color_of(raw, "1 new since your last call") == f"38;5;{GOOD_NEWS_COLOR}"
    assert _color_of(raw, "1 unread message") == f"38;5;{GOOD_NEWS_COLOR}"
    assert f"38;5;{WARNING_COLOR}m" not in raw


def test_the_eviction_count_stays_a_warning(node, monkeypatch):
    db, lane, alice, bob = node
    _letter(db, bob, alice, "New", LATE)
    current = _call(db, alice, NOW)
    monkeypatch.setattr(main_menu, "pending_eviction_notice", lambda db, user: ("EVICTED LINE", 1))
    monkeypatch.setattr(main_menu, "acknowledge_eviction_notice", lambda db, user, n: None)

    raw = _raw(_first_menu(db, lane, alice, current))
    # A warning is marked as one (issue #1109); its text keeps the usual colour.
    assert status_mark("warning") + "EVICTED LINE" in raw
    assert _color_of(raw, "You have 1 unread message") == f"38;5;{GOOD_NEWS_COLOR}"


# -- New scan -----------------------------------------------------------------


def test_new_scan_shows_the_same_two_counts_in_the_good_news_colour(node):
    db, lane, alice, bob = node
    _letter(db, bob, alice, "Old", EARLY)
    _letter(db, bob, alice, "New", LATE)
    _call(db, alice, LAST_CALL)
    current = _call(db, alice, NOW)

    # The scan needs something to list, or it returns before any key.
    create_board(db, "General", creator=alice)

    async def scenario():
        session = Session(["b"])
        await scan_and_find._new_scan_screen(
            session, db, lane, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), alice,
            current_history_id=current,
        )
        return session

    session = asyncio.run(scenario())
    line = "Mail: 1 new since your last call, 2 unread in all -- [E]-mail to read them"
    assert line in session.visible()
    assert _color_of(_raw(session), line) == f"38;5;{GOOD_NEWS_COLOR}"


def test_new_scan_on_a_first_call_counts_unread_alone(node):
    db, lane, alice, bob = node
    _letter(db, bob, alice, "Hello", LATE)

    async def scenario():
        session = Session(["b"])
        await _scan(session, db, lane, alice)
        return session

    assert "Mail: 1 unread -- [E]-mail to read it" in asyncio.run(scenario()).visible()


# -- live notices and the mailbox ----------------------------------------------


def test_live_new_mail_lines_are_in_the_good_news_colour(node):
    db, _lane, alice, bob = node

    async def scenario():
        queued_session = Session()
        chat_session = Session()
        shown: list[str] = []

        async def hook(text):
            shown.append(text)

        chat_session.pinned_notice_hook = hook
        queued = await _watching(queued_session, db, alice)
        live = await _watching(chat_session, db, alice)
        send_mail(db, bob, alice, "Lunch?", "noon")
        mail_arrivals.nudge("alice")
        await _until(lambda: pending_notices(queued_session) and shown)
        await _stop(queued)
        await _stop(live)
        return pending_notices(queued_session), shown

    queued, shown = asyncio.run(scenario())
    for line in (*queued, *shown):
        assert _color_of(line, "New mail from bob") == f"38;5;{GOOD_NEWS_COLOR}"


def test_a_sysop_accent_override_leaves_good_news_green(node):
    """Good news is a semantic colour, like success and warnings: a node's
    branding moves the caller's name, never the unread count beside it."""
    db, lane, alice, bob = node
    set_accent_color_override(db, (200, 40, 160))
    accent = nearest_256((200, 40, 160))
    _letter(db, bob, alice, "New", LATE)
    current = _call(db, alice, NOW)

    raw = _raw(_first_menu(db, lane, alice, current))
    assert _color_of(raw, "alice") == f"38;5;{accent}"
    assert _color_of(raw, "You have 1 unread message") == f"38;5;{GOOD_NEWS_COLOR}"
    assert _color_of(raw, "1 unread message ") == f"38;5;{GOOD_NEWS_COLOR}"


def test_good_news_is_its_own_colour_in_the_default_theme():
    """#917's gold was a shade off the warning amber; the two have to read
    apart at a glance, and good news apart from the gold name beside it and
    from a menu's hotkeys."""
    assert GOOD_NEWS_COLOR not in {ACCENT_COLOR, WARNING_COLOR, MENU_KEY_COLOR}
    # Green, not another yellow: its RGB has green well above red and blue.
    r, g, b = _xterm_rgb(GOOD_NEWS_COLOR)
    assert g > 2 * max(r, b)


def _xterm_rgb(index: int) -> tuple[int, int, int]:
    """The xterm 256-colour cube's RGB for a cube index (16-231)."""
    assert 16 <= index <= 231
    steps = (0, 95, 135, 175, 215, 255)
    index -= 16
    return steps[index // 36], steps[(index // 6) % 6], steps[index % 6]


def test_the_mailbox_header_counts_unread_in_the_good_news_colour(node):
    db, lane, alice, bob = node
    _letter(db, bob, alice, "One", LATE)
    _letter(db, bob, alice, "Two", LATE)

    async def scenario():
        session = Session(["b"])
        await browse_mail(session, lane, alice)
        return session

    raw = _raw(asyncio.run(scenario()))
    assert _color_of(raw, "2 unread messages") == f"38;5;{GOOD_NEWS_COLOR}"


def test_a_nearly_full_mailbox_stays_a_warning(node, monkeypatch):
    db, lane, alice, bob = node
    from netbbs.net import mail_flow

    monkeypatch.setattr(mail_flow, "MAILBOX_NEARLY_FULL", 2)
    _letter(db, bob, alice, "One", LATE)
    _letter(db, bob, alice, "Two", LATE)

    async def scenario():
        session = Session(["b"])
        await browse_mail(session, lane, alice)
        return session

    raw = _raw(asyncio.run(scenario()))
    assert _color_of(raw, f"2 of {mail_module.MAX_MAIL_PER_RECIPIENT}") == f"38;5;{WARNING_COLOR}"
    assert _color_of(raw, "2 unread messages") == f"38;5;{GOOD_NEWS_COLOR}"


def test_unread_in_kept_is_good_news_too(node):
    db, lane, alice, bob = node
    kept = _letter(db, bob, alice, "Keep me", LATE)
    set_kept(db, alice, [kept.id], kept=True)

    async def scenario():
        session = Session(["b"])
        await browse_mail(session, lane, alice)
        return session

    raw = _raw(asyncio.run(scenario()))
    assert _color_of(raw, "1 unread in Kept") == f"38;5;{GOOD_NEWS_COLOR}"
