"""A backup taken from a different shell than the node runs in (#555).

`examples/netbbs.rc` starts the node with `HOME=<state dir>`, so the door
writes its careers under the state directory. The documented backup
command is run by a SysOp from their own shell, with their own HOME, and
`_default_save_dir()` resolved `Path.home()` in *that* process -- so the
backup looked somewhere the node never writes, found nothing, exited 0,
and printed

    Voidrunner: no save directory found at /home/thiesi/.netbbs/voidrunner_saves.

which reads as a statement about the node and is in fact a statement
about the environment the CLI inherited. The archive it produced was the
rollback point for an upgrade.

These tests run the two processes' environments as the two different
things they are.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netbbs import backup as backup_module
from netbbs.doors.runtime import VOIDRUNNER_SAVE_DIR_CONFIG_KEY, record_voidrunner_save_dir
from netbbs.storage.database import Database


@pytest.fixture
def node_home(tmp_path):
    """Where the *node* runs, the way the rc.d script arranges it."""
    home = tmp_path / "node-state"
    (home / ".netbbs" / "voidrunner_saves").mkdir(parents=True)
    return home


@pytest.fixture
def operator_home(tmp_path):
    """Where the SysOp's own shell runs. Deliberately has no saves under
    it -- that is the whole point."""
    home = tmp_path / "operator"
    home.mkdir()
    return home


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _as_home(monkeypatch, home: Path) -> None:
    monkeypatch.delenv("VOIDRUNNER_SAVE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


# -- what the node records ---------------------------------------------


def test_the_node_records_the_directory_its_doors_will_use(db, node_home, monkeypatch):
    _as_home(monkeypatch, node_home)

    recorded = record_voidrunner_save_dir(db)

    assert recorded == node_home / ".netbbs" / "voidrunner_saves"
    row = db.connection.execute(
        "SELECT value FROM node_config WHERE key = ?", (VOIDRUNNER_SAVE_DIR_CONFIG_KEY,)
    ).fetchone()
    assert row[0] == str(recorded)


def test_recording_follows_a_node_whose_home_changes(db, node_home, operator_home, monkeypatch):
    """A node moved between restarts must not leave the old location
    behind for the backup to trust."""
    _as_home(monkeypatch, node_home)
    record_voidrunner_save_dir(db)

    moved = operator_home / "elsewhere"
    _as_home(monkeypatch, moved)
    assert record_voidrunner_save_dir(db) == moved / ".netbbs" / "voidrunner_saves"

    row = db.connection.execute(
        "SELECT value FROM node_config WHERE key = ?", (VOIDRUNNER_SAVE_DIR_CONFIG_KEY,)
    ).fetchone()
    assert row[0] == str(moved / ".netbbs" / "voidrunner_saves")


def test_an_explicit_save_dir_override_is_what_gets_recorded(db, node_home, monkeypatch):
    """`VOIDRUNNER_SAVE_DIR` is forwarded to the door by
    `_door_environment`, so it is also what the node must write down."""
    explicit = node_home / "careers"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: node_home))
    monkeypatch.setenv("VOIDRUNNER_SAVE_DIR", str(explicit))

    assert record_voidrunner_save_dir(db) == explicit.resolve()


# -- what the backup reads ---------------------------------------------


def test_the_backup_reads_the_nodes_answer_not_its_own_home(db, node_home, operator_home, monkeypatch):
    """The reported defect, end to end: the node records its location,
    then the CLI resolves it from a completely different home."""
    _as_home(monkeypatch, node_home)
    record_voidrunner_save_dir(db)
    db.connection.commit()

    # Now we are the operator's shell.
    _as_home(monkeypatch, operator_home)

    resolved, provenance = backup_module.voidrunner_save_directory(db.path)

    assert provenance == "node"
    assert resolved == (node_home / ".netbbs" / "voidrunner_saves").resolve()


def test_without_a_recorded_location_the_answer_is_marked_as_a_guess(operator_home, tmp_path, monkeypatch):
    """A database written before this version says nothing, so the CLI
    falls back to its own home -- and has to report that it guessed.

    This is the exact state every existing node is in until it next
    starts, so the fallback matters as much as the fix.
    """
    database = Database(tmp_path / "unrecorded.db")
    try:
        _as_home(monkeypatch, operator_home)
        resolved, provenance = backup_module.voidrunner_save_directory(database.path)
    finally:
        database.close()

    assert provenance == "guess"
    assert resolved == (operator_home / ".netbbs" / "voidrunner_saves").resolve()


def test_no_database_at_all_still_resolves(operator_home, tmp_path, monkeypatch):
    """`voidrunner_save_directory()` is called with no argument by the
    SysOp console, and by anything older that has not been updated."""
    _as_home(monkeypatch, operator_home)

    resolved, provenance = backup_module.voidrunner_save_directory()

    assert provenance == "guess"
    assert resolved == (operator_home / ".netbbs" / "voidrunner_saves").resolve()

    missing, guessed = backup_module.voidrunner_save_directory(tmp_path / "not-a-database.db")
    assert (missing, guessed) == (resolved, "guess")


# -- a guess is never reported as a finding (Codex review) -------------


def test_a_guessed_directory_that_exists_is_still_marked_as_a_guess(
    operator_home, node_home, tmp_path, monkeypatch
):
    """The nastiest case, because it reads as success.

    The node has recorded nothing, and the fallback path *happens to
    exist* -- a SysOp who once ran a node from their own shell before
    setting up the service has exactly this directory, holding exactly
    the wrong careers. The manifest has to say the location was guessed,
    so a restore months later can still answer "was that the right
    directory?".
    """
    from netbbs.backup import create_backup
    from netbbs.link.node_identity import bootstrap_node_identity

    # Saves under the operator's own home, none under the node's.
    guessed = operator_home / ".netbbs" / "voidrunner_saves"
    guessed.mkdir(parents=True)
    (guessed / "leaderboard.json").write_text("[]", encoding="utf-8")

    database = Database(tmp_path / "unrecorded.db")
    identity_dir = tmp_path / "identity"
    bootstrap_node_identity("thisnode").save(identity_dir)
    database.close()

    _as_home(monkeypatch, operator_home)
    destination = tmp_path / "backup"
    create_backup(db_path=tmp_path / "unrecorded.db", identity_dir=identity_dir, destination=destination)

    import json

    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["voidrunner"] is not None, "the guess was captured"
    assert manifest["voidrunner"]["source_provenance"] == "guess"


def test_a_recorded_directory_is_marked_as_recorded(db, node_home, tmp_path, monkeypatch):
    from netbbs.backup import create_backup
    from netbbs.link.node_identity import bootstrap_node_identity

    _as_home(monkeypatch, node_home)
    record_voidrunner_save_dir(db)
    (node_home / ".netbbs" / "voidrunner_saves" / "leaderboard.json").write_text("[]", encoding="utf-8")
    db.connection.commit()

    identity_dir = tmp_path / "identity"
    bootstrap_node_identity("thisnode").save(identity_dir)

    destination = tmp_path / "backup-recorded"
    create_backup(db_path=db.path, identity_dir=identity_dir, destination=destination)

    import json

    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["voidrunner"]["source_provenance"] == "node"


def test_the_manifest_records_where_the_run_looked_even_when_it_found_nothing(
    operator_home, tmp_path, monkeypatch
):
    """Codex review. A backup is live-safe, so the node may start while
    one is running -- and a caller that re-resolves the save directory
    afterwards can be handed the service path by a database that recorded
    it half a second ago, then report "no saves at" a directory this run
    never opened. The capture-time answer is the only one that describes
    the archive, so it has to travel with it whether or not anything was
    found.
    """
    from netbbs.backup import create_backup
    from netbbs.link.node_identity import bootstrap_node_identity

    database = Database(tmp_path / "unrecorded.db")
    identity_dir = tmp_path / "identity"
    bootstrap_node_identity("thisnode").save(identity_dir)
    database.close()

    _as_home(monkeypatch, operator_home)  # nothing under it: nothing to capture
    destination = tmp_path / "backup-empty"
    create_backup(db_path=tmp_path / "unrecorded.db", identity_dir=identity_dir, destination=destination)

    import json

    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["voidrunner"] is None, "nothing was captured"
    looked_in = manifest["voidrunner_source"]
    assert looked_in["provenance"] == "guess"
    assert looked_in["directory"] == str((operator_home / ".netbbs" / "voidrunner_saves").resolve())


def test_an_operator_supplied_path_is_not_claimed_as_the_nodes_answer(
    operator_home, node_home, tmp_path, monkeypatch
):
    """Codex review. `--voidrunner-save-dir` marked the source as
    node-recorded, so an archive made from an old or stopped node claimed
    the node had confirmed a directory it never recorded -- which defeats
    the only reason to write the provenance down at all.
    """
    from netbbs.backup import create_backup
    from netbbs.link.node_identity import bootstrap_node_identity

    explicit = node_home / ".netbbs" / "voidrunner_saves"
    (explicit / "leaderboard.json").write_text("[]", encoding="utf-8")

    database = Database(tmp_path / "unrecorded.db")
    identity_dir = tmp_path / "identity"
    bootstrap_node_identity("thisnode").save(identity_dir)
    database.close()

    _as_home(monkeypatch, operator_home)
    destination = tmp_path / "backup-explicit"
    create_backup(
        db_path=tmp_path / "unrecorded.db", identity_dir=identity_dir,
        destination=destination, voidrunner_save_dir=explicit,
    )

    import json

    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["voidrunner_source"]["provenance"] == "operator"
    assert manifest["voidrunner"]["source_provenance"] == "operator"

def test_a_door_profile_cannot_move_the_voidrunner_save_directory():
    """Why the node's recorded path is authoritative, pinned.

    The launch path applies `env.update(profile.environment)` *after*
    `_door_environment` composed the base, so a door profile setting
    `VOIDRUNNER_SAVE_DIR` would win over the node's own resolution -- and
    the node would then be recording a directory the game never writes
    to, with the backup trusting it. That was raised in review as a live
    defect, and it is not one, because `DoorProfile` refuses the key: the
    environment allowlist is `TERM`, `LANG`, `LC_ALL`, `TZ`, `PATH`,
    `WAR_DIALER_DB_PATH` and `DOOR_*`, and nothing else.

    So the only override that reaches a door is the *node process's* own
    `VOIDRUNNER_SAVE_DIR`, which `_default_save_dir` already honours and
    `record_voidrunner_save_dir` therefore records correctly.

    This test exists because that is a dependency between two modules
    with nothing else holding it together. Whoever adds
    `VOIDRUNNER_SAVE_DIR` to the allowlist will fail here, and should
    read this before deciding what the backup ought to record when two
    doors disagree -- one archive holds one Voidrunner component.
    """
    from netbbs.doors.profiles import DoorProfile, ProfileError

    with pytest.raises(ProfileError):
        DoorProfile(environment={"VOIDRUNNER_SAVE_DIR": "/tmp/elsewhere"}).validate()

    # The one that is permitted, for contrast: War Dialer's world path is
    # profile-settable, which is why `war_dialer_world_path` reads the
    # profile and this module's Voidrunner equivalent does not have to.
    DoorProfile(environment={"WAR_DIALER_DB_PATH": "/tmp/world.db"}).validate()
