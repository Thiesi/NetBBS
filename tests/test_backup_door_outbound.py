"""Door outbound result receipts as a backup component (issue #556).

A receipt is what a door reads on its next launch to learn what became of
the post it asked for. They are written beside the node database, which is
what makes them capturable -- but being beside it was never what captured
them, and until this component existed a restored node came back with the
posts and without the outcomes.

What these tests hold down is the pairing, not the copy: a captured receipt
never names a post the snapshot taken beside it lacks -- for any post the node
still had, a deleted one being the node's own doing and preserved as such -- and
a restore never leaves a newer generation's receipts standing over an older
database.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from netbbs.auth.users import create_user
from netbbs.backup import BackupError, create_backup, restore_backup
from netbbs.boards.boards import create_board, delete_board, list_boards
from netbbs.doors import create_door
from netbbs.doors import outbound as outbound_module
from netbbs.link.node_identity import bootstrap_node_identity
from netbbs.doors.outbound import allow_target, drain, enable_outbound, results_dir, results_root
from netbbs.storage.database import Database
from netbbs import backup as backup_module
# `isolated_voidrunner_backup_directory` is autouse: without it these tests
# capture this machine's real Voidrunner save directory.
from tests.test_backup import (db_path, identity_dir,  # noqa: F401
                               isolated_voidrunner_backup_directory)


def _request(workdir, name, **payload):
    directory = workdir / "outbound"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def _run_a_door(db_path, workdir):
    """One real launch: a post that is accepted and one that is refused.

    The whole path, not a hand-written file: `drain` is what names a receipt,
    and a component that captured files this code never writes would prove
    nothing about a restored node.
    """
    _request(workdir, "chronicle", subject="Season 1", body="The Chronicle opens.")
    _request(workdir, "private", board="Private", subject="Season 1", body="Not allowed.")
    db = Database(db_path)
    try:
        sysop = create_user(db, "sysop", password="hunter2", user_level=255)
        board = create_board(db, "Chronicle", creator=sysop)
        door = create_door(db, "Blacksite", "/bin/true", creator=sysop)
        enable_outbound(db, door, enabled_by=sysop)
        allow_target(db, door, board, allowed_by=sysop)
        assert drain(db, door, workdir) == (1, 1)
        return door.id, {path.name: path.read_bytes()
                         for path in sorted(results_dir(db, door.id).iterdir())}
    finally:
        db.close()


def board_named(db, name):
    return next(board for board in list_boards(db) if board.name == name)


def db_user(db, username):
    from netbbs.auth.users import get_user_by_username

    return get_user_by_username(db, username)


def _manifest(backup):
    return json.loads((backup / "manifest.json").read_text(encoding="utf-8"))


def _live_receipts(db_path):
    root = results_root(db_path)
    if not root.is_dir():
        return None
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _door_directory(db_path, door_id):
    return results_root(db_path) / str(door_id)


def test_receipts_are_captured_and_restored_with_their_own_generation(tmp_path, db_path, identity_dir):
    door_id, receipts = _run_a_door(db_path, tmp_path / "launch-one")
    assert len(receipts) == 2
    statuses = {json.loads(raw)["status"] for raw in receipts.values()}
    assert statuses == {"posted", "rejected"}

    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    metadata = _manifest(backup)["door_outbound"]
    assert metadata["version"] == 1
    assert [door["key"] for door in metadata["doors"]] == [str(door_id)]
    assert sorted(metadata["doors"][0]["receipts"]) == sorted(receipts)
    captured = backup / "door-outbound" / str(door_id)
    assert {path.name: path.read_bytes() for path in captured.iterdir()} == receipts
    for name in receipts:
        assert f"door-outbound/{door_id}/{name}" in _manifest(backup)["checksums"]

    # A generation the archive knows nothing about, standing where a door
    # would read it.
    stale = _door_directory(db_path, door_id) / "launch-later.chronicle.result.json"
    stale.write_text(json.dumps({"status": "posted", "post_id": "never-happened"}), encoding="utf-8")

    rollback = restore_backup(source=backup, db_path=db_path, identity_dir=identity_dir)

    assert _live_receipts(db_path) == {f"{door_id}/{name}": raw for name, raw in receipts.items()}
    assert not stale.exists()
    assert (rollback / "door-outbound" / str(door_id) / stale.name).exists()


def test_a_restored_receipt_still_names_a_post_the_restored_database_has(tmp_path, db_path, identity_dir):
    """The reason capture runs before the database snapshot.

    A door may act on a `"posted"` receipt; a receipt naming a post the
    restored database never had is the one failure it cannot detect. The
    reverse -- a post whose receipt is missing -- the contract already covers.
    """
    _run_a_door(db_path, tmp_path / "launch-one")

    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    posted = [json.loads((backup / "door-outbound" / door["key"] / name).read_text(encoding="utf-8"))
              for door in _manifest(backup)["door_outbound"]["doors"] for name in door["receipts"]]
    post_ids = [receipt["post_id"] for receipt in posted if receipt["status"] == "posted"]
    assert post_ids
    with sqlite3.connect(backup / "netbbs.db") as snapshot:
        for post_id in post_ids:
            assert snapshot.execute("SELECT COUNT(*) FROM posts WHERE post_id = ?", (post_id,)).fetchone()[0] == 1


def test_receipt_capture_precedes_the_database_snapshot(tmp_path, db_path, identity_dir, monkeypatch):
    door_id, receipts = _run_a_door(db_path, tmp_path / "launch-one")
    original = backup_module._snapshot_database_and_managed_dns_credentials
    calls = []

    def snapshot(db_path, destination, database_filename):
        assert sorted(path.name for path in (destination / "door-outbound" / str(door_id)).iterdir()) == sorted(receipts)
        calls.append("snapshot")
        return original(db_path, destination, database_filename)

    monkeypatch.setattr(backup_module, "_snapshot_database_and_managed_dns_credentials", snapshot)
    create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    assert calls == ["snapshot"]


def test_an_archive_from_before_this_component_restores_the_absence(tmp_path, db_path, identity_dir):
    """Older archives stay restorable, and do not leave receipts behind.

    Receipts written after this archive was taken name posts its database
    never issued, so they go to the rollback generation with everything else
    this restore replaced rather than staying where a door would read them.
    """
    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    assert _manifest(backup)["door_outbound"] is None
    assert not (backup / "door-outbound").exists()
    door_id, receipts = _run_a_door(db_path, tmp_path / "launch-one")

    rollback = restore_backup(source=backup, db_path=db_path, identity_dir=identity_dir)

    assert not results_root(db_path).exists()
    assert sorted(path.name for path in (rollback / "door-outbound" / str(door_id)).iterdir()) == sorted(receipts)


def test_an_empty_receipts_directory_is_still_an_answer(tmp_path, db_path, identity_dir):
    results_root(db_path).mkdir()

    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    assert _manifest(backup)["door_outbound"] == {"version": 1, "doors": [], "skipped": 0, "pruned": 0}
    _run_a_door(db_path, tmp_path / "launch-one")

    restore_backup(source=backup, db_path=db_path, identity_dir=identity_dir)

    assert _live_receipts(db_path) == {}


def test_only_the_receipts_this_node_wrote_are_captured(tmp_path, db_path, identity_dir):
    """A door runs as the BBS user and can leave anything here.

    None of it is node state, so it is neither captured nor treated as
    corruption -- but it is counted, so the archive never claims to hold more
    than it does.
    """
    door_id, receipts = _run_a_door(db_path, tmp_path / "launch-one")
    directory = _door_directory(db_path, door_id)
    (directory / "launch-one.chronicle.result.json.part").write_text("half", encoding="utf-8")
    (directory / "notes.txt").write_text("a door's own bookkeeping", encoding="utf-8")
    (directory / "huge.result.json").write_bytes(b"x" * (64 * 1024 + 1))
    (results_root(db_path) / "not-a-door-id").mkdir()

    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    metadata = _manifest(backup)["door_outbound"]
    assert sorted(metadata["doors"][0]["receipts"]) == sorted(receipts)
    assert metadata["skipped"] == 4
    assert not (backup / "door-outbound" / "not-a-door-id").exists()


def test_retention_bounds_what_one_door_contributes(tmp_path, db_path, identity_dir, monkeypatch):
    """Capture keeps what the next drain would have kept, not more."""
    door_id, receipts = _run_a_door(db_path, tmp_path / "launch-one")
    directory = _door_directory(db_path, door_id)
    for name in receipts:
        os.utime(directory / name, (1_700_000_000, 1_700_000_000))
    for index in range(6):
        path = directory / f"launch-{index}.extra.result.json"
        path.write_text(json.dumps({"status": "rejected", "reason": str(index)}), encoding="utf-8")
        os.utime(path, (1_700_000_100 + index, 1_700_000_100 + index))
    monkeypatch.setattr(outbound_module, "RESULTS_KEPT", 2)

    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    metadata = _manifest(backup)["door_outbound"]
    assert metadata["doors"][0]["receipts"] == ["launch-4.extra.result.json", "launch-5.extra.result.json"]
    assert metadata["skipped"] == 6


def test_too_many_door_directories_fails_clearly(tmp_path, db_path, identity_dir):
    root = results_root(db_path)
    for door_id in range(backup_module._DOOR_OUTBOUND_MAX_DOORS + 1):
        (root / str(door_id)).mkdir(parents=True)

    with pytest.raises(BackupError, match="doors this node no longer has"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    assert not (tmp_path / "backup").exists(), "a refused backup leaves no half-written destination"


def test_an_archive_may_not_carry_a_receipt_it_does_not_list(tmp_path, db_path, identity_dir):
    door_id, _ = _run_a_door(db_path, tmp_path / "launch-one")
    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    smuggled = backup / "door-outbound" / str(door_id) / "launch-x.smuggled.result.json"
    smuggled.write_text(json.dumps({"status": "posted", "post_id": "not-in-this-archive"}), encoding="utf-8")

    with pytest.raises(BackupError, match="do not match their coverage manifest"):
        restore_backup(source=backup, db_path=db_path, identity_dir=identity_dir)

    assert smuggled.exists() and not results_root(db_path).joinpath(str(door_id), smuggled.name).exists()


def test_a_component_without_a_manifest_entry_is_refused(tmp_path, db_path, identity_dir):
    _run_a_door(db_path, tmp_path / "launch-one")
    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest = _manifest(backup)
    manifest["door_outbound"] = None
    (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="no coverage manifest"):
        restore_backup(source=backup, db_path=db_path, identity_dir=identity_dir)


@pytest.mark.parametrize("filename", ["door-outbound", "Door-Outbound"])
def test_a_database_named_door_outbound_remains_backupable(tmp_path, db_path, identity_dir, filename):
    """That node's database occupies the path its receipts would live at.

    So it has none, the archive says so, and the restore must not plan the
    artifact -- doing that would rename the database it had just restored
    into the rollback directory.
    """
    custom = tmp_path / "custom-node" / filename
    custom.parent.mkdir()
    shutil.copy2(db_path, custom)

    backup = create_backup(db_path=custom, identity_dir=identity_dir, destination=tmp_path / "backup")

    assert _manifest(backup)["door_outbound"] is None
    assert _manifest(backup)["database_filename"] == filename
    restore_backup(source=backup, db_path=custom, identity_dir=identity_dir)
    assert custom.is_file()
    with sqlite3.connect(custom) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("filename", ["door-outbound", "Door-Outbound"])
def test_a_database_named_door_outbound_refuses_an_archive_with_receipts(
    tmp_path, db_path, identity_dir, filename,
):
    """Decided by name, not by comparing the two paths.

    On a case-insensitive filesystem `Door-Outbound` *is* the receipts root
    while `==` says it is not, and neither path need exist yet when the plan
    is built -- so the database would have been restored and then renamed into
    the rollback directory, with the restore reporting success.
    """
    _run_a_door(db_path, tmp_path / "launch-one")
    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    elsewhere = tmp_path / "other-node" / filename
    elsewhere.parent.mkdir()

    with pytest.raises(BackupError, match="another name"):
        restore_backup(source=backup, db_path=elsewhere, identity_dir=identity_dir)


@pytest.mark.parametrize("survivors", [1, 0])
def test_a_receipt_pruned_while_the_backup_runs_does_not_fail_it(
    tmp_path, db_path, identity_dir, monkeypatch, survivors,
):
    """A backup is safe to take against a live node, doors included.

    The node's own drain prunes receipts, so one can be gone between the
    listing and the copy. Losing a receipt the node was discarding anyway is
    what a door already handles; losing the whole archive is not.
    """
    door_id, receipts = _run_a_door(db_path, tmp_path / "launch-one")
    original = backup_module._door_outbound_receipts

    def vanishing(directory, kept):
        found, skipped, pruned = original(directory, kept)
        return ([*(found if survivors else []), directory / "launch-one.pruned.result.json"],
                skipped, pruned)

    monkeypatch.setattr(backup_module, "_door_outbound_receipts", vanishing)

    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    metadata = _manifest(backup)["door_outbound"]
    # Counted as pruned, not as a stray file: a receipt this backup lost is a
    # different fact from one a door left behind.
    assert (metadata["pruned"], metadata["skipped"]) == (1, 0)
    if survivors:
        assert sorted(metadata["doors"][0]["receipts"]) == sorted(receipts)
    else:
        # Nothing captured leaves no directory behind: the component's own
        # validator refuses one the manifest does not account for.
        assert metadata["doors"] == []
        assert not (backup / "door-outbound" / str(door_id)).exists()
    restore_backup(source=backup, db_path=db_path, identity_dir=identity_dir)
    assert _live_receipts(db_path) == (
        {f"{door_id}/{name}": raw for name, raw in receipts.items()} if survivors else {})


def test_a_door_whose_hook_is_switched_off_mid_backup_does_not_fail_it(
    tmp_path, db_path, identity_dir, monkeypatch,
):
    """`disable_outbound` releases a door's whole directory.

    A SysOp can do that while a backup runs, which is the same call the
    pruning race makes: what the node is discarding is not worth failing an
    archive over.
    """
    door_id, _ = _run_a_door(db_path, tmp_path / "launch-one")
    original = backup_module._door_outbound_receipts

    def released(directory, kept):
        shutil.rmtree(directory)
        return original(directory, kept)

    monkeypatch.setattr(backup_module, "_door_outbound_receipts", released)

    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    assert _manifest(backup)["door_outbound"]["doors"] == []
    assert not (backup / "door-outbound" / str(door_id)).exists()


def test_a_receipt_outlives_a_deleted_post_exactly_as_it_does_live(tmp_path, db_path, identity_dir):
    """Deleting a board takes its posts and leaves the receipts behind.

    Capture ordering rules out this archive *inventing* that pair; it cannot
    repair one the node already made, and must not try. A backup restores the
    node it was taken from, not a tidier one -- and a door reading a receipt
    for a deleted post gets the same answer either way.
    """
    door_id, receipts = _run_a_door(db_path, tmp_path / "launch-one")
    db = Database(db_path)
    try:
        sysop = db_user(db, "sysop")
        delete_board(db, board_named(db, "Chronicle"), deleted_by=sysop)
    finally:
        db.close()

    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    restore_backup(source=backup, db_path=db_path, identity_dir=identity_dir)

    assert _live_receipts(db_path) == {f"{door_id}/{name}": raw for name, raw in receipts.items()}
    db = Database(db_path)
    try:
        assert db.connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    finally:
        db.close()


def test_an_unreadable_receipt_fails_the_backup_rather_than_shrinking_it(
    tmp_path, db_path, identity_dir, monkeypatch,
):
    """Only a receipt that vanished is tolerated.

    Anything else -- an unreadable source, a full destination -- would present
    an incomplete archive as a complete one.
    """
    _run_a_door(db_path, tmp_path / "launch-one")

    def refuse(source, destination, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(backup_module.shutil, "copy2", refuse)

    with pytest.raises(BackupError, match="Cannot capture door outbound receipt"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")


@pytest.mark.parametrize("where", ["door", "root"])
def test_an_unbounded_directory_stops_the_scan_rather_than_the_node(
    tmp_path, db_path, identity_dir, monkeypatch, where,
):
    """A door writes into this tree, so neither listing may be unbounded.

    `iterdir()` orders a whole directory before any retention bound applies;
    the drain already refuses to enumerate a door's drop directory without a
    cap, and this is the same directory's other end.
    """
    door_id, _ = _run_a_door(db_path, tmp_path / "launch-one")
    monkeypatch.setattr(outbound_module, "RESULTS_KEPT", 2)
    if where == "door":
        directory = _door_directory(db_path, door_id)
        for index in range(2 * backup_module._DOOR_OUTBOUND_SCAN_FACTOR + 2):
            (directory / f"junk-{index}.txt").write_text("x", encoding="utf-8")
    else:
        for index in range(backup_module._DOOR_OUTBOUND_SCAN_FACTOR
                           * backup_module._DOOR_OUTBOUND_MAX_DOORS + 1):
            (results_root(db_path) / f"junk-{index}.txt").write_text("x", encoding="utf-8")

    with pytest.raises(BackupError, match="more than"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")


def test_a_restore_refuses_targets_that_would_overwrite_each_other(tmp_path, db_path, identity_dir):
    """Nothing validates `identity_dir` against the paths derived from the database.

    A node pointed at its own receipts root for identity would have had its
    keys switched into the rollback directory by whichever artifact is planned
    after them, and the restore would have reported success.
    """
    _run_a_door(db_path, tmp_path / "launch-one")
    bootstrap_node_identity("test-node").save(identity_dir)
    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    with pytest.raises(BackupError, match="Restore targets overlap"):
        restore_backup(source=backup, db_path=db_path, identity_dir=results_root(db_path))

    assert _live_receipts(db_path), "refused before the first switch"


def test_a_hook_switched_off_during_the_root_scan_does_not_fail_the_backup(
    tmp_path, db_path, identity_dir, monkeypatch,
):
    """A `DirEntry` stats lazily, so the root listing races the same call.

    `disable_outbound` releases a door's whole directory; the per-door scan
    already treats that as harmless, and the listing above it must agree or a
    SysOp switching a hook off fails a live backup.
    """
    _run_a_door(db_path, tmp_path / "launch-one")
    root = results_root(db_path)
    real_scandir = os.scandir

    class _Released:
        name = "404"
        path = str(root / "404")

        def is_dir(self):
            raise FileNotFoundError(2, "No such file or directory")

        def is_symlink(self):
            return False

    class _WithReleased:
        def __init__(self, scan):
            self._scan = scan

        def __enter__(self):
            return itertools.chain(self._scan.__enter__(), [_Released()])

        def __exit__(self, *exc_info):
            return self._scan.__exit__(*exc_info)

    def scandir(path):
        scan = real_scandir(path)
        return _WithReleased(scan) if Path(path) == root else scan

    monkeypatch.setattr(backup_module.os, "scandir", scandir)

    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    assert _manifest(backup)["door_outbound"]["pruned"] == 1


def test_an_archive_may_not_carry_a_door_directory_it_does_not_list(tmp_path, db_path, identity_dir):
    """The other half of the unlisted-receipt refusal.

    Restore switches this directory in whole, so anything the manifest does
    not claim would land beside the node database.
    """
    _run_a_door(db_path, tmp_path / "launch-one")
    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    (backup / "door-outbound" / "999").mkdir()

    with pytest.raises(BackupError, match="Door outbound directories do not match"):
        restore_backup(source=backup, db_path=db_path, identity_dir=identity_dir)


@pytest.mark.parametrize("name", ["launch.a/b.result.json", "launch.a" + chr(92) + "b.result.json",
                                  ".", "..", "launch.a.result.json.part"])
def test_capture_and_validation_agree_on_what_a_receipt_may_be_called(name):
    r"""`_write_result` builds a receipt's name from the door's own filename.

    A door on the POSIX target may legally name a request `a\b.json`, which
    makes a receipt that is one file there and a path with a directory in it
    on Windows. Capture used to take such a name and validation then refuse
    it, failing the whole backup over one door's odd request.
    """
    from netbbs.doors.outbound import RESULT_SUFFIX

    assert not backup_module._is_capturable_receipt_name(name, RESULT_SUFFIX)
    assert backup_module._is_capturable_receipt_name("launch.a.result.json", RESULT_SUFFIX)


def test_a_manifest_naming_a_receipt_outside_its_directory_is_refused(tmp_path, db_path, identity_dir):
    door_id, _ = _run_a_door(db_path, tmp_path / "launch-one")
    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    manifest = _manifest(backup)
    manifest["door_outbound"]["doors"][0]["receipts"] = ["../../escape.result.json"]
    (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="Invalid door outbound receipt name"):
        restore_backup(source=backup, db_path=db_path, identity_dir=identity_dir)


def test_a_failed_self_check_leaves_no_destination_to_retry_around(
    tmp_path, db_path, identity_dir, monkeypatch,
):
    """The capture phase already cleaned up after itself; this one did not.

    A destination left behind is not merely untidy: `create_backup` refuses a
    destination that already exists, so the operator's retry cannot use the
    same path.
    """
    _run_a_door(db_path, tmp_path / "launch-one")

    def refuse(source, manifest):
        raise BackupError("component self-check failed")

    monkeypatch.setattr(backup_module, "_validate_door_outbound_component", refuse)

    with pytest.raises(BackupError, match="self-check failed"):
        create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    assert not (tmp_path / "backup").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_a_symlinked_receipts_root_is_followed_rather_than_reported_absent(
    tmp_path, db_path, identity_dir,
):
    """A SysOp may keep this state on another volume.

    Doors reach it through the same symlink, so reporting the component
    absent would have produced a backup with no receipts at all and a restore
    that removed the link as though the node had never had any.
    """
    elsewhere = tmp_path / "other-volume"
    elsewhere.mkdir()
    results_root(db_path).symlink_to(elsewhere, target_is_directory=True)
    door_id, receipts = _run_a_door(db_path, tmp_path / "launch-one")
    assert sorted(path.name for path in (elsewhere / str(door_id)).iterdir()) == sorted(receipts)

    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")

    assert sorted(_manifest(backup)["door_outbound"]["doors"][0]["receipts"]) == sorted(receipts)


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_restoring_absence_clears_a_dangling_receipts_link(tmp_path, db_path, identity_dir):
    """`exists()` follows links, so a dangling one is invisible to it.

    Left in place, it is a directory waiting for its volume to come back --
    holding the generation this restore replaced, beside a database that
    never issued those posts.
    """
    backup = create_backup(db_path=db_path, identity_dir=identity_dir, destination=tmp_path / "backup")
    assert _manifest(backup)["door_outbound"] is None
    results_root(db_path).symlink_to(tmp_path / "volume-that-went-away", target_is_directory=True)

    rollback = restore_backup(source=backup, db_path=db_path, identity_dir=identity_dir)

    assert not results_root(db_path).is_symlink() and not results_root(db_path).exists()
    assert (rollback / "door-outbound").is_symlink()
