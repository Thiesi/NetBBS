"""
Issue #298 from the caller's side, on the same rig as
`tests/test_chat_flow_mrc.py` (whose fixtures and helpers this reuses):
MRC colours rendered or stripped per viewer, live and on replay, and
`/mrc <subcommand>` replies shown to the asker alone.
"""

from __future__ import annotations

import asyncio

from netbbs.chat.hub import ParticipantId
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.scrollback import record_message
from netbbs.mrc.bridge import MrcNotice
from netbbs.net import chat_flow
from netbbs.net.char_input import InputHistory
from netbbs.net.mrc_color_preference import set_mrc_colors_enabled
from netbbs.rendering.ansi import fg
from tests.test_chat_flow_mrc import (  # noqa: F401 -- fixtures
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


def test_mrc_colours_are_rendered_or_stripped_per_viewer(db, lane, hub, presence, channel, alice):
    async def scenario(expect_colour: bool):
        rig = await _rig(db, lane, hub, channel)
        try:
            async def push():
                await rig.fake.send_line("bob~Other~lobby~~~lobby~|03<|11bob|03>|16|07 |12blue words|07 here~")
                await rig.fake.send_line("bob~Other~lobby~~~lobby~|15* |13bob |09waves~")
                await rig.fake.send_line("SERVER~~~CLIENT~~~ROOMTOPIC:lobby:|14be |15excellent~")
                await asyncio.sleep(0.15)

            session, _ = await _run(
                lane, hub, presence, channel, alice, ["/quit"], mrc_bridge=rig.bridge, while_joined=push,
            )
            raw = "\n".join(session.written)
            text = _text(session)
            assert "<bob@Other (MRC)> blue words here" in text
            # The action bullet itself follows the viewer's Unicode-style
            # preference; what matters here is one name, then the words.
            assert "bob@Other (MRC) waves" in text
            assert "[MRC] room topic: be excellent" in text
            assert "<bob>" not in text and "|12" not in text and "|14" not in text
            assert (fg(12) in raw) is expect_colour
            assert (fg(9) in raw) is expect_colour
            assert (fg(14) in raw) is expect_colour
        finally:
            await rig.close()

    asyncio.run(scenario(True))
    set_mrc_colors_enabled(db, alice, False)
    asyncio.run(scenario(False))


def test_scrollback_replay_renders_mrc_colours_the_same_way(db, lane, hub, presence, channel, alice):
    record_message(
        db, channel, kind="message", author_label="bob@Other (MRC)", author_fingerprint=None,
        body="|12earlier|07 words", external_source="mrc", index_body="earlier words",
    )

    async def scenario():
        session, _ = await _run(lane, hub, presence, channel, alice, ["/quit"])
        raw = "\n".join(session.written)
        assert "<bob@Other (MRC)> earlier words" in _text(session)
        assert fg(12) in raw
    asyncio.run(scenario())


def test_mrc_subcommands_ask_the_hub_and_show_the_reply_to_the_asker_only(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            carol_queue = hub.join(channel.name, ParticipantId("carol", 9))
            session = _QueueSession()
            task = asyncio.create_task(
                chat_flow._chat_loop(
                    session, lane, hub, presence, MessageMailbox(), InputHistory(), channel, alice,
                    mrc_bridge=rig.bridge,
                )
            )
            await rig.fake.wait_for(lambda p: p.body == "NEWROOM::lobby" and p.from_user == "alice")
            session.inputs.put_nowait("/mrc rooms")
            await rig.fake.wait_for(lambda p: p.body == "LIST" and p.from_user == "alice")
            session.inputs.put_nowait("/mrc bbses phenom")
            await rig.fake.wait_for(lambda p: p.body == "CONNECTED phenom")
            session.inputs.put_nowait("/mrc ctcp bob version")
            await rig.fake.wait_for(lambda p: p.body == "[CTCP] alice bob VERSION")
            # Three asks spent the caller's own burst (the same allowance a
            # chat line uses); the fourth waits for it to refill.
            session.inputs.put_nowait("/mrc send TOPICS")
            await asyncio.sleep(0.2)
            assert "(not sent to MRC: you're sending faster than MRC allows)" in _text(session)
            await asyncio.sleep(1.1)
            session.inputs.put_nowait("/mrc send TOPICS")
            await rig.fake.wait_for(lambda p: p.body == "TOPICS")
            session.inputs.put_nowait("/mrc rooms extra")
            session.inputs.put_nowait("/mrc bogus")
            session.inputs.put_nowait("/mrc ctcp bob")
            await asyncio.sleep(0.3)
            session.inputs.put_nowait("/quit")
            await asyncio.wait_for(task, timeout=4)
            text = _text(session)
            assert "[MRC] LIST reply line 1" in text and "[MRC] LIST reply line 2" in text
            assert "[MRC] CONNECTED reply line 2 phenom" in text
            assert text.count("Usage: /mrc") == 3
            assert "Unknown /mrc subcommand: bogus" in text
            # Carol, in the same channel, saw none of the replies.
            while not carol_queue.empty():
                item = carol_queue.get_nowait()
                assert not (isinstance(item, MrcNotice) and item.kind == "reply"), item
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_mrc_subcommand_failures_are_explained(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            await rig.bridge.close()
            session, _ = await _run(
                lane, hub, presence, channel, alice, ["/mrc motd", "/quit"], mrc_bridge=rig.bridge,
            )
            assert "(not sent to MRC: the MRC link is offline)" in _text(session)
        finally:
            await rig.close()
    asyncio.run(scenario())


def test_help_lists_the_subcommands(db, lane, hub, presence, channel, alice):
    async def scenario():
        rig = await _rig(db, lane, hub, channel)
        try:
            session, _ = await _run(lane, hub, presence, channel, alice, ["/help mrc", "/quit"], mrc_bridge=rig.bridge)
            # Both lines wrap at the terminal width; compare with the
            # wrap-inserted line breaks folded back into single spaces.
            text = " ".join(_text(session).split())
            assert "/mrc [rooms|who|bbses [search]|info <bbs>|motd" in text
            assert "shown to you alone" in text
        finally:
            await rig.close()
    asyncio.run(scenario())
