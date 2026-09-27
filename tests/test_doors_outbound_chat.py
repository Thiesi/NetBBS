"""A door speaking in chat channels through its outbound hook (issue #520, slice 2).

What is worth guarding: a door speaks only where a SysOp allowed it and
never in an MRC-bridged channel; a line is one line, with nothing in it
that could drive the reader's terminal; chat has its own hourly budget,
separate from the board one; a channel's moderators can silence a door
there without a SysOp; a line is delivered live, and a rehearsal delivers
nothing.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.chat.channels import create_channel
from netbbs.chat.moderation import ChatModerationError
from netbbs.chat.scrollback import get_scrollback
from netbbs.doors import create_door
from netbbs.doors.outbound import (
    CHAT_LINE_MAX_BYTES,
    OUTBOUND_DIRNAME,
    OutboundError,
    allow_channel,
    allow_target,
    channel_targets,
    disable_outbound,
    door_info_block,
    drain,
    enable_outbound,
    lift_channel_suspension,
    results_dir,
    revoke_channel,
    set_chat_ceiling,
    set_rate_ceiling,
    suspend_channel,
)
from netbbs.moderation import ChannelPermission, grant_permissions
from netbbs.net import chat_flow
from tests.test_chat_flow_moderation import (  # noqa: F401
    FakeSession,
    db,
    history,
    hub,
    lane,
    mailbox,
    presence,
)


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


@pytest.fixture
def mod(db):
    return create_user(db, "carol", password="hunter2", user_level=10)


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def door(db, sysop):
    return create_door(db, "Blacksite", "/bin/true", creator=sysop)


@pytest.fixture
def channel(db, sysop, mod):
    created = create_channel(db, "lobby", creator=sysop)
    grant_permissions(db, mod, object_type="channel", object_id=created.id,
                      permissions=ChannelPermission.MODERATE, granted_by=sysop)
    return created


def _enable(db, door, sysop, channel=None):
    config = enable_outbound(db, door, enabled_by=sysop)
    if channel is not None:
        allow_channel(db, door, channel, allowed_by=sysop)
    return config


_ordinal = iter(range(1_000_000))


def _say(workdir, **payload):
    directory = workdir / OUTBOUND_DIRNAME
    directory.mkdir(exist_ok=True)
    name = f"say{next(_ordinal)}"
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return name


def _receipt(db, door, name):
    found = [json.loads(path.read_text(encoding="utf-8"))
             for path in results_dir(db, door.id).glob(f"*.{name}.result.json")]
    assert len(found) == 1, found
    return found[0]


def _lines(db, channel):
    return [(m.author_label, m.body) for m in get_scrollback(db, channel) if m.kind == "message"]


# -- speaking ---------------------------------------------------------------


def test_a_door_speaks_in_an_allowed_channel_under_its_label(db, door, sysop, channel, tmp_path):
    _enable(db, door, sysop, channel)
    name = _say(tmp_path, channel="lobby", body="Sector 7 has fallen.")
    published = []

    assert drain(db, door, tmp_path, published=published) == (1, 0)

    assert _lines(db, channel) == [("Blacksite.door", "Sector 7 has fallen.")]
    assert _receipt(db, door, name)["status"] == "posted"
    assert _receipt(db, door, name)["channel"] == "lobby"
    assert [(c.name, m.body) for c, m in published] == [("lobby", "Sector 7 has fallen.")], \
        "the caller must be handed the line to deliver live"


def test_a_channel_may_be_named_with_its_hash(db, door, sysop, channel, tmp_path):
    _enable(db, door, sysop, channel)
    _say(tmp_path, channel="#lobby", body="hello")
    assert drain(db, door, tmp_path) == (1, 0)


def test_a_door_cannot_speak_where_it_was_not_allowed(db, door, sysop, channel, tmp_path):
    other = create_channel(db, "offtopic", creator=sysop)
    _enable(db, door, sysop, channel)
    name = _say(tmp_path, channel="offtopic", body="hello")

    assert drain(db, door, tmp_path) == (0, 1)
    assert "not allowlisted" in _receipt(db, door, name)["reason"]
    assert _lines(db, other) == []


def test_a_request_names_a_board_or_a_channel_not_both(db, door, sysop, channel, tmp_path):
    board = create_board(db, "Chronicle", creator=sysop)
    _enable(db, door, sysop, channel)
    allow_target(db, door, board, allowed_by=sysop)
    name = _say(tmp_path, channel="lobby", board="Chronicle", subject="x", body="hello")

    assert drain(db, door, tmp_path) == (0, 1)
    assert "not both" in _receipt(db, door, name)["reason"]


def test_a_board_request_still_posts_beside_chat(db, door, sysop, channel, tmp_path):
    """The board shape is unchanged: no `channel` key, a board post."""
    board = create_board(db, "Chronicle", creator=sysop)
    _enable(db, door, sysop, channel)
    allow_target(db, door, board, allowed_by=sysop)
    _say(tmp_path, subject="Season 1", body="The Chronicle.")
    _say(tmp_path, channel="lobby", body="Season 1 is over.")

    assert drain(db, door, tmp_path) == (2, 0)
    assert _lines(db, channel) == [("Blacksite.door", "Season 1 is over.")]


# -- one line, and a safe one -----------------------------------------------


def test_a_trailing_newline_is_not_a_second_line(db, door, sysop, channel, tmp_path):
    _enable(db, door, sysop, channel)
    _say(tmp_path, channel="lobby", body="The fleet jumps.\n")
    assert drain(db, door, tmp_path) == (1, 0)
    assert _lines(db, channel) == [("Blacksite.door", "The fleet jumps.")]


def test_two_lines_in_one_request_are_refused(db, door, sysop, channel, tmp_path):
    _enable(db, door, sysop, channel)
    name = _say(tmp_path, channel="lobby", body="one\ntwo")
    assert drain(db, door, tmp_path) == (0, 1)
    assert "one line" in _receipt(db, door, name)["reason"]
    assert _lines(db, channel) == []


def test_control_characters_never_reach_a_reader(db, door, sysop, channel, tmp_path):
    """An escape sequence in a door's line would move every reader's cursor."""
    _enable(db, door, sysop, channel)
    _say(tmp_path, channel="lobby", body="\x1b[2Jalert\x07 \x9b31mred")
    assert drain(db, door, tmp_path) == (1, 0)
    assert _lines(db, channel) == [("Blacksite.door", "[2Jalert 31mred")]


def test_a_line_of_only_control_characters_is_refused(db, door, sysop, channel, tmp_path):
    _enable(db, door, sysop, channel)
    name = _say(tmp_path, channel="lobby", body="\x1b\x07")
    assert drain(db, door, tmp_path) == (0, 1)
    assert "non-empty" in _receipt(db, door, name)["reason"]


def test_a_line_longer_than_a_live_frame_is_refused(db, door, sysop, channel, tmp_path):
    _enable(db, door, sysop, channel)
    name = _say(tmp_path, channel="lobby", body="é" * (CHAT_LINE_MAX_BYTES // 2 + 1))
    assert drain(db, door, tmp_path) == (0, 1)
    assert "at most" in _receipt(db, door, name)["reason"]


def test_only_message_lines_are_accepted(db, door, sysop, channel, tmp_path):
    _enable(db, door, sysop, channel)
    name = _say(tmp_path, channel="lobby", kind="action", body="waves")
    assert drain(db, door, tmp_path) == (0, 1)
    assert "'message'" in _receipt(db, door, name)["reason"]


# -- budgets ----------------------------------------------------------------


def test_chat_has_its_own_ceiling(db, door, sysop, channel, tmp_path):
    _enable(db, door, sysop, channel)
    set_chat_ceiling(db, door, 1, changed_by=sysop)
    _say(tmp_path, channel="lobby", body="first")
    second = _say(tmp_path, channel="lobby", body="second")

    assert drain(db, door, tmp_path) == (1, 1)
    assert "1 chat lines per hour" in _receipt(db, door, second)["reason"]


def test_chat_lines_do_not_spend_the_board_budget(db, door, sysop, channel, tmp_path):
    board = create_board(db, "Chronicle", creator=sysop)
    _enable(db, door, sysop, channel)
    allow_target(db, door, board, allowed_by=sysop)
    set_rate_ceiling(db, door, 1, changed_by=sysop)
    for ordinal in range(3):
        _say(tmp_path, channel="lobby", body=f"line {ordinal}")
    _say(tmp_path, subject="Season 1", body="The Chronicle.")

    assert drain(db, door, tmp_path) == (4, 0)


def test_the_chat_ceiling_is_bounded(db, door, sysop):
    _enable(db, door, sysop)
    with pytest.raises(OutboundError):
        set_chat_ceiling(db, door, 0, changed_by=sysop)
    with pytest.raises(OutboundError):
        set_chat_ceiling(db, door, 241, changed_by=sysop)


# -- rehearsal --------------------------------------------------------------


def test_a_rehearsal_says_what_it_would_have_said_and_says_nothing(db, door, sysop, channel, tmp_path):
    _enable(db, door, sysop, channel)
    set_chat_ceiling(db, door, 1, changed_by=sysop)
    first = _say(tmp_path, channel="lobby", body="first")
    second = _say(tmp_path, channel="lobby", body="second")
    published = []

    drain(db, door, tmp_path, rehearsal=True, published=published)

    assert _lines(db, channel) == []
    assert published == []
    assert _receipt(db, door, first) | {"at": None} == {
        "status": "rehearsal", "would": "posted", "channel": "lobby", "request": first, "at": None}
    assert _receipt(db, door, second)["would"] == "rejected", "judged against the session's spend"


# -- where a door may never speak ---------------------------------------------


def test_an_mrc_bridged_channel_can_never_be_allowed(db, door, sysop, channel):
    from netbbs.mrc.settings import set_mrc_room

    set_mrc_room(db, channel, "lobby")
    _enable(db, door, sysop)
    with pytest.raises(OutboundError, match="MRC"):
        allow_channel(db, door, channel, allowed_by=sysop)


def test_a_channel_bridged_after_it_was_allowed_refuses_the_door(db, door, sysop, channel, tmp_path):
    from netbbs.mrc.settings import set_mrc_room

    _enable(db, door, sysop, channel)
    set_mrc_room(db, channel, "lobby")
    name = _say(tmp_path, channel="lobby", body="hello")

    assert drain(db, door, tmp_path) == (0, 1)
    assert "MRC" in _receipt(db, door, name)["reason"]
    assert _lines(db, channel) == []


# -- a channel's moderators -------------------------------------------------


def test_a_moderator_can_mute_a_door_in_their_channel(db, door, sysop, mod, channel, tmp_path):
    _enable(db, door, sysop, channel)
    assert suspend_channel(db, door, channel, duration=None, reason="spam", suspended_by=mod)
    name = _say(tmp_path, channel="lobby", body="hello")

    assert drain(db, door, tmp_path) == (0, 1)
    assert "muted this door" in _receipt(db, door, name)["reason"]

    assert lift_channel_suspension(db, door, channel, lifted_by=mod)
    _say(tmp_path, channel="lobby", body="hello again")
    assert drain(db, door, tmp_path) == (1, 0)


def test_a_timed_mute_ends_on_its_own(db, door, sysop, mod, channel, tmp_path):
    import datetime

    _enable(db, door, sysop, channel)
    suspend_channel(db, door, channel, duration=datetime.timedelta(minutes=5), reason=None, suspended_by=mod)
    db.connection.execute("UPDATE door_outbound_channel_targets SET suspended_until = '2000-01-01T00:00:00.000000Z'")
    db.connection.commit()
    _say(tmp_path, channel="lobby", body="back")

    assert drain(db, door, tmp_path) == (1, 0)


def test_only_a_moderator_can_mute_a_door(db, door, sysop, bob, channel):
    _enable(db, door, sysop, channel)
    with pytest.raises(ChatModerationError):
        suspend_channel(db, door, channel, duration=None, reason=None, suspended_by=bob)


def test_muting_a_door_that_does_not_speak_here_changes_nothing(db, door, sysop, mod, channel):
    _enable(db, door, sysop)
    assert suspend_channel(db, door, channel, duration=None, reason=None, suspended_by=mod) is False


def test_mute_in_chat_silences_the_door_and_says_so(db, lane, hub, presence, mailbox, history,
                                                    door, sysop, mod, channel, tmp_path):
    _enable(db, door, sysop, channel)

    async def scenario():
        session = FakeSession(["/mute blacksite.door 10m too chatty", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history,
                                                    channel, mod), timeout=5)
        return session

    session = asyncio.run(scenario())
    text = "\n".join(session.written)
    assert "Blacksite.door was muted" in text, "the channel is told, under the door's real label"
    [target] = channel_targets(db, door.id)
    assert target.suspended and target.suspension_reason == "too chatty"
    assert any(m.kind == "mute" and m.author_label == "Blacksite.door" for m in get_scrollback(db, channel))


def test_unmute_in_chat_lets_the_door_speak_again(db, lane, hub, presence, mailbox, history,
                                                 door, sysop, mod, channel):
    _enable(db, door, sysop, channel)
    suspend_channel(db, door, channel, duration=None, reason=None, suspended_by=mod)

    async def scenario():
        session = FakeSession(["/unmute Blacksite.door", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history,
                                                    channel, mod), timeout=5)

    asyncio.run(scenario())
    [target] = channel_targets(db, door.id)
    assert not target.suspended


def test_mute_of_a_door_elsewhere_says_it_does_not_speak_here(db, lane, hub, presence, mailbox, history,
                                                             door, sysop, mod, channel):
    _enable(db, door, sysop)

    async def scenario():
        session = FakeSession(["/mute Blacksite.door", "/quit"])
        await asyncio.wait_for(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history,
                                                    channel, mod), timeout=5)
        return session

    assert "does not speak in this channel" in "\n".join(asyncio.run(scenario()).written)


# -- the allowlist ----------------------------------------------------------


def test_a_door_is_told_the_channels_it_may_speak_in(db, door, sysop, channel):
    _enable(db, door, sysop, channel)
    block = door_info_block(db, door.id)
    assert block["channels"] == ["lobby"]
    assert block["chat_lines_per_hour"] == 30


def test_revoking_a_channel_stops_the_door(db, door, sysop, channel, tmp_path):
    _enable(db, door, sysop, channel)
    revoke_channel(db, door, channel, revoked_by=sysop)
    _say(tmp_path, channel="lobby", body="hello")
    assert drain(db, door, tmp_path) == (0, 1)


def test_switching_the_hook_off_releases_its_channels(db, door, sysop, channel):
    _enable(db, door, sysop, channel)
    disable_outbound(db, door, disabled_by=sysop)
    enable_outbound(db, door, enabled_by=sysop)
    assert channel_targets(db, door.id) == []


# -- Link -------------------------------------------------------------------


def test_a_line_in_a_linked_channel_is_queued_for_peers(db, door, sysop, channel, tmp_path):
    from netbbs.link.channels import link_channel
    from netbbs.link.node_identity import bootstrap_node_identity

    identity = bootstrap_node_identity("thisnode")
    link_channel(db, channel, node_identity=identity)
    _enable(db, door, sysop, channel)
    _say(tmp_path, channel="lobby", body="Across the network.")

    assert drain(db, door, tmp_path, node_identity=identity) == (1, 0)
    row = db.connection.execute(
        "SELECT link_event_json FROM channel_messages WHERE kind = 'message'").fetchone()
    event = json.loads(row["link_event_json"])
    assert "Blacksite.door" in json.dumps(event)


# -- upgrade ----------------------------------------------------------------


def test_an_upgraded_node_keeps_its_doors_hooks_and_board_budget(tmp_path, monkeypatch):
    """A node with a door already posting upgrades to: the same label and
    board ceiling, the default chat ceiling, no channels, and every debit it
    had already spent still counted against boards."""
    from netbbs.storage import database as database_module
    from netbbs.storage.database import Database
    from netbbs.storage.migrations import MIGRATIONS
    from netbbs.timeutil import utc_now_iso

    index = next(i for i, m in enumerate(MIGRATIONS) if "door_outbound_channel_targets" in m.description)
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    old = Database(tmp_path / "node.db")
    sysop = create_user(old, "sysop", password="hunter2", user_level=255)
    door = create_door(old, "Blacksite", "/bin/true", creator=sysop)
    now = utc_now_iso()
    old.connection.execute(
        "INSERT INTO door_outbound (door_id, label, posts_per_hour, enabled_by_user_id, created_at) "
        "VALUES (?, 'Blacksite.door', 2, ?, ?)", (door.id, sysop.id, now))
    old.connection.executemany(
        "INSERT INTO door_outbound_history (door_id, created_at) VALUES (?, ?)", [(door.id, now)] * 2)
    old.connection.commit()
    old.close()

    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)
    upgraded = Database(tmp_path / "node.db")
    try:
        from netbbs.doors.outbound import _recent_post_count, outbound_config

        config = outbound_config(upgraded, door.id)
        assert (config.label, config.posts_per_hour, config.chat_lines_per_hour) == ("Blacksite.door", 2, 30)
        assert channel_targets(upgraded, door.id) == []
        assert (_recent_post_count(upgraded, door.id, "board"), _recent_post_count(upgraded, door.id, "chat")) \
            == (2, 0)
    finally:
        upgraded.close()


# -- how a door's line looks ------------------------------------------------


def test_a_door_line_is_marked_and_muted(db, door, sysop, bob, channel, tmp_path):
    from netbbs.rendering import MUTED_COLOR, colored

    _enable(db, door, sysop, channel)
    _say(tmp_path, channel="lobby", body="Sector 7 has fallen.")
    drain(db, door, tmp_path)
    [message] = [m for m in get_scrollback(db, channel) if m.kind == "message"]

    rendered = chat_flow._render_channel_message(db, channel, bob, message)

    assert colored("» <Blacksite.door> Sector 7 has fallen.", fg_color=MUTED_COLOR) in rendered


def test_a_peers_door_line_is_marked_too(db, bob, channel):
    from netbbs.chat.scrollback import ChannelMessage

    message = ChannelMessage(id=-1, channel_id=channel.id, kind="message",
                             author_label="blacksite.door@Elsewhere", author_fingerprint=None,
                             body="hi", created_at="2026-09-26T00:00:00.000000Z")
    assert "» <blacksite.door@Elsewhere>" in chat_flow._render_channel_message(db, channel, bob, message)


def test_a_person_is_never_styled_as_a_door(db, bob, channel):
    from netbbs.chat.scrollback import record_message

    message = record_message(db, channel, kind="message", author_label="bob", body="hi")
    rendered = chat_flow._render_channel_message(db, channel, bob, message)
    assert "» " not in rendered and ">> " not in rendered


# -- live delivery ----------------------------------------------------------


def test_a_running_door_speaks_live(db, lane, sysop, channel, tmp_path, monkeypatch):
    """The point of a chat line is the moment: it reaches the channel while
    the door is still running, not when the player leaves."""
    import sys

    from netbbs.doors import runtime
    from tests.test_doors_runtime import FakeSession as DoorSession
    from tests.test_doors_runtime import _run, _write_script

    monkeypatch.setattr(runtime, "_OUTBOUND_TICK_SECONDS", 0.2)
    script = _write_script(tmp_path, "speaker.py", """
        import json, os, pathlib, sys, time
        info = json.load(open(os.environ["NETBBS_DOOR_INFO"]))
        hook = info["outbound"]
        drop = pathlib.Path(os.environ["NETBBS_DOOR_INFO"]).parent / hook["directory"]
        (drop / "say.part").write_text(json.dumps({"channel": hook["channels"][0], "body": "live!"}))
        (drop / "say.part").replace(drop / "say.json")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for path in pathlib.Path(hook["results"]).glob("*.result.json"):
                if json.loads(path.read_text())["request"] == "say":
                    time.sleep(0.5)
                    sys.exit(0)
            time.sleep(0.1)
        sys.exit(1)
    """)
    door = create_door(db, "Blacksite", sys.executable, args=(str(script),), creator=sysop)
    _enable(db, door, sysop, channel)
    delivered = []

    async def fanout(published):
        delivered.append([(c.name, m.body, door_still_running()) for c, m in published])

    tasks = {}

    def door_still_running():
        return not tasks["run"].done()

    async def scenario():
        tasks["run"] = asyncio.ensure_future(_run(DoorSession(), lane, door, sysop, chat_fanout=fanout))
        return await tasks["run"]

    assert asyncio.run(scenario()).reason == "exited"
    assert delivered == [[("lobby", "live!", True)]], "delivered once, while the door ran"


def test_stopping_the_ticker_waits_for_a_pass_already_under_way(db, lane, door, tmp_path, monkeypatch):
    """A cancelled await does not stop the thread it was waiting on. The
    session's teardown deletes the working directory right after stopping the
    ticker, so a pass still running in a thread would race that deletion --
    and a pass that had just recorded chat lines would never deliver them."""
    import threading
    import time

    from netbbs.doors import outbound, runtime

    started, finished = threading.Event(), threading.Event()

    def slow_has_requests(workdir):
        started.set()
        time.sleep(0.5)
        finished.set()
        return False

    monkeypatch.setattr(outbound, "has_requests", slow_has_requests)

    async def scenario():
        task = asyncio.ensure_future(runtime._drain_while_running(
            lane, door, tmp_path, None, False, interval=0.01))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Read here, not after `asyncio.run`: its shutdown joins every
        # executor thread, which would hide exactly this race.
        return task.cancelled(), finished.is_set()

    cancelled, pass_finished = asyncio.run(scenario())
    assert cancelled, "the ticker still ends as cancelled"
    assert pass_finished, "but only once the pass it was in had finished"


def test_door_flow_fanout_reaches_the_channel_hub(db, hub, channel):
    """What `browse_doors` hands the runtime: the same hub broadcast a
    caller's own line gets."""
    from netbbs.chat.hub import ParticipantId
    from netbbs.chat.scrollback import record_message
    from netbbs.net.door_flow import chat_fanout

    async def scenario():
        participant = ParticipantId("bob", 1)
        queue = hub.join(channel.name, participant)
        message = record_message(db, channel, kind="message", author_label="Blacksite.door", body="hi")
        await chat_fanout(hub, None)([(channel, message)])
        return queue.get_nowait()

    assert asyncio.run(scenario()).body == "hi"
