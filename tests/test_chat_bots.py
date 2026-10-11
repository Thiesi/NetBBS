"""Bot identity and local channel policy at real storage and chat boundaries."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace

import pytest

from netbbs.auth.users import AuthError, _validate_username, create_user
from netbbs.chat.bots import BotPolicyError, bot_origin, channel_bot_policy, set_channel_bot_policy
from netbbs.chat.channels import create_channel
from netbbs.chat.scrollback import ChannelMessage, get_scrollback, record_message
from netbbs.link.channels import link_channel, materialize_carried_channel_message, queue_channel_message_if_linked
from netbbs.link.events import build_channel_message
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.net import chat_flow
from netbbs.search import search_channel_messages
from netbbs.storage.database import Database
from netbbs.moderation import ChannelPermission, grant_permissions
from tests.legacy_schema import insert_user_on_old_schema
from tests.test_chat_flow_moderation import FakeSession, db, lane, hub, presence, mailbox, history  # noqa: F401


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def channel(db, sysop):
    return create_channel(db, "lobby", creator=sysop)


def _line(channel, label="Baud.bot", *, kind="message", **kwargs):
    return ChannelMessage(
        id=-1, channel_id=channel.id, kind=kind, author_label=label, author_fingerprint=None,
        body="botword", created_at="2026-10-11T12:00:00Z", **kwargs,
    )


@pytest.mark.parametrize("name", ["Baud.bot", "BAUD.BOT", "someone.Bot"])
def test_bot_suffix_is_reserved_for_new_accounts(name):
    with pytest.raises(AuthError, match="reserved for chat bots"):
        _validate_username(name)


@pytest.mark.parametrize("label", ["Baud.bot", "Baud.BOT@Elsewhere", 'Baud.bot@"Cats @ Night"'])
@pytest.mark.parametrize("kind", ["message", "action", "join", "leave"])
def test_live_and_replay_mark_bot_authors(db, alice, channel, label, kind):
    line = _line(channel, label, kind=kind)
    live = chat_flow._render_channel_message(db, channel, alice, line)
    replay = chat_flow._render_scrollback_message(db, channel, alice, line)
    assert "[BOT]" in live
    assert live == replay


def test_local_bot_kind_survives_database_reopen(db, channel):
    line = record_message(db, channel, kind="message", author_label="Baud.bot", body="botword")
    assert line.author_kind == "bot"
    with Database(db.path) as reopened:
        [stored] = get_scrollback(reopened, channel)
        assert stored.author_kind == "bot"
        assert bot_origin(reopened, stored) == "local"


def test_legacy_local_account_is_a_person_even_under_none_policy(db, sysop, channel):
    legacy = insert_user_on_old_schema(db, "old.bot")
    set_channel_bot_policy(db, channel, "none", changed_by=sysop)
    line = record_message(db, channel, kind="message", author_label=legacy.username, body="humanword")
    assert line.author_kind == "user"
    rendered = chat_flow._render_channel_message(db, channel, legacy, line)
    assert "humanword" in rendered and "[BOT]" not in rendered


@pytest.mark.parametrize("policy", ["all", "local", "none"])
def test_legacy_account_with_signing_key_remains_human_through_chat(
    db, lane, hub, presence, mailbox, history, channel, sysop, policy,
):
    from nacl.signing import SigningKey
    from netbbs.auth.users import add_ssh_key

    legacy = insert_user_on_old_schema(db, "Chatty.bot")
    legacy = add_ssh_key(db, legacy, SigningKey.generate().verify_key, label="laptop", changed_by=sysop)
    assert legacy.fingerprint is not None
    set_channel_bot_policy(db, channel, policy, changed_by=sysop)

    text = asyncio.run(_run_chat(lane, hub, presence, mailbox, history, channel, legacy,
                                ["humanword", "/me waves", "/quit"]))
    messages = get_scrollback(db, channel)
    assert {message.kind for message in messages} >= {"message", "action", "join", "leave"}
    assert all(message.author_fingerprint == legacy.fingerprint for message in messages)
    assert all(message.author_kind == "user" for message in messages)
    assert "humanword" in text and "waves" in text and "[BOT]" not in text
    replay = chat_flow._render_all_scrollback(db, channel, legacy, messages)
    assert len(replay) == len(messages)
    assert all("[BOT]" not in line for line in replay)
    assert len(search_channel_messages(db, legacy, "humanword", visible_channels=[channel])) == 1


def test_an_mrc_handle_with_the_suffix_is_not_a_netbbs_bot(db, alice, channel):
    line = _line(channel, "Baud.bot@Other (MRC)", external_source="mrc")
    assert bot_origin(db, line) is None
    rendered = chat_flow._render_channel_message(db, channel, alice, line)
    assert "[MRC]" in rendered and "[BOT]" not in rendered


@pytest.mark.parametrize("policy,local,remote", [("all", True, True), ("local", True, False), ("none", False, False)])
def test_policy_filters_live_and_replay_without_deleting_history(db, sysop, alice, channel, policy, local, remote):
    a = record_message(db, channel, kind="message", author_label="Baud.bot", body="localbot")
    b = record_message(db, channel, kind="message", author_label="Baud.bot@Remote", body="remotebot")
    human = record_message(db, channel, kind="message", author_label=alice.username, body="humanword")
    # Keep the original Channel object to prove mid-session changes are read fresh.
    set_channel_bot_policy(db, channel, policy, changed_by=sysop)
    assert bool(chat_flow._render_channel_message(db, channel, alice, a)) is local
    assert bool(chat_flow._render_channel_message(db, channel, alice, b)) is remote
    replay = chat_flow._render_all_scrollback(db, channel, alice, [a, b, human])
    assert len(replay) == int(local) + int(remote) + 1
    assert all(replay)
    assert len(get_scrollback(db, channel)) == 3


def test_policy_also_filters_search_and_can_be_reversed(db, sysop, alice, channel):
    record_message(db, channel, kind="message", author_label="Baud.bot", body="botword")
    assert len(search_channel_messages(db, alice, "botword", visible_channels=[channel])) == 1
    set_channel_bot_policy(db, channel, "none", changed_by=sysop)
    assert search_channel_messages(db, alice, "botword", visible_channels=[channel]) == []
    set_channel_bot_policy(db, channel, "all", changed_by=sysop)
    assert len(search_channel_messages(db, alice, "botword", visible_channels=[channel])) == 1


def test_policy_requires_channel_moderation_and_is_audited(db, sysop, alice, channel):
    with pytest.raises(BotPolicyError, match="moderation permission"):
        set_channel_bot_policy(db, channel, "none", changed_by=alice)
    grant_permissions(db, alice, object_type="channel", object_id=channel.id,
                      permissions=ChannelPermission.MODERATE, granted_by=sysop)
    set_channel_bot_policy(db, channel, "local", changed_by=alice)
    set_channel_bot_policy(db, channel, "local", changed_by=alice)
    rows = db.connection.execute("SELECT * FROM moderation_log WHERE action = 'channel_bot_policy'").fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == alice.id
    assert rows[0]["object_id"] == channel.id
    assert "all -> local" in rows[0]["detail"]
    other = create_channel(db, "other", creator=sysop)
    with pytest.raises(BotPolicyError):
        set_channel_bot_policy(db, other, "none", changed_by=alice)


def test_bad_policy_does_not_write(db, sysop, channel):
    with pytest.raises(BotPolicyError, match="all, local or none"):
        set_channel_bot_policy(db, channel, "bogus", changed_by=sysop)
    assert channel_bot_policy(db, channel) == "all"


def test_failed_audit_rolls_back_policy(db, sysop, channel, monkeypatch):
    from netbbs.chat import bots

    def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(bots, "record_action", unavailable)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        set_channel_bot_policy(db, channel, "none", changed_by=sysop)
    assert channel_bot_policy(db, channel) == "all"
    with Database(db.path) as reader:
        assert channel_bot_policy(reader, channel) == "all"


@pytest.mark.parametrize("paused", [0, 1])
def test_mrc_mapping_overrides_bot_policy_even_when_paused(db, sysop, alice, channel, paused):
    db.connection.execute("UPDATE channels SET mrc_room = 'lobby', mrc_paused = ? WHERE id = ?", (paused, channel.id))
    db.connection.commit()
    assert channel_bot_policy(db, channel) == "none"
    for policy in ("all", "local"):
        with pytest.raises(BotPolicyError, match="MRC-bridged"):
            set_channel_bot_policy(db, channel, policy, changed_by=sysop)
    assert chat_flow._render_channel_message(db, channel, alice, _line(channel)) == ""


def test_signed_remote_author_controls_marking_and_local_policy(db, sysop, alice, channel):
    own = bootstrap_node_identity("thisnode")
    remote = bootstrap_node_identity("remote")
    link_channel(db, channel, node_identity=own)
    event = build_channel_message(
        signing_identity=remote.signing_key, home_node_fingerprint=remote.fingerprint,
        local_user_id="Baud.bot", channel_id=channel.channel_id, body="botword",
        created_at="2026-10-11T12:00:00Z", nonce="bot-message",
    )
    stored = materialize_carried_channel_message(db, event, sender_fingerprint=remote.fingerprint)
    assert stored.author_kind == "bot"
    assert bot_origin(db, stored) == "remote"
    # A stale/human-looking display label cannot disguise the signed bot identity.
    disguised = replace(stored, author_label="OrdinaryName")
    assert "[BOT]" in chat_flow._render_channel_message(db, channel, alice, disguised)
    set_channel_bot_policy(db, channel, "local", changed_by=sysop)
    assert chat_flow._render_channel_message(db, channel, alice, disguised) == ""
    assert db.connection.execute("SELECT 1 FROM link_events WHERE content_id = ?", (event.content_id,)).fetchone()


def test_own_signed_bot_message_stays_local_and_policy_is_not_federated(db, sysop, channel):
    own = bootstrap_node_identity("thisnode")
    set_channel_bot_policy(db, channel, "local", changed_by=sysop)
    genesis = link_channel(db, channel, node_identity=own)
    assert "bot_policy" not in genesis.payload
    line = record_message(db, channel, kind="message", author_label="Baud.bot", body="botword")
    queue_channel_message_if_linked(db, line, channel, node_identity=own)
    [stored] = get_scrollback(db, channel)
    assert stored.link_event_json is not None
    assert bot_origin(db, stored) == "local"


def test_upgrade_preserves_legacy_people_and_existing_history(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, migration in enumerate(MIGRATIONS) if "Issue #1210:" in migration.description)
    path = tmp_path / "upgrade.db"
    with monkeypatch.context() as old_schema:
        old_schema.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
        with Database(path) as old:
            legacy = insert_user_on_old_schema(old, "old.bot", user_level=255)
            channel = create_channel(old, "lobby", creator=legacy)
            line = record_message(old, channel, kind="message", author_label=legacy.username, body="humanword")
    with Database(path) as upgraded:
        [stored] = get_scrollback(upgraded, channel)
        assert stored.id == line.id and stored.body == "humanword"
        assert stored.author_kind == "user" and bot_origin(upgraded, stored) is None
        assert channel_bot_policy(upgraded, channel) == "all"
        assert upgraded.connection.execute("PRAGMA foreign_key_check").fetchall() == []


async def _run_chat(lane, hub, presence, mailbox, history, channel, user, lines):
    session = FakeSession(lines)
    await asyncio.wait_for(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, user), 3)
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", "\n".join(session.written))


def test_slash_command_is_discoverable_and_read_only_without_arguments(db, lane, hub, presence, mailbox,
                                                                     history, channel, alice):
    text = asyncio.run(_run_chat(lane, hub, presence, mailbox, history, channel, alice,
                                ["/help bots", "/bots", "/bots none", "/quit"]))
    assert "/bots [all|local|none]" in text
    assert "all bots" in text and "moderation permission" in text
    assert channel_bot_policy(db, channel) == "all"
    assert db.connection.execute("SELECT COUNT(*) FROM moderation_log WHERE action = 'channel_bot_policy'").fetchone()[0] == 0


def test_moderator_slash_command_changes_policy(db, lane, hub, presence, mailbox, history, channel, sysop):
    text = asyncio.run(_run_chat(lane, hub, presence, mailbox, history, channel, sysop,
                                ["/bots local", "/bots", "/quit"]))
    assert "only this node's own bots" in text
    assert channel_bot_policy(db, channel) == "local"


def test_live_delivery_rechecks_policy_after_join(db, lane, hub, presence, mailbox, history, channel, alice, sysop):
    async def run():
        session = FakeSession([])
        task = asyncio.create_task(chat_flow._chat_loop(session, lane, hub, presence, mailbox, history, channel, alice))
        try:
            for _ in range(200):
                if hub.participant_count(channel.name):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("chat never joined")
            set_channel_bot_policy(db, channel, "none", changed_by=sysop)
            await hub.broadcast(channel.name, _line(channel))
            await hub.broadcast(channel.name, _line(channel, alice.username))
            for _ in range(200):
                if "botword" in "".join(session.written):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("human line was not delivered")
            text = "".join(session.written)
            assert "[BOT]" not in text and "Baud.bot" not in text
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("policy,shown", [("all", True), ("local", False), ("none", False)])
def test_remote_roster_uses_canonical_identity_and_policy(db, lane, hub, presence, channel, alice, sysop, policy, shown):
    from netbbs.link.boards import LinkContext
    from netbbs.link.protocol import LinkNode
    from tests.test_chat_flow_remote_roster import _bridge_with_roster, _run

    bridge = _bridge_with_roster(
        hub, lane, presence, channel_id=channel.channel_id,
        roster={"Baud.bot": "Friendly Alias", "human": "HumanAlias.bot"}, origin_fingerprint="remote-fingerprint",
    )
    ctx = LinkContext(link_node=LinkNode(identity=bootstrap_node_identity("thisnode")), realtime_bridge=bridge)
    set_channel_bot_policy(db, channel, policy, changed_by=sysop)
    session = asyncio.run(_run(lane, hub, presence, channel, alice, ["/names", "/who", "/quit"], link_context=ctx))
    text = "\n".join(session.written)
    assert ("[BOT] Baud.bot" in text) is shown
    assert "Friendly Alias" not in text
    assert "HumanAlias.bot" in text
    assert "[BOT] HumanAlias.bot" not in text


def test_hidden_origin_snapshot_does_not_draw_empty_history_panel(db, lane, sysop, alice, channel):
    class Bridge:
        def pop_channel_scrollback(self, channel_id, request_id):
            return [_line(channel, "Baud.bot@Remote", body_truncated=True)]

        def finish_scrollback_request(self, request_id):
            self.finished = request_id

    set_channel_bot_policy(db, channel, "none", changed_by=sysop)
    bridge = Bridge()
    written = []

    async def deliver(line):
        written.append(line)

    asyncio.run(chat_flow._deliver_remote_scrollback_snapshot(
        deliver, lane, channel, alice, bridge, "request-1", locally_rendered_content_ids=set(),
        unicode_style=True, truecolor=True, terminal_width=80,
    ))
    assert written == []
    assert bridge.finished == "request-1"
