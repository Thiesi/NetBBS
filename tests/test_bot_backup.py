"""Bot state is restored as the database's generation, never merged into it."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from netbbs.auth.users import create_user
from netbbs.backup import BackupError, create_backup, restore_backup
from netbbs.bots.registry import BotError, delete_bot, get_bot, save_bot, stop_bots
from netbbs.bots.runtime import BotRuntime
from netbbs.bots.state import read_state, state_path, state_root, write_state
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub, ParticipantId
from netbbs.chat.scrollback import get_scrollback, record_message
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_backup import db_path, identity_dir, isolated_voidrunner_backup_directory  # noqa: F401
from tests.test_bot_runtime import eventually, next_line


def seed(db_path):
    with Database(db_path) as db:
        sysop = create_user(db, "sysop", password="hunter2", user_level=255)
        channel = create_channel(db, "lobby", creator=sysop)
        bot = save_bot(db, actor=sysop, name="Baud", channels=[channel.name], defaults=[channel.name], enabled=True)
    return sysop, channel, bot


def test_real_baud_reminder_survives_node_backup_restore(tmp_path, db_path, identity_dir):
    sysop, channel, bot = seed(db_path)

    async def chat(command, expected, backup=None):
        db = Database(db_path)
        lane = DatabaseLane(db_path)
        hub = ChatHub()
        pid = ParticipantId("sysop", 1)
        queue = hub.join(channel.name, pid)
        runtime = BotRuntime(lane, hub, poll_seconds=.03)
        runtime.start()
        try:
            await eventually(lambda: bot.id in runtime.running_ids)
            line = record_message(db, channel, kind="message", author_label=sysop.username, body=command)
            assert await runtime.addressed(channel, sysop, line, pid) is None
            await next_line(queue, expected)
            if backup:
                await asyncio.to_thread(create_backup, db_path=db_path, identity_dir=identity_dir, destination=backup)
        finally:
            await runtime.close()
            lane.close()
            db.close()

    archive = tmp_path / "backup"
    asyncio.run(chat("!remind 1d backup-owned", "Saved reminder", archive))
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["bot_state"] == {"version": 1, "instances": ["Baud"]}
    captured = archive / "bot-state" / "Baud" / "state.json"
    assert manifest["checksums"]["bot-state/Baud/state.json"] == hashlib.sha256(captured.read_bytes()).hexdigest()
    with Database(db_path) as db:
        write_state(db, "Baud", {"next": 22, "reminders": []})
        write_state(db, "Later", {"secret": "new generation"})
    rollback = restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    assert not (state_root(db_path) / "Later").exists()
    assert (rollback / "bot-state" / "Later" / "state.json").exists()
    asyncio.run(chat("!reminders", "backup-owned"))


@pytest.mark.parametrize("filename", ["custom.sqlite", "bot-state", "Bot-State"])
@pytest.mark.parametrize("has_state", [True, False])
def test_named_database_and_absent_state_restore(tmp_path, identity_dir, filename, has_state):
    db_path = tmp_path / filename
    with Database(db_path) as db:
        if has_state:
            write_state(db, "Baud", {"old": True})
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    with Database(db_path) as db:
        write_state(db, "Baud", {"new": True})
    target = tmp_path / "restored" / "renamed.db"
    target.parent.mkdir()
    with Database(target) as db:
        write_state(db, "Newer", {"wrong": True})
    restore_backup(source=archive, db_path=target, identity_dir=target.parent / "identity")
    if has_state:
        with Database(target) as db:
            assert read_state(db, "Baud") == {"old": True}
        assert not (state_root(target) / "Newer").exists()
    else:
        assert not state_root(target).exists()


def test_legacy_archive_removes_newer_state_and_capture_precedes_database(tmp_path, db_path, identity_dir, monkeypatch):
    from netbbs import backup as module
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "old")
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["bot_state"]
    manifest_path.write_text(json.dumps(manifest))
    with Database(db_path) as db:
        write_state(db, "Baud", {"later": True})
    original = module._snapshot_database_and_managed_dns_credentials

    def snapshot(db_path, destination, database_filename):
        assert (destination / "bot-state" / "Baud" / "state.json").exists()
        original(db_path, destination, database_filename)

    monkeypatch.setattr(module, "_snapshot_database_and_managed_dns_credentials", snapshot)
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "new")
    rollback = restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    assert not state_root(db_path).exists()
    assert (rollback / "bot-state" / "Baud" / "state.json").exists()


@pytest.mark.parametrize("tamper", ["bytes", "oversize", "json", "unlisted", "missing-checksum", "name", "collision"])
def test_corrupt_bot_state_is_refused_before_live_paths_change(tmp_path, db_path, identity_dir, tamper):
    with Database(db_path) as db:
        write_state(db, "Baud", {"original": True})
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    state = archive / "bot-state" / "Baud" / "state.json"
    manifest_path = archive / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if tamper == "bytes":
        state.write_text('{}')
    elif tamper == "oversize":
        state.write_bytes(b'"' + b'x' * 16384 + b'"')
    elif tamper == "json":
        state.write_text('NaN')
    elif tamper == "unlisted":
        (state.parent / "extra.json").write_text('{}')
    elif tamper == "missing-checksum":
        del manifest["checksums"]["bot-state/Baud/state.json"]
    elif tamper == "name":
        manifest["bot_state"]["instances"] = ["../outside"]
    else:
        manifest["bot_state"]["instances"] = ["Baud", "baud"]
    manifest_path.write_text(json.dumps(manifest))
    before = db_path.read_bytes()
    with pytest.raises(BackupError):
        restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    assert db_path.read_bytes() == before
    with Database(db_path) as db:
        assert read_state(db, "Baud") == {"original": True}


def test_backup_rejects_symbolic_state_and_destination_inside_store(tmp_path, db_path, identity_dir):
    with Database(db_path) as db:
        write_state(db, "Baud", {})
    with pytest.raises(BackupError, match="inside"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=state_root(db_path) / "backup")
    state = state_root(db_path) / "Baud" / "state.json"
    outside = tmp_path / "outside.json"
    outside.write_text('{}')
    state.unlink()
    try:
        state.symlink_to(outside)
    except OSError:
        pytest.skip("This host cannot create symlinks")
    with pytest.raises(BackupError, match="symbolic"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "symlink-backup")
    assert outside.read_text() == '{}'


def test_failed_restore_rolls_back_bot_state_with_the_database(tmp_path, db_path, identity_dir, monkeypatch):
    from netbbs import backup as module
    with Database(db_path) as db:
        write_state(db, "Baud", {"old": True})
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    with Database(db_path) as db:
        create_user(db, "newer", password="hunter2", user_level=10)
        write_state(db, "Baud", {"new": True})
    original = module._switch_one

    def switch(name, staged, live, rollback):
        if name == "door-outbound":
            raise OSError("simulated later component failure")
        original(name, staged, live, rollback)

    monkeypatch.setattr(module, "_switch_one", switch)
    with pytest.raises(BackupError, match="rolled back"):
        restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    with Database(db_path) as db:
        assert read_state(db, "Baud") == {"new": True}
        assert db.connection.execute("SELECT 1 FROM users WHERE username='newer'").fetchone()


def test_delete_confirmation_refusal_keeps_configuration_and_state(tmp_path, db_path, monkeypatch):
    from netbbs.net import bot_flow
    from tests.test_bot_runtime import FakeSession
    sysop, channel, bot = seed(db_path)
    with Database(db_path) as db:
        stop_bots(db, actor=sysop)
        write_state(db, "Baud", {"kept": True})

    async def run():
        lane = DatabaseLane(db_path)
        answers = iter((False, True))

        async def confirm(session, prompt, **kwargs):
            assert "entire state directory" in prompt and "history" in prompt
            assert kwargs["default"] is False
            with Database(db_path) as db:
                assert get_bot(db, bot.id)
                assert read_state(db, "Baud") == {"kept": True}
            return next(answers)

        monkeypatch.setattr(bot_flow, "prompt_yes_no", confirm)
        try:
            assert not await bot_flow._delete_bot(FakeSession(), lane, sysop, bot.id)
            assert await bot_flow._delete_bot(FakeSession(), lane, sysop, bot.id)
            with Database(db_path) as db:
                with pytest.raises(BotError, match="no longer"):
                    get_bot(db, bot.id)
            assert not (state_root(db_path) / "Baud").exists()
        finally:
            lane.close()

    asyncio.run(run())


def test_delete_requires_stopped_process_and_removes_only_its_store(tmp_path, db_path):
    sysop, channel, bot = seed(db_path)
    with Database(db_path) as db:
        write_state(db, "Baud", {"reminders": ["old"]})
        write_state(db, "Other", {"kept": True})
        record_message(db, channel, kind="message", author_label=bot.label, body="kept history")
        with pytest.raises(BotError, match="Kill"):
            delete_bot(db, bot.id, actor=sysop)
        stop_bots(db, actor=sysop)
        db.connection.execute("UPDATE chat_bots SET runtime_state='running'")
        db.connection.commit()
        with pytest.raises(BotError, match="Kill"):
            delete_bot(db, bot.id, actor=sysop)

    async def run():
        lane = DatabaseLane(db_path)
        runtime = BotRuntime(lane, ChatHub(), poll_seconds=.03)
        runtime.start()
        try:
            with Database(db_path) as db:
                await eventually(lambda: db.connection.execute("SELECT runtime_state FROM chat_bots").fetchone()[0] == "stopped")
                delete_bot(db, bot.id, actor=sysop)
                assert not (state_root(db_path) / "Baud").exists()
                assert read_state(db, "Other") == {"kept": True}
                assert get_scrollback(db, channel)[0].body == "kept history"
                replacement = save_bot(db, actor=sysop, name="Baud", channels=[], defaults=[])
                assert read_state(db, replacement.storage_name) == {}
        finally:
            await runtime.close()
            lane.close()

    asyncio.run(run())
