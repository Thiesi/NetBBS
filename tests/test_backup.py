"""
Tests for netbbs.backup (design doc §13.4, issue #60's first
operational slice): create_backup/restore_backup capturing and
restoring all fourteen recoverable-state artifacts, the ordering/safety
invariants around them, and the `python -m netbbs.backup` CLI.
"""

from __future__ import annotations

import hashlib
import contextlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from netbbs.backup import (
    BackupError,
    create_backup,
    default_backup_destination,
    get_last_backup_summary,
    main,
    remove_pid_file,
    restore_backup,
    write_pid_file,
)
from netbbs import backup as backup_module
from netbbs.link.node_identity import NodeIdentity, bootstrap_node_identity
from netbbs.managed_dns.state import (
    RegistrationStatus,
    get_previous_name,
    get_registered_name,
    get_registration_status,
    set_cancelled_rename_state,
    set_pending_rename_state,
)
from netbbs.storage.database import Database


@pytest.fixture(autouse=True)
def isolated_voidrunner_backup_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("VOIDRUNNER_SAVE_DIR", str(tmp_path / "voidrunner-careers"))


def _populate_voidrunner():
    from netbbs.doors.bundled import voidrunner as vr
    directory = backup_module.voidrunner_save_directory()
    world = vr.World(vr._new_career("Backup Pilot"))
    vr.persist(world, directory, 77)
    world.save.pilot.credits += 100
    vr.persist(world, directory, 77)
    (directory / "77.recovery-retained.json").write_bytes(b"damaged original")
    (directory / "77.corrupt-1234").write_bytes(b"legacy damaged original")
    (directory / "leaderboard.json").write_text("[]", encoding="utf-8")
    return directory


def _retained_game_bytes(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file() and not path.name.startswith(".")}


def test_voidrunner_component_round_trip_preserves_every_retained_file(tmp_path, db_path, identity_dir):
    game = _populate_voidrunner()
    expected = _retained_game_bytes(game)
    (game / ".77.lock").write_bytes(b"0")
    (game / ".77-private.tmp").write_bytes(b"incomplete")
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest = json.loads((source / "manifest.json").read_text())
    assert manifest["voidrunner"]["files"] == sorted(expected)
    assert _retained_game_bytes(source / "voidrunner") == expected
    assert not list((source / "voidrunner").glob(".*"))
    for relative, raw in expected.items():
        assert manifest["checksums"][f"voidrunner/{relative}"] == hashlib.sha256(raw).hexdigest()
    restored = tmp_path / "restored-games"
    restored.mkdir()
    (restored / "99.json").write_bytes(b"old generation")
    rollback = restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=restored)
    assert _retained_game_bytes(restored) == expected
    external = json.loads((rollback / "voidrunner-rollback.json").read_text())
    from pathlib import Path
    assert (Path(external["rollback"]) / "voidrunner" / "99.json").read_bytes() == b"old generation"
    assert _retained_game_bytes(game) == expected


def test_voidrunner_capture_precedes_database_snapshot(tmp_path, db_path, identity_dir, monkeypatch):
    _populate_voidrunner()
    original = backup_module._snapshot_database_and_managed_dns_credentials
    calls = []

    def snapshot(db_path, destination, database_filename):
        assert (destination / "voidrunner" / "77.json").exists()
        calls.append("snapshot")
        return original(db_path, destination, database_filename)

    monkeypatch.setattr(backup_module, "_snapshot_database_and_managed_dns_credentials", snapshot)
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    assert calls == ["snapshot"]


@pytest.mark.parametrize("filename", ["voidrunner", "Voidrunner"])
@pytest.mark.parametrize("include_game", [False, True])
def test_voidrunner_named_databases_and_legacy_archives_remain_restorable(
    tmp_path, db_path, identity_dir, filename, include_game,
):
    custom = tmp_path / "custom-node" / filename
    custom.parent.mkdir()
    shutil.copy2(db_path, custom)
    with sqlite3.connect(custom) as connection:
        connection.execute("INSERT OR REPLACE INTO node_config(key,value) VALUES('node_name','Custom DB')")
    if include_game:
        game = _populate_voidrunner()
        expected = _retained_game_bytes(game)
    source = create_backup(db_path=custom, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if include_game:
        assert manifest["database_filename"] == "netbbs.db"
    else:
        assert manifest["database_filename"] == filename
        del manifest["voidrunner"]  # The manifest shape used before game coverage.
        manifest_path.write_text(json.dumps(manifest))
    restored_db = tmp_path / "restored-node" / filename
    target = tmp_path / "restored-games" if include_game else None
    restore_backup(source=source, db_path=restored_db, identity_dir=tmp_path / "restored-identity", voidrunner_to=target)
    with sqlite3.connect(restored_db) as connection:
        assert connection.execute("SELECT value FROM node_config WHERE key='node_name'").fetchone()[0] == "Custom DB"
    if include_game:
        assert _retained_game_bytes(target) == expected


@pytest.mark.parametrize("damage", ["bytes", "missing", "unlisted", "unchecked", "metadata", "traversal", "temporary"])
def test_voidrunner_component_rejects_damaged_or_ambiguous_archives_before_node_changes(
    tmp_path, db_path, identity_dir, damage,
):
    _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if damage == "bytes":
        (source / "voidrunner" / "77.json").write_bytes(b"changed")
    elif damage == "missing":
        (source / "voidrunner" / "77.json").unlink()
    elif damage == "unlisted":
        (source / "voidrunner" / "88.json").write_bytes(b"extra")
    elif damage == "unchecked":
        del manifest["checksums"]["voidrunner/77.json"]
    elif damage == "metadata":
        del manifest["voidrunner"]
    elif damage == "traversal":
        manifest["voidrunner"]["files"].append("../elsewhere.json")
    else:
        (source / "voidrunner" / ".77-private.tmp").write_bytes(b"unlisted")
    manifest_path.write_text(json.dumps(manifest))
    before = db_path.read_bytes()
    target = tmp_path / "restored-games"
    with pytest.raises(BackupError):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=target)
    assert db_path.read_bytes() == before and not target.exists()


def test_voidrunner_restore_requires_explicit_destination_and_ignores_recorded_source_path(
    tmp_path, db_path, identity_dir,
):
    game = _populate_voidrunner()
    original = _retained_game_bytes(game)
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    with pytest.raises(BackupError, match="voidrunner-to"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["voidrunner"]["source_directory"] = str(tmp_path / "must-not-touch")
    manifest_path.write_text(json.dumps(manifest))
    restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=tmp_path / "chosen")
    assert not (tmp_path / "must-not-touch").exists()
    assert _retained_game_bytes(game) == original


def test_legacy_backup_leaves_existing_voidrunner_data_unchanged(tmp_path, db_path, identity_dir):
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("voidrunner")
    manifest_path.write_text(json.dumps(manifest))
    game = _populate_voidrunner()
    original = _retained_game_bytes(game)
    restore_backup(source=source, db_path=db_path, identity_dir=identity_dir)
    assert _retained_game_bytes(game) == original


@pytest.mark.parametrize("failed_entry", [1, 3])
def test_game_switch_failure_rolls_back_both_node_and_game(tmp_path, db_path, identity_dir, monkeypatch, failed_entry):
    from pathlib import Path
    from netbbs.config import get_config, set_config

    _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    live = Database(db_path)
    set_config(live, "node_name", "Before rollback")
    live.close()
    target = tmp_path / "live-games"
    target.mkdir()
    (target / "99.json").write_bytes(b"previous game generation")
    rename = Path.rename
    entries = []

    def fail_game_stage(path, destination):
        if path.parent.name.startswith(".live-games.netbbs-stage-"):
            entries.append(path.name)
            if len(entries) == failed_entry:
                raise OSError("game stage switch failed")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_game_stage)
    with pytest.raises(BackupError, match="automatically rolled back"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=target)
    assert (target / "99.json").read_bytes() == b"previous game generation"
    restored = Database(db_path)
    try:
        assert get_config(restored, "node_name") == "Before rollback"
    finally:
        restored.close()
    assert not (db_path.parent / ".netbbs-restore-state.json").exists()


def test_failed_rollback_pointer_names_the_retained_journal(tmp_path, db_path, identity_dir, monkeypatch):
    from pathlib import Path

    game = _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    (game / "99.json").write_bytes(b"previous generation")
    write_text = Path.write_text

    def fail_pointer(path, *args, **kwargs):
        if path.name == "voidrunner-rollback.json":
            raise OSError("rollback pointer disk full")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_pointer)
    journal_path = db_path.parent / ".netbbs-restore-state.json"
    with pytest.raises(BackupError, match="Restored data is in place") as caught:
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=game)
    assert str(journal_path) in str(caught.value)
    external = json.loads(journal_path.read_text())["external_components"]["voidrunner"]
    assert (Path(external["rollback"]) / "voidrunner" / "99.json").read_bytes() == b"previous generation"
    assert _retained_game_bytes(game) == _retained_game_bytes(source / "voidrunner")


def test_game_local_stage_and_rollback_stay_beside_destination(tmp_path, db_path, identity_dir, monkeypatch):
    _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    target = tmp_path / "different-mount" / "games"
    original = backup_module._switch_one
    calls = []

    def checked(name, staged, live, rollback):
        if name == "voidrunner":
            assert staged.parent == rollback.parent == live.parent == target.parent
            calls.append(name)
        return original(name, staged, live, rollback)

    monkeypatch.setattr(backup_module, "_switch_one", checked)
    restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=target)
    assert calls == ["voidrunner"]


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_post_game_journal_failure_rolls_back_or_retains_usable_external_paths(
    tmp_path, db_path, identity_dir, monkeypatch, rollback_fails,
):
    from pathlib import Path
    from netbbs.config import get_config, set_config

    _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    live = Database(db_path)
    set_config(live, "node_name", "Previous node")
    live.close()
    target = tmp_path / "live-games"
    target.mkdir()
    (target / "99.json").write_bytes(b"previous game")
    original = backup_module._write_restore_state
    rename = Path.rename

    def fail_last_update(path, **kwargs):
        if not kwargs["pending"]:
            raise OSError("journal disk full")
        original(path, **kwargs)

    def fail_rollback(path, destination):
        if rollback_fails and path.name == "99.json" and path.parent.name == "voidrunner":
            raise OSError("rollback unavailable")
        return rename(path, destination)

    monkeypatch.setattr(backup_module, "_write_restore_state", fail_last_update)
    monkeypatch.setattr(Path, "rename", fail_rollback)
    match = "manual recovery" if rollback_fails else "automatically rolled back"
    with pytest.raises(BackupError, match=match):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=target)
    journal_path = db_path.parent / ".netbbs-restore-state.json"
    if rollback_fails:
        journal = json.loads(journal_path.read_text())
        external = journal["external_components"]["voidrunner"]
        assert (Path(external["rollback"]) / "voidrunner" / "99.json").read_bytes() == b"previous game"
        assert Path(external["staging"]).exists()
    else:
        assert not journal_path.exists()
        assert _retained_game_bytes(target) == {"99.json": b"previous game"}
        restored = Database(db_path)
        try:
            assert get_config(restored, "node_name") == "Previous node"
        finally:
            restored.close()


def test_restore_journal_replacement_failure_preserves_previous_json(tmp_path, monkeypatch):
    import os

    state = tmp_path / "state.json"
    args = dict(staging_dir=tmp_path / "stage", rollback_dir=tmp_path / "old",
                external={"voidrunner": {"rollback": "original-game-location"}})
    backup_module._write_restore_state(state, pending=["voidrunner"], **args)
    previous = state.read_bytes()
    replace = os.replace

    def fail_journal(source, destination):
        if destination == state:
            raise OSError("journal replacement denied")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_journal)
    with pytest.raises(OSError, match="replacement denied"):
        backup_module._write_restore_state(state, pending=[], **args)
    assert state.read_bytes() == previous
    assert json.loads(previous)["external_components"]["voidrunner"]["rollback"] == "original-game-location"
    assert list(tmp_path.iterdir()) == [state]


def test_existing_game_directory_needs_no_parent_write_for_play_or_capture(
    tmp_path, db_path, identity_dir, monkeypatch,
):
    import errno
    from pathlib import Path
    from netbbs.doors.bundled import voidrunner as vr

    parent = tmp_path / "operator-owned"
    game = parent / "service-owned"
    game.mkdir(parents=True)
    vr.persist(vr.World(vr._new_career("Provisioned")), game, 77)
    original = Path.open

    def restricted_open(path, mode="r", *args, **kwargs):
        if path.parent == parent and any(flag in mode for flag in "wax+"):
            raise PermissionError(errno.EACCES, "parent is not writable", str(path))
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", restricted_open)
    with vr.pilot_session(game, 77):
        save, _, _ = vr.load_or_create_save(game, 77, "Provisioned")
        save.pilot.credits += 1
        vr.persist(vr.World(save), game, 77)
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup",
                           voidrunner_save_dir=game)
    assert (source / "voidrunner" / "77.json").read_bytes() == (game / "77.json").read_bytes()
    assert list(parent.iterdir()) == [game]


def test_restore_keeps_lock_inodes_and_excludes_launch_after_game_switch(
    tmp_path, db_path, identity_dir, monkeypatch,
):
    import os
    import subprocess
    import sys
    from netbbs.doors.bundled import voidrunner as vr

    game = _populate_voidrunner()
    with vr.pilot_session(game, 77):
        pass
    inodes = [path.stat().st_ino for path in (game, game / ".maintenance.lock", game / ".77.lock")]
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    info = tmp_path / "door-info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}))
    original = backup_module._switch_one
    checked = []

    def check_switch(name, staged, live, rollback):
        original(name, staged, live, rollback)
        if name == "voidrunner":
            result = subprocess.run([sys.executable, vr.__file__], input=b"Q", capture_output=True,
                                    env=dict(os.environ, NETBBS_DOOR_INFO=str(info)), timeout=5)
            assert result.returncode == 0
            assert b"maintenance is in progress" in b" ".join(result.stdout.split())
            checked.append(name)

    monkeypatch.setattr(backup_module, "_switch_one", check_switch)
    restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=game)
    assert checked == ["voidrunner"]
    assert [path.stat().st_ino for path in (game, game / ".maintenance.lock", game / ".77.lock")] == inodes
    with vr.pilot_session(game, 77):
        pass


def test_voidrunner_cli_create_and_restore_reports_activation_step(tmp_path, db_path, identity_dir, capsys):
    game = _populate_voidrunner()
    source, target = tmp_path / "backup", tmp_path / "restored-games"
    main(["create", "--db", str(db_path), "--identity-dir", str(identity_dir), "--to", str(source),
          "--voidrunner-save-dir", str(game)])
    main(["restore", "--db", str(db_path), "--identity-dir", str(identity_dir), "--from", str(source),
          "--voidrunner-to", str(target)])
    output = capsys.readouterr().out
    assert "MANUAL" in output and "VOIDRUNNER_SAVE_DIR" in output
    assert (target / "77.json").exists()


@contextlib.contextmanager
def _real_pilot_lease(directory, ready):
    import subprocess
    import sys
    import time
    from netbbs.doors.bundled import voidrunner as vr

    script = """
import runpy, sys
from pathlib import Path
vr = runpy.run_path(sys.argv[1])
with vr['pilot_session'](Path(sys.argv[2]), 77):
    Path(sys.argv[3]).write_text('ready')
    sys.stdin.read(1)
"""
    proc = subprocess.Popen([sys.executable, "-c", script, vr.__file__, str(directory), str(ready)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists() and proc.poll() is None
        yield
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_backup_and_restore_refuse_an_active_real_pilot(tmp_path, db_path, identity_dir, operation):
    game = _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    before = _retained_game_bytes(game)
    db_before = db_path.read_bytes()
    with _real_pilot_lease(game, tmp_path / "ready"):
        with pytest.raises(BackupError, match="Voidrunner is active"):
            if operation == "create":
                create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "second")
            else:
                restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=game)
    assert _retained_game_bytes(game) == before and db_path.read_bytes() == db_before


def test_maintenance_prevents_a_new_real_game_launch(tmp_path):
    import os
    import subprocess
    import sys
    from netbbs.doors.bundled import voidrunner as vr

    game = _populate_voidrunner()
    before = _retained_game_bytes(game)
    info = tmp_path / "door-info.json"
    info.write_text(json.dumps({"user_id": 77, "handle": "Tester"}))
    with vr.maintenance_session(game):
        result = subprocess.run([sys.executable, vr.__file__], input=b"Q", capture_output=True,
                                env=dict(os.environ, NETBBS_DOOR_INFO=str(info)), timeout=5)
    assert result.returncode == 0 and b"maintenance is in progress" in b" ".join(result.stdout.split())
    assert _retained_game_bytes(game) == before


@pytest.mark.parametrize("limit", ["files", "file_bytes", "total_bytes"])
def test_voidrunner_backup_limits_fail_without_success_manifest(tmp_path, db_path, identity_dir, monkeypatch, limit):
    _populate_voidrunner()
    key = {"files": "_VOIDRUNNER_MAX_FILES", "file_bytes": "_VOIDRUNNER_MAX_FILE_BYTES",
           "total_bytes": "_VOIDRUNNER_MAX_TOTAL_BYTES"}[limit]
    monkeypatch.setattr(backup_module, key, 1)
    destination = tmp_path / "backup"
    with pytest.raises(BackupError, match="exceeds"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)
    assert not (destination / "manifest.json").exists()


@pytest.mark.parametrize("target_kind", ["backup", "database_parent", "identity", "files", "inside_files", "credential", "pid"])
def test_voidrunner_restore_rejects_overlapping_destinations(tmp_path, db_path, identity_dir, target_kind):
    _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    storage = backup_module._storage_root_for(db_path)
    target = {"backup": source, "database_parent": db_path.parent, "identity": identity_dir,
              "files": storage, "inside_files": storage / "game",
              "credential": backup_module._managed_dns_credential_path_for(db_path),
              "pid": backup_module._pid_file_path_for(db_path)}[target_kind]
    with pytest.raises(BackupError, match="overlaps"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=target)


@pytest.mark.parametrize("parent", ["", "scores"])
def test_voidrunner_backup_does_not_ignore_directories_named_like_temporary_files(
    tmp_path, db_path, identity_dir, parent,
):
    game = _populate_voidrunner()
    (game / parent / ".unrelated.tmp").mkdir()
    with pytest.raises(BackupError, match="Unsupported Voidrunner"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")


@pytest.mark.parametrize("suffix", ["_ssh_host_key", "_welcome_banner.ans", "_main_menu_banner.ans", "_logoff_banner.ans",
                                   "_new_account_banner_before.ans", "_new_account_banner_after.ans", "_board_list_banner.ans",
                                   "_file_area_banner.ans", "_chat_channel_picker_banner.ans", "-wal", "-shm", "-journal"])
def test_voidrunner_restore_protects_node_paths_absent_from_the_archive(tmp_path, db_path, identity_dir, suffix):
    _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    restored_db = tmp_path / "fresh-node" / "restored.db"
    name = (restored_db.name if suffix.startswith("-") else restored_db.stem) + suffix
    target = restored_db.parent / name
    assert not target.exists()
    with pytest.raises(BackupError, match="overlaps"):
        restore_backup(source=source, db_path=restored_db, identity_dir=tmp_path / "new-identity", voidrunner_to=target)
    assert not restored_db.exists() and not target.exists()


@pytest.mark.parametrize("name", ["netbbs.log", "netbbs.log.1", "netbbs.log.5", "restored_github_pat",
                                "restored_github_pat.tmp", "restored_managed_dns_credential.tmp", "doors",
                                "door-nodes", "restored.db_drafts", "restored_backups"])
def test_voidrunner_restore_protects_fixed_runtime_namespaces(tmp_path, db_path, identity_dir, name):
    _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    restored_db = tmp_path / "fresh-node" / "restored.db"
    target = restored_db.parent / name
    with pytest.raises(BackupError, match="overlaps"):
        restore_backup(source=source, db_path=restored_db, identity_dir=tmp_path / "new-identity", voidrunner_to=target)
    assert not restored_db.exists() and not target.exists()


def test_reserved_runtime_paths_match_the_actual_log_handler_and_token_path(db_path):
    from pathlib import Path
    from netbbs.__main__ import _create_log_file_handler
    from netbbs.selfupdate import github_pat_path
    reserved = {path.resolve() for path in backup_module._runtime_reserved_paths(db_path)}
    db = Database(db_path)
    log_path = db_path.parent / "netbbs.log"
    handler = _create_log_file_handler(log_path)
    try:
        pat = github_pat_path(db)
        assert pat.resolve() in reserved and Path(str(pat) + ".tmp").resolve() in reserved
        assert log_path.resolve() in reserved
        for index in range(1, handler.backupCount + 1):
            assert Path(str(log_path) + f".{index}").resolve() in reserved
    finally:
        handler.close()
        db.close()


@pytest.mark.parametrize("banner", ["welcome", "main_menu", "logoff", "new_account_banner_before",
                                    "new_account_banner_after", "board_list", "file_area", "chat_channel_picker"])
def test_restore_protects_absent_banner_recovery_drafts(tmp_path, db_path, identity_dir, banner):
    _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    restored_db = tmp_path / "restored.db"
    suffix = banner if banner.startswith("new_account") else banner + "_banner"
    target = tmp_path / f"restored_{suffix}.ans.draft"
    with pytest.raises(BackupError, match="overlaps"):
        restore_backup(source=source, db_path=restored_db, identity_dir=tmp_path / "new-identity", voidrunner_to=target)
    assert not target.exists() and not restored_db.exists()


def test_maintenance_probes_historical_pilot_files_with_bounded_descriptors(tmp_path, monkeypatch):
    import errno
    from netbbs.doors.bundled import voidrunner as vr

    game = tmp_path / "many-pilots"
    game.mkdir()
    for pilot in range(128):
        (game / f".{pilot}.lock").write_bytes(b"0")
    original = vr._file_lease
    live = maximum = 0

    @contextlib.contextmanager
    def limited(path, **kwargs):
        nonlocal live, maximum
        if live >= 3:
            raise OSError(errno.EMFILE, "descriptor ceiling")
        with original(path, **kwargs):
            live += 1
            maximum = max(maximum, live)
            try:
                yield
            finally:
                live -= 1

    monkeypatch.setattr(vr, "_file_lease", limited)
    with vr.maintenance_session(game):
        assert live == 1  # Only the gate survives into capture or restore.
    assert maximum == 2 and live == 0
    with original(game / ".127.lock"):
        with pytest.raises(vr.PilotBusy):
            with vr.maintenance_session(game):
                pytest.fail("Active pilot was missed")
    assert live == 0
    assert all((game / f".{pilot}.lock").read_bytes() == b"0" for pilot in range(128))


@pytest.mark.parametrize("failure", ["active", "unsupported", "oversized"])
def test_failed_game_capture_cleans_only_its_destination_and_allows_same_path_retry(
    tmp_path, db_path, identity_dir, failure,
):
    game = _populate_voidrunner()
    primary = (game / "77.json").read_bytes()
    bad = None
    if failure == "unsupported":
        bad = game / "unexpected.txt"
        bad.write_bytes(b"unrelated")
    elif failure == "oversized":
        bad = game / "scores" / "999.json"
        bad.write_bytes(b"x" * (backup_module._VOIDRUNNER_MAX_FILE_BYTES + 1))
    lease = _real_pilot_lease(game, tmp_path / "ready") if failure == "active" else contextlib.nullcontext()
    destination = tmp_path / "retry-backup"
    with lease:
        with pytest.raises(BackupError):
            create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)
    assert not destination.exists()
    assert (game / "77.json").read_bytes() == primary
    if bad is not None:
        assert bad.exists()
        bad.unlink()
    assert create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination) == destination
    assert (destination / "voidrunner" / "77.json").read_bytes() == primary


def test_failed_capture_cleanup_reports_the_incomplete_path_for_manual_removal(tmp_path, db_path, identity_dir, monkeypatch):
    game = _populate_voidrunner()
    (game / "unexpected.txt").write_bytes(b"keep source")
    destination = tmp_path / "incomplete"

    def fail_cleanup(path, *args, **kwargs):
        assert path == destination
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(backup_module.shutil, "rmtree", fail_cleanup)
    with pytest.raises(BackupError, match="Remove it manually before retrying") as error:
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)
    assert str(destination) in str(error.value) and "Unsupported Voidrunner" in str(error.value)
    assert destination.exists() and (game / "unexpected.txt").read_bytes() == b"keep source"


def test_voidrunner_restore_refuses_a_directory_with_unrelated_files(tmp_path, db_path, identity_dir):
    _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    target = tmp_path / "unrelated"
    target.mkdir()
    (target / "important.txt").write_bytes(b"keep")
    with pytest.raises(BackupError, match="Unsupported Voidrunner"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=target)
    assert (target / "important.txt").read_bytes() == b"keep"


def test_voidrunner_backup_refuses_nested_destination_before_creating_it(tmp_path, db_path, identity_dir):
    game = _populate_voidrunner()
    destination = game / "nested-backup"
    with pytest.raises(BackupError, match="inside"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)
    assert not destination.exists()


def test_failed_game_rollback_retains_journal_with_external_paths(tmp_path, db_path, identity_dir, monkeypatch):
    from pathlib import Path

    _populate_voidrunner()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    target = tmp_path / "live-games"
    target.mkdir()
    (target / "99.json").write_bytes(b"old game")
    rename = Path.rename

    def fail_stage_and_rollback(path, destination):
        if path.parent.name.startswith(".live-games.netbbs-stage-") or path.parent.parent.name.startswith(".live-games.netbbs-rollback-"):
            raise OSError("stage and rollback unavailable")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_stage_and_rollback)
    with pytest.raises(BackupError, match="manual recovery"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, voidrunner_to=target)
    journal = json.loads((db_path.parent / ".netbbs-restore-state.json").read_text())
    external = journal["external_components"]["voidrunner"]
    assert external["target"] == str(target)
    assert (Path(external["rollback"]) / "voidrunner" / "99.json").read_bytes() == b"old game"
    assert Path(external["staging"]).exists()

_BLOB_CONTENT = b"blob content"
# A real content-addressed store always names a blob after its own
# sha256 (netbbs.files.storage) -- issue #75's restore validation now
# actually checks this, so a fixture with a fabricated, non-matching
# hash (this test's own pre-issue-#75 shape) would be correctly
# rejected as "corrupt."
_BLOB_HASH = hashlib.sha256(_BLOB_CONTENT).hexdigest()


def _storage_root(db_path):
    return db_path.parent / f"{db_path.stem}_files"


def _ssh_host_key_path(db_path):
    return db_path.parent / f"{db_path.stem}_ssh_host_key"


def _managed_dns_credential_path(db_path):
    return db_path.parent / f"{db_path.stem}_managed_dns_credential"


def _managed_dns_previous_credential_path(db_path):
    return db_path.parent / f"{db_path.stem}_managed_dns_previous_credential"


def _managed_dns_transition_credential_path(db_path):
    return db_path.parent / f"{db_path.stem}_managed_dns_credential_transition"


def _welcome_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_welcome_banner.ans"


def _main_menu_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_main_menu_banner.ans"


def _logoff_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_logoff_banner.ans"


def _new_account_banner_before_path(db_path):
    return db_path.parent / f"{db_path.stem}_new_account_banner_before.ans"


def _new_account_banner_after_path(db_path):
    return db_path.parent / f"{db_path.stem}_new_account_banner_after.ans"


def _board_list_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_board_list_banner.ans"


def _file_area_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_file_area_banner.ans"


def _chat_channel_picker_banner_path(db_path):
    return db_path.parent / f"{db_path.stem}_chat_channel_picker_banner.ans"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "netbbs.db"
    Database(path).close()
    return path


@pytest.fixture
def identity_dir(tmp_path):
    return tmp_path / "netbbs_identity"


def _seed_full_node(db_path, identity_dir) -> NodeIdentity:
    """Populate the ordinary backup artifacts with
    distinguishable content, including the transient `.incoming`
    staging file that must never survive into a backup."""
    blob_path = _storage_root(db_path) / _BLOB_HASH[:2] / _BLOB_HASH
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(_BLOB_CONTENT)

    incoming_path = _storage_root(db_path) / ".incoming" / "partial-upload"
    incoming_path.parent.mkdir(parents=True)
    incoming_path.write_bytes(b"should never be backed up")

    identity = bootstrap_node_identity("test-node")
    identity.save(identity_dir)

    _ssh_host_key_path(db_path).write_bytes(b"fake ssh host key")
    _managed_dns_credential_path(db_path).write_text("fake managed-dns credential")
    _managed_dns_previous_credential_path(db_path).write_text("fake previous managed-dns credential")
    _welcome_banner_path(db_path).write_text("fake banner")
    _main_menu_banner_path(db_path).write_text("fake masthead")
    _logoff_banner_path(db_path).write_text("fake logoff banner")
    _new_account_banner_before_path(db_path).write_text("fake before banner")
    _new_account_banner_after_path(db_path).write_text("fake after banner")
    _board_list_banner_path(db_path).write_text("fake board masthead")
    _file_area_banner_path(db_path).write_text("fake file area masthead")
    _chat_channel_picker_banner_path(db_path).write_text("fake channel masthead")

    return identity


# -- create_backup --------------------------------------------------------


def test_default_backup_destination_is_timestamped_beside_the_database(tmp_path):
    db_path = tmp_path / "data" / "node.db"

    destination = default_backup_destination(
        db_path, created_at="2026-09-04T12:34:56.123456+00:00"
    )

    assert destination == tmp_path / "data" / "node_backups" / "backup-20260904T123456Z"


def test_default_backup_destination_does_not_reuse_an_existing_directory(tmp_path):
    db_path = tmp_path / "node.db"
    first = default_backup_destination(db_path, created_at="2026-09-04T12:34:56+00:00")
    first.mkdir(parents=True)

    second = default_backup_destination(db_path, created_at="2026-09-04T12:34:56+00:00")

    assert second == first.with_name(first.name + "-2")


def test_create_backup_still_succeeds_if_status_bookkeeping_fails(
    tmp_path, db_path, identity_dir, monkeypatch,
):
    destination = tmp_path / "backup1"

    def fail_status_open(*args, **kwargs):
        raise sqlite3.OperationalError("database busy")

    monkeypatch.setattr(backup_module, "Database", fail_status_open)

    assert create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=destination
    ) == destination
    assert (destination / "manifest.json").exists()


def test_create_backup_captures_a_staged_credential_transition(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    _managed_dns_transition_credential_path(db_path).write_text("staged secrets")

    destination = create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup-transition"
    )

    assert (destination / _managed_dns_transition_credential_path(db_path).name).read_text() == "staged secrets"


def test_create_backup_retries_when_cancellation_overlaps_the_database_and_credential_snapshot(
    tmp_path, db_path, identity_dir, monkeypatch,
):
    live_db = Database(db_path)
    set_pending_rename_state(
        live_db,
        name="new-name",
        previous_name="old-name",
        previous_status=RegistrationStatus.MATURED,
        previous_published=True,
    )
    primary = _managed_dns_credential_path(db_path)
    previous = _managed_dns_previous_credential_path(db_path)
    transition = _managed_dns_transition_credential_path(db_path)
    primary.write_text("replacement-secret")
    previous.write_text("old-secret")
    transition.write_text('{"primary":"replacement-secret","previous":"old-secret"}')

    real_snapshot = backup_module.snapshot_database
    snapshot_calls = 0

    def cancellation_after_first_snapshot(source, target):
        nonlocal snapshot_calls
        snapshot_calls += 1
        real_snapshot(source, target)
        if snapshot_calls == 1:
            set_cancelled_rename_state(
                live_db, name="old-name", status=RegistrationStatus.MATURED, published=True,
            )
            primary.write_text("old-secret")
            previous.unlink()
            transition.unlink()

    monkeypatch.setattr(backup_module, "snapshot_database", cancellation_after_first_snapshot)

    destination = create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup-raced"
    )

    assert snapshot_calls == 2
    backed_up_db = Database(destination / "netbbs.db")
    try:
        assert get_registered_name(backed_up_db) == "old-name"
        assert get_registration_status(backed_up_db) is RegistrationStatus.MATURED
        assert get_previous_name(backed_up_db) is None
    finally:
        backed_up_db.close()
        live_db.close()
    assert (destination / primary.name).read_text() == "old-secret"
    assert not (destination / previous.name).exists()
    assert not (destination / transition.name).exists()


def test_create_backup_captures_all_ordinary_artifacts(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    assert (destination / "netbbs.db").exists()
    assert (destination / "files" / _BLOB_HASH[:2] / _BLOB_HASH).read_bytes() == b"blob content"
    assert (destination / "identity" / "root.identity").exists()
    assert (destination / "identity" / "transitions.json").exists()
    assert (destination / f"{db_path.stem}_ssh_host_key").read_bytes() == b"fake ssh host key"
    assert (destination / f"{db_path.stem}_managed_dns_credential").read_text() == "fake managed-dns credential"
    assert (destination / f"{db_path.stem}_managed_dns_previous_credential").read_text() == "fake previous managed-dns credential"
    assert (destination / f"{db_path.stem}_welcome_banner.ans").read_text() == "fake banner"
    assert (destination / f"{db_path.stem}_main_menu_banner.ans").read_text() == "fake masthead"
    assert (destination / f"{db_path.stem}_logoff_banner.ans").read_text() == "fake logoff banner"
    assert (destination / f"{db_path.stem}_new_account_banner_before.ans").read_text() == "fake before banner"
    assert (destination / f"{db_path.stem}_new_account_banner_after.ans").read_text() == "fake after banner"
    assert (destination / f"{db_path.stem}_board_list_banner.ans").read_text() == "fake board masthead"
    assert (destination / f"{db_path.stem}_file_area_banner.ans").read_text() == "fake file area masthead"
    assert (destination / f"{db_path.stem}_chat_channel_picker_banner.ans").read_text() == "fake channel masthead"
    assert (destination / "manifest.json").exists()


def test_create_backup_excludes_incoming_staging(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    assert not (destination / "files" / ".incoming").exists()


def test_create_backup_writes_a_readable_manifest(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["source_db_path"] == str(db_path)
    assert manifest["source_identity_dir"] == str(identity_dir)
    assert manifest["database_filename"] == db_path.name
    assert isinstance(manifest["db_user_version"], int)
    assert manifest["netbbs_version"]
    assert manifest["created_at"]


def test_backup_preserves_a_custom_database_filename(tmp_path, identity_dir):
    db_path = tmp_path / "dogfood-blue.db"
    Database(db_path).close()
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    assert (destination / db_path.name).exists()
    assert not (destination / "netbbs.db").exists()
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["database_filename"] == db_path.name
    assert db_path.name in manifest["checksums"]

    db_path.unlink()
    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)
    assert db_path.exists()


def test_restore_accepts_legacy_backup_without_database_filename(tmp_path, db_path, identity_dir):
    destination = create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup1"
    )
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["database_filename"]
    manifest_path.write_text(json.dumps(manifest, indent=2))

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_rejects_unsafe_database_filename(tmp_path, db_path, identity_dir):
    destination = create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup1"
    )
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["database_filename"] = "../outside.db"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    with pytest.raises(BackupError, match="invalid database filename"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_create_backup_refuses_if_destination_already_exists(tmp_path, db_path, identity_dir):
    destination = tmp_path / "backup1"
    destination.mkdir()

    with pytest.raises(BackupError, match="already exists"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)


def test_create_backup_refuses_if_database_missing(tmp_path, identity_dir):
    with pytest.raises(BackupError, match="no database found"):
        create_backup(db_path=tmp_path / "missing.db", identity_dir=identity_dir, destination=tmp_path / "backup1")


def test_create_backup_tolerates_no_identity_files_or_extras(tmp_path, db_path, identity_dir):
    """A brand-new node that has never uploaded a file, never had its
    welcome banner or main-menu masthead customized, or (implausibly,
    but not this module's job to assume otherwise) has no identity
    directory yet should still back up cleanly -- every artifact past
    the database is optional."""
    destination = tmp_path / "backup1"

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    assert (destination / "netbbs.db").exists()
    assert not (destination / "files").exists()
    assert not (destination / "identity").exists()


def test_create_backup_records_last_backup_state(tmp_path, db_path, identity_dir):
    destination = tmp_path / "backup1"
    assert get_last_backup_summary(Database(db_path)) == (None, None)

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    checked_at, path = get_last_backup_summary(Database(db_path))
    assert checked_at is not None
    assert path == str(destination)


def test_create_backup_rolls_back_both_summary_fields_if_either_write_fails(
    tmp_path, db_path, identity_dir,
):
    db = Database(db_path)
    db.connection.execute(
        """
        CREATE TRIGGER fail_last_backup_path
        BEFORE INSERT ON node_config
        WHEN NEW.key = 'last_backup_path'
        BEGIN
            SELECT RAISE(ABORT, 'simulated summary write failure');
        END
        """
    )
    db.connection.commit()
    db.close()

    create_backup(
        db_path=db_path,
        identity_dir=identity_dir,
        destination=tmp_path / "backup1",
    )

    db = Database(db_path)
    try:
        assert get_last_backup_summary(db) == (None, None)
    finally:
        db.close()


def test_create_backup_appends_to_operational_run_history(tmp_path, db_path, identity_dir):
    # Dogfood follow-up: get_last_backup_summary only ever tracks the
    # single most recent point in time -- a SysOp couldn't tell "this
    # runs on a healthy schedule" from "it happened to succeed once".
    from netbbs.operational_history import list_operational_run_history

    create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup1")
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup2")

    history = list_operational_run_history(Database(db_path), "backup")
    assert [r.outcome for r in history] == ["succeeded", "succeeded"]
    assert history[0].detail == str(tmp_path / "backup2")
    assert history[1].detail == str(tmp_path / "backup1")


# -- restore_backup ---------------------------------------------------------


def test_restore_backup_round_trip(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    # A marker written *before* the snapshot -- proves the database
    # itself round-trips, distinct from create_backup's own last-backup
    # bookkeeping (netbbs.config), which is written to the live node
    # *after* the snapshot is taken and so is never itself present
    # inside the backup it describes.
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO node_config (key, value) VALUES ('marker', 'present-before-backup')")
    conn.commit()
    conn.close()
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    # Simulate data loss: wipe every ordinary artifact.
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM node_config")
    conn.commit()
    conn.close()

    shutil.rmtree(_storage_root(db_path))
    shutil.rmtree(identity_dir)
    _ssh_host_key_path(db_path).unlink()
    _managed_dns_credential_path(db_path).unlink()
    _managed_dns_previous_credential_path(db_path).unlink()
    _welcome_banner_path(db_path).unlink()
    _main_menu_banner_path(db_path).unlink()
    _logoff_banner_path(db_path).unlink()
    _new_account_banner_before_path(db_path).unlink()
    _new_account_banner_after_path(db_path).unlink()
    _board_list_banner_path(db_path).unlink()
    _file_area_banner_path(db_path).unlink()
    _chat_channel_picker_banner_path(db_path).unlink()

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    assert (_storage_root(db_path) / _BLOB_HASH[:2] / _BLOB_HASH).read_bytes() == b"blob content"
    assert (identity_dir / "root.identity").exists()
    assert _ssh_host_key_path(db_path).read_bytes() == b"fake ssh host key"
    assert _managed_dns_credential_path(db_path).read_text() == "fake managed-dns credential"
    assert _managed_dns_previous_credential_path(db_path).read_text() == "fake previous managed-dns credential"
    assert _welcome_banner_path(db_path).read_text() == "fake banner"
    assert _main_menu_banner_path(db_path).read_text() == "fake masthead"
    assert _logoff_banner_path(db_path).read_text() == "fake logoff banner"
    assert _new_account_banner_before_path(db_path).read_text() == "fake before banner"
    assert _new_account_banner_after_path(db_path).read_text() == "fake after banner"
    assert _board_list_banner_path(db_path).read_text() == "fake board masthead"
    assert _file_area_banner_path(db_path).read_text() == "fake file area masthead"
    assert _chat_channel_picker_banner_path(db_path).read_text() == "fake channel masthead"
    conn = sqlite3.connect(str(db_path))
    marker = conn.execute("SELECT value FROM node_config WHERE key = 'marker'").fetchone()
    conn.close()
    assert marker == ("present-before-backup",)


def test_restore_removes_newer_managed_dns_credentials_absent_from_backup(
    tmp_path, db_path, identity_dir,
):
    _seed_full_node(db_path, identity_dir)
    primary = _managed_dns_credential_path(db_path)
    previous = _managed_dns_previous_credential_path(db_path)
    primary.unlink()
    previous.unlink()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup1")
    transition = _managed_dns_transition_credential_path(db_path)
    primary.write_text("post-backup primary secret")
    previous.write_text("post-backup previous secret")
    transition.write_text("post-backup staged secrets")

    rollback = restore_backup(source=source, db_path=db_path, identity_dir=identity_dir)

    assert not primary.exists()
    assert not previous.exists()
    assert not transition.exists()
    assert rollback is not None
    assert (rollback / primary.name).read_text() == "post-backup primary secret"
    assert (rollback / previous.name).read_text() == "post-backup previous secret"
    assert (rollback / transition.name).read_text() == "post-backup staged secrets"


def test_restore_backup_onto_a_fresh_target_with_nothing_existing_yet(tmp_path, db_path, identity_dir):
    """Restoring into a brand-new location -- no prior files/identity
    directory at all -- must not assume there's anything there to
    remove first."""
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    fresh_db_path = tmp_path / "restored" / "netbbs.db"
    fresh_identity_dir = tmp_path / "restored_identity"
    fresh_db_path.parent.mkdir()

    restore_backup(source=destination, db_path=fresh_db_path, identity_dir=fresh_identity_dir)

    assert fresh_db_path.exists()
    assert (_storage_root(fresh_db_path) / _BLOB_HASH[:2] / _BLOB_HASH).exists()
    assert (fresh_identity_dir / "root.identity").exists()


def test_restore_backup_refuses_without_a_manifest(tmp_path, db_path, identity_dir):
    not_a_backup = tmp_path / "not-a-backup"
    not_a_backup.mkdir()

    with pytest.raises(BackupError, match="not a backup directory"):
        restore_backup(source=not_a_backup, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_refuses_if_the_target_database_is_in_use(tmp_path, db_path, identity_dir):
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    holder = sqlite3.connect(str(db_path), timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(BackupError, match="appears to be in use"):
            restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_restore_backup_succeeds_once_the_holder_releases_the_lock(tmp_path, db_path, identity_dir):
    """Confirms the precondition check isn't just permanently tripped
    by the backup/restore process's own prior connections -- it
    reflects real, current lock state."""
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    holder = sqlite3.connect(str(db_path), timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("ROLLBACK")
    holder.close()

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)  # must not raise


# -- staged/validated restore (design doc §13.10, issue #75) ----------------


def test_restore_backup_validates_before_touching_any_live_path(tmp_path, db_path, identity_dir):
    """A corrupt backup must be refused before a single live artifact
    is overwritten -- not partway through, and not after."""
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    # Corrupt the database snapshot after the fact -- a truncated file,
    # not a well-formed-but-tampered one, so PRAGMA integrity_check
    # itself (not just the checksum) has something real to catch too.
    (destination / "netbbs.db").write_bytes(b"not a real sqlite file")

    live_db_bytes_before = db_path.read_bytes()

    with pytest.raises(BackupError):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    assert db_path.read_bytes() == live_db_bytes_before  # untouched


def test_restore_backup_refuses_on_checksum_mismatch(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    # Tamper with the SSH host key after the manifest recorded its
    # checksum -- a well-formed file, just not the one the manifest
    # says it should be.
    (destination / "netbbs_ssh_host_key").write_bytes(b"tampered")

    with pytest.raises(BackupError, match="checksum mismatch"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_refuses_on_a_corrupted_blob(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    blob_in_backup = destination / "files" / _BLOB_HASH[:2] / _BLOB_HASH
    blob_in_backup.write_bytes(b"corrupted content, wrong hash now")

    with pytest.raises(BackupError, match="does not match its own content hash"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_refuses_on_missing_checksummed_file(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    (destination / "identity" / "signing.identity").unlink()

    with pytest.raises(BackupError, match="missing"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_refuses_if_identity_does_not_load_cleanly(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    # Swap the root identity file's bytes for the signing key's --
    # still a file that "exists" and (if checksums didn't already catch
    # it) wouldn't parse/verify as the right key, so this also proves
    # the identity check is a real functional load, not just presence.
    # Recompute the checksum too, isolating this test to the identity-
    # load check specifically rather than tripping the checksum check
    # first.
    swapped = (destination / "identity" / "signing.identity").read_bytes()
    (destination / "identity" / "root.identity").write_bytes(swapped)
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["checksums"]["identity/root.identity"] = hashlib.sha256(swapped).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2))

    with pytest.raises(BackupError, match="node identity does not load cleanly"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_refuses_a_snapshot_from_a_newer_schema_version(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    db_snapshot = destination / "netbbs.db"
    conn = sqlite3.connect(str(db_snapshot))
    conn.execute("PRAGMA user_version = 999999")
    conn.commit()
    conn.close()
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["checksums"]["netbbs.db"] = backup_module._sha256_of_file(db_snapshot)
    manifest_path.write_text(json.dumps(manifest, indent=2))

    with pytest.raises(BackupError, match="newer than this NetBBS build supports"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


def test_restore_backup_source_directory_is_never_mutated_by_validation(tmp_path, db_path, identity_dir):
    """A backup must stay byte-identical across repeated restores --
    validating it must never itself apply a schema migration to the
    original snapshot (only to the disposable staged copy)."""
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    db_snapshot_bytes_before = (destination / "netbbs.db").read_bytes()

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    assert (destination / "netbbs.db").read_bytes() == db_snapshot_bytes_before


def test_restore_backup_does_not_delete_the_rollback_generation(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO node_config (key, value) VALUES ('marker', 'pre-restore-generation')")
    conn.commit()
    conn.close()

    rollback_dir = restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    assert rollback_dir is not None
    assert rollback_dir.exists()
    conn = sqlite3.connect(str(rollback_dir / "db"))
    marker = conn.execute("SELECT value FROM node_config WHERE key = 'marker'").fetchone()
    conn.close()
    assert marker == ("pre-restore-generation",)


def test_restore_backup_returns_none_when_nothing_was_live_to_preserve(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    fresh_db_path = tmp_path / "restored" / "netbbs.db"
    fresh_identity_dir = tmp_path / "restored_identity"
    fresh_db_path.parent.mkdir()

    rollback_dir = restore_backup(source=destination, db_path=fresh_db_path, identity_dir=fresh_identity_dir)

    assert rollback_dir is None


def test_restore_rebases_managed_dns_credential_to_a_different_database_stem(
    tmp_path, db_path, identity_dir,
):
    _seed_full_node(db_path, identity_dir)
    source = create_backup(
        db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "portable-backup"
    )
    target_db = tmp_path / "restored" / "renamed.db"
    target_identity = tmp_path / "restored-identity"

    restore_backup(source=source, db_path=target_db, identity_dir=target_identity)

    assert _managed_dns_credential_path(target_db).read_text() == "fake managed-dns credential"
    assert _managed_dns_previous_credential_path(target_db).read_text() == "fake previous managed-dns credential"
    assert not (target_db.parent / f"{db_path.stem}_managed_dns_credential").exists()
    assert not (target_db.parent / f"{db_path.stem}_managed_dns_previous_credential").exists()


def test_restore_backup_no_staging_or_state_files_left_behind_on_success(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    remaining = {p.name for p in db_path.parent.iterdir()}
    assert not any(name.startswith(".netbbs-restore-staging-") for name in remaining)
    assert ".netbbs-restore-state.json" not in remaining


def test_restore_backup_recovers_the_previous_generation_when_a_switch_step_fails(
    tmp_path, db_path, identity_dir, monkeypatch
):
    """Simulates an interruption partway through the switch phase (the
    third artifact fails) and confirms everything already switched is
    rolled back automatically -- the live node ends up exactly as it
    was before the restore was attempted, not a mixture."""
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO node_config (key, value) VALUES ('marker', 'original-before-failed-restore')")
    conn.commit()
    conn.close()
    original_identity_root_bytes = (identity_dir / "root.identity").read_bytes()

    real_switch_one = backup_module._switch_one
    call_count = 0

    def _flaky_switch_one(name, staged_path, live_path, rollback_dir):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise OSError("simulated interruption")
        real_switch_one(name, staged_path, live_path, rollback_dir)

    monkeypatch.setattr(backup_module, "_switch_one", _flaky_switch_one)

    with pytest.raises(BackupError, match="automatically rolled back"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)

    # The live node is back to exactly its pre-restore state.
    conn = sqlite3.connect(str(db_path))
    marker = conn.execute("SELECT value FROM node_config WHERE key = 'marker'").fetchone()
    conn.close()
    assert marker == ("original-before-failed-restore",)
    assert (identity_dir / "root.identity").read_bytes() == original_identity_root_bytes

    # No leftover state file -- the rollback fully recovered, so the
    # marker is cleared, not left as a stuck "restore in progress" sign.
    assert not (db_path.parent / ".netbbs-restore-state.json").exists()


def test_restore_backup_refuses_a_second_restore_over_an_unresolved_state_file(tmp_path, db_path, identity_dir):
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    state_path = db_path.parent / ".netbbs-restore-state.json"
    state_path.write_text(json.dumps({"started_at": "2026-01-01T00:00:00Z", "pending_artifacts": ["db"]}))

    with pytest.raises(BackupError, match="did not complete cleanly"):
        restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)


# -- PID-file liveness check (design doc §13.10, issue #75) -----------------


def test_restore_backup_refuses_while_the_pid_file_names_a_live_process(tmp_path, db_path, identity_dir):
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    write_pid_file(db_path)  # writes this test process's own PID -- genuinely alive
    try:
        with pytest.raises(BackupError, match="appears to still be running"):
            restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)
    finally:
        remove_pid_file(db_path)


def test_restore_backup_tolerates_a_stale_pid_file(tmp_path, db_path, identity_dir):
    """A PID file naming a process that no longer exists (crash, kill
    -9, power loss -- anything that skipped the normal remove_pid_file
    cleanup) must not permanently block restore."""
    destination = tmp_path / "backup1"
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)

    # An implausibly large PID essentially guaranteed not to be a real,
    # currently-running process on any platform this runs on.
    (db_path.parent / f"{db_path.stem}.pid").write_text("999999999")

    restore_backup(source=destination, db_path=db_path, identity_dir=identity_dir)  # must not raise


def test_write_and_remove_pid_file_round_trip(tmp_path, db_path):
    pid_path = db_path.parent / f"{db_path.stem}.pid"
    assert not pid_path.exists()

    write_pid_file(db_path)
    assert pid_path.exists()

    remove_pid_file(db_path)
    assert not pid_path.exists()

    remove_pid_file(db_path)  # must not raise if already gone


# -- CLI ----------------------------------------------------------------


def test_cli_create_then_restore_round_trip(tmp_path, capsys):
    db_path = tmp_path / "netbbs.db"
    identity_dir = tmp_path / "netbbs_identity"
    Database(db_path).close()
    _seed_full_node(db_path, identity_dir)
    destination = tmp_path / "backup1"

    main(["create", "--db", str(db_path), "--identity-dir", str(identity_dir), "--to", str(destination)])
    assert "Backup created" in capsys.readouterr().out
    assert (destination / "manifest.json").exists()

    main(["restore", "--from", str(destination), "--db", str(db_path), "--identity-dir", str(identity_dir)])
    assert "Restored" in capsys.readouterr().out


def test_cli_create_exits_cleanly_on_failure(tmp_path, capsys):
    with pytest.raises(SystemExit, match="backup failed"):
        main(["create", "--db", str(tmp_path / "missing.db"), "--to", str(tmp_path / "backup1")])


def _populate_war_dialer(db_path, *, world_path=None, owner="a" * 32):
    from netbbs.doors.bundled import war_dialer as wd
    from netbbs.auth.users import create_user
    from netbbs.config import set_config
    node = Database(db_path)
    try:
        user = create_user(node, "WarPilot", password="hunter2", user_level=10)
        set_config(node, "war_dialer_owner", owner)
    finally:
        node.close()
    path = world_path or db_path.parent / (db_path.name + ".doors") / "war-dialer.db"
    conn = wd.connect(path)
    wd.ensure_schema(conn)
    wd.bind_world_owner(conn, owner)
    now = wd.now_utc()
    wd.get_or_create_season_anchor(conn, now)
    wd.ensure_exchanges_seeded(conn, 1, now)
    wd.load_or_create_player(conn, user.id, user.username, now, 1)
    conn.execute("UPDATE players SET cash=4321, income_remainder=9876")
    wd.record_event(conn, user.id, "Rival", "A retained receipt", now)
    conn.close()
    return path


def _war_cash(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT cash FROM players").fetchone()[0]


def test_war_dialer_backup_round_trip_includes_committed_wal(tmp_path, db_path, identity_dir):
    from netbbs.doors.bundled import war_dialer as wd
    path = _populate_war_dialer(db_path)
    held = wd.connect(path)
    held.execute("PRAGMA wal_autocheckpoint=0")
    held.execute("UPDATE players SET cash=98765, specialty='fixers', support='stash', operation_contract=4, operation_approach=2, operation_stage=2, successful_operations=3")
    held.execute("INSERT INTO recon VALUES (1,2,'Historical Rival',1234,7,'2026-09-10T00:00:00+00:00','2026-09-11T00:00:00+00:00',1)")
    try:
        assert Path(str(path) + "-wal").stat().st_size > 0
        source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    finally:
        held.close()
    manifest = json.loads((source / "manifest.json").read_text())
    assert manifest["war_dialer"]["worlds"][0]["source_path"] == str(path.resolve())
    archived = source / "war-dialer/1.db"
    assert _war_cash(archived) == 98765
    assert manifest["checksums"]["war-dialer/1.db"] == hashlib.sha256(archived.read_bytes()).hexdigest()
    with pytest.raises(BackupError, match="--war-dialer-to"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir)
    target = tmp_path / "restored-world.db"
    restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, war_dialer_to=target)
    assert _war_cash(target) == 98765
    with contextlib.closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT income_remainder FROM players").fetchone()[0] == 9876
        assert conn.execute("SELECT specialty,support FROM players").fetchone() == ("fixers", "stash")
        assert conn.execute("SELECT role FROM exchanges ORDER BY id LIMIT 1").fetchone() == ("carrier",)
        assert conn.execute("SELECT operation_contract,operation_approach,operation_stage,successful_operations FROM players").fetchone() == (4, 2, 2, 3)
        assert conn.execute("SELECT cash,crew FROM recon WHERE viewer=1 AND target=2").fetchone() == (1234, 7)
        assert conn.execute("SELECT summary_text FROM events").fetchone()[0] == "A retained receipt"
    backup_module._validate_backup_source(source, allow_migrate=False)


def test_war_dialer_backup_and_restore_exclude_idle_sessions(tmp_path, db_path, identity_dir):
    from netbbs.doors.bundled import war_dialer as wd
    path = _populate_war_dialer(db_path)
    destination = tmp_path / "backup"
    with wd.world_session(path):
        with pytest.raises(BackupError, match="busy"):
            create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)
    assert not destination.exists()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=destination)
    before = path.read_bytes()
    with wd.world_session(path):
        with pytest.raises(BackupError, match="busy"):
            restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, war_dialer_to=path)
    assert path.read_bytes() == before
    with wd.world_session(path, maintenance=True):
        with pytest.raises(wd.WorldStateError, match="busy"):
            with wd.world_session(path):
                pytest.fail("play entered maintenance")
    with wd.world_session(path), wd.world_session(path):
        pass  # Duplicate player sessions remain supported.


def test_war_dialer_restore_failure_rolls_back_world_and_node(tmp_path, db_path, identity_dir, monkeypatch):
    path = _populate_war_dialer(db_path)
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE players SET cash=2468")
        conn.commit()
    node = Database(db_path)
    node.connection.execute("INSERT INTO node_config (key, value) VALUES ('after_backup', 'retained')")
    node.connection.commit()
    node.close()
    original = backup_module._write_restore_state
    def fail_after_world(*args, **kwargs):
        if not kwargs["pending"]:
            raise OSError("injected final journal failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(backup_module, "_write_restore_state", fail_after_world)
    with pytest.raises(BackupError, match="automatically rolled back"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, war_dialer_to=path)
    assert _war_cash(path) == 2468
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT value FROM node_config WHERE key='after_backup'").fetchone()[0] == "retained"
    assert not (db_path.parent / ".netbbs-restore-state.json").exists()


def test_war_dialer_restore_rejects_corruption_and_wrong_node(tmp_path, db_path, identity_dir):
    path = _populate_war_dialer(db_path)
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    other = tmp_path / "other-node.db"
    Database(other).close()
    target = _populate_war_dialer(other, owner="b" * 32)
    before = target.read_bytes()
    with pytest.raises(BackupError, match="user-ID namespace"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, war_dialer_to=target)
    assert target.read_bytes() == before
    with (source / "war-dialer/1.db").open("ab") as handle:
        handle.write(b"modified backup")
    with pytest.raises(BackupError, match="checksum mismatch"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, war_dialer_to=path)
    assert _war_cash(path) == 4321


def test_war_dialer_profiles_capture_all_worlds_and_require_all_destinations(tmp_path, db_path, identity_dir):
    import sys
    from netbbs.doors import create_door
    from netbbs.doors.profiles import DoorProfile
    from netbbs.auth.users import get_user_by_username
    first = _populate_war_dialer(db_path)
    second = tmp_path / "profile-world.db"
    shutil.copy2(first, second)
    node = Database(db_path)
    try:
        create_door(node, "Alternate world", sys.executable, creator=get_user_by_username(node, "WarPilot"),
                    profile=DoorProfile(environment={"WAR_DIALER_DB_PATH": str(second)}))
    finally:
        node.close()
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest = json.loads((source / "manifest.json").read_text())
    assert len(manifest["war_dialer"]["worlds"]) == 2
    with pytest.raises(BackupError, match="every archived world"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, war_dialer_to={"1": tmp_path / "one.db"})
    targets = {str(i): tmp_path / f"restored-{i}.db" for i in (1, 2)}
    restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, war_dialer_to=targets)
    assert [_war_cash(target) for target in targets.values()] == [4321, 4321]



@pytest.mark.parametrize("damage", ["future", "unbound", "missing_checksum"])
def test_war_dialer_bad_archive_is_refused_before_live_changes(tmp_path, db_path, identity_dir, damage):
    path = _populate_war_dialer(db_path)
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    archive = source / "war-dialer/1.db"
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if damage == "missing_checksum":
        del manifest["checksums"]["war-dialer/1.db"]
    else:
        with contextlib.closing(sqlite3.connect(archive)) as conn:
            if damage == "future":
                conn.execute("PRAGMA user_version=99")
            else:
                conn.execute("DELETE FROM meta WHERE key='node_owner'")
            conn.commit()
        manifest["checksums"]["war-dialer/1.db"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    before = path.read_bytes()
    with pytest.raises(BackupError):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir, war_dialer_to=path)
    assert path.read_bytes() == before


def test_war_dialer_cli_create_and_restore_names_destinations(tmp_path, db_path, identity_dir, capsys):
    _populate_war_dialer(db_path)
    destination = tmp_path / "backup"
    main(["create", "--db", str(db_path), "--identity-dir", str(identity_dir), "--to", str(destination)])
    assert "War Dialer world 1: included" in capsys.readouterr().out
    target = tmp_path / "restored.db"
    main(["restore", "--db", str(db_path), "--identity-dir", str(identity_dir), "--from", str(destination),
          "--war-dialer-to", "1=" + str(target)])
    assert _war_cash(target) == 4321
    assert "MANUAL" in capsys.readouterr().out



@pytest.mark.parametrize("reset", [False, True])
def test_war_dialer_sysop_competition_change_has_backup_and_preserves_identity(tmp_path, db_path, identity_dir, reset):
    from netbbs.doors import war_dialer_admin as admin
    bootstrap_node_identity("test-node").save(identity_dir)
    (identity_dir / "operator-marker").write_bytes(b"retain identity contents")
    path = _populate_war_dialer(db_path)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        before_identity = conn.execute("SELECT user_id,handle,created_at FROM players").fetchall()
        conn.execute("UPDATE players SET crew=40, crew_recruited_total=40, turns_used=8, specialty='lookouts', support='burner', operation_contract=3, operation_approach=1, operation_stage=2, successful_operations=4")
        conn.execute("UPDATE exchanges SET controller_user_id=1, garrison=30")
        conn.execute("INSERT INTO recon VALUES (1,2,'Historical Rival',1234,7,'2026-09-10T00:00:00+00:00','2026-09-11T00:00:00+00:00',1)")
        conn.commit()
    admin.set_maintenance(db_path, path, True)
    destination = tmp_path / "before-reset"
    result = admin.change_competition(db_path, path, identity_dir=identity_dir, backup_to=destination,
                                     confirm=path.name, reason="operator-selected fresh competition", reset=reset)
    assert result["maintenance"] == "on"
    assert result["stored_season"] == "2"
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT user_id,handle,created_at FROM players").fetchall() == before_identity
        cash, crew, turns, rank_count = conn.execute("SELECT cash,crew,turns_used,crew_recruited_total FROM players").fetchone()
        assert (cash, crew, turns, rank_count) == (300, 3, 0, 0)
        assert conn.execute("SELECT specialty,support FROM players").fetchone() == ("", "")
        assert conn.execute("SELECT operation_stage,successful_operations FROM players").fetchone() == (0, 0)
        assert conn.execute("SELECT role FROM exchanges ORDER BY id LIMIT 1").fetchone() == ("carrier",)
        assert conn.execute("SELECT COUNT(*) FROM recon").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM exchanges WHERE controller_user_id IS NOT NULL").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == (0 if reset else 1)
    audit = result["recent_operations"][-1]
    assert audit["action"] == ("reset competition" if reset else "advance season")
    assert audit["manifest_sha256"] == hashlib.sha256((destination / "manifest.json").read_bytes()).hexdigest()
    assert _war_cash(destination / "war-dialer/1.db") == 4321
    assert (destination / "identity/operator-marker").read_bytes() == b"retain identity contents"
    admin.set_maintenance(db_path, path, False)
    assert admin.world_status(db_path, path)["maintenance"] == "off"


@pytest.mark.parametrize("blocker", ["confirmation", "maintenance", "active", "backup", "running_node"])
def test_war_dialer_sysop_change_rejects_before_reset(tmp_path, db_path, identity_dir, blocker):
    from netbbs.doors import war_dialer_admin as admin
    from netbbs.doors.bundled import war_dialer as wd
    bootstrap_node_identity("test-node").save(identity_dir)
    path = _populate_war_dialer(db_path)
    if blocker != "maintenance":
        admin.set_maintenance(db_path, path, True)
    destination = tmp_path / "backup"
    if blocker == "backup":
        destination.mkdir()
    if blocker == "running_node":
        write_pid_file(db_path)
    with wd.world_session(path) if blocker == "active" else contextlib.nullcontext():
        with pytest.raises(BackupError):
            admin.change_competition(db_path, path, identity_dir=identity_dir, backup_to=destination,
                                     confirm="wrong" if blocker == "confirmation" else path.name, reason="test", reset=True)
    assert _war_cash(path) == 4321
    assert admin.world_status(db_path, path)["events"] == 1


def test_war_dialer_sysop_failure_rolls_back_reset_and_audit(tmp_path, db_path, identity_dir, monkeypatch):
    from netbbs.doors import war_dialer_admin as admin
    bootstrap_node_identity("test-node").save(identity_dir)
    path = _populate_war_dialer(db_path)
    admin.set_maintenance(db_path, path, True)
    before = admin.world_status(db_path, path)
    def fail_audit(*args, **kwargs):
        raise OSError("audit write failed")
    monkeypatch.setattr(admin, "_audit", fail_audit)
    with pytest.raises(BackupError, match="audit write failed"):
        admin.change_competition(db_path, path, identity_dir=identity_dir, backup_to=tmp_path / "backup",
                                 confirm=path.name, reason="test", reset=True)
    assert _war_cash(path) == 4321
    assert admin.world_status(db_path, path) == before


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_war_dialer_reset_requires_existing_identity_directory(tmp_path, db_path, kind):
    from netbbs.doors import war_dialer_admin as admin
    path = _populate_war_dialer(db_path)
    admin.set_maintenance(db_path, path, True)
    before = admin.world_status(db_path, path)
    identity = tmp_path / "mistyped-identity"
    if kind == "file":
        identity.write_text("not a directory")
    destination = tmp_path / "pre-reset"
    with pytest.raises(BackupError, match="identity directory"):
        admin.change_competition(db_path, path, identity_dir=identity, backup_to=destination,
                                 confirm=path.name, reason="test", reset=True)
    assert not destination.exists()
    assert _war_cash(path) == 4321
    assert admin.world_status(db_path, path) == before


def test_war_dialer_status_is_read_only_and_cli_is_bounded(tmp_path, db_path, identity_dir, capsys):
    from netbbs.doors import war_dialer_admin as admin
    path = _populate_war_dialer(db_path)
    before = path.read_bytes()
    assert not Path(str(path) + ".sessions").exists()
    admin.main(["--db", str(db_path), "--world", str(path), "status"])
    assert "maintenance: off" in capsys.readouterr().out
    assert path.read_bytes() == before
    assert not Path(str(path) + ".sessions").exists()
    missing = tmp_path / "missing.db"
    with pytest.raises(SystemExit, match="operation failed"):
        admin.main(["--db", str(db_path), "--world", str(missing), "status"])
    assert not missing.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal", ".sessions"])
@pytest.mark.parametrize("reverse", [False, True])
def test_war_dialer_restore_rejects_cross_world_sidecar_collisions(tmp_path, db_path, identity_dir, suffix, reverse):
    _populate_war_dialer(db_path)
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    shutil.copy2(source / "war-dialer/1.db", source / "war-dialer/2.db")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["war_dialer"]["worlds"].append({**manifest["war_dialer"]["worlds"][0], "key": "2"})
    manifest["checksums"]["war-dialer/2.db"] = manifest["checksums"]["war-dialer/1.db"]
    manifest_path.write_text(json.dumps(manifest))
    target = tmp_path / "target.db"
    paths = [target, Path(str(target) + suffix)]
    if reverse:
        paths.reverse()
    with pytest.raises(BackupError, match="overlaps"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir,
                       war_dialer_to={"1": paths[0], "2": paths[1]})
    assert not target.exists()


def test_empty_war_dialer_override_uses_normal_backup_error(tmp_path, db_path, identity_dir, monkeypatch):
    import sys
    from netbbs.doors import create_door
    from netbbs.doors.bundled import war_dialer as wd
    from netbbs.auth.users import get_user_by_username
    _populate_war_dialer(db_path)
    node = Database(db_path)
    try:
        create_door(node, "War Dialer", sys.executable, args=(wd.__file__,),
                    creator=get_user_by_username(node, "WarPilot"))
    finally:
        node.close()
    monkeypatch.setenv("WAR_DIALER_DB_PATH", "")
    with pytest.raises(BackupError, match="WAR_DIALER_DB_PATH"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    with pytest.raises(SystemExit, match="backup failed"):
        main(["create", "--db", str(db_path), "--identity-dir", str(identity_dir), "--to", str(tmp_path / "cli")])


def test_legacy_restore_refuses_to_orphan_an_existing_world(tmp_path, db_path, identity_dir):
    source = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "legacy")
    world = _populate_war_dialer(db_path)
    before = world.read_bytes()
    with pytest.raises(BackupError, match="does not cover existing War Dialer"):
        restore_backup(source=source, db_path=db_path, identity_dir=identity_dir)
    assert world.read_bytes() == before
    assert backup_module._war_dialer_owner(db_path) == "a" * 32
