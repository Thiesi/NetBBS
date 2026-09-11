"""Opt-in backup of door installation directories.

Off by default, because those directories are operator-owned game
installations outside NetBBS's own state and can dwarf it.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from netbbs.auth.users import create_user
from netbbs.backup import (BackupError, create_backup, door_installs_included,
                           set_door_installs_included)
from netbbs.doors import create_door
from netbbs.doors.profiles import DoorProfile
from netbbs.storage.database import Database
# `isolated_voidrunner_backup_directory` is autouse: without it these
# tests capture this machine's real Voidrunner save directory.
from tests.test_backup import (db_path, identity_dir,  # noqa: F401
                              isolated_voidrunner_backup_directory)


def _install(tmp_path, name, *, files=(("scores.dat", "top: 42"),)):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in files:
        (directory / filename).write_text(content, encoding="utf-8")
    return directory


def _register(db_path, name, install_dir):
    db = Database(db_path)
    try:
        actor = create_user(db, name.lower() + "op", password="hunter2", user_level=255)
        create_door(db, name, sys.executable, args=(), creator=actor,
                    profile=DoorProfile(install_dir=str(install_dir)))
    finally:
        db.close()


def _manifest(backup):
    return json.loads((backup / "manifest.json").read_text(encoding="utf-8"))


def test_installations_are_not_backed_up_by_default(tmp_path, db_path, identity_dir):
    _register(db_path, "Game", _install(tmp_path, "game"))

    backup = create_backup(db_path=db_path, identity_dir=identity_dir,
                           destination=tmp_path / "backup")

    assert _manifest(backup)["door_installs"] is None
    assert not (backup / "door-installs").exists()


def test_the_setting_round_trips_and_defaults_off(db_path):
    db = Database(db_path)
    try:
        assert door_installs_included(db) is False
        set_door_installs_included(db, True)
        assert door_installs_included(db) is True
        set_door_installs_included(db, False)
        assert door_installs_included(db) is False
    finally:
        db.close()


def _enable(db_path):
    db = Database(db_path)
    try:
        set_door_installs_included(db, True)
    finally:
        db.close()


def test_opting_in_copies_each_installation_verbatim(tmp_path, db_path, identity_dir):
    first = _install(tmp_path, "game-one", files=(("scores.dat", "one"), ("readme.txt", "hello")))
    second = _install(tmp_path, "game-two", files=(("world.db", "two"),))
    _register(db_path, "One", first)
    _register(db_path, "Two", second)
    _enable(db_path)

    backup = create_backup(db_path=db_path, identity_dir=identity_dir,
                           destination=tmp_path / "backup")

    metadata = _manifest(backup)["door_installs"]
    assert metadata is not None and len(metadata["installations"]) == 2
    copied = sorted((backup / "door-installs").iterdir(), key=lambda p: p.name)
    contents = {path.name: path.read_text(encoding="utf-8")
                for directory in copied for path in directory.iterdir()}
    assert contents == {"scores.dat": "one", "readme.txt": "hello", "world.db": "two"}
    assert {entry["door_name"] for entry in metadata["installations"]} == {"One", "Two"}
    assert sum(entry["file_count"] for entry in metadata["installations"]) == 3
    assert all(entry["total_bytes"] > 0 for entry in metadata["installations"])


def test_a_directory_shared_by_two_doors_is_copied_once(tmp_path, db_path, identity_dir):
    shared = _install(tmp_path, "shared")
    _register(db_path, "First", shared)
    _register(db_path, "Second", shared)
    _enable(db_path)

    backup = create_backup(db_path=db_path, identity_dir=identity_dir,
                           destination=tmp_path / "backup")

    assert len(_manifest(backup)["door_installs"]["installations"]) == 1


def test_a_nested_installation_is_not_copied_twice(tmp_path, db_path, identity_dir):
    outer = _install(tmp_path, "outer")
    inner = _install(tmp_path, "outer/inner", files=(("inner.dat", "x"),))
    _register(db_path, "Outer", outer)
    _register(db_path, "Inner", inner)
    _enable(db_path)

    backup = create_backup(db_path=db_path, identity_dir=identity_dir,
                           destination=tmp_path / "backup")

    installations = _manifest(backup)["door_installs"]["installations"]
    assert len(installations) == 1 and installations[0]["source_path"] == str(outer.resolve())
    assert (backup / "door-installs" / "1" / "inner" / "inner.dat").exists()


def test_a_missing_installation_directory_fails_the_backup_by_name(tmp_path, db_path, identity_dir):
    """Silently omitting data the SysOp asked for would be worse than failing."""
    _register(db_path, "Gone", tmp_path / "never-created")
    _enable(db_path)

    with pytest.raises(BackupError, match="Gone"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")


def test_a_failed_capture_leaves_no_partial_backup(tmp_path, db_path, identity_dir):
    _register(db_path, "Gone", tmp_path / "never-created")
    _enable(db_path)

    with pytest.raises(BackupError):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    assert not (tmp_path / "backup").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_symlinks_are_copied_as_links_not_followed(tmp_path, db_path, identity_dir):
    """Following one would pull unrelated host data into the backup."""
    outside = tmp_path / "outside-secret"
    outside.mkdir()
    (outside / "keys").write_text("do not copy me", encoding="utf-8")
    directory = _install(tmp_path, "linky")
    (directory / "elsewhere").symlink_to(outside, target_is_directory=True)
    _register(db_path, "Linky", directory)
    _enable(db_path)

    backup = create_backup(db_path=db_path, identity_dir=identity_dir,
                           destination=tmp_path / "backup")

    link = backup / "door-installs" / "1" / "elsewhere"
    assert link.is_symlink(), "the link was followed and its target copied in"
    assert not (link / "keys").exists() or link.readlink() == outside


def test_a_database_named_door_installs_does_not_collide(tmp_path, identity_dir):
    """The same archive-name fallback voidrunner and war-dialer already get."""
    db_path = tmp_path / "door-installs"
    Database(db_path).close()
    _register(db_path, "Game", _install(tmp_path, "game"))
    _enable(db_path)

    backup = create_backup(db_path=db_path, identity_dir=identity_dir,
                           destination=tmp_path / "backup")

    manifest = _manifest(backup)
    assert manifest["database_filename"] == "netbbs.db", "the snapshot kept a colliding name"
    assert (backup / "door-installs").is_dir()
    assert (backup / "netbbs.db").is_file()
    assert manifest["door_installs"] is not None


def test_restore_does_not_write_installations_back(tmp_path, db_path, identity_dir):
    """They are capture-only; the archive keeps them for manual recovery."""
    from netbbs.backup import restore_backup

    _register(db_path, "Game", _install(tmp_path, "game", files=(("scores.dat", "keep me"),)))
    _enable(db_path)
    backup = create_backup(db_path=db_path, identity_dir=identity_dir,
                           destination=tmp_path / "backup")
    assert (backup / "door-installs").is_dir(), "precondition: the archive holds installations"

    target = tmp_path / "restored" / "netbbs.db"
    target.parent.mkdir()
    restore_backup(source=backup, db_path=target, identity_dir=tmp_path / "restored-identity")

    assert target.is_file(), "the node itself restored"
    assert not (target.parent / "door-installs").exists(), "capture-only data was written back"
    # Still present in the archive for the operator to copy back by hand.
    assert (backup / "door-installs" / "1" / "scores.dat").read_text(encoding="utf-8") == "keep me"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_a_dangling_link_in_an_installation_cannot_break_a_node_restore(
        tmp_path, db_path, identity_dir):
    """The regression this guards, and why the tree is excluded from staging.

    The archive stores symlinks as symlinks; restore stages the whole archive
    with copytree, which follows them by default. A link whose target is gone
    by recovery time -- the normal case on a fresh disaster-recovery host --
    would then abort a node restore over data restore does not even use.
    """
    from netbbs.backup import restore_backup

    directory = _install(tmp_path, "linky")
    (directory / "elsewhere").symlink_to(tmp_path / "target-that-will-vanish",
                                         target_is_directory=True)
    _register(db_path, "Linky", directory)
    _enable(db_path)
    backup = create_backup(db_path=db_path, identity_dir=identity_dir,
                           destination=tmp_path / "backup")
    assert (backup / "door-installs" / "1" / "elsewhere").is_symlink()

    target = tmp_path / "restored" / "netbbs.db"
    target.parent.mkdir()
    restore_backup(source=backup, db_path=target, identity_dir=tmp_path / "restored-identity")

    assert target.is_file()
