"""Real content, transactions and subprocesses for audience-safe announcements."""
import asyncio
from pathlib import Path
import sys
import time

import pytest

from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, approve_post, edit_post, tombstone_post
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file, approve_file
from netbbs.bots.registry import BotError, save_bot, publish, get_bot
from netbbs.bots.runtime import BotRuntime, _Worker
from netbbs.bots.sources import source_event, source_head, source_batch
from netbbs.chat.hub import ParticipantId
from netbbs.chat.scrollback import get_scrollback
from netbbs.communities import create_community
from tests.test_bot_runtime import (  # noqa: F401
    db, lane, hub, sysop, alice, channel, install, eventually, next_line,
)
from tests.test_link_file_transfer import origin_db, origin_identity, puller_identity  # noqa: F401


def setup_sources(db, sysop, channel, **kwargs):
    board = create_board(db, "News", creator=sysop, moderated=kwargs.pop("moderated", False))
    area = create_file_area(db, "Files", creator=sysop, moderated=board.moderated)
    bot = install(db, sysop, channel, board_sources=[board.name], file_sources=[area.name],
                  announcement_channel=channel.name, announce_posts=True, announce_files=True, **kwargs)
    return board, area, bot


def reference(db, kind):
    return dict(db.connection.execute("SELECT * FROM bot_source_events WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone())


def test_default_off_explicit_sources_and_public_audience_required(db, sysop, channel):
    bot = install(db, sysop, channel)
    assert not bot.board_sources and not bot.file_sources and not bot.announce_posts and not bot.announce_files
    with pytest.raises(BotError, match="requires its own|requires their own|ability requires"):
        install(db, sysop, channel, bot_id=bot.id, announce_posts=True)
    private = create_board(db, "Private", creator=sysop, min_read_level=10)
    with pytest.raises(BotError, match="readable by everyone"):
        install(db, sysop, channel, bot_id=bot.id, board_sources=[private.name], announcement_channel=channel.name)
    community = create_community(db, "Restricted", creator=sysop, default_min_read_level=10)
    inherited = create_file_area(db, "Inherited", creator=sysop, min_read_level=None, community_id=community.id)
    with pytest.raises(BotError, match="inherited Community"):
        install(db, sysop, channel, bot_id=bot.id, file_sources=[inherited.name], announcement_channel=channel.name)
    assert get_bot(db, bot.id).board_sources == ()


@pytest.mark.parametrize("external", [False, True])
def test_baud_and_external_program_announce_new_content_not_startup_history(db, lane, hub, sysop, alice, channel, external):
    command = [sys.executable, "-u", str(Path(__file__).parent / "fixtures" / "bot_announcement_peer.py")] if external else []
    board, area, bot = setup_sources(db, sysop, channel, command=command)
    create_post(db, board, alice, "old post", "old private body")
    upload_file(db, area, alice, "old.txt", b"old")
    async def run():
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        queue = hub.join(channel.name, ParticipantId("alice", 1))
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            create_post(db, board, alice, "fresh post", "body must not be shared")
            await next_line(queue, "New post in News: fresh post")
            upload_file(db, area, alice, "fresh.txt", b"file bytes must not be shared")
            await next_line(queue, "New file in Files: fresh.txt")
            posts = [m for m in get_scrollback(db, channel) if m.author_kind == "bot"]
            assert len(posts) == 2 and "old" not in str(posts) and "forged" not in str(posts)
            assert "must not be shared" not in str(posts)
            assert not runtime.workers[bot.id].pending
            await runtime.close()
            runtime.start()
            await eventually(lambda: bot.id in runtime.running_ids)
            await asyncio.sleep(.1)
            assert len([m for m in get_scrollback(db, channel) if m.author_kind == "bot"]) == 2
        finally:
            await runtime.close()
    asyncio.run(run())


def test_approval_events_are_transactional_and_edits_do_not_reannounce(db, sysop, alice, channel):
    board, area, bot = setup_sources(db, sysop, channel, moderated=True)
    post = create_post(db, board, alice, "pending title", "body")
    file = upload_file(db, area, alice, "pending.txt", b"content")
    assert source_head(db) == 0
    post = approve_post(db, post, approved_by=sysop)
    approve_file(db, file, approved_by=sysop)
    rows, cursor, _ = source_batch(db, bot.id, 0)
    assert [r["kind"] for r in rows] == ["post", "file"]
    assert source_event(db, bot.id, rows[0])["title"] == "pending title"
    edit = edit_post(db, post, board, subject="edited title", body="new body", edited_by=alice)
    approve_post(db, edit, approved_by=sysop)
    assert source_batch(db, bot.id, cursor)[0] == []
    db.connection.execute("UPDATE files SET status='pending' WHERE file_id=?", (file.file_id,))
    db.connection.execute("UPDATE files SET status='approved' WHERE file_id=?", (file.file_id,))
    db.connection.rollback()
    assert len(source_batch(db, bot.id, 0)[0]) == 2


def test_restricted_or_withdrawn_source_is_refused_at_publication_without_rate_debit(db, sysop, alice, channel):
    board, _, bot = setup_sources(db, sysop, channel)
    post = create_post(db, board, alice, "sensitive now", "body")
    ref = reference(db, "post")
    ref["destination"] = channel.name
    assert source_event(db, bot.id, ref)["title"] == "sensitive now"
    db.connection.execute("UPDATE boards SET min_read_level=255 WHERE id=?", (board.id,))
    db.connection.commit()
    with pytest.raises(BotError, match="readable by everyone"):
        publish(db, bot.id, channel.name, "placeholder", source_reference=ref)
    assert not db.connection.in_transaction and get_scrollback(db, channel) == []
    assert db.connection.execute("SELECT COUNT(*) FROM bot_output_history").fetchone()[0] == 0
    db.connection.execute("UPDATE boards SET min_read_level=0 WHERE id=?", (board.id,))
    db.connection.commit()
    tombstone_post(db, post, board, tombstoned_by=sysop)
    with pytest.raises(BotError, match="no longer public"):
        source_event(db, bot.id, ref)


def test_announcement_tokens_are_owned_expire_and_cannot_follow_changed_destination(db, lane, hub, sysop, alice, channel):
    board, _, bot = setup_sources(db, sysop, channel)
    create_post(db, board, alice, "token post", "body")
    ref = {**reference(db, "post"), "destination": channel.name}
    async def run():
        worker = _Worker(BotRuntime(lane, hub), bot)
        with pytest.raises(BotError, match="live source-event"):
            await worker._action({"action": "announce", "event": "forged"})
        worker.announcements["old"] = (ref, time.monotonic() - 1)
        with pytest.raises(BotError, match="live source-event"):
            await worker._action({"action": "announce", "event": "old"})
        worker.announcements["valid"] = ({**ref, "destination": "old-destination"}, time.monotonic() + 60)
        with pytest.raises(BotError):
            await worker._action({"action": "announce", "event": "valid"})
        assert not get_scrollback(db, channel)
    asyncio.run(run())


def test_source_journal_is_bounded_and_contains_no_titles_or_bodies(db):
    db.connection.executemany("INSERT INTO bot_source_events(kind, source_id, item_id) VALUES ('post', 1, ?)",
                              [(str(i),) for i in range(1100)])
    db.connection.commit()
    assert db.connection.execute("SELECT COUNT(*) FROM bot_source_events").fetchone()[0] == 1024
    assert {r["name"] for r in db.connection.execute("PRAGMA table_info(bot_source_events)")} == {"id", "kind", "source_id", "item_id"}
    rows, cursor, advanced = source_batch(db, 0, 0)
    assert not rows and advanced
    assert not source_batch(db, 0, cursor)[2]


@pytest.mark.parametrize("change", ["age", "name", "hidden", "expired", "grant"])
def test_file_events_recheck_all_public_gates_and_revocation(db, sysop, alice, channel, change):
    _, area, bot = setup_sources(db, sysop, channel)
    file = upload_file(db, area, alice, "public.txt", b"data")
    ref = reference(db, "file")
    assert source_event(db, bot.id, ref)["title"] == "public.txt"
    if change == "age":
        db.connection.execute("UPDATE file_areas SET min_age=18 WHERE id=?", (area.id,))
    elif change == "name":
        db.connection.execute("UPDATE file_areas SET name_requirement='verified' WHERE id=?", (area.id,))
    elif change == "hidden":
        db.connection.execute("UPDATE file_areas SET link_hidden_at='2026-01-01' WHERE id=?", (area.id,))
    elif change == "expired":
        db.connection.execute("UPDATE file_areas SET max_file_age_days=1 WHERE id=?", (area.id,))
        db.connection.execute("UPDATE files SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (file.id,))
    else:
        db.connection.execute("DELETE FROM bot_file_sources WHERE bot_id=?", (bot.id,))
    db.connection.commit()
    with pytest.raises(BotError):
        source_event(db, bot.id, ref)


def test_carried_file_arrival_announces_metadata_once_and_download_is_not_a_new_file(
        db, sysop, channel, origin_db, origin_identity, puller_identity):
    from tests.test_link_file_transfer import _linked_remote_file, _drive_transfer
    from tests.test_link_realtime_channels import _establish_trust
    from netbbs.link.trust import TrustSubject, TrustDimension, TrustState, set_trust_override
    from netbbs.timeutil import utc_now_iso
    _, remote = _linked_remote_file(origin_db, db, origin_identity, puller_identity, content=b"actual transferred bytes")
    _establish_trust(db, origin_identity.fingerprint)
    bot = install(db, sysop, channel, file_sources=["files"], announcement_channel=channel.name)
    ref = reference(db, "file")
    assert source_event(db, bot.id, ref)["title"] == "game.bin"
    head = source_head(db)
    transfer = _drive_transfer(origin_db, db, remote, requester_fingerprint=puller_identity.fingerprint, chunk_size=4096)
    assert transfer.status == "completed" and source_head(db) == head
    assert source_event(db, bot.id, ref)["title"] == "game.bin"
    set_trust_override(db, TrustSubject.node(origin_identity.fingerprint), TrustDimension.IDENTITY_INTEGRITY,
                       TrustState.BLOCKED, reason="blocked origin", now_iso=utc_now_iso())
    with pytest.raises(BotError, match="no longer public"):
        source_event(db, bot.id, ref)


def test_editor_saves_explicit_source_grants_and_separate_baud_abilities(db, lane, sysop, channel, monkeypatch):
    from netbbs.net import bot_flow
    from netbbs.net.admin_flow import _load_chrome
    from tests.test_bot_runtime import FakeSession
    board = create_board(db, "Public", creator=sysop)
    area = create_file_area(db, "Uploads", creator=sysop)
    async def editor(session, lane, **kwargs):
        draft = kwargs["draft"]
        assert not draft["announce_posts"] and not draft["announce_files"]
        draft.update(channels=channel.name, boards=board.name, files=area.name,
                     announcement_channel=channel.name, announce_posts=True, announce_files=True)
        return await kwargs["save"](draft)
    monkeypatch.setattr(bot_flow, "edit_resource_draft", editor)
    async def run():
        bot = await bot_flow.bot_editor(FakeSession(), lane, sysop, await _load_chrome(lane, sysop))
        assert bot.board_sources == (board.name,) and bot.file_sources == (area.name,)
        assert bot.announce_posts and bot.announce_files and bot.announcement_channel == channel.name
        assert not bot.reading_channels and not bot.event_channels
    asyncio.run(run())


def test_real_link_announcements_use_approved_destination_and_signed_chat(tmp_path):
    from tests.test_bot_link import Node, connect, _setup_linked_channel
    from netbbs.link.protocol import build_subscribe_frame
    async def run():
        host, peer = Node(tmp_path, "announcement-host"), Node(tmp_path, "announcement-peer")
        hc, pc = _setup_linked_channel(host, peer)
        board = create_board(host.db, "News", creator=host.sysop)
        bot = save_bot(host.db, actor=host.sysop, name="Baud", channels=[hc.name], defaults=[], enabled=True,
                       linked_channels=[hc.name], board_sources=[board.name], announcement_channel=hc.name,
                       announce_posts=True)
        queue = peer.hub.join(pc.name, ParticipantId("alice", 1))
        try:
            await connect((host, peer))
            await eventually(lambda: bot.id in host.runtime.running_ids)
            session = await peer.direct.ensure_session(host.identity.fingerprint)
            await session.send(build_subscribe_frame(pc.channel_id))
            await eventually(lambda: peer.bots.visible_roster(peer.db, pc))
            create_post(host.db, board, host.alice, "Across Link", "body")
            line = await next_line(queue, "New post in News: Across Link")
            assert line.author_label.startswith("Baud.bot@")
            assert host.db.connection.execute(
                "SELECT link_event_json FROM channel_messages WHERE author_kind='bot' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        finally:
            await peer.close()
            await host.close()
    asyncio.run(run())
