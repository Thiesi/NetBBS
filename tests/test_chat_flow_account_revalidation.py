"""
Regression tests for GitHub issue #29, reopened: `netbbs.net.login_flow.
_main_menu`'s cross-process disable/delete revalidation
(`netbbs.auth.users.account_still_active`) only ever ran at the main-menu
boundary -- a session that stayed inside real-time chat (or any other
long-running submenu) never returned there to pick up an account change
made through a separate `python -m netbbs.admin` invocation, and so kept
reading/sending indefinitely. `netbbs.net.chat_flow._chat_loop`'s send
loop now performs the identical check at its own equivalent boundary
(every attempted message or command); these tests drive the real
`_chat_loop` (not a mock of it) to prove that.

Mirrors tests/test_menu_invalid_key.py's own GitHub issue #29 section --
the account change is made via a direct domain-function call against the
same `Database` handle the test's own session shares, which is the
established, sufficient-fidelity way this suite already simulates "some
other process/code path changed this account" (this project's own
production model is a single shared long-lived SQLite connection per
process; a literal second `sqlite3` connection isn't needed to exercise
the actual revalidation *logic* under test here, only a second logical
caller mutating the row this session doesn't otherwise know changed).
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import create_user, delete_user, set_user_disabled
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.storage.database import Database
from tests.test_chat_flow_moderation import FakeSession


def _written_text(session: FakeSession) -> str:
    return "\n".join(session.written)


async def _run(db, hub, presence, mailbox, channel, user, lines):
    session = FakeSession(lines)
    history = InputHistory()
    action = await asyncio.wait_for(
        chat_flow._chat_loop(session, db, hub, presence, mailbox, history, channel, user),
        timeout=2,
    )
    return session, action


def test_disabling_the_account_mid_chat_disconnects_on_the_next_message(tmp_path):
    """The reopened issue's core scenario: the account is disabled
    *after* the session has already joined the channel (so the
    in-process live-disconnect path -- which only fires at the moment
    admin_flow's own disable screen runs -- never touches this
    session), simulating a completely separate `python -m netbbs.admin`
    invocation. The next attempted chat message must still catch it
    and terminate the session cleanly, not silently keep accepting
    input forever."""
    db = Database(tmp_path / "node.db")
    try:
        user = create_user(db, "alice", password="hunter2", user_level=10)
        channel = create_channel(db, "lobby", creator=user)

        set_user_disabled(db, user, True, changed_by=user)

        session, action = asyncio.run(
            _run(db, ChatHub(), PresenceRegistry(), MessageMailbox(), channel, user, ["hello"])
        )

        assert "no longer active" in _written_text(session)
        assert "hello" not in _written_text(session)  # never actually posted to the channel
        assert isinstance(action, chat_flow._Quit)
    finally:
        db.close()


def test_deleting_the_account_mid_chat_disconnects_on_the_next_message(tmp_path):
    db = Database(tmp_path / "node.db")
    try:
        sysop = create_user(db, "sysop", password="hunter2", user_level=100)
        user = create_user(db, "alice", password="hunter2", user_level=10)
        channel = create_channel(db, "lobby", creator=sysop)

        delete_user(db, user, deleted_by=sysop)

        session, action = asyncio.run(
            _run(db, ChatHub(), PresenceRegistry(), MessageMailbox(), channel, user, ["hello"])
        )

        assert "no longer active" in _written_text(session)
        assert isinstance(action, chat_flow._Quit)
    finally:
        db.close()


def test_the_check_also_catches_an_empty_line_not_just_a_real_message(tmp_path):
    """The revalidation check runs before *any* input is processed,
    including a bare Enter with nothing typed -- matching
    _main_menu's own placement (checked before dispatching on the read
    key at all, regardless of what that key turns out to be)."""
    db = Database(tmp_path / "node.db")
    try:
        user = create_user(db, "alice", password="hunter2", user_level=10)
        channel = create_channel(db, "lobby", creator=user)

        set_user_disabled(db, user, True, changed_by=user)

        session, action = asyncio.run(
            _run(db, ChatHub(), PresenceRegistry(), MessageMailbox(), channel, user, [""])
        )

        assert "no longer active" in _written_text(session)
        assert isinstance(action, chat_flow._Quit)
    finally:
        db.close()


def test_the_check_also_catches_a_slash_command_not_just_a_plain_message(tmp_path):
    db = Database(tmp_path / "node.db")
    try:
        user = create_user(db, "alice", password="hunter2", user_level=10)
        channel = create_channel(db, "lobby", creator=user)

        set_user_disabled(db, user, True, changed_by=user)

        session, action = asyncio.run(
            _run(db, ChatHub(), PresenceRegistry(), MessageMailbox(), channel, user, ["/topic"])
        )

        assert "no longer active" in _written_text(session)
        assert isinstance(action, chat_flow._Quit)
    finally:
        db.close()


def test_still_active_account_is_not_disconnected_mid_chat(tmp_path):
    db = Database(tmp_path / "node.db")
    try:
        user = create_user(db, "alice", password="hunter2", user_level=10)
        channel = create_channel(db, "lobby", creator=user)

        session, action = asyncio.run(
            _run(db, ChatHub(), PresenceRegistry(), MessageMailbox(), channel, user, ["hello", "/quit"])
        )

        assert "no longer active" not in _written_text(session)
        assert "hello" in _written_text(session)
        assert isinstance(action, chat_flow._Quit)
    finally:
        db.close()


def test_disabling_the_account_mid_chat_still_broadcasts_a_leave_notice(tmp_path):
    """The disconnect goes through _chat_loop's own normal `finally`
    cleanup (hub.leave + a "has left the channel" broadcast), same as
    any other exit path -- confirmed here via a second, still-active
    participant who should see it."""
    db = Database(tmp_path / "node.db")
    try:
        alice = create_user(db, "alice", password="hunter2", user_level=10)
        bob = create_user(db, "bob", password="hunter2", user_level=10)
        channel = create_channel(db, "lobby", creator=alice)
        hub = ChatHub()
        presence = PresenceRegistry()
        mailbox = MessageMailbox()

        bob_session = FakeSession()  # never types anything -- just observes

        async def scenario():
            bob_task = asyncio.create_task(
                chat_flow._chat_loop(bob_session, db, hub, presence, mailbox, InputHistory(), channel, bob)
            )
            await asyncio.sleep(0.05)  # let bob actually join before alice acts

            set_user_disabled(db, alice, True, changed_by=alice)
            alice_session, alice_action = await _run(
                db, hub, presence, mailbox, channel, alice, ["hello"]
            )

            bob_task.cancel()
            try:
                await bob_task
            except asyncio.CancelledError:
                pass
            return alice_session, alice_action

        alice_session, alice_action = asyncio.run(scenario())

        assert "no longer active" in _written_text(alice_session)
        assert isinstance(alice_action, chat_flow._Quit)
        assert "has left the channel" in _written_text(bob_session)
    finally:
        db.close()
