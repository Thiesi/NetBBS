"""
Issue #305 from the caller's side: `/mrc msg` and `/mrc r`, the
once-per-session "not private" note, the bell on an inbound private
line, and the opt-in line of the bare `/mrc` -- on the rig of
`tests/test_chat_flow_mrc.py` with a real bridge on the loopback fake
hub.
"""

from __future__ import annotations

import asyncio

from netbbs.chat.mailbox import MessageMailbox
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.mrc_private_preference import set_mrc_private_messages_enabled
from tests.test_chat_flow_mrc import (  # noqa: F401 -- fixtures and helpers
    _QueueSession,
    _rig,
    _run,
    _text,
    alice,
    channel,
    db,
    hub,
    lane,
    presence,
    sysop,
)

NOTE = "Private MRC messages are not private on that network"


async def _run_session(lane, hub, presence, channel, user, lines, *, mrc_bridge, while_joined=None):
    """`_run` with a session state, as `browse_channels` gives the loop
    one: the once-per-session note needs it."""
    state: dict = {}
    mailbox = MessageMailbox()
    history = InputHistory()
    session = _QueueSession()
    task = asyncio.create_task(chat_flow._chat_loop(
        session, lane, hub, presence, mailbox, history, channel, user, mrc_bridge=mrc_bridge, mrc_session_state=state,
    ))
    deadline = asyncio.get_running_loop().time() + 2
    while hub.participant_count(channel.name) == 0:
        assert asyncio.get_running_loop().time() < deadline, "caller never joined"
        await asyncio.sleep(0.01)
    if while_joined is not None:
        await while_joined()
    for line in lines:
        session.inputs.put_nowait(line)
    await asyncio.wait_for(task, timeout=4)
    return session


def test_msg_is_refused_until_the_caller_opts_in(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            session, _ = await _run(
                lane, hub, presence, channel, alice, ["/mrc", "/mrc msg bob hi", "/mrc r hi", "/quit"], mrc_bridge=rig.bridge,
            )
            text = _text(session)
            assert "Private MRC messages: off (Profile > Communication)" in text
            assert "(not sent to MRC: you have not opted in to private MRC messages (Profile))" in text
            assert "Nobody on MRC has messaged you privately yet." in text
            assert NOTE not in text
            await rig.fake.wait_for(lambda p: p.body == "LOGOFF")
            assert not [p for p in rig.fake.received if p.to_user.lower() == "bob"]
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_msg_sends_echoes_and_notes_once(db, lane, hub, presence, channel, alice):
    set_mrc_private_messages_enabled(db, alice, True)

    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            # The welcome's MOTD ask and every private chunk draw on the
            # caller's own allowance (PER_USER_BURST, refilled one a
            # second), so each session waits for it to refill first.
            session = await _run_session(
                lane, hub, presence, channel, alice,
                ["/mrc", "/mrc msg carol " + "y" * 600, "/mrc msg", "/mrc msg bob", "/quit"],
                mrc_bridge=rig.bridge, while_joined=lambda: asyncio.sleep(1.5),
            )
            text = _text(session)
            assert "Private MRC messages: on (Profile > Communication)" in text
            assert text.count(NOTE) == 1
            assert "(that line was too long for MRC -- only its first part was sent)" in text
            assert text.count("Usage: /mrc") == 2
            assert len([p for p in rig.fake.received if p.to_user == "carol"]) == 3
            session = await _run_session(
                lane, hub, presence, channel, alice, ["/mrc msg bob hi there", "/mrc msg bob again", "/quit"],
                mrc_bridge=rig.bridge, while_joined=lambda: asyncio.sleep(2.5),
            )
            text = _text(session)
            assert text.count(NOTE) == 1
            assert "[MRC private] -> bob: hi there" in text and "[MRC private] -> bob: again" in text
            sent = await rig.fake.wait_for(lambda p: p.to_user == "bob")
            assert (sent.from_user, sent.msg_ext, sent.to_room) == ("alice", "", "")
            assert sent.body == "|08<|14alice|08>|16|07 hi there"
            # Private lines are shown to the sender only, never as chat.
            assert "<alice> hi there" not in text
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_inbound_private_line_rings_the_bell_and_r_answers_it(db, lane, hub, presence, channel, alice):
    set_mrc_private_messages_enabled(db, alice, True)

    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            async def push_private():
                await rig.fake.wait_for(lambda p: p.body == "NEWROOM::lobby" and p.from_user == "alice")
                await rig.fake.send_line("bob~Other~garden~alice~My_Board~~|03<|11bob|03>|16|07 psst~")
                await asyncio.sleep(0.2)

            session = await _run_session(
                lane, hub, presence, channel, alice, ["/mrc r right back", "/quit"],
                mrc_bridge=rig.bridge, while_joined=push_private,
            )
            raw = "\n".join(session.written)
            text = _text(session)
            assert "\x07" in raw
            assert "[MRC private] bob@Other: psst" in text
            assert text.count(NOTE) == 1
            assert "[MRC private] -> bob@Other: right back" in text
            sent = await rig.fake.wait_for(lambda p: p.to_user == "bob")
            assert (sent.msg_ext, sent.body) == ("Other", "|08<|14alice|08>|16|07 right back")
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_a_caller_who_did_not_opt_in_sees_only_the_old_notice(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            async def push_private():
                await rig.fake.wait_for(lambda p: p.body == "NEWROOM::lobby" and p.from_user == "alice")
                await rig.fake.send_line("bob~Other~garden~alice~My_Board~~|03<|11bob|03>|16|07 psst~")
                await asyncio.sleep(0.2)

            session, _ = await _run(
                lane, hub, presence, channel, alice, ["/quit"], mrc_bridge=rig.bridge, while_joined=push_private,
            )
            text = _text(session)
            assert "bob@Other tried to message you privately" in text
            assert "psst" not in text and "\x07" not in "\n".join(session.written)
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_command_help_lists_the_private_forms():
    syntax, _ = chat_flow._COMMAND_INFO["mrc"]
    assert "msg <nick> <text>|r <text>" in syntax
