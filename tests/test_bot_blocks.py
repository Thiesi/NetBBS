"""Titled block replies for bots (issue #1373): the shape and its bounds,
storage, rendering, real bot processes through the real chat loop, and a
real loopback Link where one node lacks `chat_bot_blocks_v1`."""

import asyncio
from collections import deque
import json
from pathlib import Path
import re
import sys

import pytest

from netbbs.bots.baud import Baud
from netbbs.bots.registry import BotError, check_block, deliver_private, mute_bot, publish, save_bot
from netbbs.bots.runtime import BotRuntime
from netbbs.chat.blocks import MAX_FALLBACK, BotBlock, block_from_json, block_from_wire, parse_block
from netbbs.chat.bots import BotPrivateAnswer, mute_remote_bot, set_channel_bot_policy
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import get_scrollback
from netbbs.chat.timestamps import set_timestamps_enabled
from netbbs.link.events import (
    CHAT_BOT_BLOCKS_CAPABILITY, LINK_CAPABILITIES, ChannelGenesis, ChannelMessage as SignedChannelMessage,
    build_channel_message, build_endpoint_descriptor,
)
from netbbs.link.protocol import (
    BOT_BLOCK_MAX_BYTES, LinkProtocolError, bot_block_bytes, build_bot_frame, build_channel_message_frame,
    build_scrollback_snapshot_frame, build_subscribe_frame,
)
from netbbs.net import chat_flow
from netbbs.timeutil import utc_now_iso
from tests.test_bot_runtime import CROSS_PROCESS_TIMEOUT, FakeSession, eventually, next_line
from tests.test_chat_bots import sysop, alice, channel  # noqa: F401
from tests.test_chat_flow_moderation import db, lane, hub, presence, mailbox, history  # noqa: F401

PEER = [sys.executable, "-u", str(Path(__file__).parent / "fixtures" / "bot_block_peer.py")]
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
STATS = {"title": "Node stats", "rows": [{"label": "Online", "text": "4 people"}, {"label": "Uptime", "text": "3h 12m"}]}


def _plain(text) -> str:
    return ANSI.sub("", "".join(text) if isinstance(text, (list, tuple)) else text)


def _install(db, sysop, channel, **kwargs):
    return save_bot(db, actor=sysop, name="Peer", channels=[channel.name], defaults=[], enabled=True,
                    command=PEER, **kwargs)


# -- the shape -----------------------------------------------------------------

def test_a_block_is_a_title_and_up_to_twelve_rows_of_plain_text():
    block = parse_block({"title": "Commands", "rows": [{"label": "help", "text": "this list"}, {"text": "No model."}]})
    assert block == BotBlock("Commands", (("help", "this list"), ("", "No model.")))
    assert block.fallback() == "Commands: help this list; No model."
    assert parse_block(block.to_wire()) == block
    assert parse_block({"title": "T", "rows": [{"label": "", "text": "x"}]}).rows == (("", "x"),)
    for bad in (
        "text", {"title": "T"}, {"title": "T", "rows": []}, {"title": "", "rows": [{"text": "x"}]},
        {"title": "x" * 41, "rows": [{"text": "x"}]}, {"title": "T", "rows": [{"text": "x"}] * 13},
        {"title": "T", "rows": [{"text": "x" * 201}]}, {"title": "T", "rows": [{"label": "x" * 25, "text": "x"}]},
        {"title": "T", "rows": [{"text": "two\nlines"}]}, {"title": "T", "rows": [{"text": "\x1b[2Jcontrol"}]},
        {"title": "T", "rows": [{"text": "  "}]}, {"title": "T", "rows": ["bare"]},
        {"title": "T", "rows": [{"text": "x", "color": 1}]}, {"title": "T", "rows": [{"text": "x"}], "body": "y"},
    ):
        with pytest.raises(BotError):
            check_block(bad)
        assert block_from_wire(bad) is None
    assert block_from_json("{not json") is None and block_from_json(None) is None


def test_the_fallback_is_cut_to_one_line_and_the_largest_block_fits_the_wire_bound():
    largest = {"title": "\U0001F4E1" * 40,
               "rows": [{"label": "\U0001F4E1" * 24, "text": "\U0001F4E1" * 200} for _ in range(12)]}
    block = parse_block(largest)
    assert len(block.fallback()) == MAX_FALLBACK and block.fallback().endswith("\u2026")
    assert bot_block_bytes(block.to_wire()) <= BOT_BLOCK_MAX_BYTES
    # It still fits a live frame beside its fallback body.
    build_channel_message_frame("c", "Baud.bot", "Baud.bot", block.fallback(), utc_now_iso(), block=block.to_wire())


# -- storage and rendering ----------------------------------------------------

def test_a_block_is_one_row_one_line_against_the_limit_and_found_by_its_fallback(db, sysop, alice, channel):
    bot = _install(db, sysop, channel, lines_per_hour=1)
    _, message = publish(db, bot.id, channel.name, None, block=check_block(STATS))
    assert message.body == "Node stats: Online 4 people; Uptime 3h 12m" and message.block.title == "Node stats"
    assert get_scrollback(db, channel)[-1].block == message.block
    assert db.connection.execute("SELECT COUNT(*) FROM bot_output_history").fetchone()[0] == 1
    with pytest.raises(BotError, match="lines per hour"):
        publish(db, bot.id, channel.name, None, block=check_block(STATS))
    from netbbs.search import search_channel_messages
    hits = search_channel_messages(db, alice, "Uptime", visible_channels=[channel])
    assert [hit.message_id for hit in hits] == [message.id]
    # A malformed stored block is drawn as its fallback line.
    db.connection.execute("UPDATE channel_messages SET block_json='[1]' WHERE id=?", (message.id,))
    db.connection.commit()
    assert get_scrollback(db, channel)[-1].block is None


def test_a_block_renders_like_who_with_the_hosts_bot_label_and_no_cut_text(db, sysop, alice, channel):
    bot = _install(db, sysop, channel)
    long_text = "a fairly long answer that has to wrap under itself " * 3
    _, message = publish(db, bot.id, channel.name, None,
                         block=check_block({"title": "Node stats", "rows": [*STATS["rows"], {"label": "Note",
                                                                                             "text": long_text.strip()}]}))
    rendered = chat_flow._render_channel_message(db, channel, alice, message, width=40)
    assert isinstance(rendered, chat_flow._BlockLines)
    lines = [_plain(line) for line in rendered.lines]
    # At 40 columns the bot's own title gives way first; the host's label stays.
    assert lines[0].startswith("── [BOT] Peer.bot · ") and re.search(r"\d\d:\d\d ──$", lines[0])
    wide = _plain(chat_flow._render_channel_message(db, channel, alice, message, width=80).lines[0])
    assert wide.startswith("── [BOT] Peer.bot · Node stats ─")
    assert all(len(line) <= 40 for line in lines)
    assert lines[1].split() == ["Online", "4", "people"] and lines[1].index("4") == lines[3].index("a")
    assert " ".join(" ".join(lines[3:]).split()) == "Note " + " ".join(long_text.split())
    # The plain string is the fallback line, for a writer that knows only lines.
    assert "[BOT] <Peer.bot> Node stats: Online 4 people" in _plain(rendered)
    # Replay draws the same block, unpaged and counted as one event.
    replay = chat_flow._render_all_scrollback(db, channel, alice, get_scrollback(db, channel), width=40)
    assert len(replay) == 1 and replay[0].lines == rendered.lines
    # No timestamp for a viewer without them.
    set_timestamps_enabled(db, alice, False)
    title = _plain(chat_flow._render_channel_message(db, channel, alice, message, width=40).lines[0])
    assert not re.search(r"\d\d:\d\d", title)
    # Mutes and /bots filter it like any bot line.
    mute_bot(db, channel, bot, actor=sysop, mute=True)
    assert get_scrollback(db, channel)[-1].block is not None
    set_channel_bot_policy(db, channel, "none", changed_by=sysop)
    assert chat_flow._render_channel_message(db, channel, alice, message, width=40) == ""


def test_a_private_block_says_to_you_where_a_narrow_title_cannot_cut_it(db, sysop, alice, channel):
    block = check_block({"title": "A forty character title, which is long", "rows": [{"text": "x" * 40}]})
    answer = BotPrivateAnswer("Peer.bot", block.fallback(), utc_now_iso(), block=block)
    rendered = chat_flow._render_bot_private_answer(db, alice, answer, width=40)
    title = _plain(rendered.lines[0])
    assert title.startswith("── [BOT] Peer.bot") and re.search(r"to you · \d\d:\d\d ──$", title)
    assert len(title) <= 40


def test_baud_help_is_a_block_on_a_host_that_takes_them():
    sent = []
    baud = Baud({"blocks": True, "queries": ["stats"], "phrases": {"help": "Hallo {name}."}}, sent.append)
    baud.handle({"type": "message", "request": "r", "caller": {"id": 7, "name": "alice"}, "channel": "c",
                 "text": "help"})
    block = check_block(sent[-1]["block"])  # the host would take it as sent
    assert sent[-1]["action"] == "reply" and "text" not in sent[-1]
    assert block.title == "Commands" and block.rows[0] == ("", "Hallo alice.")
    assert ("stats", "who is on, uptime") in block.rows
    assert {"help", "tell <name> <text>", "tells, cancel tell <id>", "roll [NdS+M], coin"} <= {label for label, _ in block.rows}
    assert block.rows[-1] == ("", "No language model is enabled. Replies are public.")
    # An older host gets the one line it always did.
    baud = Baud({}, sent.append)
    baud.handle({"type": "message", "request": "r", "caller": {"id": 7, "name": "alice"}, "channel": "c",
                 "text": "help"})
    assert sent[-1]["text"].startswith("alice: Happy to help! help;")


def test_botkit_reply_block_cuts_to_the_bounds_the_host_checks():
    from netbbs.bots.botkit import Bot, Message
    sent = []
    bot = Bot({"blocks": True}, sent.append)
    assert bot.blocks
    message = Message({"request": "r", "caller": {"name": "alice"}})
    message.bot = bot
    message.reply_block("T" * 60, [("l" * 30, "x\ty " * 100), "plain"] * 10, private=True)
    action = sent[-1]
    block = check_block(action["block"])
    assert action["private"] is True and len(block.title) == 40 and len(block.rows) == 12
    assert block.rows[0][0] == "l" * 24 and len(block.rows[0][1]) == 200 and block.rows[1] == ("", "plain")


# -- labels as command syntax ---------------------------------------------------

COMMANDS = {"title": "Commands", "labels": "commands", "rows": [
    {"text": "Happy to help!"},
    {"label": "remind <when> <text>", "text": "a reminder here, shown publicly"},
    {"label": "reminders, cancel <id>", "text": "your reminders here; cancel one of yours"},
    {"label": "roll [NdS+M], coin", "text": "dice and a coin"},
]}


def test_a_block_may_mark_its_labels_as_command_syntax_and_nothing_else():
    block = parse_block(COMMANDS)
    assert block.labels == "commands" and block.to_wire()["labels"] == "commands"
    assert parse_block(block.to_wire()) == block and block_from_json(block.to_json()) == block
    assert block.fallback().startswith("Commands: Happy to help!; remind <when> <text> a reminder")
    # Absent means plain labels, and a plain block's wire shape is unchanged.
    plain = parse_block(STATS)
    assert plain.labels is None and "labels" not in plain.to_wire()
    assert plain.to_json() == json.dumps(STATS, separators=(",", ":"))
    for bad in ("plain", "Commands", None, "", 1, True, ["commands"], {"commands": True}):
        with pytest.raises(BotError, match="labels"):
            check_block({**STATS, "labels": bad})
        assert block_from_wire({**STATS, "labels": bad}) is None
    # The marker costs a few bytes: the largest block still fits the wire bound.
    largest = {"title": "\U0001F4E1" * 40, "labels": "commands",
               "rows": [{"label": "\U0001F4E1" * 24, "text": "\U0001F4E1" * 200} for _ in range(12)]}
    assert bot_block_bytes(parse_block(largest).to_wire()) <= BOT_BLOCK_MAX_BYTES


def test_command_labels_are_styled_as_help_styles_the_same_syntax(db, sysop, alice, channel):
    from netbbs.net.node_theme import effective_accent_color_256
    from netbbs.rendering import colored
    from netbbs.rendering.theme import MUTED_COLOR
    bot = _install(db, sysop, channel)
    _, message = publish(db, bot.id, channel.name, None, block=check_block(COMMANDS))
    lines = chat_flow._render_channel_message(db, channel, alice, message, width=80).lines
    accent = effective_accent_color_256(db)
    help_row = chat_flow._render_help_entry("remind <when> <text>", "x", width=80, syntax_width=24,
                                            accent_color=accent)[0]
    remind = chat_flow._styled_help_syntax("remind <when> <text>", "remind", first=True, accent_color=accent)
    assert remind in help_row and remind in lines[2]  # /help's styling, byte for byte
    assert colored("remind", fg_color=accent, bold=True) in lines[2]
    assert colored("<", fg_color=MUTED_COLOR) in lines[2] and colored("when", fg_color=chat_flow.LABEL_COLOR) in lines[2]
    # Each command of a list styled on its own, the separators muted.
    listed = (chat_flow._styled_help_syntax("reminders", "reminders", first=True, accent_color=accent)
              + colored(", ", fg_color=MUTED_COLOR)
              + chat_flow._styled_help_syntax("cancel <id>", "cancel", first=True, accent_color=accent))
    assert listed in lines[3]
    assert colored("coin", fg_color=accent, bold=True) in lines[4]
    # The text column lines up by visible width, as the plain block's does.
    plain = check_block({key: value for key, value in COMMANDS.items() if key != "labels"})
    _, plain_message = publish(db, bot.id, channel.name, None, block=plain)
    plain_lines = chat_flow._render_channel_message(db, channel, alice, plain_message, width=80).lines
    assert [_plain(line) for line in lines[1:]] == [_plain(line) for line in plain_lines[1:]]
    # The same rule draws a private answer.
    answer = BotPrivateAnswer("Peer.bot", message.body, utc_now_iso(), block=message.block)
    assert remind in chat_flow._render_bot_private_answer(db, alice, answer, width=80).lines[2]


def test_a_plain_block_draws_its_labels_as_before(db, sysop, alice, channel):
    from netbbs.rendering import colored
    bot = _install(db, sysop, channel)
    _, message = publish(db, bot.id, channel.name, None, block=check_block(STATS))
    lines = chat_flow._render_channel_message(db, channel, alice, message, width=80).lines
    assert list(lines[1:]) == [
        " " + colored("Online", fg_color=chat_flow.LABEL_COLOR) + "  " + colored("4 people", fg_color=chat_flow.VALUE_COLOR),
        " " + colored("Uptime", fg_color=chat_flow.LABEL_COLOR) + "  " + colored("3h 12m", fg_color=chat_flow.VALUE_COLOR),
    ]


def test_botkit_and_baud_mark_the_help_list_as_commands():
    from netbbs.bots.botkit import Bot, Message
    sent = []
    bot = Bot({"blocks": True}, sent.append)
    message = Message({"request": "r", "caller": {"name": "alice"}})
    message.bot = bot
    message.reply_block("T", [("help", "this")])
    assert "labels" not in sent[-1]["block"]
    message.reply_block("T", [("help", "this")], labels="commands")
    assert check_block(sent[-1]["block"]).labels == "commands"
    Baud({"blocks": True, "queries": ["stats", "seen", "top"]}, sent.append).handle(
        {"type": "message", "request": "r", "caller": {"id": 7, "name": "alice"}, "channel": "c", "text": "help"})
    assert check_block(sent[-1]["block"]).labels == "commands"


# -- real bot processes and the real chat loop -----------------------------------

class _Interactive(FakeSession):
    def __init__(self, height=24):
        super().__init__()
        self.input = asyncio.Queue()
        self.line_transcript = deque(maxlen=500)
        self.terminal_height = height

    async def read_line(self, **kwargs):
        return await self.input.get()


def test_block_replies_through_the_real_chat_loop_paged_when_tall(db, lane, hub, presence, mailbox, history,
                                                                  sysop, alice, channel):
    bot = _install(db, sysop, channel)

    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        runtime.start()
        session = _Interactive()
        other = hub.join(channel.name, ParticipantId(alice.username, 9))
        task = None

        async def say(text, needle):
            session.input.put_nowait(text)
            try:
                await eventually(lambda: needle in _plain(session.written))
            except TimeoutError:
                raise AssertionError(_plain(session.written)[-2000:])

        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            task = asyncio.create_task(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history,
                                                            channel, sysop))
            await eventually(lambda: hub.participant_count(channel.name) == 2)
            await say("Peer.bot: hello", "blocks=True")
            await say("Peer.bot: block all good", "[BOT] Peer.bot · Node stats")
            drawn = _plain(session.written)
            assert re.search(r"Online\s+4 people", drawn) and re.search(r"all good", drawn)
            # Everyone else in the channel gets the same one message.
            seen = []
            while not other.empty():
                seen.append(other.get_nowait())
            blocks = [m for m in seen if getattr(m, "block", None) is not None]
            assert len(blocks) == 1 and blocks[0].body.startswith("Node stats: Online 4 people")
            # Taller than the chat area: a page at a time.
            await say("Peer.bot: tall", "page 1 of")
            assert "row 12" not in _plain(session.written).split("Everything")[-1]
            # A private block, seen by this session only.
            await say("/msg Peer.bot private check the oven", "Your reminders")
            assert re.search(r"Your reminders ─+ to you", _plain(session.written))
            assert not [m for m in get_scrollback(db, channel) if "oven" in (m.body or "")]
            # Refusals: say and announce stay one line; a bad block is told.
            await say("Peer.bot: say-block", "refused: Only reply takes a block")
            await say("Peer.bot: both", "A reply carries text or a block, not both.")
            await say("Peer.bot: bad", "A block has 1-12 rows.")
            # Join replay draws the stored blocks, unpaged.
            replay = _Interactive()
            replay.input.put_nowait("/quit")
            await chat_flow._chat_loop(replay, lane, hub, presence, mailbox, history, channel, alice)
            replayed = _plain(replay.written)
            assert "[BOT] Peer.bot · Node stats" in replayed and "row 12" in replayed
            assert "page 1 of" not in replayed
            session.input.put_nowait("/quit")
            await asyncio.wait_for(task, CROSS_PROCESS_TIMEOUT)
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runtime.close()

    asyncio.run(run())


# -- Link ------------------------------------------------------------------------

def _without_blocks(node, peer):
    """`node`'s view of `peer`: an older endpoint, without chat_bot_blocks_v1."""
    current = node.link_node.peers[peer.identity.fingerprint].descriptor.payload
    node.link_node.peers[peer.identity.fingerprint].descriptor = build_endpoint_descriptor(
        signing_identity=peer.identity.signing_key, subject_fingerprint=peer.identity.fingerprint,
        addresses=current.get("addresses"), outgoing_only=bool(current.get("outgoing_only")),
        created_at=utc_now_iso(),
        capabilities=tuple(c for c in LINK_CAPABILITIES if c != CHAT_BOT_BLOCKS_CAPABILITY),
    )


def test_blocks_over_a_real_loopback_link_with_one_node_lacking_the_capability(tmp_path):
    from tests.test_bot_link import Node, connect
    from tests.test_link_realtime_channels import _setup_linked_channel
    from netbbs.link.channels import materialize_carried_channel, materialize_carried_channel_message

    async def run():
        host, caller, older = Node(tmp_path, "host"), Node(tmp_path, "caller"), Node(tmp_path, "older")
        for node in (host, caller, older):
            node.bridge._blocks_capable = lambda fp, node=node: node.bots.capable(fp, CHAT_BOT_BLOCKS_CAPABILITY)
        hc, cc = _setup_linked_channel(host, caller)
        genesis_json = host.db.connection.execute(
            "SELECT link_genesis_json FROM channels WHERE id=?", (hc.id,)).fetchone()[0]
        oc = materialize_carried_channel(older.db, ChannelGenesis.from_dict(json.loads(genesis_json)))
        bot = save_bot(host.db, actor=host.sysop, name="Peer", channels=[hc.name], defaults=[],
                       linked_channels=[hc.name], enabled=True, command=PEER)
        cq = caller.hub.join(cc.name, ParticipantId("alice", 1))
        oq = older.hub.join(oc.name, ParticipantId("alice", 1))
        fingerprint = host.identity.fingerprint
        try:
            await connect((host, caller, older))
            _without_blocks(host, older)
            _without_blocks(older, host)
            await eventually(lambda: bot.id in host.runtime.running_ids)
            # The older node subscribes live.
            upstream = await older.direct.ensure_session(fingerprint)
            await upstream.send(build_subscribe_frame(oc.channel_id))
            await eventually(lambda: host.bridge._subscribers.get(hc.channel_id))

            # A remote request answered with a block: the requester's node
            # gets it by the direct copy, block and all.
            assert await caller.address(cc, f"Peer.bot@{fingerprint}: block over link") is None
            live = await next_line(cq, "Node stats: Online 4 people")
            assert live.block is not None and live.block.rows[-1] == ("", "over link")
            # The node without the capability gets the same message as its
            # fallback line, live -- no block key it would refuse.
            older_live = await next_line(oq, "Node stats: Online 4 people")
            assert older_live.block is None and older_live.body == live.body
            await eventually(lambda: not caller.bots.requests)

            # The signed event carries the block beside the fallback body.
            row = host.db.connection.execute(
                "SELECT link_event_json FROM channel_messages WHERE author_kind='bot' ORDER BY id").fetchone()
            signed = SignedChannelMessage.from_dict(json.loads(row[0]))
            assert signed.payload["body"] == live.body and signed.payload["block"] == live.block.to_wire()
            # Async catch-up materializes it once; live dedup sees one line.
            materialized = materialize_carried_channel_message(caller.db, signed, sender_fingerprint=fingerprint)
            assert materialized.block == live.block
            await caller.bridge.on_carried_message_materialized(signed.content_id, signed.payload, materialized)
            await asyncio.sleep(.1)
            assert cq.empty()
            # A remote mute filters the block like any remote bot line.
            mute_remote_bot(caller.db, cc, "Peer.bot", fingerprint, actor=caller.sysop, mute=True)
            assert chat_flow._render_channel_message(caller.db, cc, caller.alice, materialized) == ""
            mute_remote_bot(caller.db, cc, "Peer.bot", fingerprint, actor=caller.sysop, mute=False)
            remote = chat_flow._render_channel_message(caller.db, cc, caller.alice, materialized)
            assert _plain(remote.lines[0]).startswith("── [BOT] Peer.bot@")

            # A block whose labels are command syntax keeps the marker, live
            # and in the signed event, between two current nodes.
            assert await caller.address(cc, f"Peer.bot@{fingerprint}: commands over link") is None
            commands = await next_line(cq, "Commands: remind <when> <text> a reminder")
            assert commands.block.labels == "commands" and commands.block.rows[-1] == ("", "over link")
            await eventually(lambda: not caller.bots.requests)
            row = host.db.connection.execute(
                "SELECT link_event_json FROM channel_messages WHERE author_kind='bot' AND body LIKE 'Commands:%'"
            ).fetchone()
            signed_commands = SignedChannelMessage.from_dict(json.loads(row[0]))
            assert signed_commands.payload["block"]["labels"] == "commands"
            assert materialize_carried_channel_message(
                caller.db, signed_commands, sender_fingerprint=fingerprint).block == commands.block
            older_commands = await next_line(oq, "Commands: remind <when> <text> a reminder")
            assert older_commands.block is None

            # A private block comes back inside bot_result.
            assert caller.bots.request(cc, caller.alice, ParticipantId("alice", 1), fingerprint, "Peer.bot",
                                       "private the oven", private=True) is None
            answer = await next_line(cq, "Your reminders: #1 the oven")
            assert isinstance(answer, BotPrivateAnswer) and answer.block.title == "Your reminders"
            await eventually(lambda: not caller.bots.requests)

            # Join snapshots: the block rides only to the capable subscriber;
            # the snapshot's request is the subscribe frame's own id.
            async def snapshot(node, channel):
                request_id = node.bridge.begin_scrollback_request(channel.channel_id, fingerprint)
                session = await node.direct.ensure_session(fingerprint)
                await session.send(build_subscribe_frame(channel.channel_id, message_id=request_id))
                await eventually(lambda: node.bridge._remote_channel_scrollback.get(request_id) is not None)
                entries = node.bridge.pop_channel_scrollback(channel.channel_id, request_id)
                return [m for m in entries if m.body and m.body.startswith("Node stats")]

            older_entries = await snapshot(older, oc)
            assert older_entries and all(m.block is None for m in older_entries)
            caller_entries = await snapshot(caller, cc)
            assert caller_entries and caller_entries[0].block == live.block
            replay = chat_flow._render_all_scrollback(caller.db, cc, caller.alice, caller_entries)
            assert "Uptime" in _plain(replay[0].lines)
        finally:
            for node in (older, caller, host):
                await node.close()

    asyncio.run(run())


def chat_flow_snapshot_entries(db, channel, blocks):
    from netbbs.link.realtime_channels import _snapshot_entries
    return _snapshot_entries(db, channel, blocks=blocks)


def test_wire_bounds_for_blocks(tmp_path):
    block = check_block(STATS).to_wire()
    oversized = {"title": "T", "rows": [{"text": "x" * 200}] * 80}
    frame = build_channel_message_frame("c", "Baud.bot", "Baud.bot", "fallback", utc_now_iso(), block=block)
    assert frame.payload["block"] == block
    with pytest.raises(LinkProtocolError):
        build_channel_message_frame("c", "Baud.bot", "Baud.bot", "fallback", utc_now_iso(), block=oversized)
    with pytest.raises(LinkProtocolError):
        build_channel_message_frame("c", "Baud.bot", "Baud.bot", "fallback", utc_now_iso(), block="text")
    build_bot_frame("bot_result", {"request_id": "r", "error": "", "private": {"text": "x", "block": block}})
    with pytest.raises(LinkProtocolError):
        build_bot_frame("bot_result", {"request_id": "r", "error": "", "private": {"text": "x", "block": oversized}})
    entry = {"kind": "message", "author_label": "Baud.bot@n", "author_node_fingerprint": "n",
             "author_user_id": "Baud.bot", "content_id": None, "body": "fallback", "body_truncated": False,
             "created_at": utc_now_iso()}
    build_scrollback_snapshot_frame("c", "r", [{**entry, "block": block}])
    with pytest.raises(LinkProtocolError):
        build_scrollback_snapshot_frame("c", "r", [{**entry, "kind": "action", "block": block}])


def test_a_signed_block_is_kept_by_size_and_an_oversized_one_refused(tmp_path):
    from tests.test_bot_link import Node, connect
    from tests.test_link_realtime_channels import _setup_linked_channel

    async def run():
        host, peer = Node(tmp_path, "signer"), Node(tmp_path, "receiver")
        hc, _ = _setup_linked_channel(host, peer)
        try:
            await connect((host, peer))
            genesis = json.loads(host.db.connection.execute(
                "SELECT link_genesis_json FROM channels WHERE id=?", (hc.id,)).fetchone()[0])

            def signed(block):
                return build_channel_message(
                    signing_identity=host.identity.signing_key, home_node_fingerprint=host.identity.fingerprint,
                    local_user_id="Peer.bot", channel_id=hc.channel_id, body="fallback", created_at=utc_now_iso(),
                    block=block).to_dict()

            malformed = signed({"title": "T", "rows": "not rows"})
            accepted = peer.link_node.handle_events(host.identity.fingerprint, [genesis, malformed])
            assert len(accepted) == 2  # kept, and shown as its fallback
            with pytest.raises(LinkProtocolError, match="byte block"):
                peer.link_node.handle_events(host.identity.fingerprint,
                                             [signed({"title": "T", "rows": [{"text": "x" * 200}] * 80})])
        finally:
            for node in (peer, host):
                await node.close()

    asyncio.run(run())


def test_private_block_debits_one_private_answer(db, sysop, channel):
    bot = _install(db, sysop, channel)
    block = check_block(STATS)
    deliver_private(db, bot.id, channel.name, block.fallback(), "7")
    assert db.connection.execute("SELECT COUNT(*) FROM bot_private_history").fetchone()[0] == 1
    assert get_scrollback(db, channel) == []


def test_a_block_too_big_for_one_frame_falls_back_to_the_plain_line():
    # Review of #1424: a peer's body full of escaped characters beside a
    # near-maximal block fits each field's bound but not one frame; relaying
    # it with a block must not enqueue a frame the writer would die on.
    from netbbs.link.realtime_channels import _frame_with_block
    block = parse_block({"title": "T", "rows": [{"label": "\U0001F4E1" * 24, "text": "\U0001F4E1" * 200}] * 12})
    body = '"' * 3000
    with pytest.raises(LinkProtocolError, match="16 KiB"):
        build_channel_message_frame("c", "Peer.bot", "Peer.bot", body, utc_now_iso(), block=block.to_wire(),
                                    author_node_fingerprint="n" * 32)
    assert _frame_with_block("c", "Peer.bot", "Peer.bot", body, utc_now_iso(), block=block,
                             author_node_fingerprint="n" * 32) is None
    plain = build_channel_message_frame("c", "Peer.bot", "Peer.bot", body, utc_now_iso())
    assert "block" not in plain.payload


def test_a_line_is_a_block_or_an_announcement_never_both(db, sysop, channel):
    # Issue #1380's marker is a SysOp's line, a block a bot's.
    block = check_block(STATS)
    with pytest.raises(LinkProtocolError, match="not both"):
        build_channel_message_frame("c", "Peer.bot", "Peer.bot", block.fallback(), utc_now_iso(),
                                    block=block.to_wire(), announcement=True)
    entry = {"kind": "message", "author_label": "Baud.bot@n", "author_node_fingerprint": "n",
             "author_user_id": "Baud.bot", "content_id": None, "body": "fallback", "body_truncated": False,
             "created_at": utc_now_iso(), "block": block.to_wire(), "announcement": True}
    with pytest.raises(LinkProtocolError, match="not both"):
        build_scrollback_snapshot_frame("c", "r", [entry])
    from netbbs.chat.scrollback import record_message
    with pytest.raises(ValueError, match="not both"):
        record_message(db, channel, kind="message", author_label="Peer.bot", body="x", block=block,
                       announcement=True)
    assert get_scrollback(db, channel) == []


@pytest.mark.parametrize("model", [False, True])
def test_baud_help_block_stays_in_bounds_with_every_grant_and_the_longest_opening(model):
    sent = []
    longest = "x" * 168 + "{name}"  # the longest help opening the editor saves
    Baud({"blocks": True, "queries": ["stats", "seen", "top"], "model_conversation": model, "quotes": True, "polls": True,
          "phrases": {"help": longest}}, sent.append).handle(
        {"type": "message", "request": "r", "caller": {"id": 7, "name": "n" * 32}, "channel": "c", "text": "help"})
    block = check_block(sent[-1]["block"])
    assert len(block.rows) == 12 and len(block.fallback()) <= MAX_FALLBACK
    assert block.rows[-1][1].endswith("Replies are public.") and "private" not in sent[-1]
    # Issue #1372: answered privately, its last row says how to ask
    # privately; with the longest bot name and a Link caller it still fits.
    Baud({"blocks": True, "name": "B" * 24 + ".bot", "queries": ["stats", "seen", "top"], "model_conversation": model, "quotes": True, "polls": True,
          "phrases": {"help": longest}}, sent.append).handle(
        {"type": "message", "request": "r", "caller": {"id": "link:abc:bob", "name": "n" * 32}, "channel": "c",
         "text": "help", "private": True})
    block = check_block(sent[-1]["block"])
    assert sent[-1]["private"] is True and len(block.rows) == 12 and len(block.fallback()) <= MAX_FALLBACK
    assert block.rows[-1][1].endswith(f"Ask privately: /msg {'B' * 24}.bot@Node <text>.")
    assert ("reminders, cancel <id>", "your reminders in every channel; cancel one") in block.rows
    assert ("stats, seen, top", "who is on, uptime; seen <name>: when someone was last on; "
            "top <door>: a door game's top ten") in block.rows


@pytest.mark.parametrize("private", [False, True])
def test_baud_help_line_with_top_fits_whole_in_the_worst_case(private):
    """Issue #1408: the one-line help for a host without blocks, with
    `top <door>; ` added, is never cut: longest opening, 24-character bot
    name, 32-character Link caller, model conversation, every grant and
    switch."""
    from netbbs.bots import botkit
    sent = []
    Baud({"name": "B" * 24 + ".bot", "queries": ["stats", "seen", "top"], "model_conversation": True,
          "quotes": True, "polls": True, "phrases": {"help": "x" * 168 + "{name}"}}, sent.append).handle(
        {"type": "message", "request": "r", "caller": {"id": "link:" + "a" * 64 + ":nib", "name": "n" * 26 + "@Node1"},
         "channel": "c", "text": "help", "private": private})
    text = sent[-1]["text"]
    assert len(text) <= botkit.LINE_LIMIT and "seen <name>; top <door>; roll|coin; " in text
    assert text.endswith(f"Conversation is on; reset clears your context. Ask privately: /msg {'B' * 24}.bot@Node <text>."
                         if private else "Conversation is on; reset clears your context. Replies are public.")
