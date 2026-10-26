"""Lunar Lander's pilot records in a node backup (issue #1542).

The records are one SQLite file in the node's own `<db>.doors/lunar_lander/`.
A backup snapshots it through SQLite's backup API, checksums it, and a restore
puts it back beside the database it was taken with -- or, for an archive
without it, moves newer records aside, since they name user IDs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3

import pytest

from netbbs import backup as backup_module
from netbbs.backup import BackupError, create_backup, main, restore_backup
from netbbs.doors.bundled import lunar_lander as ll
from netbbs.doors.runtime import node_lunar_lander_dir
from netbbs.storage.database import Database
from tests.test_backup import db_path, identity_dir, isolated_voidrunner_backup_directory  # noqa: F401


def _records(db_path):
    return node_lunar_lander_dir(db_path) / "pilots.db"


def _land(db_path, user, handle, mission_index=1):
    """A ranked landing, saved through the door's own records code."""
    host = ll.Host(handle=handle, node="node-a", user=user)
    mission = ll.missions("2026-10-24")[mission_index]
    flight = mission.launch()
    flight.status = "landed"
    records = ll.Records(_records(db_path))
    try:
        assert records.save(host, mission, flight)
    finally:
        records.close()


def _pilots(path):
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return sorted(row[0] for row in conn.execute("SELECT handle FROM pilots"))


def test_records_round_trip_through_a_backup(tmp_path, db_path, identity_dir):
    _land(db_path, 1, "Armstrong")
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["lunar_lander"] == {"version": 1, "source_path": str(_records(db_path)), "schema_version": 2}
    snapshot = archive / "lunar-lander" / "pilots.db"
    assert manifest["checksums"]["lunar-lander/pilots.db"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()

    _land(db_path, 2, "Aldrin")
    rollback = restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    assert _pilots(_records(db_path)) == ["Armstrong"]
    # The generation it replaced is kept, not deleted.
    assert _pilots(rollback / "lunar-lander" / "pilots.db") == ["Aldrin", "Armstrong"]


def test_restore_onto_a_fresh_node_creates_the_records_directory(tmp_path, db_path, identity_dir):
    _land(db_path, 1, "Armstrong")
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    target = tmp_path / "elsewhere" / "node.db"
    target.parent.mkdir()
    restore_backup(source=archive, db_path=target, identity_dir=tmp_path / "elsewhere" / "identity")
    assert _pilots(_records(target)) == ["Armstrong"]


def test_a_node_without_records_backs_up_and_restores(tmp_path, db_path, identity_dir):
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["lunar_lander"] is None and not (archive / "lunar-lander").exists()
    restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    assert not node_lunar_lander_dir(db_path).exists()


def test_an_archive_without_the_component_restores_the_absence(tmp_path, db_path, identity_dir):
    """An archive made before Lunar Lander was bundled still restores, and the
    live records -- newer than its database -- move into the rollback set."""
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest = json.loads((archive / "manifest.json").read_text())
    del manifest["lunar_lander"]  # as an older build wrote it
    (archive / "manifest.json").write_text(json.dumps(manifest))
    _land(db_path, 1, "Armstrong")
    rollback = restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    assert not node_lunar_lander_dir(db_path).exists()
    assert _pilots(rollback / "lunar-lander" / "pilots.db") == ["Armstrong"]


def test_a_failed_switch_rolls_the_records_back(tmp_path, db_path, identity_dir, monkeypatch):
    _land(db_path, 1, "Armstrong")
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    _land(db_path, 2, "Aldrin")
    switch = backup_module._switch_one

    switched = []

    def fail_after_records(name, staged, live, rollback):
        # The artifact after the records fails, so theirs is undone by rollback.
        if "lunar-lander" in switched:
            raise OSError("disk went away")
        switch(name, staged, live, rollback)
        switched.append(name)

    monkeypatch.setattr(backup_module, "_switch_one", fail_after_records)
    with pytest.raises(BackupError, match="automatically rolled back"):
        restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    assert _pilots(_records(db_path)) == ["Aldrin", "Armstrong"]


@pytest.mark.parametrize("damage", ["checksum", "unlisted", "foreign", "missing"])
def test_a_damaged_component_is_refused_before_anything_changes(tmp_path, db_path, identity_dir, damage):
    _land(db_path, 1, "Armstrong")
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    snapshot = archive / "lunar-lander" / "pilots.db"
    if damage == "checksum":
        with sqlite3.connect(snapshot) as conn:
            conn.execute("UPDATE pilots SET handle = 'Impostor'")
        conn.close()
    elif damage == "unlisted":
        (archive / "lunar-lander" / "extra.db").write_bytes(b"")
    elif damage == "foreign":
        snapshot.unlink()
        with sqlite3.connect(snapshot) as conn:
            conn.execute("CREATE TABLE pilots (x)")
        conn.close()
    else:
        snapshot.unlink()
    _land(db_path, 2, "Aldrin")
    with pytest.raises(BackupError):
        restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    assert _pilots(_records(db_path)) == ["Aldrin", "Armstrong"]


def test_a_component_without_its_manifest_entry_is_refused(tmp_path, db_path, identity_dir):
    _land(db_path, 1, "Armstrong")
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest = json.loads((archive / "manifest.json").read_text())
    manifest["lunar_lander"] = None
    (archive / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupError, match="Lunar Lander component has no coverage manifest"):
        restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)


def test_backup_refuses_records_that_are_not_lunar_landers(tmp_path, db_path, identity_dir):
    records = _records(db_path)
    records.parent.mkdir(parents=True)
    with sqlite3.connect(records) as conn:
        conn.execute("CREATE TABLE something (x)")
    conn.close()
    with pytest.raises(BackupError, match="not a Lunar Lander records database"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


@pytest.mark.skipif(os.name == "nt", reason="creating symbolic links needs privileges on Windows")
@pytest.mark.parametrize("which", ["directory", "file"])
def test_backup_refuses_symlinked_records(tmp_path, db_path, identity_dir, which):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ll.Records(elsewhere / "pilots.db").close()
    directory = node_lunar_lander_dir(db_path)
    if which == "directory":
        directory.parent.mkdir(parents=True)
        directory.symlink_to(elsewhere, target_is_directory=True)
    else:
        directory.mkdir(parents=True)
        (directory / "pilots.db").symlink_to(elsewhere / "pilots.db")
    with pytest.raises(BackupError, match="Lunar Lander"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")


def test_backup_bounds_the_records_file(tmp_path, db_path, identity_dir, monkeypatch):
    _land(db_path, 1, "Armstrong")
    monkeypatch.setattr(backup_module, "_LUNAR_LANDER_MAX_BYTES", 1024)
    with pytest.raises(BackupError, match="limit"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_the_snapshot_is_consistent_while_a_game_holds_the_records_open(tmp_path, db_path, identity_dir):
    """The door commits a landing with `isolation_level=None` and keeps its
    connection open for the whole session; a backup taken meanwhile still
    reads one committed generation."""
    _land(db_path, 1, "Armstrong")
    game = ll.Records(_records(db_path))
    try:
        archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    finally:
        game.close()
    assert _pilots(archive / "lunar-lander" / "pilots.db") == ["Armstrong"]


def test_restore_refuses_records_a_game_is_writing(tmp_path, db_path, identity_dir):
    _land(db_path, 1, "Armstrong")
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    writer = sqlite3.connect(_records(db_path), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(BackupError, match="in use"):
            restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_a_backup_destination_inside_the_records_directory_is_refused(tmp_path, db_path, identity_dir):
    _land(db_path, 1, "Armstrong")
    with pytest.raises(BackupError, match="inside the Lunar Lander records directory"):
        create_backup(db_path=db_path, identity_dir=identity_dir,
                      destination=node_lunar_lander_dir(db_path) / "backup")


@pytest.mark.parametrize("filename", ["lunar-lander", "Lunar-Lander"])
def test_a_database_named_like_the_component_still_restores(tmp_path, identity_dir, filename):
    db_path = tmp_path / filename
    Database(db_path).close()
    _land(db_path, 1, "Armstrong")
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    assert json.loads((archive / "manifest.json").read_text())["database_filename"] == "netbbs.db"
    target = tmp_path / "restored" / "node.db"
    target.parent.mkdir()
    restore_backup(source=archive, db_path=target, identity_dir=tmp_path / "restored" / "identity")
    assert _pilots(_records(target)) == ["Armstrong"]


def test_the_cli_says_what_it_captured_and_restored(tmp_path, db_path, identity_dir, capsys):
    _land(db_path, 1, "Armstrong")
    archive = tmp_path / "backup"
    main(["create", "--db", str(db_path), "--identity-dir", str(identity_dir), "--to", str(archive)])
    assert "Lunar Lander: included pilot records" in capsys.readouterr().out
    main(["restore", "--from", str(archive), "--db", str(db_path), "--identity-dir", str(identity_dir)])
    assert "Lunar Lander pilot records restored to" in capsys.readouterr().out


def test_the_backup_screen_counts_records_as_door_data(db_path):
    assert not backup_module.door_data_left_behind(db_path)
    _land(db_path, 1, "Armstrong")
    assert backup_module.door_data_left_behind(db_path)


def test_restore_over_damaged_live_records_moves_them_aside(tmp_path, db_path, identity_dir):
    _land(db_path, 1, "Armstrong")
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    _records(db_path).write_bytes(b"not a database at all" * 100)
    rollback = restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    assert _pilots(_records(db_path)) == ["Armstrong"]
    assert (rollback / "lunar-lander" / "pilots.db").read_bytes().startswith(b"not a database")


# Schema 2 (issue #1561): the bundled Lunar Lander (v0.5.0 and later) writes
# achievements and programs beside the pilots and scores schema 1 had.
_SCHEMA_2_TABLES = ("counters", "achievements", "programs", "program_missions")


def _as_schema_1(path):
    """The file an older Lunar Lander kept: schema 2 without its additions."""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        for table in _SCHEMA_2_TABLES:
            conn.execute(f"DROP TABLE {table}")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()


def test_the_bundled_game_writes_schema_2(db_path):
    _land(db_path, 1, "Armstrong")
    assert backup_module._inspect_lunar_lander(_records(db_path)) == 2


def test_a_schema_1_backup_restores_and_the_game_upgrades_it(tmp_path, db_path, identity_dir):
    """A node backed up under an older Lunar Lander restores under this one."""
    _land(db_path, 1, "Armstrong")
    _as_schema_1(_records(db_path))
    archive = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    assert json.loads((archive / "manifest.json").read_text())["lunar_lander"]["schema_version"] == 1
    restore_backup(source=archive, db_path=db_path, identity_dir=identity_dir)
    assert backup_module._inspect_lunar_lander(_records(db_path)) == 1
    _land(db_path, 2, "Aldrin")  # The game's first open upgrades it in place.
    assert backup_module._inspect_lunar_lander(_records(db_path)) == 2
    assert _pilots(_records(db_path)) == ["Aldrin", "Armstrong"]


@pytest.mark.parametrize("damage", ["unknown version", "missing table", "schema 1 tables at version 2"])
def test_records_this_build_does_not_know_are_refused(tmp_path, db_path, identity_dir, damage):
    _land(db_path, 1, "Armstrong")
    path = _records(db_path)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        if damage == "unknown version":
            conn.execute("PRAGMA user_version = 3")
        elif damage == "missing table":
            conn.execute("DROP TABLE program_missions")
        else:
            for table in _SCHEMA_2_TABLES:
                conn.execute(f"DROP TABLE {table}")
        conn.commit()
    with pytest.raises(BackupError, match="not a Lunar Lander records database this build knows"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
