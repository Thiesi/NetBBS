"""Private bot replies (issue #1372): real bot processes, real SQLite, the
real chat loop, and a real loopback Link between two nodes."""

import asyncio
from collections import deque
import datetime as dt
from pathlib import Path
import re
import sys

import pytest

from netbbs.bots import link as bot_link_module
from netbbs.bots.registry import (
    PRIVATE_PER_HOUR, BotError, allowed_channel, deliver_private, mute_bot, publish, save_bot,
)
from netbbs.bots.runtime import BotRuntime
from netbbs.chat.bots import BotPrivateAnswer
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import get_scrollback
from netbbs.link.events import CHAT_BOT_PRIVATE_CAPABILITY, LINK_CAPABILITIES, build_endpoint_descriptor
from netbbs.link.protocol import LinkProtocolError, build_bot_frame
from netbbs.net import chat_flow
from netbbs.timeutil import utc_iso, utc_now_iso
from tests.test_bot_runtime import CROSS_PROCESS_TIMEOUT, FakeSession, eventually, next_line
from tests.test_chat_bots import sysop, alice, channel  # noqa: F401
from tests.test_chat_flow_moderation import db, lane, hub, presence, mailbox, history  # noqa: F401

PEER = [sys.executable, "-u", str(Path(__file__).parent / "fixtures" / "bot_private_peer.py")]
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(session) -> str:
    return " ".join(ANSI.sub("", "".join(session.written)).split())


def _install(db, sysop, channel, **kwargs):
    return save_bot(db, actor=sysop, name="Peer", channels=[channel.name], defaults=[], enabled=True,
                    command=PEER, **kwargs)


def _private_rows(db, bot):
    return db.connection.execute("SELECT COUNT(*) FROM bot_private_history WHERE bot_id=?", (bot.id,)).fetchone()[0]


def _channel_rows(db, bot):
    return db.connection.execute("SELECT COUNT(*) FROM bot_output_history WHERE bot_id=?", (bot.id,)).fetchone()[0]


def test_private_answers_have_their_own_per_caller_limit(db, sysop, channel):
    bot = _install(db, sysop, channel, lines_per_hour=1)
    publish(db, bot.id, channel.name, "the channel's one line")
    # A spent channel limit does not block private help...
    with pytest.raises(BotError, match="lines per hour"):
        allowed_channel(db, bot.id, channel.name, check_rate=True)
    allowed_channel(db, bot.id, channel.name, check_rate=True, private_caller="7")
    for _ in range(PRIVATE_PER_HOUR):
        deliver_private(db, bot.id, channel.name, "just for you", "7")
    # ...and private answers never debit the channel's.
    assert _channel_rows(db, bot) == 1
    assert get_scrollback(db, channel)[-1].body == "the channel's one line"
    with pytest.raises(BotError, match="30 private answers per hour for you"):
        deliver_private(db, bot.id, channel.name, "one too many", "7")
    with pytest.raises(BotError, match="private answers"):
        allowed_channel(db, bot.id, channel.name, check_rate=True, private_caller="7")
    # The limit is per caller, and an hour old answers no longer count.
    deliver_private(db, bot.id, channel.name, "someone else", "link:abc:bob")
    old = utc_iso(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2))
    db.connection.execute("UPDATE bot_private_history SET created_at=? WHERE caller='7'", (old,))
    db.connection.commit()
    deliver_private(db, bot.id, channel.name, "an hour later", "7")
    # Text rules are a reply's, and so are the channel's gates.
    for text in ("", "x" * 501, "line\nbreak", "\x1b[2Jcontrol"):
        with pytest.raises(BotError):
            deliver_private(db, bot.id, channel.name, text, "7")
    mute_bot(db, channel, bot, actor=sysop, mute=True)
    with pytest.raises(BotError, match="muted"):
        deliver_private(db, bot.id, channel.name, "muted", "7")
    assert not db.connection.in_transaction


def test_private_requests_and_answers_through_the_real_chat_loop(db, lane, hub, presence, mailbox, history,
                                                                 sysop, alice, channel, monkeypatch):
    bot = _install(db, sysop, channel)
    observed = []

    class InteractiveSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.input = asyncio.Queue()
            self.line_transcript = deque(maxlen=500)

        async def read_line(self, **kwargs):
            return await self.input.get()

    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        original_observe = runtime.observe

        async def observe(channel, message, **kwargs):
            if message.kind == "message":
                observed.append(message.body)
            await original_observe(channel, message, **kwargs)

        runtime.observe = observe
        runtime.start()
        session = InteractiveSession()
        other = ParticipantId(alice.username, 9)
        other_queue = hub.join(channel.name, other)
        task = None

        async def say(text, needle):
            session.input.put_nowait(text)
            try:
                await eventually(lambda: needle in _plain(session))
            except TimeoutError:
                raise AssertionError(_plain(session)[-1500:])

        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            task = asyncio.create_task(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history,
                                                            channel, sysop))
            await eventually(lambda: hub.participant_count(channel.name) == 2)

            # /msg: asked privately, answered privately, seen by no one else.
            await say("/msg Peer.bot where are my reminders",
                      "secret where are my reminders (private=True asked=True)")
            plain = _plain(session)
            assert "(privately to Peer.bot: where are my reminders)" in plain
            assert re.search(r"\[BOT\] <Peer\.bot> \(to you\) secret where are my reminders", plain)
            assert "\x07" not in "".join(session.written)  # no bell for a bot's answer
            assert any("(to you) secret where are my reminders" in ANSI.sub("", kept)
                       for kept in session.line_transcript)  # PgUp and a redraw show it
            # The host refuses a public reply to a privately asked request.
            await say("/msg Peer.bot try-public oops", "refused public: This request was asked privately")
            assert "leaked" not in _plain(session)

            # A public request may be answered privately: the question is
            # public, the answer is not, and it debits the caller's limit.
            await say("Peer.bot: hello", "secret hello (private=True asked=False)")
            await say("Peer.bot: public hi", "public hi (private=True asked=False)")

            # /private Peer.bot: every plain line is a private request.
            await say("/private Peer.bot", "Entering private conversation with Peer.bot")
            await say("how do I upload a file?", "secret how do I upload a file? (private=True asked=True)")
            await say("/close", "Returned to #lobby")

            bodies = [m.body or "" for m in get_scrollback(db, channel)]
            assert "Peer.bot: hello" in bodies and any(b.startswith("public hi") for b in bodies)
            assert not [b for b in bodies if "reminders" in b or "upload" in b or "oops" in b or "secret" in b]
            assert observed == ["Peer.bot: hello", "Peer.bot: public hi"]
            assert _private_rows(db, bot) == 4 and _channel_rows(db, bot) == 1
            others = []
            while not other_queue.empty():
                others.append(str(getattr(other_queue.get_nowait(), "body", "")))
            assert not [b for b in others if "secret" in b or "reminders" in b or "upload" in b]

            # A muted bot is refused with the reason, before the bot sees it.
            mute_bot(db, channel, bot, actor=sysop, mute=True)
            await say("/msg Peer.bot are you there", "Peer.bot is muted here.")
            await say("/private Peer.bot", "Peer.bot is muted here.")
            mute_bot(db, channel, bot, actor=sysop, mute=False)
            # A spent private limit is said, never silent.
            db.connection.executemany("INSERT INTO bot_private_history VALUES (?, ?, ?)",
                                      [(bot.id, str(sysop.id), utc_now_iso())] * PRIVATE_PER_HOUR)
            db.connection.commit()
            await say("/msg Peer.bot one more", "30 private answers per hour for you")
            # A name no bot has stays a person's name.
            await say("/msg Nobody.bot hi", "Nobody.bot")
            session.input.put_nowait("/quit")
            await asyncio.wait_for(task, CROSS_PROCESS_TIMEOUT)
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runtime.close()

    asyncio.run(run())


def test_private_flags_on_the_wire_are_bounded():
    base = {"channel_id": "c", "bot": "Baud.bot", "caller": "bob", "text": "help"}
    assert build_bot_frame("bot_request", {**base, "private": True}).payload["private"] is True
    build_bot_frame("bot_request", {**base, "private": True, "access": {"level": 1, "authenticated": True}})
    build_bot_frame("bot_result", {"request_id": "r", "error": "", "private": {"text": "x" * 500}})
    for kind, payload in (
        ("bot_request", {**base, "private": False}),
        ("bot_request", {**base, "other": 1}),
        ("bot_result", {"request_id": "r", "error": "", "private": {"text": "x" * 501}}),
        ("bot_result", {"request_id": "r", "error": "", "private": {"text": "  "}}),
        ("bot_result", {"request_id": "r", "error": "", "private": "bare text"}),
        ("bot_result", {"request_id": "r", "error": "", "private": {"text": "x", "to": "someone"}}),
        ("bot_result", {"request_id": "r", "error": "refused", "private": {"text": "x"}}),
    ):
        with pytest.raises(LinkProtocolError):
            build_bot_frame(kind, payload)


def _without_private(node, peer):
    """`node`'s view of `peer`: an older endpoint, without chat_bot_private_v1."""
    node.link_node.peers[peer.identity.fingerprint].descriptor = build_endpoint_descriptor(
        signing_identity=peer.identity.signing_key, subject_fingerprint=peer.identity.fingerprint,
        addresses=[], outgoing_only=True, created_at=utc_now_iso(),
        capabilities=tuple(c for c in LINK_CAPABILITIES if c != CHAT_BOT_PRIVATE_CAPABILITY),
    )


def test_private_requests_and_answers_over_a_real_loopback_link(tmp_path, monkeypatch):
    from tests.test_bot_link import Node, connect
    from tests.test_link_realtime_channels import _setup_linked_channel

    async def run():
        host, caller = Node(tmp_path, "host"), Node(tmp_path, "caller")
        hc, cc = _setup_linked_channel(host, caller)
        bot = save_bot(host.db, actor=host.sysop, name="Peer", channels=[hc.name], defaults=[],
                       linked_channels=[hc.name], enabled=True, command=PEER)
        pid = ParticipantId("alice", 1)
        queue = caller.hub.join(cc.name, pid)
        host_queue = host.hub.join(hc.name, ParticipantId("watcher", 1))
        fingerprint = host.identity.fingerprint

        def request(text, **kwargs):
            return caller.bots.request(cc, caller.alice, pid, fingerprint, "Peer.bot", text, **kwargs)

        try:
            await connect((host, caller))
            await eventually(lambda: bot.id in host.runtime.running_ids)
            # Asked privately: the answer comes back in bot_result only.
            assert request("my boards", private=True) is None
            answer = await next_line(queue, "secret my boards (private=True asked=True)")
            assert isinstance(answer, BotPrivateAnswer) and answer.node_fingerprint == fingerprint
            await eventually(lambda: not caller.bots.requests)
            # A public request from a capable node may be answered privately.
            assert await caller.address(cc, f"Peer.bot@{fingerprint}: plain") is None
            assert isinstance(await next_line(queue, "secret plain (private=True asked=False)"), BotPrivateAnswer)
            await eventually(lambda: not caller.bots.requests)
            assert not [m for m in get_scrollback(host.db, hc) if m.author_kind == "bot"]
            assert _private_rows(host.db, bot) == 2
            remote_caller = f"link:{caller.identity.fingerprint}:alice"
            assert host.db.connection.execute("SELECT DISTINCT caller FROM bot_private_history").fetchall()[0][0] \
                == remote_caller
            # The privately asked line was never a channel line on either node.
            assert not [m for m in get_scrollback(caller.db, cc) if "my boards" in m.body]
            assert not [m for m in get_scrollback(host.db, hc) if "my boards" in m.body]
            while not host_queue.empty():
                assert "my boards" not in str(host_queue.get_nowait())

            # Checked again on receipt: hidden by a mute set while the bot worked.
            calls = []

            def visible_then_hidden(db, channel, label, fp):
                calls.append(label)
                return len(calls) == 1

            monkeypatch.setattr(bot_link_module, "remote_bot_visible", visible_then_hidden)
            assert request("hidden later", private=True) is None
            await eventually(lambda: len(calls) == 2 and not caller.bots.requests)
            await asyncio.sleep(.2)
            while not queue.empty():
                assert "hidden later" not in str(queue.get_nowait())
            monkeypatch.undo()

            # A host without the capability is never asked privately.
            current = caller.link_node.peers[fingerprint].descriptor
            _without_private(caller, host)
            assert "cannot answer privately" in request("x", private=True)
            caller.link_node.peers[fingerprint].descriptor = current

            # A requester without it: the host refuses the private answer,
            # never makes it public, and the bot may then reply publicly.
            _without_private(host, caller)
            assert await caller.address(cc, f"Peer.bot@{fingerprint}: older") is None
            fallback = await next_line(queue, "fallback older")
            assert "cannot take private answers" in fallback.body
            assert not isinstance(fallback, BotPrivateAnswer)
            # And it refuses a private key from a peer that lacks the capability.
            session = host.registry.get(caller.identity.fingerprint)
            with pytest.raises(LinkProtocolError, match="chat_bot_private_v1"):
                await host.bots.handle_frame(session, build_bot_frame("bot_request", {
                    "channel_id": hc.channel_id, "bot": "Peer.bot", "caller": "alice", "text": "x", "private": True}))
        finally:
            for node in (caller, host):
                await node.close()

    asyncio.run(run())
