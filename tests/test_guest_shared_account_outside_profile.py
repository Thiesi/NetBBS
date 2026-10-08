"""Outside Profile, a guest session cannot change what other callers see of
the shared guest account either (issue #1073).

Three ways in that the survey for #1073 found: the chat alias (`/nick`,
announced and kept in the channel's scrollback), a block from Who's online
(the blocked person is told the guest account refuses them, and every later
guest inherits it), and registering the account's handle with the MRC hub
(the first guest to do so would own it for every guest).
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.nick import get_nick
from netbbs.chat.presence import PresenceRegistry
from netbbs.mail import blocks_local_sender, list_mail_blocks
from netbbs.net import chat_flow, mail_flow
from netbbs.net.char_input import InputHistory
from tests.test_chat_flow_moderation import FakeSession as ChatSession
from tests.test_chat_flow_mrc import (  # noqa: F401 -- fixtures and helpers
    _rig,
    alice,
    channel,
    db,
    hub,
    lane,
    presence,
    sysop,
)
from tests.test_guest_profile_refusals import FakeSession as KeyedSession
from tests.test_live_blocks import _visible
from tests.test_mail_from_meeting_places import FakeSession as WhoSession, _run_who

_REASON = "signed in without a password"


def _guest_chat_session(lines):
    session = ChatSession(lines)
    session.authenticated_without_credential = True
    return session


async def _chat(session, lane, hub, presence, channel, user, *, mrc_bridge=None):
    await asyncio.wait_for(
        chat_flow._chat_loop(
            session, lane, hub, presence, MessageMailbox(), InputHistory(), channel, user, mrc_bridge=mrc_bridge,
        ),
        timeout=4,
    )


def test_a_guest_session_cannot_set_the_chat_alias(db, lane, hub, presence, channel, alice):
    session = _guest_chat_session(["/nick Defaced", "/quit"])

    asyncio.run(_chat(session, lane, hub, presence, channel, alice))

    text = "\n".join(session.written)
    assert get_nick(db, alice) is None
    assert "is now known as" not in text
    assert _REASON in text


def test_a_guest_session_cannot_register_the_accounts_mrc_handle(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            session = _guest_chat_session(["/mrc register", "s3cret", "/mrc identify", "s3cret", "/quit"])
            await _chat(session, lane, hub, presence, channel, alice, mrc_bridge=rig.bridge)
            text = "\n".join(session.written)
            assert _REASON in text
            assert "Password (not shown" not in text
            await asyncio.sleep(0.2)
            assert not [p for p in rig.fake.received if p.body.startswith(("REGISTER", "IDENTIFY"))]
        finally:
            await rig.close()

    asyncio.run(scenario())


def test_a_guest_session_cannot_send_raw_hub_commands(db, lane, hub, presence, channel, alice):
    """Review of #1074: `/mrc send REGISTER ...` reached the hub under the
    shared account's nick, past the register/identify guard."""
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            session = _guest_chat_session([
                "/mrc send REGISTER s3cret", "/mrc send identify s3cret", "/mrc send UPDATE password x", "/quit",
            ])
            await _chat(session, lane, hub, presence, channel, alice, mrc_bridge=rig.bridge)
            text = "\n".join(session.written)
            assert _REASON in text
            await asyncio.sleep(0.2)
            assert not [
                p for p in rig.fake.received
                if p.body.upper().startswith(("REGISTER", "IDENTIFY", "UPDATE"))
            ]
        finally:
            await rig.close()

    asyncio.run(scenario())


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


def test_a_guest_session_cannot_block_from_whos_online(db, lane, alice, bob):
    session = WhoSession(keys=["0", "1", "s", "b"])
    session.authenticated_without_credential = True

    _run_who(db, lane, alice, session)

    assert not blocks_local_sender(db, alice, bob)
    assert _REASON in " ".join(_visible(session).split())


def test_the_blocked_people_screen_refuses_a_guest_session(db, lane, alice, bob):
    session = KeyedSession(keys=[], lines=["bob"], guest=True)

    asyncio.run(mail_flow.blocked_senders_screen(session, lane, alice))

    assert list_mail_blocks(db, alice) == []
