"""Login-time notices reach the first main menu (issue #923).

What login used to write just before the main menu -- the pending chat
invitation count, the drain warning, the outcome of the Unicode-style
question and of first-run onboarding -- was cleared unseen by the menu's
redraw-in-place clear unless the previous-callers roll happened to come in
between. Each is now carried above the first menu's prompt, told once, in the
menu's notice order. These drive the real `run_authenticated_session` and
assert on what is on the terminal after the last clear, which is the only
check that fails against the bug (a line written "somewhere" passes).
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.chat.channels import create_channel
from netbbs.chat.membership import create_invitation
from netbbs.link.onboarding import Participation, get_participation
from netbbs.mail import send_mail
from netbbs.managed_dns.state import OptIn, set_opt_in
from netbbs.config import set_node_display_name
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.net.char_input import REDRAW_KEY
from netbbs.net.login_flow import run_authenticated_session
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.shutdown import NodeControls
from netbbs.net.unicode_style_preference import set_unicode_style_enabled
from netbbs.session_history import record_session_end, record_session_start, set_previous_callers_enabled
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_CLEAR = "\x1b[2J"
_INVITES = "pending chat channel invitation"


class FakeSession:
    def __init__(self, keys=None, lines=None):
        self._keys = iter(keys or ["l"])
        self._lines = iter(lines if lines is not None else ["y"])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"
        self.any_keys = 0

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("read_key() called with no more scripted keys")
        return key

    async def read_any_key(self, echo: bool = True) -> str:
        self.any_keys += 1
        return " "

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        line = next(self._lines, None)
        if line is None:
            raise AssertionError("read_line() called with no more scripted lines")
        return line

    @property
    def output(self) -> str:
        return "".join(self.written)

    def first_menu(self) -> str:
        """From the first main menu's clear to its prompt."""
        start = self.output.index(_CLEAR)
        return self.output[start:self.output.index("Choice", start)]

    def before_first_menu(self) -> str:
        return self.output[:self.output.index("Main menu")]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _caller(db, name="bob", *, level=10, redraw=True):
    user = create_user(db, name, password="hunter2", user_level=level)
    set_redraw_in_place_enabled(db, user, redraw)
    # The one-time Unicode question is answered already unless a test asks it.
    set_unicode_style_enabled(db, user, True)
    return user


def _invite(db, invitee, *, channel="lobby"):
    alice = create_user(db, f"host-{channel}", password="hunter2", user_level=10)
    room = create_channel(db, channel, creator=alice, members_only=True)
    grant_permissions(
        db, alice, object_type="channel", object_id=room.id,
        permissions=ChannelPermission.MANAGE_MEMBERS, granted_by=alice,
    )
    create_invitation(db, room, invitee, invited_by=alice)


def _previous_caller(db):
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    record_session_end(db, record_session_start(db, carol))


def _login(db, user, session, **kwargs):
    asyncio.run(
        run_authenticated_session(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), user, **kwargs)
    )
    return session


def test_invitations_survive_the_redraw_in_place_clear(db):
    bob = _caller(db)
    _invite(db, bob)
    set_previous_callers_enabled(db, False)

    session = _login(db, bob, FakeSession())

    assert "You have 1 pending chat channel invitation -- [I]nvitations to see it." in session.first_menu()
    assert session.output.count(_INVITES) == 1


def test_invitations_are_above_the_prompt_without_redraw_in_place(db):
    bob = _caller(db, redraw=False)
    _invite(db, bob)
    _invite(db, bob, channel="vip")
    set_previous_callers_enabled(db, False)

    session = _login(db, bob, FakeSession())

    output = session.output
    assert _CLEAR not in output
    assert output.index("Main menu") < output.index("2 pending chat channel invitations") < output.index("Choice")
    assert output.count(_INVITES) == 1


@pytest.mark.parametrize("redraw", [True, False])
def test_invitations_are_told_once_after_the_previous_callers_roll(db, redraw):
    bob = _caller(db, redraw=redraw)
    _invite(db, bob)
    _previous_caller(db)

    session = _login(db, bob, FakeSession())

    assert session.any_keys == 1  # the roll was shown
    output = session.output
    assert _INVITES not in session.before_first_menu()
    assert output.index("Main menu") < output.index(_INVITES) < output.index("Choice")
    assert output.count(_INVITES) == 1


def test_invitations_are_told_on_the_first_draw_only(db):
    bob = _caller(db)
    _invite(db, bob)
    set_previous_callers_enabled(db, False)

    session = _login(db, bob, FakeSession(keys=[REDRAW_KEY, "l"]))

    output = session.output
    assert output.count("Main menu") >= 2  # Ctrl-L redrew it
    assert output.count(_INVITES) == 1
    redrawn = output[output.rindex(_CLEAR):]
    assert _INVITES not in redrawn
    # [I]nvitations itself stays on the menu for as long as one is pending.
    assert "Pending invitations for you" in redrawn


def test_no_invitation_line_when_nothing_is_pending(db):
    bob = _caller(db)
    set_previous_callers_enabled(db, False)

    session = _login(db, bob, FakeSession())

    assert _INVITES not in session.output


def test_login_notices_keep_the_main_menu_order(db):
    """Drain first, then mail, then chat: the invitations, then a queued
    `/msg` line."""
    bob = _caller(db)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    send_mail(db, alice, bob, "Lunch?", "Noon?")
    _invite(db, bob)
    set_previous_callers_enabled(db, False)
    mailbox = MessageMailbox()

    async def scenario():
        node_controls = NodeControls(
            session_registry=ActiveSessionRegistry(), maintenance=MaintenanceMode(),
            shutdown_event=asyncio.Event(), graceful_delay_seconds=60.0,
        )
        drain_task = asyncio.create_task(asyncio.Event().wait())
        node_controls.drain_scheduler.schedule(
            drain_task, deadline=asyncio.get_running_loop().time() + 30.0, message=None
        )
        session = FakeSession()
        mailbox.deliver(session, "*** Private message from alice: psst", "2026-01-01T00:00:00.000000Z")
        try:
            await run_authenticated_session(
                session, db, ChatHub(), PresenceRegistry(), mailbox, bob, node_controls=node_controls,
            )
        finally:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        return session

    session = asyncio.run(scenario())

    menu = session.first_menu()
    order = [
        menu.index("being drained for maintenance"),
        menu.index("You have 1 unread message"),
        menu.index(_INVITES),
        menu.index("Private message from alice"),
    ]
    assert order == sorted(order)
    assert "drained" not in session.before_first_menu()
    assert session.output.count("being drained for maintenance") == 1


@pytest.mark.parametrize("roll", [False, True])
def test_the_unicode_style_outcome_is_carried_to_the_menu(db, roll):
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    set_redraw_in_place_enabled(db, bob, True)
    if roll:
        _previous_caller(db)
    else:
        set_previous_callers_enabled(db, False)

    # "y" switches to plain ASCII; the second "y" confirms Log off.
    session = _login(db, bob, FakeSession(lines=["y", "y"]))

    assert "Does that look garbled" in session.before_first_menu()  # still asked first
    assert "Switched to plain ASCII style" in session.first_menu()
    assert session.output.count("Switched to plain ASCII style") == 1


def test_a_first_run_onboarding_outcome_is_carried_to_the_menu(db):
    sysop = _caller(db, "sysop", level=SYSOP_LEVEL)
    set_opt_in(db, OptIn.DECLINED)
    set_node_display_name(db, "Named Node")
    set_previous_callers_enabled(db, False)

    async def scenario():
        lane = DatabaseLane(db.path)
        try:
            # "n" declines NetBBS Link; "y" confirms Log off.
            session = FakeSession(lines=["n", "y"])
            await run_authenticated_session(
                session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), sysop, lane=lane,
            )
            return session
        finally:
            lane.close()

    session = asyncio.run(scenario())

    assert get_participation(db) is Participation.DECLINED
    assert "Join NetBBS Link" in session.before_first_menu()  # the question is still asked in place
    assert "(Noted." in session.first_menu()
    assert session.output.count("(Noted.") == 1
