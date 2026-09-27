"""Where a node's Voidrunner careers live, and how a backup finds them.

#555: `examples/netbbs.rc` starts the node with `HOME=<state dir>`, and the
careers used to live under that home. The documented backup command is run
by a SysOp from their own shell, with their own HOME, so the backup looked
somewhere the node never writes, found nothing, exited 0, and printed

    Voidrunner: no save directory found at /home/thiesi/.netbbs/voidrunner_saves.

#648: the careers now live beside the database, in `<db>.doors/voidrunner/`,
which both processes derive from the same path. The home directory is only
where a node upgraded from the old default copies them *from*, once.

These tests run the two processes' environments as the two different
things they are.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netbbs import backup as backup_module
from netbbs.doors.runtime import (
    VOIDRUNNER_SAVE_DIR_CONFIG_KEY, migrate_voidrunner_saves, node_voidrunner_save_dir,
    record_voidrunner_save_dir, voidrunner_save_dir,
)
from netbbs.storage.database import Database


@pytest.fixture
def node_home(tmp_path):
    """Where the *node* runs, the way the rc.d script arranges it. The
    legacy directory exists, as it does on every node that ever ran the
    door, but holds nothing."""
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


def _legacy_careers(home: Path) -> Path:
    """A legacy directory the way a running node leaves it: careers, a
    score, the retained leaderboard, and lock files."""
    legacy = home / ".netbbs" / "voidrunner_saves"
    (legacy / "scores").mkdir(parents=True, exist_ok=True)
    (legacy / "5.json").write_text('{"career": 5}', encoding="utf-8")
    (legacy / "5.previous.json").write_text('{"career": 4}', encoding="utf-8")
    (legacy / "scores" / "5.json").write_text('{"score": 5}', encoding="utf-8")
    (legacy / "leaderboard.json").write_text("[]", encoding="utf-8")
    (legacy / ".5.lock").write_bytes(b"")
    (legacy / ".maintenance.lock").write_bytes(b"")
    return legacy


# -- where the node keeps them (#648) ------------------------------------


def test_a_node_keeps_its_careers_beside_its_database(db, node_home, monkeypatch):
    _as_home(monkeypatch, node_home)

    assert voidrunner_save_dir(db.path) == db.path.resolve().parent / "node.db.doors" / "voidrunner"


def test_two_nodes_under_one_account_no_longer_share_careers(tmp_path, node_home, monkeypatch):
    _as_home(monkeypatch, node_home)

    assert voidrunner_save_dir(tmp_path / "one" / "netbbs.db") != voidrunner_save_dir(tmp_path / "two" / "netbbs.db")


def test_legacy_careers_are_copied_once_and_left_in_place(db, node_home, monkeypatch):
    _as_home(monkeypatch, node_home)
    legacy = _legacy_careers(node_home)
    before = {path.relative_to(legacy): path.read_bytes() for path in legacy.rglob("*") if path.is_file()}
    # Until the copy has run, the node plays the careers it has.
    assert voidrunner_save_dir(db.path) == legacy.resolve()

    assert migrate_voidrunner_saves(db.path) == legacy.resolve()

    own = node_voidrunner_save_dir(db.path)
    copied = {path.relative_to(own) for path in own.rglob("*") if path.is_file()}
    assert copied == {Path("5.json"), Path("5.previous.json"), Path("scores/5.json"), Path("leaderboard.json")}
    assert (own / "5.json").read_bytes() == before[Path("5.json")]
    # A copy, not a move: a second node under this account still has them.
    assert {path.relative_to(legacy): path.read_bytes() for path in legacy.rglob("*") if path.is_file()} == before
    assert voidrunner_save_dir(db.path) == own
    assert record_voidrunner_save_dir(db) == own
    assert migrate_voidrunner_saves(db.path) is None, "once is enough"


def test_nothing_is_copied_when_the_sysop_chose_a_directory(db, node_home, tmp_path, monkeypatch):
    _as_home(monkeypatch, node_home)
    _legacy_careers(node_home)
    monkeypatch.setenv("VOIDRUNNER_SAVE_DIR", str(tmp_path / "chosen"))

    assert migrate_voidrunner_saves(db.path) is None
    assert not node_voidrunner_save_dir(db.path).exists()
    assert voidrunner_save_dir(db.path) == (tmp_path / "chosen").resolve()


def test_a_pilot_in_flight_postpones_the_copy(db, node_home, monkeypatch):
    """A second node under the same account may be playing from the
    legacy directory. The copy waits for its next start rather than
    capturing a career mid-session, and the node keeps using what it has."""
    from netbbs.doors.bundled.voidrunner import pilot_session

    _as_home(monkeypatch, node_home)
    legacy = _legacy_careers(node_home)

    with pilot_session(legacy, 5):
        assert migrate_voidrunner_saves(db.path) is None

    own = node_voidrunner_save_dir(db.path)
    assert not own.exists() and not own.with_name(own.name + ".migrating").exists()
    assert voidrunner_save_dir(db.path) == legacy.resolve()
    assert migrate_voidrunner_saves(db.path) == legacy.resolve()


def test_a_legacy_directory_holding_foreign_files_is_left_alone(db, node_home, monkeypatch):
    _as_home(monkeypatch, node_home)
    legacy = _legacy_careers(node_home)
    (legacy / "notes.txt").write_text("not a career", encoding="utf-8")

    assert migrate_voidrunner_saves(db.path) is None
    assert voidrunner_save_dir(db.path) == legacy.resolve()


def test_lock_files_alone_are_not_careers(db, node_home, monkeypatch):
    _as_home(monkeypatch, node_home)
    (node_home / ".netbbs" / "voidrunner_saves" / ".maintenance.lock").write_bytes(b"")

    assert migrate_voidrunner_saves(db.path) is None
    assert voidrunner_save_dir(db.path) == node_voidrunner_save_dir(db.path)


# -- what the node records ---------------------------------------------


def test_the_node_records_the_directory_its_doors_will_use(db, node_home, monkeypatch):
    _as_home(monkeypatch, node_home)

    recorded = record_voidrunner_save_dir(db)

    assert recorded == node_voidrunner_save_dir(db.path)
    row = db.connection.execute(
        "SELECT value FROM node_config WHERE key = ?", (VOIDRUNNER_SAVE_DIR_CONFIG_KEY,)
    ).fetchone()
    assert row[0] == str(recorded)


def test_recording_follows_an_override_set_between_restarts(db, node_home, tmp_path, monkeypatch):
    """A node reconfigured between restarts must not leave the old
    location behind for the backup to trust."""
    _as_home(monkeypatch, node_home)
    record_voidrunner_save_dir(db)

    moved = tmp_path / "elsewhere"
    monkeypatch.setenv("VOIDRUNNER_SAVE_DIR", str(moved))
    assert record_voidrunner_save_dir(db) == moved.resolve()

    row = db.connection.execute(
        "SELECT value FROM node_config WHERE key = ?", (VOIDRUNNER_SAVE_DIR_CONFIG_KEY,)
    ).fetchone()
    assert row[0] == str(moved.resolve())


def test_an_explicit_save_dir_override_is_what_gets_recorded(db, node_home, monkeypatch):
    """`VOIDRUNNER_SAVE_DIR` is forwarded to the door by
    `_door_environment`, so it is also what the node must write down."""
    explicit = node_home / "careers"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: node_home))
    monkeypatch.setenv("VOIDRUNNER_SAVE_DIR", str(explicit))

    assert record_voidrunner_save_dir(db) == explicit.resolve()


# -- what the backup reads ---------------------------------------------


def test_the_backup_reads_the_nodes_answer_not_its_own_environment(db, node_home, operator_home, monkeypatch):
    """The reported defect, end to end: the node records its location,
    then the CLI resolves it from a shell that shares none of the node's
    environment -- here, not even its override."""
    explicit = node_home / "careers"
    _as_home(monkeypatch, node_home)
    monkeypatch.setenv("VOIDRUNNER_SAVE_DIR", str(explicit))
    record_voidrunner_save_dir(db)
    db.connection.commit()

    # Now we are the operator's shell.
    _as_home(monkeypatch, operator_home)

    resolved, provenance = backup_module.voidrunner_save_directory(db.path)

    assert provenance == "node"
    assert resolved == explicit.resolve()


def test_an_unrecorded_node_directory_is_found_from_the_database_path(operator_home, tmp_path, monkeypatch):
    """Derived from `--db`, not from anyone's home, so it is the node's
    own answer even before the node has recorded it."""
    database = Database(tmp_path / "unrecorded.db")
    try:
        node_voidrunner_save_dir(database.path).mkdir(parents=True)
        _as_home(monkeypatch, operator_home)
        resolved, provenance = backup_module.voidrunner_save_directory(database.path)
    finally:
        database.close()

    assert (resolved, provenance) == (node_voidrunner_save_dir(database.path), "node")


def test_without_a_recorded_location_the_answer_is_marked_as_a_guess(operator_home, tmp_path, monkeypatch):
    """A database written before this version says nothing and has no
    directory beside it yet, so the CLI has to report that it guessed.

    This is the exact state every upgraded node is in until it next
    starts, so the fallback matters as much as the fix.
    """
    database = Database(tmp_path / "unrecorded.db")
    try:
        _as_home(monkeypatch, operator_home)
        resolved, provenance = backup_module.voidrunner_save_directory(database.path)
    finally:
        database.close()

    assert provenance == "guess"
    assert resolved == node_voidrunner_save_dir(tmp_path / "unrecorded.db")


def test_no_database_at_all_still_resolves(operator_home, tmp_path, monkeypatch):
    """`voidrunner_save_directory()` with no argument has only this
    process's home to go on."""
    _as_home(monkeypatch, operator_home)

    resolved, provenance = backup_module.voidrunner_save_directory()

    assert provenance == "guess"
    assert resolved == (operator_home / ".netbbs" / "voidrunner_saves").resolve()

    missing, guessed = backup_module.voidrunner_save_directory(tmp_path / "not-a-database.db")
    assert (missing, guessed) == (node_voidrunner_save_dir(tmp_path / "not-a-database.db"), "guess")


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
    own = record_voidrunner_save_dir(db)
    own.mkdir(parents=True)
    (own / "leaderboard.json").write_text("[]", encoding="utf-8")
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
    assert looked_in["directory"] == str(node_voidrunner_save_dir(tmp_path / "unrecorded.db"))


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

    So the only directory that reaches a door is the node's own
    `voidrunner_save_dir`, which `record_voidrunner_save_dir` records.

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
