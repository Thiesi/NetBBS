"""Door events and standings, door API 5, announced through a bot (issue #1408).

What is worth guarding (design §16, issue #1378): a door reports an event
only while its SysOp switched game events on, and only while every caller
may play it; the door supplies references and the host words the line,
naming only callers who still allow it at publication; a guest or a
rehearsal publishes nothing; a bot announces only doors it was granted,
and only what happened while it ran; `top` answers a door's own snapshot
with the same gates. The end-to-end tests run a real door process
(`tests/fixtures/door_events_peer.py`) and a real bot process.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from netbbs.auth.users import create_user, set_user_disabled
from netbbs.bots.queries import run_query
from netbbs.bots.registry import BotError, get_bot, publish
from netbbs.bots.runtime import BotRuntime
from netbbs.bots.sources import door_batch, door_head, source_event
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.doors import create_door
from netbbs.doors.events import (
    EVENT_TYPES,
    announced_event,
    game_results_announced,
    public_doors,
    public_standings,
    set_events_ceiling,
    set_events_enabled,
    set_game_results_announced,
)
from netbbs.doors.outbound import (
    OUTBOUND_DIRNAME,
    OutboundError,
    disable_outbound,
    door_info_block,
    drain,
    enable_outbound,
    results_dir,
)
from netbbs.guest import set_guest_user
from tests.test_bot_runtime import (  # noqa: F401
    db, lane, hub, sysop, alice, channel, presence, install, eventually, next_line,
)

PEER = Path(__file__).parent / "fixtures" / "door_events_peer.py"


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


@pytest.fixture
def door(db, sysop):
    return create_door(db, "Voidrunner", "/bin/true", creator=sysop)


def _events_on(db, door, sysop, **kwargs):
    enable_outbound(db, door, enabled_by=sysop)
    set_events_enabled(db, door, True, changed_by=sysop)
    if "per_hour" in kwargs:
        set_events_ceiling(db, door, kwargs["per_hour"], changed_by=sysop)


_ordinal = iter(range(1_000_000))


def _drop(workdir, payload):
    directory = workdir / OUTBOUND_DIRNAME
    directory.mkdir(exist_ok=True)
    name = f"req{next(_ordinal):07d}"  # Zero-padded: a drain answers in name order (#1429).
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return name


def _receipt(db, door, name):
    found = [json.loads(p.read_text(encoding="utf-8")) for p in results_dir(db, door.id).glob(f"*.{name}.result.json")]
    assert len(found) == 1, found
    return found[0]


def _event(db, door, tmp_path, rehearsal=False, **event):
    name = _drop(tmp_path, {"event": event})
    drain(db, door, tmp_path, rehearsal=rehearsal)
    return _receipt(db, door, name)


def _journal(db):
    return [dict(r) for r in db.connection.execute("SELECT * FROM door_events ORDER BY id")]


def _last_text(db, door):
    return announced_event(db, door, _journal(db)[-1]["id"]).text


# -- the door's side -----------------------------------------------------------


def test_events_are_off_by_default_and_advertised_only_when_on(db, door, sysop, alice, tmp_path):
    enable_outbound(db, door, enabled_by=sysop)
    assert "events" not in door_info_block(db, door.id)
    receipt = _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Merchant Captain")
    assert receipt["status"] == "rejected" and "not switched on" in receipt["reason"]
    assert _journal(db) == []

    set_events_enabled(db, door, True, changed_by=sysop)
    block = door_info_block(db, door.id)["events"]
    assert block["types"] == list(EVENT_TYPES) and block["events_per_hour"] == 12
    assert _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Merchant Captain")["status"] == "accepted"


def test_events_need_the_outbound_hook(db, door, sysop):
    with pytest.raises(OutboundError, match="not switched on"):
        set_events_enabled(db, door, True, changed_by=sysop)


@pytest.mark.parametrize("kind, title, expected", [
    ("rank", "Merchant Captain", "alice reached Merchant Captain in Voidrunner."),
    ("achievement", "Untouched", 'alice earned the achievement "Untouched" in Voidrunner.'),
    ("capture", "the Kowloon exchange", "alice captured the Kowloon exchange in Voidrunner."),
    ("perfect", "Arcade Classics", "alice answered every question in a Voidrunner round (Arcade Classics)."),
    ("perfect", None, "alice answered every question in a Voidrunner round."),
])
def test_the_host_words_each_event_type(db, door, sysop, alice, tmp_path, kind, title, expected):
    _events_on(db, door, sysop)
    event = {"type": kind, "user_id": alice.id}
    if title is not None:
        event["title"] = title
    assert _event(db, door, tmp_path, **event)["status"] == "accepted"
    assert _last_text(db, door) == expected


def test_a_new_season_names_nobody_and_is_announced(db, door, sysop, alice, tmp_path):
    """Issue #1561: the game's own news, so no consent check can drop it."""
    _events_on(db, door, sysop)
    receipt = _event(db, door, tmp_path, type="season", title="7 missions, through November")
    assert receipt["status"] == "accepted" and receipt["named"] == 0
    assert _last_text(db, door) == "A new Voidrunner season begins: 7 missions, through November."
    assert db.connection.execute("SELECT COUNT(*) FROM door_event_subjects").fetchone()[0] == 0


@pytest.mark.parametrize("event, reason", [
    ({"type": "season", "user_id": 1, "title": "x"}, "names no caller"),
    ({"type": "season", "user_ids": [1], "title": "x"}, "names no caller"),
    ({"type": "season"}, "must be a string"),
    ({"type": "season", "title": " "}, "must not be empty"),
])
def test_a_season_event_needs_a_title_and_no_caller(db, door, sysop, alice, tmp_path, event, reason):
    _events_on(db, door, sysop)
    receipt = _event(db, door, tmp_path, **event)
    assert receipt["status"] == "rejected" and reason in receipt["reason"]


def test_a_season_counts_against_the_hourly_ceiling(db, door, sysop, alice, tmp_path):
    _events_on(db, door, sysop, per_hour=1)
    assert _event(db, door, tmp_path, type="season", title="Season 1")["status"] == "accepted"
    receipt = _event(db, door, tmp_path, type="season", title="Season 2")
    assert receipt["status"] == "rejected" and "rate limit" in receipt["reason"]


def test_a_bot_receives_a_season_with_no_subjects(db, sysop, channel, door, tmp_path):
    bot = _bot(db, sysop, channel, door)
    _events_on(db, door, sysop)
    _event(db, door, tmp_path, type="season", title="Season 2")
    event = source_event(db, bot.id, door_batch(db, bot.id, 0)[0][0])
    assert event["kind"] == "season" and event["subjects"] == []
    assert event["text"] == "A new Voidrunner season begins: Season 2."
    # The door's audience gate still applies.
    db.connection.execute("UPDATE doors SET min_play_level = 20 WHERE id = ?", (door.id,))
    db.connection.commit()
    with pytest.raises(BotError, match="no longer announced"):
        source_event(db, bot.id, door_batch(db, bot.id, 0)[0][0])


def test_a_season_close_names_only_medallists_who_allow_it(db, door, sysop, alice, bob, tmp_path):
    carol = create_user(db, "carol", password="hunter2", user_level=10)
    _events_on(db, door, sysop)
    set_game_results_announced(db, bob, False)
    receipt = _event(db, door, tmp_path, type="medal", user_ids=[alice.id, bob.id, carol.id], title="Season 4")
    assert receipt["status"] == "accepted" and receipt["named"] == 2
    # Bronze stays bronze when silver is left out; bob is not even journaled.
    assert _last_text(db, door) == "Season 4 of Voidrunner closed: gold alice, bronze carol."
    subjects = [r[0] for r in db.connection.execute("SELECT user_id FROM door_event_subjects")]
    assert bob.id not in subjects
    # Consent is checked again at publication.
    set_game_results_announced(db, carol, False)
    assert _last_text(db, door) == "Season 4 of Voidrunner closed: gold alice."


def test_an_event_naming_nobody_announceable_is_dropped_not_debited(db, door, sysop, alice, tmp_path):
    _events_on(db, door, sysop, per_hour=1)
    assert game_results_announced(db, alice)
    set_game_results_announced(db, alice, False)
    receipt = _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Void Baron")
    assert receipt["status"] == "dropped" and _journal(db) == []
    set_game_results_announced(db, alice, True)
    assert _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Void Baron")["status"] == "accepted"


def test_the_guest_account_is_never_announced(db, door, sysop, tmp_path):
    guest = create_user(db, "guest", password="hunter2", user_level=10)
    set_guest_user(db, guest)
    _events_on(db, door, sysop)
    assert _event(db, door, tmp_path, type="rank", user_id=guest.id, title="Void Baron")["status"] == "dropped"


@pytest.mark.parametrize("event, reason", [
    ({"type": "boast", "user_id": 1, "title": "x"}, "'type' must be one of"),
    ({"type": "rank", "user_id": "1", "title": "x"}, "needs 'user_id'"),
    ({"type": "rank", "user_id": True, "title": "x"}, "needs 'user_id'"),
    ({"type": "rank", "user_id": 1, "title": ""}, "must not be empty"),
    ({"type": "rank", "user_id": 1, "title": "x" * 81}, "at most 80"),
    ({"type": "medal", "user_ids": [], "title": "S1"}, "1-3 different"),
    ({"type": "medal", "user_ids": [1, 1], "title": "S1"}, "1-3 different"),
    ({"type": "rank", "user_id": 999, "title": "x"}, "no account has user_id 999"),
])
def test_malformed_events_are_refused_with_a_reason(db, door, sysop, alice, tmp_path, event, reason):
    _events_on(db, door, sysop)
    receipt = _event(db, door, tmp_path, **event)
    assert receipt["status"] == "rejected" and reason in receipt["reason"]


def test_a_title_is_one_plain_line(db, door, sysop, alice, tmp_path):
    _events_on(db, door, sysop)
    _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Void\x1b[31m\n Baron")
    assert _journal(db)[-1]["title"] == "Void [31m Baron"


def test_an_oversized_event_request_is_refused(db, door, sysop, alice, tmp_path):
    _events_on(db, door, sysop)
    name = _drop(tmp_path, {"event": {"type": "rank", "user_id": alice.id, "title": "x", "pad": "y" * 3000}})
    drain(db, door, tmp_path)
    assert "at most 2048 bytes" in _receipt(db, door, name)["reason"]


def test_events_have_their_own_hourly_ceiling(db, door, sysop, alice, tmp_path):
    _events_on(db, door, sysop, per_hour=2)
    for _ in range(2):
        assert _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Rookie")["status"] == "accepted"
    receipt = _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Rookie")
    assert receipt["status"] == "rejected" and "2 events per hour" in receipt["reason"]
    assert len(_journal(db)) == 2


def test_a_rehearsal_journals_nothing_and_says_what_would_happen(db, door, sysop, alice, tmp_path):
    _events_on(db, door, sysop)
    receipt = _event(db, door, tmp_path, rehearsal=True, type="rank", user_id=alice.id, title="Rookie")
    assert receipt["status"] == "rehearsal" and receipt["would"] == "accepted"
    name = _drop(tmp_path, {"standings": {"title": "Hall of Fame", "rows": [{"user_id": alice.id, "value": "1"}]}})
    drain(db, door, tmp_path, rehearsal=True)
    assert _receipt(db, door, name)["would"] == "accepted"
    assert _journal(db) == [] and public_standings(db, door) is None


def test_a_gated_door_reports_nothing(db, door, sysop, alice, tmp_path):
    _events_on(db, door, sysop)
    db.connection.execute("UPDATE doors SET min_play_level = 20 WHERE id = ?", (door.id,))
    db.connection.commit()
    receipt = _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Rookie")
    assert receipt["status"] == "rejected" and "level 20" in receipt["reason"]
    assert _journal(db) == []


def test_a_request_is_one_thing(db, door, sysop, alice, tmp_path):
    _events_on(db, door, sysop)
    name = _drop(tmp_path, {"event": {"type": "rank", "user_id": alice.id, "title": "x"}, "channel": "lobby"})
    drain(db, door, tmp_path)
    assert "one of" in _receipt(db, door, name)["reason"]


def test_standings_replace_the_last_snapshot_and_are_validated(db, door, sysop, alice, bob, tmp_path):
    _events_on(db, door, sysop)
    rows = [{"user_id": alice.id, "value": "182,400 cr", "label": "Legend of the Frontier"},
            {"user_id": bob.id, "value": "96,150 cr"}]
    name = _drop(tmp_path, {"standings": {"title": "Hall of Fame", "rows": rows}})
    drain(db, door, tmp_path)
    assert _receipt(db, door, name)["status"] == "accepted"
    title, shown, _ = public_standings(db, door)
    assert title == "Hall of Fame" and [(r.rank, r.name, r.value, r.label) for r in shown] == [
        (1, "alice", "182,400 cr", "Legend of the Frontier"), (2, "bob", "96,150 cr", "")]
    _drop(tmp_path, {"standings": {"title": "Season 5", "rows": rows[1:]}})
    drain(db, door, tmp_path)
    assert public_standings(db, door)[0] == "Season 5" and len(public_standings(db, door)[1]) == 1
    for bad in ([{"user_id": alice.id, "value": "1"}] * 2, [{"user_id": alice.id, "value": "1"}] * 11,
                [{"user_id": alice.id}], [{"user_id": "x", "value": "1"}]):
        name = _drop(tmp_path, {"standings": {"title": "T", "rows": bad}})
        drain(db, door, tmp_path)
        assert _receipt(db, door, name)["status"] == "rejected"
    assert public_standings(db, door)[0] == "Season 5"


def test_switching_the_hook_off_releases_events_and_standings(db, door, sysop, alice, tmp_path):
    _events_on(db, door, sysop)
    _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Rookie")
    _drop(tmp_path, {"standings": {"title": "Hall of Fame", "rows": [{"user_id": alice.id, "value": "1"}]}})
    drain(db, door, tmp_path)
    disable_outbound(db, door, disabled_by=sysop)
    assert _journal(db) == [] and public_standings(db, door) is None


def test_the_journal_is_bounded(db, door, sysop, alice):
    for index in range(1030):
        db.connection.execute("INSERT INTO door_events (door_id, type, title, created_at) VALUES (?, 'rank', ?, '')",
                              (door.id, str(index)))
    db.connection.commit()
    assert db.connection.execute("SELECT COUNT(*) FROM door_events").fetchone()[0] == 1024


# -- the bot's side --------------------------------------------------------------


def _bot(db, sysop, channel, door, **kwargs):
    return install(db, sysop, channel, door_sources=[door.name], announcement_channel=channel.name,
                   announce_doors=True, **kwargs)


def test_door_grants_need_a_public_door_and_count_toward_the_cap(db, sysop, channel, door):
    bot = install(db, sysop, channel)
    assert not bot.door_sources and not bot.announce_doors
    with pytest.raises(BotError, match="ability requires"):
        install(db, sysop, channel, bot_id=bot.id, announce_doors=True)
    gated = create_door(db, "Members", "/bin/true", min_play_level=20, creator=sysop)
    assert [d.name for d in public_doors(db)] == ["Voidrunner"]
    with pytest.raises(BotError, match="minimum play level 0"):
        install(db, sysop, channel, bot_id=bot.id, door_sources=[gated.name], announcement_channel=channel.name)
    from netbbs.boards.boards import create_board
    boards = [create_board(db, f"B{i}", creator=sysop).name for i in range(32)]
    with pytest.raises(BotError, match="32 board, file and door"):
        install(db, sysop, channel, bot_id=bot.id, board_sources=boards, door_sources=[door.name],
                announcement_channel=channel.name)
    bot = _bot(db, sysop, channel, door, bot_id=bot.id)
    assert get_bot(db, bot.id).door_sources == ("Voidrunner",) and bot.announce_doors


def test_door_batch_reads_only_granted_doors_from_its_own_cursor(db, sysop, alice, channel, door, tmp_path):
    other = create_door(db, "War Dialer", "/bin/true", creator=sysop)
    bot = _bot(db, sysop, channel, door)
    _events_on(db, door, sysop)
    _events_on(db, other, sysop)
    head = door_head(db)
    _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Rookie")
    _event(db, other, tmp_path, type="rank", user_id=alice.id, title="Elite")
    rows, cursor, advanced = door_batch(db, bot.id, head)
    assert [r["source_id"] for r in rows] == [door.id] and not advanced
    assert door_batch(db, bot.id, cursor)[0] == []
    event = source_event(db, bot.id, rows[0])
    assert event["type"] == "door" and event["kind"] == "rank" and event["subjects"] == ["alice"]
    assert event["text"] == "alice reached Rookie in Voidrunner."


def test_publication_rechecks_consent_the_door_gate_and_the_switch(db, sysop, alice, channel, door, tmp_path):
    bot = _bot(db, sysop, channel, door)
    _events_on(db, door, sysop)
    _event(db, door, tmp_path, type="rank", user_id=alice.id, title="Rookie")
    reference = door_batch(db, bot.id, 0)[0][0]

    set_game_results_announced(db, alice, False)
    with pytest.raises(BotError, match="names no caller"):
        publish(db, bot.id, channel.name, "x", source_reference=reference)
    set_game_results_announced(db, alice, True)
    disabled = set_user_disabled(db, alice, True, changed_by=sysop)
    with pytest.raises(BotError, match="names no caller"):
        source_event(db, bot.id, reference)
    set_user_disabled(db, disabled, False, changed_by=sysop)
    set_events_enabled(db, door, False, changed_by=sysop)
    with pytest.raises(BotError, match="no longer announced"):
        source_event(db, bot.id, reference)
    set_events_enabled(db, door, True, changed_by=sysop)
    db.connection.execute("UPDATE doors SET min_play_level = 20 WHERE id = ?", (door.id,))
    db.connection.commit()
    with pytest.raises(BotError, match="no longer announced"):
        source_event(db, bot.id, reference)
    db.connection.execute("UPDATE doors SET min_play_level = 0 WHERE id = ?", (door.id,))
    db.connection.commit()
    _channel, message = publish(db, bot.id, channel.name, "forged text", source_reference=reference)
    assert message.body == "alice reached Rookie in Voidrunner."
    db.connection.execute("UPDATE users SET username = 'alicia' WHERE id = ?", (alice.id,))
    db.connection.commit()
    assert source_event(db, bot.id, reference)["text"] == "alicia reached Rookie in Voidrunner."


def test_top_answers_the_snapshot_with_the_gates_applied(db, sysop, alice, bob, channel, door, tmp_path):
    bot = install(db, sysop, channel, queries=["top"])
    caller = {"id": alice.id, "name": alice.username}

    def top(target):
        return run_query(db, bot.id, channel.name, "top", target, caller=caller, online=set(), uptime_seconds=0)

    assert top("")["text"] == "No door has published standings yet."
    _events_on(db, door, sysop)
    rows = [{"user_id": bob.id, "value": "182,400 cr", "label": "Void Baron"},
            {"user_id": alice.id, "value": "96,150 cr"}]
    _drop(tmp_path, {"standings": {"title": "Hall of Fame", "rows": rows}})
    drain(db, door, tmp_path)
    assert top("")["doors"] == ["Voidrunner"]
    result = top("void")
    assert result["available"] and result["title"] == "Hall of Fame"
    assert result["text"] == "Voidrunner Hall of Fame: 1 bob 182,400 cr; 2 alice 96,150 cr."
    set_game_results_announced(db, bob, False)
    assert [(r["rank"], r["name"]) for r in top("Voidrunner")["rows"]] == [(1, "alice")]
    db.connection.execute("UPDATE doors SET min_play_level = 20 WHERE id = ?", (door.id,))
    db.connection.commit()
    assert not top("Voidrunner")["available"]
    install(db, sysop, channel, bot_id=bot.id, queries=[])
    with pytest.raises(BotError, match="not granted"):
        top("Voidrunner")


# -- end to end: a real door process, a real bot process --------------------------


def test_a_real_door_reports_and_baud_announces_it(db, lane, hub, presence, sysop, alice, bob, channel):
    from tests.test_doors_runtime import FakeSession, _run

    door = create_door(db, "Voidrunner", sys.executable, args=(str(PEER), str(bob.id)), creator=sysop)
    _events_on(db, door, sysop)
    bot = _bot(db, sysop, channel, door, queries=["top"])

    async def run():
        runtime = BotRuntime(lane, hub, presence=presence, poll_seconds=.03)
        pid = ParticipantId(bob.username, 1)
        queue = hub.join(channel.name, pid)
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            assert (await _run(FakeSession(), lane, door, alice)).reason == "exited"
            # The drain answers requests in name order, so the lines may
            # come in any order.
            expected = {"alice reached Merchant Captain in Voidrunner.",
                        'alice earned the achievement "Untouched" in Voidrunner.',
                        "alice captured the Kowloon exchange in Voidrunner.",
                        "alice answered every question in a Voidrunner round (Arcade Classics).",
                        "Season 4 of Voidrunner closed: gold alice, silver bob."}
            async with asyncio.timeout(30):
                while expected:
                    expected.discard(getattr(await queue.get(), "body", None))
            presence.enter(bob.username)
            message = record_message(db, channel, kind="message", author_label=bob.username, body="!top voidrunner")
            assert await runtime.addressed(channel, bob, message, pid) is None
            await next_line(queue, "Hall of Fame")
        finally:
            await runtime.close()

    asyncio.run(run())
    bot_lines = [m.body for m in get_scrollback(db, channel) if m.author_kind == "bot"]
    assert len([line for line in bot_lines if "Voidrunner" in line]) >= 5


def test_a_rehearsal_of_the_real_door_announces_nothing(db, lane, sysop, alice, bob, channel):
    from tests.test_doors_runtime import FakeSession, _run

    door = create_door(db, "Voidrunner", sys.executable, args=(str(PEER), str(bob.id)), creator=sysop)
    _events_on(db, door, sysop)
    assert asyncio.run(_run(FakeSession(), lane, door, alice, rehearsal=True)).reason == "exited"
    verdicts = {json.loads(p.read_text())["request"]: json.loads(p.read_text())["would"]
                for p in results_dir(db, door.id).glob("*.result.json")}
    assert verdicts == dict.fromkeys(("rank", "achievement", "capture", "perfect", "medal", "standings"), "accepted")
    assert _journal(db) == [] and public_standings(db, door) is None


_TOP = {"available": True, "door": "Voidrunner", "title": "Hall of Fame", "updated_at": "",
        "rows": [{"rank": 1, "name": "Vex", "value": "182,400 cr", "label": "Legend of the Frontier"},
                 {"rank": 2, "name": "Nib", "value": "96,150 cr", "label": ""}],
        "text": "Voidrunner Hall of Fame: 1 Vex 182,400 cr; 2 Nib 96,150 cr."}


@pytest.mark.parametrize("blocks", [True, False])
def test_baud_answers_top_as_a_block_where_the_host_takes_blocks(blocks):
    """§16 #1378 Decision 4: `!top` is a titled block; a host without
    blocks (#1373) gets the one-line text."""
    from netbbs.bots.baud import Baud
    from netbbs.bots.registry import check_block

    sent = []
    baud = Baud({"blocks": blocks, "queries": ["top"]}, sent.append)
    baud.handle({"type": "message", "request": "r", "caller": {"id": 7, "name": "alice"}, "channel": "c",
                 "text": "top voidrunner"})
    query = sent[-1]
    assert query["action"] == "query" and query["query"] == "top" and query["target"] == "voidrunner"
    baud.handle({"type": "result", "id": query["id"], "ok": True, "value": _TOP})
    answer = sent[-1]
    assert answer["action"] == "reply"
    if blocks:
        block = check_block(answer["block"])
        assert block.title == "Voidrunner · Hall of Fame"
        assert block.rows == (("1 Vex", "182,400 cr · Legend of the Frontier"), ("2 Nib", "96,150 cr"))
        assert "text" not in answer
    else:
        assert "block" not in answer
        assert answer["text"] == "alice: Voidrunner Hall of Fame: 1 Vex 182,400 cr; 2 Nib 96,150 cr."


def test_baud_answers_top_with_no_rows_as_a_line_even_with_blocks():
    from netbbs.bots.baud import Baud

    sent = []
    baud = Baud({"blocks": True, "queries": ["top"]}, sent.append)
    baud.handle({"type": "message", "request": "r", "caller": {"id": 7, "name": "alice"}, "channel": "c",
                 "text": "top"})
    baud.handle({"type": "result", "id": sent[-1]["id"], "ok": True,
                 "value": {"available": False, "doors": [], "text": "No door has published standings yet."}})
    assert sent[-1]["text"] == "alice: No door has published standings yet."


def test_the_season_migration_keeps_events_subjects_and_ids(tmp_path, monkeypatch):
    """Issue #1561: widening the type CHECK rebuilds `door_events`, whose
    subjects cascade from it and whose ids feed every bot's cursor."""
    from netbbs.storage import database as database_module
    from netbbs.storage.database import Database
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if "Issue #1561" in m.description)
    db_path = tmp_path / "node.db"
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    old = Database(db_path)
    sysop = create_user(old, "sysop", password="hunter2", user_level=255)
    alice = create_user(old, "alice", password="hunter2", user_level=10)
    door = create_door(old, "Voidrunner", "/bin/true", creator=sysop)
    connection = old.connection
    for title in ("Rookie", "Ace", "Gone"):
        cursor = connection.execute(
            "INSERT INTO door_events (door_id, type, title, created_at) VALUES (?, 'rank', ?, 'x')",
            (door.id, title))
        connection.execute("INSERT INTO door_event_subjects (event_id, position, user_id) VALUES (?, 0, ?)",
                           (cursor.lastrowid, alice.id))
    # The newest event is gone, as `forget_door` leaves it: its id stays spent.
    connection.execute("DELETE FROM door_events WHERE title = 'Gone'")
    connection.commit()
    old.connection.close()

    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS)
    db = Database(db_path)
    rows = [tuple(r) for r in db.connection.execute("SELECT id, title FROM door_events ORDER BY id")]
    assert rows == [(1, "Rookie"), (2, "Ace")]
    assert [tuple(r) for r in db.connection.execute(
        "SELECT event_id, user_id FROM door_event_subjects ORDER BY event_id")] == [(1, alice.id), (2, alice.id)]
    cursor = db.connection.execute(
        "INSERT INTO door_events (door_id, type, title, created_at) VALUES (?, 'season', 'Season 1', 'x')",
        (door.id,))
    assert cursor.lastrowid == 4, "id 3 was handed out before the upgrade"
    # The subjects still cascade, and the journal is still bounded.
    db.connection.execute("DELETE FROM door_events WHERE id = 1")
    assert db.connection.execute("SELECT COUNT(*) FROM door_event_subjects").fetchone()[0] == 1
    triggers = {r[0] for r in db.connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert "door_events_bound" in triggers
    assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
